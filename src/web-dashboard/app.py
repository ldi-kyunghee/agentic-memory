"""정성분석 웹앱 백엔드: 산출물 4종(러너/A'/trace/judge)을 유저 단위로 조인해 API로 제공.

원칙: results/·traces/는 읽기 전용. 쓰기는 src/web-dashboard/data/ 안에서만 (comments.sqlite3).
실행: uv run --project src/web-dashboard uvicorn app:app --app-dir src/web-dashboard --port 8501
"""

import os
import json
import sqlite3
import threading
from datetime import datetime, timezone
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

HERE = Path(__file__).parent
ROOT = HERE.parent.parent  # 리포 루트: runs.yaml의 상대경로 기준
DATA_DIR = HERE / "data"
DATA_DIR.mkdir(exist_ok=True)

app = FastAPI(title="mem0-halumem qualitative dashboard")

# 유저 번들은 세션 대화·검색 context가 모두 담겨 2MB를 넘는다. 분석가는 SSH 터널 너머에서
# 접속하므로 전송량이 곧 체감 지연이다. JSON은 반복 문자열이 많아 gzip이 매우 잘 듣는다.
app.add_middleware(GZipMiddleware, minimum_size=1024)


# ---------- 레지스트리 / 사전 ----------

_reg_cache: dict = {}


def load_registry_doc() -> dict:
    """runs.yaml 파싱 결과: 파일 mtime이 바뀔 때만 다시 읽는다.

    ⚠ 매번 파싱하면(28ms) 항목 단위로 레인을 조회하는 경로에서 수천 번 호출돼 API가 분 단위로
    느려진다(실측: /api/iaa 77초 → 캐시 후 1초 미만). mtime 기준이라 파일을 고치면 즉시 반영된다.
    """
    p = HERE / "runs.yaml"
    mt = p.stat().st_mtime_ns
    if _reg_cache.get("mt") != mt:
        with open(p, encoding="utf-8") as f:
            _reg_cache.update(mt=mt, doc=yaml.safe_load(f))
    return _reg_cache["doc"]


def hide_groups() -> dict:
    """숨김 그룹 정의: 실험 갈래 하나를 통째로 접었다 펴는 단위. runs.yaml의 hide_groups 섹션."""
    return load_registry_doc().get("hide_groups", {}) or {}


def load_registry(include_hidden: bool = False, show: set | None = None) -> dict:
    """런 레지스트리. hidden: true 인 런은 기본적으로 화면·집계에서 제외한다.

    ⚠ 숨김은 '삭제'가 아니다. 산출물과 이미 기록된 정성분석 주석은 그대로 두고 노출만 막는다.
    (custom 프롬프트 갈래는 정성분석 결과 품질이 낮아 제외했으나, 이미 완료된 판정 검토 큐 작업이
     걸려 있어 데이터를 지우면 안 된다.) include_hidden=True로 언제든 되살릴 수 있다.

    show: 켜진 숨김 그룹 이름들. 갈래가 여러 개라 단일 토글로는 부족해서
          (custom 프롬프트 / BM25 검색기) 그룹 단위로 켠다.
    """
    runs = load_registry_doc()["runs"]
    if include_hidden:
        return runs
    on = show or set()
    return {k: v for k, v in runs.items()
            if not (v.get("hidden") and v.get("hide_group", "custom") not in on)}


def gen_registry() -> dict:
    """generator 레인 정의 (전역 섹션). base 레인은 각 런의 기본 results/judges를 사용."""
    return load_registry_doc().get("generators", {"qwen4b": {"label": "Qwen3-4B", "base": True}})


def resolve_lane(run: str, generator: str):
    """run×generator -> (results_path, judges{name: dir상대경로}). 미지의 run/generator면 404.
    ⚠ 숨김 런도 열어준다. 이미 기록된 주석·trace를 직접 조회하는 경로이므로 막으면 과거 작업이 깨진다."""
    reg = load_registry(include_hidden=True)
    if run not in reg:
        raise HTTPException(404, f"unknown run: {run}")
    g = gen_registry().get(generator)
    if g is None:
        raise HTTPException(404, f"unknown generator: {generator}")
    if g.get("base"):
        return ROOT / reg[run]["results"], dict(reg[run].get("judges", {}))
    return ROOT / g["results"].format(run=run), {k: v.format(run=run) for k, v in g.get("judges", {}).items()}


@lru_cache(maxsize=1)
def load_fielddict() -> dict:
    with open(HERE / "fielddict.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------- 데이터 로딩 (읽기 전용, 런 단위 캐시) ----------

_cache_lock = threading.Lock()


@lru_cache(maxsize=64)
def load_run_users(run: str, generator: str = "qwen4b") -> dict:
    """레인(jsonl) -> {uuid: user_record}. A' 완료본이면 questions에 system_response 포함."""
    path, _ = resolve_lane(run, generator)
    if not path.exists():
        raise HTTPException(404, f"results file missing: {path}")
    users = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                u = json.loads(line)
                users[u["uuid"]] = u
    return users


# ⚠ lru_cache를 쓰지 않는 이유: 파일 부재/로드 실패 시의 None까지 영구 캐싱돼
#    이후 파일이 생겨도(동기화 완료 등) 회색 '–' 라벨로 남는 버그가 됨. 성공한 로드만 캐싱한다.
_judge_cache: dict = {}


def load_judge(run: str, judge_name: str, uuid: str, generator: str = "qwen4b") -> dict | None:
    key = (run, generator, judge_name, uuid)
    if key in _judge_cache:
        return _judge_cache[key]
    _, judges = resolve_lane(run, generator)
    if judge_name not in judges:
        return None
    path = ROOT / judges[judge_name] / f"{uuid}.json"
    if not path.exists():
        return None  # 실패는 캐싱하지 않음. 다음 요청에서 재시도
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    _judge_cache[key] = data
    return data


# ---------- 조인 (유저 번들) ----------

def build_bundle(run: str, uuid: str, judge_name: str, generator: str = "qwen4b") -> dict:
    user = load_run_users(run, generator).get(uuid)
    if user is None:
        raise HTTPException(404, f"user {uuid} not in run {run} (generator={generator})")
    judge = load_judge(run, judge_name, uuid, generator)

    # judge 라벨 룩업 테이블 (키: 세션 인덱스 + 텍스트)
    integ, acc, upd, qa_lbl = {}, {}, {}, {}
    if judge:
        for r in judge.get("memory_integrity_records", []):
            integ[(r["session_id"], r["memory_content"])] = r.get("memory_integrity_score")
        for r in judge.get("memory_accuracy_records", []):
            acc[(r["session_id"], r["memory_content"])] = {
                "score": r.get("memory_accuracy_score"),
                "is_included": r.get("is_included_in_golden_memories"),
            }
        for r in judge.get("memory_update_records", []):
            upd[(r["session_id"], r["memory_content"])] = r.get("memory_update_type")
        for r in judge.get("question_answering_records", []):
            qa_lbl[(r["session_id"], r["question"])] = r.get("result_type")

    sessions = []
    for si, s in enumerate(user["sessions"]):
        if s.get("is_generated_qa_session"):
            sessions.append({"session_id": si, "generated_qa_session": True})
            continue

        # 이벤트: 메모리 텍스트 -> op (유래 판정용)
        ev_by_text = {}
        for e in s.get("memory_events", []):
            ev_by_text[e.get("memory", "")] = e

        golden = []
        for mp in s.get("memory_points", []):
            key = (si, mp["memory_content"])
            golden.append({
                **mp,
                "judge": (
                    {"kind": "update", "label": upd[key]} if key in upd
                    else {"kind": "integrity", "score": integ.get(key)}
                ),
            })

        extracted = []
        for m in s.get("extracted_memories", []):
            ev = ev_by_text.get(m)
            extracted.append({
                "text": m,
                "origin": ev.get("event") if ev else None,
                "previous_memory": (ev or {}).get("previous_memory"),
                "judge": acc.get((si, m)),
            })

        qas = []
        for q in s.get("questions", []):
            qas.append({**q, "judge": qa_lbl.get((si, q["question"]))})

        sessions.append({
            "session_id": si,
            "start_time": s.get("start_time"),
            "add_dialogue_duration_ms": s.get("add_dialogue_duration_ms"),
            "dialogue": s.get("dialogue", []),
            "golden": golden,
            "extracted": extracted,
            "events": s.get("memory_events", []),
            "questions": qas,
        })

    return {
        "run": run,
        "generator": generator,
        "judge": judge_name if judge else None,
        "uuid": uuid,
        "user_name": user.get("user_name"),
        "sessions": sessions,
    }


# ---------- API ----------

@app.get("/api/runs")
def api_runs(include_hidden: int = 0):
    reg = load_registry(include_hidden=bool(include_hidden))
    gens = gen_registry()
    out = []
    for name, r in reg.items():
        exists = (ROOT / r["results"]).exists()
        # generator 레인별 가용성/judge 목록
        lanes = {}
        for gname, g in gens.items():
            if g.get("base"):
                lanes[gname] = {"label": g.get("label", gname), "available": exists,
                                "judges": list(r.get("judges", {}).keys())}
            else:
                rp = ROOT / g["results"].format(run=name)
                judges = [k for k, v in g.get("judges", {}).items() if (ROOT / v.format(run=name)).exists()]
                lanes[gname] = {"label": g.get("label", gname), "available": rp.exists(), "judges": judges}
        out.append({
            "run": name, "label": r.get("label", name),
            "backbone": r.get("backbone"), "prompt": r.get("prompt"),
            "oracle": r.get("oracle", ""),   # 오라클로 대체한 단계 (프롬프트 종류와 독립)
            "retriever": r.get("retriever") or "Qwen3-Embedding-4B",
            "backbone_effort": r.get("backbone_effort"),
            "embedder": r.get("embedder"),
            "users": r.get("users"), "judges": list(r.get("judges", {}).keys()),
            "generators": lanes,
            "available": exists,
        })
    return out


@app.get("/api/runs/{run}/users")
def api_users(run: str, generator: str = "qwen4b"):
    users = load_run_users(run, generator)
    return [{"uuid": u, "user_name": rec.get("user_name")} for u, rec in sorted(users.items())]


@app.get("/api/bundle/{run}/{uuid}")
def api_bundle(run: str, uuid: str, judge: str = "nano", generator: str = "qwen4b"):
    return build_bundle(run, uuid, judge, generator)


@app.get("/api/trace/{run}/{uuid}")
def api_trace(run: str, uuid: str, session: int | None = None):
    reg = load_registry(include_hidden=True)   # 과거 주석에서 열던 trace가 막히면 안 된다
    if run not in reg:
        raise HTTPException(404, f"unknown run: {run}")
    path = ROOT / reg[run]["traces"] / f"{uuid}.jsonl"
    if not path.exists():
        raise HTTPException(404, "trace not found")
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if session is None or r.get("session") == session:
                records.append(r)
    return records


@app.get("/api/fielddict")
def api_fielddict():
    return load_fielddict()


# ---------- 지표 테이블 (공식 집계 함수 재사용: 문서 테이블과 수치 일치 보장) ----------

import sys as _sys
_sys.path.insert(0, str(ROOT / "HaluMem" / "eval"))
from evaluation import aggregate_eval_results  # noqa: E402
# judge.py가 쓰는 것과 동일한 프롬프트 템플릿: 분석가에게 judge와 똑같은 입력을 재현해 보여주기 위함
from eval_tools import (  # noqa: E402
    EVALUATION_PROMPT_FOR_MEMORY_INTEGRITY,
    EVALUATION_PROMPT_FOR_MEMORY_ACCURACY,
    EVALUATION_PROMPT_FOR_UPDATE_MEMORY,
    EVALUATION_PROMPT_FOR_QUESTION,
)


# ---------- judge 입력 재현 (판정 검토·주석용) ----------

def _dialogue_str(session: dict) -> str:
    """judge.py build_inputs와 동일한 대화 직렬화 (assistant 발화 뒤 빈 줄)."""
    out = []
    for turn in session["dialogue"]:
        out.append(f'[{turn["timestamp"]}]{turn["role"]}: {turn["content"]}')
        if turn["role"] == "assistant":
            out.append("")
    return "\n".join(out)


def build_judge_input(run: str, uuid: str, rec_type: str, session_id: int, idx: int,
                      generator: str = "qwen4b") -> dict:
    """judge가 실제로 받았던 프롬프트를 그대로 재구성한다 (judge.py build_inputs와 동일 로직)."""
    user = load_run_users(run, generator).get(uuid)
    if user is None:
        raise HTTPException(404, f"user {uuid} not in run {run}")
    try:
        s = user["sessions"][session_id]
    except IndexError:
        raise HTTPException(404, "session out of range")
    if s.get("is_generated_qa_session"):
        raise HTTPException(400, "generated qa session")

    golden = s.get("memory_points", [])
    extracted = s.get("extracted_memories", [])
    fields: dict = {}

    # ⚠ fields에는 judge가 프롬프트로 실제 받은 값만 담는다 (judge가 못 본 메타데이터를
    #    분석가에게 보여주면 동일 조건 재현이 깨진다). template은 채점 기준 표시용.
    if rec_type == "integrity":
        mp = golden[idx]
        key = mp["memory_content"]
        template = EVALUATION_PROMPT_FOR_MEMORY_INTEGRITY
        prompt = template.format(memories="\n".join(extracted), expected_memory_point=key)
        fields = {"memories": extracted, "expected_memory_point": key}
        label_field, label_key = "memory_integrity_records", "memory_integrity_score"
    elif rec_type == "accuracy":
        key = extracted[idx]
        golden_str = "\n".join(m["memory_content"] for m in golden if m["memory_source"] != "interference")
        template = EVALUATION_PROMPT_FOR_MEMORY_ACCURACY
        prompt = template.format(
            dialogue=_dialogue_str(s), golden_memories=golden_str, candidate_memory=key)
        fields = {"dialogue": s["dialogue"], "golden_memories": golden_str.split("\n") if golden_str else [],
                  "candidate_memory": key}
        label_field, label_key = "memory_accuracy_records", "memory_accuracy_score"
    elif rec_type == "update":
        mp = golden[idx]
        key = mp["memory_content"]
        if mp.get("is_update") != "True" or not mp.get("memories_from_system"):
            raise HTTPException(400, "not an update-evaluated golden")
        template = EVALUATION_PROMPT_FOR_UPDATE_MEMORY
        prompt = template.format(
            memories="\n".join(mp["memories_from_system"]),
            updated_memory=key, original_memory="\n".join(mp.get("original_memories", [])))
        fields = {"memories": mp["memories_from_system"], "updated_memory": key,
                  "original_memory": mp.get("original_memories", [])}
        label_field, label_key = "memory_update_records", "memory_update_type"
    elif rec_type in ("qa", "gold_qa"):
        q = s.get("questions", [])[idx]
        key = q["question"]
        template = EVALUATION_PROMPT_FOR_QUESTION
        prompt = template.format(
            question=key, reference_answer=q["answer"],
            key_memory_points="\n".join(e["memory_content"] for e in q.get("evidence", [])),
            response=q.get("system_response", ""))
        fields = {"question": key, "reference_answer": q["answer"],
                  "key_memory_points": [e["memory_content"] for e in q.get("evidence", [])],
                  "response": q.get("system_response", "")}
        label_field, label_key = "question_answering_records", "result_type"
    else:
        raise HTTPException(400, f"unknown rec_type: {rec_type}")

    # gold_qa는 '벤치마크 정답 자체'를 검토하므로 judge 라벨 대조가 의미 없다
    if rec_type == "gold_qa":
        return {"run": run, "uuid": uuid, "generator": generator, "rec_type": rec_type,
                "session_id": session_id, "idx": idx, "target": key,
                "fields": fields, "prompt": prompt, "template": template, "judge_labels": {}}

    # 이 항목에 대한 전체 judge 라벨 (integrity/accuracy/update는 입력이 레인 무관 동일 → 모든 judge와 비교 가능)
    labels = {}
    lanes = [generator] if rec_type == "qa" else list(gen_registry().keys())
    for lane in lanes:
        try:
            _, judges = resolve_lane(run, lane)
        except HTTPException:
            continue
        for jname in judges:
            if jname in labels:
                continue
            jd = load_judge(run, jname, uuid, lane)
            if not jd:
                continue
            for r in jd.get(label_field, []):
                same = r.get("question") == key if rec_type == "qa" else r.get("memory_content") == key
                if r.get("session_id") == session_id and same:
                    labels[jname] = r.get(label_key)
                    break
    return {"run": run, "uuid": uuid, "generator": generator, "rec_type": rec_type,
            "session_id": session_id, "idx": idx, "target": key,
            "fields": fields, "prompt": prompt, "template": template, "judge_labels": labels}


@app.get("/api/judge-input/{run}/{uuid}")
def api_judge_input(run: str, uuid: str, rec_type: str, session_id: int, idx: int,
                    generator: str = "qwen4b"):
    return build_judge_input(run, uuid, rec_type, session_id, idx, generator)


@lru_cache(maxsize=1)
def first4_uuids() -> tuple:
    """모든 실험이 공유하는 데이터셋 첫 4유저: 4u 런의 judge 디렉토리 파일 목록에서 확보.
    ⚠ 숨김과 무관해야 한다. 런을 숨겼다고 유저 범위가 달라지면 과거 수치가 재현되지 않는다."""
    reg = load_registry(include_hidden=True)
    for r in reg.values():
        if r.get("users") == 4:
            d = ROOT / list(r["judges"].values())[0]
            if d.exists():
                return tuple(sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json")))
    return ()


_metrics_cache: dict = {}


def compute_metrics(run: str, judge_name: str, scope: str, generator: str = "qwen4b") -> dict | None:
    """scope: 'first4' | 'all' | <uuid>. 선택 범위의 judge 레코드를 모아 공식 집계로 지표 산출.
    성공 결과만 캐싱 (None 캐싱 금지: load_judge와 동일 이유)."""
    key = (run, generator, judge_name, scope)
    if key in _metrics_cache:
        return _metrics_cache[key]
    result = _compute_metrics_uncached(run, judge_name, scope, generator)
    if result is not None:
        _metrics_cache[key] = result
    return result


def _compute_metrics_uncached(run: str, judge_name: str, scope: str, generator: str = "qwen4b") -> dict | None:
    _, judges = resolve_lane(run, generator)
    jd = judges.get(judge_name)
    if not jd or not (ROOT / jd).exists():
        return None
    files = sorted(f for f in os.listdir(ROOT / jd) if f.endswith(".json"))
    if scope == "first4":
        f4 = set(first4_uuids())
        files = [f for f in files if f[:-5] in f4]
    elif scope != "all":
        files = [f for f in files if f[:-5] == scope]
    if not files:
        return None

    skeleton = {
        "overall_score": {
            "memory_integrity": {}, "memory_accuracy": {}, "memory_extraction_f1": 0,
            "memory_update": {}, "question_answering": {},
            "memory_type_accuracy": {k: {"memory_integrity_acc": 0, "memory_update_acc": 0, "total_num": 0}
                                     for k in ["Event Memory", "Persona Memory", "Relationship Memory"]},
            "time_consuming": {"add_dialogue_duration_time": 0, "search_memory_duration_time": 0, "total_duration_time": 0},
        },
        "memory_integrity_records": [], "memory_accuracy_records": [],
        "memory_update_records": [], "question_answering_records": [],
    }
    for fn in files:
        with open(ROOT / jd / fn, encoding="utf-8") as f:
            u = json.load(f)
        for k in ["memory_integrity_records", "memory_accuracy_records", "memory_update_records", "question_answering_records"]:
            skeleton[k].extend(u.get(k, []))
    # QA 지표는 레코드 카운트만으로 계산: 일부 종류만 채점된 파일(--only qa)에서도 항상 유효
    qrecs = skeleton["question_answering_records"]
    nq = len(qrecs) or 1
    qcount = lambda t: round(sum(1 for r in qrecs if r.get("result_type") == t) / nq * 100, 2)
    try:
        o = aggregate_eval_results(skeleton)["overall_score"]
    except ZeroDivisionError:
        # 메모리 판정이 비어 있는 QA 전용 채점본: QA 지표만 돌려준다
        return {"n_users": len(files), "qa_only": True,
                "r": None, "wr": None, "acc": None, "acc_n": 0, "tp": None, "tp_n": 0,
                "fmr": None, "f1": None, "upd_c": None, "upd_h": None, "upd_o": None,
                "qa_c": qcount("Correct"), "qa_h": qcount("Hallucination"), "qa_o": qcount("Omission")}
    mi, ma, mu, qa = o["memory_integrity"], o["memory_accuracy"], o["memory_update"], o["question_answering"]
    pct = lambda v: round(v * 100, 2)
    return {
        "n_users": len(files),
        "r": pct(mi["recall(all)"]), "wr": pct(mi["weighted_recall(all)"]),
        "acc": pct(ma["weighted_accuracy(all)"]), "acc_n": ma["memory_num"],
        "tp": pct(ma["target_accuracy(all)"]), "tp_n": ma["target_memory_num"],
        "fmr": pct(ma["interference_accuracy(all)"]), "f1": pct(o["memory_extraction_f1"]),
        "upd_c": pct(mu["correct_update_memory_ratio(all)"]), "upd_h": pct(mu["hallucination_update_memory_ratio(all)"]),
        "upd_o": pct(mu["omission_update_memory_ratio(all)"]),
        "qa_c": pct(qa["correct_qa_ratio(all)"]), "qa_h": pct(qa["hallucination_qa_ratio(all)"]),
        "qa_o": pct(qa["omission_qa_ratio(all)"]),
    }


@app.post("/api/reload")
def api_reload():
    """서버측 캐시 전체 무효화: 새 런 동기화·judge 재채점 후 강제 재로딩용."""
    _judge_cache.clear()
    _metrics_cache.clear()
    load_run_users.cache_clear()
    first4_uuids.cache_clear()
    noise_floor.cache_clear()
    return {"ok": True}


@app.get("/api/first4")
def api_first4():
    """nano judge가 라벨을 보유한 데이터셋 첫 4유저 uuid: UI의 ★ 표시용."""
    return list(first4_uuids())


def compute_latency(run: str, scope: str) -> dict | None:
    """Stage A가 기록한 시간 실측: 세션 투입(mem0.add: LLM 콜 포함)과 질문 검색. 백본 속도 비교용."""
    try:
        users = load_run_users(run)
    except HTTPException:
        return None
    if scope == "first4":
        keep = set(first4_uuids())
    elif scope == "all":
        keep = set(users.keys())
    else:
        keep = {scope}
    adds, searches = [], []
    for uid, u in users.items():
        if uid not in keep:
            continue
        for s in u["sessions"]:
            d = s.get("add_dialogue_duration_ms")
            if d:
                adds.append(d)
            for q in s.get("questions", []):
                sd = q.get("search_duration_ms")
                if sd:
                    searches.append(sd)
    if not adds:
        return None
    adds.sort()
    return {
        "ingest_avg_s": round(sum(adds) / len(adds) / 1000, 1),
        "ingest_p50_s": round(adds[len(adds) // 2] / 1000, 1),
        "search_avg_ms": round(sum(searches) / max(len(searches), 1), 0),
        "n_sessions": len(adds),
    }


# 오라클 단계별로 '읽을 수 없게 되는' 지표: 화면에서 '–'로 가린다.
# 규칙은 단순한 계단이 아니다. 오라클을 넣으면 그 단계의 *내용 품질* 지표는 자명해져 죽지만,
# *생존율* 지표(R·Weighted R)와 다음 단계 지표(Upd)는 오히려 그 단계만 단독으로 재게 되어 살아난다.
#   추출 오라클  → 저장물이 골든 원문이라 Acc·Target P·F1이 자명하게 높고, FMR은 미끼를 원천 제외해 무의미
#                  (R·Weighted R은 '완벽한 추출이 저장까지 살아남은 비율' = 갱신 단계 손실의 단독 측정이라 유효)
#   갱신 오라클  → R·Weighted R이 정의상 100, Upd도 정의상 ~100이라 무의미
#   검색 오라클  → 답변이 저장소를 안 거치므로 QA 외 전부 무의미
ORACLE_MASK = {
    "extraction": ["acc", "tp", "fmr", "f1"],
    "update": ["r", "wr", "upd_c", "upd_h", "upd_o"],
    "retrieval": [],
}


def oracle_masked(oracle: str) -> list:
    """oracle 단계 문자열('extraction+update+retrieval')에서 가릴 지표 키 목록을 유도."""
    if not oracle:
        return []
    out = []
    for stage in oracle.split("+"):
        out += ORACLE_MASK.get(stage.strip(), [])
    return sorted(set(out))


def repeat_qa(tpl: str, run: str, scope: str, n_rep: int) -> tuple:
    """반복 회차 judge 디렉토리들에서 QA 지표 평균과 표본표준편차.

    같은 행이 표(단일 회차)와 사다리(반복 평균)에서 다른 값으로 보이면 안 되므로,
    반복 산출물이 있는 런은 표에서도 평균을 대표값으로 쓴다. 반환: (metrics|None, 회차값들, sd)
    """
    stats = [s for i in range(1, n_rep + 1)
             if (s := _qa_stats_from_dir(ROOT / tpl.format(i=i, run=run), scope))]
    if not stats:
        return None, [], None
    reps = [s["qa_c"] for s in stats]
    mean = lambda k: round(sum(s[k] for s in stats) / len(stats), 2)
    sd = None
    if len(reps) > 1:
        mu = sum(reps) / len(reps)
        sd = round((sum((x - mu) ** 2 for x in reps) / (len(reps) - 1)) ** 0.5, 2)
    return ({"n_users": stats[0]["n_users"], "qa_c": mean("qa_c"),
             "qa_h": mean("qa_h"), "qa_o": mean("qa_o")}, reps, sd)


def _metrics_row(name: str, r: dict, scope: str, metrics: dict, extra: dict | None = None) -> dict:
    row = {
        "run": name, "label": r.get("label", name),
        "backbone": r.get("backbone"), "prompt": r.get("prompt"),
        "oracle": r.get("oracle", ""),
        # retriever 종류. 미기재면 기존 전 실험의 기본값(임베딩). BM25 레인과 구분하기 위한 칼럼
        "retriever": r.get("retriever") or "Qwen3-Embedding-4B",
        "backbone_effort": r.get("backbone_effort"),
        "note": r.get("note", ""),
        "metrics": metrics,
        "latency": compute_latency(name, scope),  # Stage A 실측이라 generator 무관 (base 레인 기준)
    }
    row.update(extra or {})
    row["masked"] = oracle_masked(row.get("oracle", ""))
    return row


def _sd(xs: list) -> float:
    mu = sum(xs) / len(xs)
    return round((sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5, 2)


def _spearman(xs: list) -> float | None:
    """값 계열이 회차 순서에 따라 단조 증감하는지: 반복이 독립 시행인지 점검하는 지표.
    연속 루프로 돌린 회차들은 서버 상태를 공유해 드리프트가 생길 수 있다."""
    n = len(xs)
    if n < 3:
        return None
    order = sorted(range(n), key=lambda i: xs[i])
    rank = [0.0] * n
    for pos, i in enumerate(order):          # 동점은 평균 순위
        rank[i] = pos + 1.0
    for v in set(xs):
        idx = [i for i in range(n) if xs[i] == v]
        if len(idx) > 1:
            avg = sum(rank[i] for i in idx) / len(idx)
            for i in idx:
                rank[i] = avg
    d2 = sum((rank[i] - (i + 1)) ** 2 for i in range(n))
    return round(1 - 6 * d2 / (n * (n * n - 1)), 3)


@lru_cache(maxsize=4)
def noise_floor() -> dict | None:
    """'같은 실험을 다시 돌리면 수치가 얼마나 흔들리는가'의 실측값.

    ⚠ 연속 루프로 돌린 반복 회차만 쓰면 흔들림을 과소평가한다. 같은 실험을 **다른 날 다른 배치**로
    돌리면 배치 내부 표준편차의 몇 배씩 어긋나는 것이 실측됐다(4유저 oss120b4: 배치 내부 ±0.41인데
    7/31 배치는 그 범위 밖인 61.28). 회차들이 서버 상태를 공유해 독립 시행이 아니기 때문이다.
    그래서 **base 패스(별도 배치) + 반복 회차 전부를 한 표본으로 묶어** 통합 표준편차를 낸다.
    배치 내부 값(*_within)도 함께 돌려주어 둘의 차이를 화면에서 드러낸다.
    """
    doc = load_registry_doc()
    # 노이즈 바닥은 기준선(임베딩 실측)에서 잰다. 첫 번째 사다리의 actual 행
    lads = doc.get("oracle_ladders") or ([doc["oracle_ladder"]] if doc.get("oracle_ladder") else [])
    cfg = lads[0] if lads else {}
    step = next((s for s in cfg.get("steps", []) if s.get("key") == "actual"), None)
    if not step or not step.get("repeat_judge"):
        return None
    run = step["run"]
    within = [s for i in range(1, int(cfg.get("repeats", 0)) + 1)
              if (s := _qa_stats_from_dir(ROOT / step["repeat_judge"].format(i=i, run=run), "all"))]
    if len(within) < 2:
        return None
    # base 패스는 별개의 배치(다른 날 실행)라 배치 간 변동을 잡아준다
    pooled = list(within)
    try:
        _, judges = resolve_lane(run, step.get("generator"))
        jd = judges.get(step.get("judge"))
        if jd and (b := _qa_stats_from_dir(ROOT / jd, "all")) and b["n_q"] == within[0]["n_q"]:
            pooled = [b] + within
    except HTTPException:
        pass

    out = {"n_repeats": len(within), "n_obs": len(pooled), "n_batches": 1 + (len(pooled) > len(within)),
           "n_users": within[0]["n_users"], "run": run}
    for k in ("qa_c", "qa_h", "qa_o"):
        xs_w, xs_p = [s[k] for s in within], [s[k] for s in pooled]
        out[k] = _sd(xs_p)                       # 대표값 = 배치 간을 포함한 통합 SD
        out[k + "_within"] = _sd(xs_w)
        out[k + "_range"] = round(max(xs_p) - min(xs_p), 2)
    out["values"] = [s["qa_c"] for s in pooled]
    out["drift_rho"] = _spearman([s["qa_c"] for s in within])   # 회차 순서와의 상관 (독립성 점검)
    return out


def available_lane(run: str) -> tuple[str, str] | None:
    """이 런이 실제로 채점본을 가진 (generator, judge)를 하나 찾는다.

    BM25 레인처럼 4유저 배치로만 채점된 런은 기본 레인(1유저)에 judge가 없어
    compute_metrics가 None을 돌려주고, 그러면 Metrics 표에서 **행이 통째로 사라진다**.
    사라지면 Retriever 같은 칼럼이 영영 한 값만 보이게 되므로, 자기 레인으로
    대체해 보여주고 UI에는 📌로 '고정 레인'임을 밝힌다.
    """
    for gname in gen_registry():
        try:
            path, judges = resolve_lane(run, gname)
        except HTTPException:
            continue
        if not path.exists():
            continue
        for jname, jdir in judges.items():
            if (ROOT / jdir).exists():
                return gname, jname
    return None


# ---------- BEAM ----------

def beam_cfg() -> dict:
    return load_registry_doc().get("beam", {}) or {}


_beam_cache: dict = {}


def load_beam(bucket: str) -> dict:
    """버킷 하나의 채점본과 투입 산출물을 읽어 합침. mtime 기준 캐시.

    judge 레코드에 ability/cutoff/score/nugget_scores/used/stored 가 이미 들어 있음.
    투입 산출물에서는 대화 메타(주제·청크 수)와 검색된 메모리 원문만 가져옴.
    """
    cfg = (beam_cfg().get("buckets") or {}).get(bucket)
    if not cfg:
        raise HTTPException(404, f"unknown beam bucket: {bucket}")
    jdir = ROOT / cfg["judge"]
    if not jdir.exists():
        return {"ready": False, "records": [], "convs": {}}
    files = sorted(jdir.glob("*.json"))
    key = tuple(sorted((f.name, f.stat().st_mtime_ns) for f in files))
    # ⚠ 버킷마다 칸을 따로 둔다. 한 칸짜리로 두면 종합 화면(/api/beam/overview)이 버킷 여섯 개를
    #    연달아 읽을 때 서로를 밀어내 매번 전부 다시 파싱한다 (버킷당 수천 레코드).
    hit = _beam_cache.get(bucket)
    if hit and hit[0] == key:
        return hit[1]

    records = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            records += json.load(fh).get("records", [])

    convs = {}
    ing = ROOT / cfg["ingest"]
    if ing.exists():
        with open(ing, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                d = json.loads(line)
                convs[d["conv_id"]] = {
                    "category": d.get("category"), "title": d.get("title"),
                    "chunks": len(d.get("ingest") or []), "batches": d.get("batches"),
                    "stored": d.get("stored_memories"),
                    # 문항별 검색 결과는 상세 화면에서만 쓰므로 인덱스만 만들어 둠
                    "questions": {f'{q["ability"]}#{q["idx"]}': q for q in (d.get("questions") or [])},
                }
    val = {"ready": True, "records": records, "convs": convs, "cfg": cfg}
    _beam_cache[bucket] = (key, val)
    return val


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


@app.get("/api/beam")
def api_beam(bucket: str = "100k"):
    """능력 x cutoff 집계. 이 화면의 주 산출물임.

    ⚠ cutoff 가 그 대화의 저장 메모리 수보다 크면 검색이 작동하지 않은 칸임.
       used < cutoff 인 레코드 비율을 함께 내려 화면에서 표시하게 함.
    """
    cfg = beam_cfg()
    cuts = cfg.get("cutoffs") or [20, 50, 100, 200]
    abil_labels = cfg.get("abilities") or {}
    buckets = [{"key": k, "label": v.get("label", k), "note": v.get("note", ""),
                "ready": (ROOT / v["judge"]).exists()}
               for k, v in (cfg.get("buckets") or {}).items()]

    d = load_beam(bucket)
    rec = d["records"]
    if not rec:
        return {"bucket": bucket, "buckets": buckets, "cutoffs": cuts, "ready": False,
                "abilities": [], "overall": {}, "convs": [], "event_ordering": None}

    def cell(rows):
        if not rows:
            return None
        full = sum(1 for r in rows if r.get("stored") and (r.get("used") or 0) < r["cutoff"])
        return {"score": _mean([r["score"] for r in rows]), "n": len(rows),
                "full": full,  # 저장소가 모자라 cutoff 를 못 채운 건수
                "used": _mean([r.get("used") for r in rows])}

    abilities = []
    for a in sorted({r["ability"] for r in rec}):
        cells = {str(k): cell([r for r in rec if r["ability"] == a and r["cutoff"] == k]) for k in cuts}
        lo, hi = cells[str(cuts[0])], cells[str(cuts[-1])]
        abilities.append({"key": a, "label": abil_labels.get(a, a), "cells": cells,
                          "delta": round(hi["score"] - lo["score"], 4) if lo and hi else None})
    abilities.sort(key=lambda x: -(x["delta"] if x["delta"] is not None else 0))

    overall = {str(k): cell([r for r in rec if r["cutoff"] == k]) for k in cuts}

    # event_ordering 은 정의가 셋으로 갈림. 셋 다 내려보내고 화면에서 나란히 보여줌
    eo_rows = [r for r in rec if r["ability"] == "event_ordering" and r.get("event_ordering")]
    eo = None
    if eo_rows:
        g = lambda k: _mean([r["event_ordering"].get(k) for r in eo_rows])
        zero = lambda k: sum(1 for r in eo_rows if (r["event_ordering"].get(k) or 0) == 0)
        eo = {"n": len(eo_rows), "nugget": _mean([r["score"] for r in eo_rows]),
              "tau_norm": g("tau_norm"), "final_score": g("final_score"),
              "f1": g("f1"), "precision": g("precision"), "recall": g("recall"),
              "f1_zero": zero("f1"), "tau_zero": zero("tau_norm")}

    convs = []
    for cid in sorted({r["conv"] for r in rec}):
        meta = d["convs"].get(cid, {})
        rows = [r for r in rec if r["conv"] == cid]
        convs.append({"conv": cid, "category": meta.get("category"), "chunks": meta.get("chunks"),
                      "stored": meta.get("stored"),
                      "cells": {str(k): _mean([r["score"] for r in rows if r["cutoff"] == k]) for k in cuts}})

    stored = [c["stored"] for c in convs if c["stored"]]
    return {"bucket": bucket, "buckets": buckets, "cutoffs": cuts, "ready": True,
            "note": d["cfg"].get("note", ""), "n_records": len(rec),
            "n_questions": len({(r["conv"], r["ability"], r["idx"]) for r in rec}),
            "n_convs": len(convs), "stored_min": min(stored) if stored else None,
            "stored_max": max(stored) if stored else None,
            "abilities": abilities, "overall": overall, "convs": convs, "event_ordering": eo}


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else round((xs[n // 2 - 1] + xs[n // 2]) / 2, 4)


@app.get("/api/beam/overview")
def api_beam_overview():
    """모든 버킷을 한 번에. 규모 x 답변 프롬프트로 묶어 내려보냄.

    이 화면이 필요한 이유: BEAM 결론이 전부 '버킷 간' 또는 '프롬프트 간' 비교인데
    /api/beam 은 버킷 하나만 본다. 화면에서 버튼을 눌러가며 기억으로 비교하게 하면
    2026-08-19 에 겪은 것 같은 오독이 난다 (부분 표본 12대화를 확정값으로 읽음).

    ⚠ 규모 간 절대 비교는 성립하지 않는다. 버킷마다 대화 집합이 다르고 제목 겹침이 0이다.
       화면에서 그 경고를 함께 띄우도록 diff_ok 플래그를 내려준다.
    """
    cfg = beam_cfg()
    cuts = cfg.get("cutoffs") or [20, 50, 100, 200]
    abil_labels = cfg.get("abilities") or {}
    out = []
    for k, v in (cfg.get("buckets") or {}).items():
        if not (ROOT / v["judge"]).exists():
            out.append({"key": k, "label": v.get("label", k), "scale": v.get("scale"),
                        "prompt": v.get("prompt"), "ready": False})
            continue
        d = load_beam(k)
        rec = d["records"]
        if not rec:
            out.append({"key": k, "label": v.get("label", k), "scale": v.get("scale"),
                        "prompt": v.get("prompt"), "ready": False})
            continue
        by_cut = {}
        for c in cuts:
            rows = [r for r in rec if r["cutoff"] == c]
            by_cut[str(c)] = None if not rows else {
                "score": _mean([r["score"] for r in rows]), "n": len(rows),
                # 저장소가 모자라 cutoff 를 못 채운 건수. 이 칸은 검색이 작동하지 않았음
                "full": sum(1 for r in rows if r.get("stored") and (r.get("used") or 0) < r["cutoff"]),
            }
        ab = {}
        for a in sorted({r["ability"] for r in rec}):
            ab[a] = {str(c): _mean([r["score"] for r in rec if r["ability"] == a and r["cutoff"] == c])
                     for c in cuts}
        lens = [len(r.get("system_response") or "") for r in rec]
        len_ab = {a: _median([len(r.get("system_response") or "") for r in rec if r["ability"] == a])
                  for a in ab}
        stored = [c.get("stored") for c in d["convs"].values() if c.get("stored")]
        out.append({
            "key": k, "label": v.get("label", k), "scale": v.get("scale"),
            "prompt": v.get("prompt"), "ready": True, "note": v.get("note", ""),
            "n_convs": len(d["convs"]), "n_records": len(rec),
            "n_questions": len({(r["conv"], r["ability"], r["idx"]) for r in rec}),
            "stored_min": min(stored) if stored else None,
            "stored_max": max(stored) if stored else None,
            "stored_med": _median(stored),
            "overall": by_cut, "abilities": ab,
            "len_median": _median(lens), "len_by_ability": len_ab,
        })
    scales, prompts = [], []
    for b in out:
        if b.get("scale") and b["scale"] not in scales:
            scales.append(b["scale"])
        if b.get("prompt") and b["prompt"] not in prompts:
            prompts.append(b["prompt"])
    return {"cutoffs": cuts, "abilities": abil_labels, "buckets": out,
            "scales": scales, "prompts": prompts,
            # 규모 간 대화 집합이 달라 절대 비교 불가. 화면 경고문의 근거임
            "cross_scale_comparable": False}


@app.get("/api/beam/questions")
def api_beam_questions(bucket: str, ability: str):
    """능력 하나의 문항 목록. cutoff 별 점수를 나란히 놓아 어느 문항이 흔들리는지 보게 함."""
    d = load_beam(bucket)
    cuts = beam_cfg().get("cutoffs") or [20, 50, 100, 200]
    rows = [r for r in d["records"] if r["ability"] == ability]
    out = []
    for (conv, idx) in sorted({(r["conv"], r["idx"]) for r in rows}):
        rs = [r for r in rows if r["conv"] == conv and r["idx"] == idx]
        by = {str(r["cutoff"]): r for r in rs}
        cells = {str(k): (by[str(k)]["score"] if str(k) in by else None) for k in cuts}
        vals = [v for v in cells.values() if v is not None]
        out.append({
            "conv": conv, "idx": idx, "question": rs[0]["question"],
            "n_rubric": len(rs[0]["rubric"]), "stored": rs[0].get("stored"),
            "category": d["convs"].get(conv, {}).get("category"),
            "cells": cells,
            "spread": round(max(vals) - min(vals), 4) if vals else None,
        })
    out.sort(key=lambda x: -(x["spread"] or 0))
    return {"bucket": bucket, "ability": ability, "cutoffs": cuts, "questions": out}


@app.get("/api/beam/question")
def api_beam_question(bucket: str, conv: str, ability: str, idx: int):
    """문항 하나의 cutoff 4벌을 나란히. nugget 채점과 투입된 메모리까지 보여줌."""
    d = load_beam(bucket)
    rows = [r for r in d["records"] if r["conv"] == conv and r["ability"] == ability and r["idx"] == idx]
    if not rows:
        raise HTTPException(404, "no such question")
    rows.sort(key=lambda r: r["cutoff"])
    q = (d["convs"].get(conv, {}).get("questions") or {}).get(f"{ability}#{idx}") or {}
    return {"conv": conv, "ability": ability, "idx": idx,
            "question": rows[0]["question"], "rubric": rows[0]["rubric"],
            "reference": q.get("reference"), "difficulty": q.get("difficulty"),
            "category": d["convs"].get(conv, {}).get("category"),
            "stored": rows[0].get("stored"),
            "retrieved": (q.get("retrieved") or [])[:200],
            "cutoffs": [{"cutoff": r["cutoff"], "used": r.get("used"), "score": r["score"],
                         "system_response": r["system_response"],
                         "nugget_scores": r["nugget_scores"],
                         "event_ordering": r.get("event_ordering")} for r in rows]}


@app.get("/api/labels")
def api_labels():
    """UI 표시용 라벨. 내부 키(ctxmatch-4u 등)가 화면에 새어 나가지 않도록
    runs.yaml에서 사람용 이름을 받아 프런트의 기본 표와 병합한다."""
    return {"judge_names": load_registry_doc().get("judge_names", {}) or {}}


@app.get("/api/metrics")
def api_metrics(judge: str = "nano", scope: str = "first4", generator: str = "qwen4b",
                include_hidden: int = 0, show: str = ""):
    on = {g.strip() for g in show.split(",") if g.strip()}
    reg = load_registry(include_hidden=bool(include_hidden), show=on)
    rows = []
    for name, r in reg.items():
        if not (ROOT / r["results"]).exists():
            continue
        m = compute_metrics(name, judge, scope, generator)
        lane_gen, lane_jd, pinned = generator, judge, None
        if not m:
            alt = available_lane(name)
            if alt:
                lane_gen, lane_jd = alt
                m = compute_metrics(name, lane_jd, scope, lane_gen)
                if m:
                    # ⚠ extra_rows의 '고정 레인'과 구분해야 한다. 저쪽은 설계상 항상 그 조합이고,
                    #    이쪽은 사용자가 고른 레인이 없어서 대체된 것이다. 대체된 행은 다른 배치라
                    #    유저 집합·문항 수가 다를 수 있어 행 간 비교가 깨진다 (실측 사고:
                    #    oss120b4가 1유저 배치, oss120b4-bm25가 4유저 배치로 잡혀 UPD를 잘못 대조).
                    pinned = {"generator": lane_gen, "judge": lane_jd, "run": name,
                              "fallback": True,
                              "want_generator": generator, "want_judge": judge}
        # 반복 산출물이 있으면 QA만 평균으로 덮어쓴다 (메모리측 지표는 Stage A 산출이라 회차 무관)
        reps, sd = [], None
        tpl = r.get("repeat_judge")
        if m and tpl:
            rm, reps, sd = repeat_qa(tpl, name, scope, int(r.get("repeats", 5)))
            if rm:
                m = {**m, **{k: rm[k] for k in ("qa_c", "qa_h", "qa_o")}}
        extra = {}
        if reps:
            extra.update({"repeats": reps, "sd": sd})
        if pinned:
            extra["pinned_lane"] = pinned
        rows.append(_metrics_row(name, r, scope, m, extra=extra or None))

    # 고정 레인 행: generator·judge 드롭다운을 따르지 않고 runs.yaml에 박아둔 조합으로만 집계한다.
    # 검색 오라클처럼 '답변 생성 레인 자체가 실험 조건'인 세팅은 일반 런으로 표현할 수 없어서 필요하다.
    doc = load_registry_doc()
    for er in doc.get("extra_rows", []) or []:
        run, gen, jd = er.get("run"), er.get("generator"), er.get("judge")
        if run not in reg:
            continue
        # repeat_judge가 있으면 반복 회차 평균을 대표값으로 쓴다. 오라클 행은 QA 외 지표가 전부
        # 가려지므로 QA만 평균내면 충분하고, 단일 회차 노이즈(SD ~2.2p)에 휘둘리지 않는다.
        m, reps, sd = None, [], None
        tpl = er.get("repeat_judge")
        if tpl:
            n_rep = int(er.get("repeats", doc.get("oracle_ladder", {}).get("repeats", 0)) or 0)
            stats = [s for i in range(1, n_rep + 1)
                     if (s := _qa_stats_from_dir(ROOT / tpl.format(i=i, run=run), scope))]
            if stats:
                reps = [s["qa_c"] for s in stats]
                mean = lambda k: round(sum(s[k] for s in stats) / len(stats), 2)
                m = {"n_users": stats[0]["n_users"], "qa_only": True,
                     "r": None, "wr": None, "acc": None, "acc_n": 0, "tp": None, "tp_n": 0,
                     "fmr": None, "f1": None, "upd_c": None, "upd_h": None, "upd_o": None,
                     "qa_c": mean("qa_c"), "qa_h": mean("qa_h"), "qa_o": mean("qa_o")}
                if len(reps) > 1:
                    mu = sum(reps) / len(reps)
                    sd = round((sum((x - mu) ** 2 for x in reps) / (len(reps) - 1)) ** 0.5, 2)
        if m is None:
            try:
                m = compute_metrics(run, jd, scope, gen)
            except (HTTPException, ZeroDivisionError, FileNotFoundError):
                continue
        if not m or not m.get("n_users"):
            continue
        base = reg[run]
        rows.append(_metrics_row(run, base, scope, m, extra={
            "run": er.get("key", f"{run}:{gen}"),      # 행 식별자: 접기·하이라이트가 런 이름과 충돌하지 않게 분리
            "label": er.get("label", run),
            "backbone": er.get("backbone", base.get("backbone")),
            "prompt": er.get("prompt", base.get("prompt")),
            "oracle": er.get("oracle", ""),
            "retriever": er.get("retriever") or base.get("retriever") or "Qwen3-Embedding-4B",
            "backbone_effort": er.get("backbone_effort", ""),
            "note": er.get("note", ""),
            "metrics": m,
            "repeats": reps, "sd": sd,
            "pinned_lane": {"generator": gen, "judge": jd, "run": run},
        }))
    # 짝 재배열: pair_with가 있는 행(BM25)을 짝(임베딩) 바로 아래로 옮긴다.
    # 같은 오라클 단계끼리 임베딩/BM25가 붙어 있어야 검색기 차이를 눈으로 비교할 수 있다.
    idx = {r["run"]: i for i, r in enumerate(rows)}
    paired = [r for r in rows if reg.get(r["run"], {}).get("pair_with") in idx]
    if paired:
        rest = [r for r in rows if r not in paired]
        out = []
        for r in rest:
            out.append(r)
            out.extend(p for p in paired if reg[p["run"]]["pair_with"] == r["run"])
        placed = {id(x) for x in out}
        out.extend(p for p in paired if id(p) not in placed)   # 짝이 숨겨진 경우 뒤에 붙인다
        rows = out

    return {"judge": judge, "scope": scope, "generator": generator, "first4": list(first4_uuids()),
            "noise": noise_floor(), "rows": rows,
            "hide_groups": [{"key": k, **v} for k, v in hide_groups().items()]}


# ---------- 코멘트 ----------

DB_PATH = DATA_DIR / "comments.sqlite3"


@contextmanager
def db():
    """⚠ sqlite3 Connection의 `with`는 트랜잭션만 커밋하고 연결을 닫지 않는다.
    프론트가 4초마다 /api/comments를 폴링하므로 닫지 않으면 FD가 시간당 ~900개씩 쌓여
    'Too many open files'로 서버가 죽는다 (실제 발생). 반드시 finally에서 닫는다."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run TEXT NOT NULL, uuid TEXT NOT NULL, anchor TEXT NOT NULL,
        author TEXT NOT NULL, tag TEXT DEFAULT '', body TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    # additive 마이그레이션 (기존 레코드 보존: 새 컬럼은 빈 값)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(comments)")]
    if "quote" not in cols:
        conn.execute("ALTER TABLE comments ADD COLUMN quote TEXT DEFAULT ''")
    # 코멘트 작성 당시의 관측 세팅 (어떤 generator/judge 라벨을 보며 단 코멘트인지 재구성용)
    for col in ("generator", "judge", "run_b"):
        if col not in cols:
            conn.execute(f"ALTER TABLE comments ADD COLUMN {col} TEXT DEFAULT ''")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


class CommentIn(BaseModel):
    run: str
    uuid: str
    anchor: str  # 예: "session:12" / "session:12/mp:3" / "session:12/qa:1" / "session:12/turn:4" / "run"
    author: str
    tag: str = ""
    body: str
    quote: str = ""      # 드래그 하이라이트로 지정한 인용 텍스트 (선택)
    generator: str = ""  # 작성 당시 선택돼 있던 A' 레인 (예: mini)
    judge: str = ""      # 작성 당시 judge 라벨 세트 (예: mini-genmini)
    run_b: str = ""      # 작성 당시 비교(B) 런: extb 앵커의 대상 식별용


@app.post("/api/comments")
def add_comment(c: CommentIn):
    if not c.body.strip() or not c.author.strip():
        raise HTTPException(400, "author/body required")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO comments (run, uuid, anchor, author, tag, body, quote, generator, judge, run_b, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (c.run, c.uuid, c.anchor, c.author.strip(), c.tag.strip(), c.body, c.quote,
             c.generator, c.judge, c.run_b, datetime.now(timezone.utc).isoformat()),
        )
        return {"id": cur.lastrowid}


@app.get("/api/comments/{run}/{uuid}")
def list_comments(run: str, uuid: str):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM comments WHERE run=? AND uuid=? ORDER BY created_at", (run, uuid)).fetchall()
        return [dict(r) for r in rows]


@app.delete("/api/comments/{cid}")
def delete_comment(cid: int, author: str):
    # 인증 없는 내부 툴: 본인 이름이 일치할 때만 삭제 허용 (실수 방지 수준)
    with db() as conn:
        row = conn.execute("SELECT author FROM comments WHERE id=?", (cid,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such comment")
        if row["author"] != author:
            raise HTTPException(403, "author mismatch")
        conn.execute("DELETE FROM comments WHERE id=?", (cid,))
        return {"ok": True}


# ---------- 판정 주석 (analyst annotation) + IAA ----------

ANN_COLS = """id INTEGER PRIMARY KEY AUTOINCREMENT,
    run TEXT NOT NULL, uuid TEXT NOT NULL, session_id INTEGER NOT NULL,
    rec_type TEXT NOT NULL, idx INTEGER NOT NULL, target TEXT NOT NULL,
    generator TEXT DEFAULT '', annotator TEXT NOT NULL,
    label TEXT DEFAULT '', agree INTEGER, judge_name TEXT DEFAULT '', judge_label TEXT DEFAULT '',
    blind INTEGER DEFAULT 1, note TEXT DEFAULT '', gt_answer TEXT DEFAULT '', created_at TEXT NOT NULL"""


@contextmanager
def adb():
    """db()와 동일: 반드시 닫는다 (FD 누수 방지)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(f"CREATE TABLE IF NOT EXISTS annotations ({ANN_COLS})")
    # 한 분석가가 같은 항목을 두 번 라벨하면 갱신되도록 (QA는 generator 레인 종속이라 키에 포함)
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ann_key
        ON annotations (annotator, run, uuid, session_id, rec_type, idx, generator)""")
    # additive 마이그레이션 (기존 주석 보존)
    acols = [r["name"] for r in conn.execute("PRAGMA table_info(annotations)")]
    if "gt_answer" not in acols:
        conn.execute("ALTER TABLE annotations ADD COLUMN gt_answer TEXT DEFAULT ''")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


class AnnotationIn(BaseModel):
    run: str
    uuid: str
    session_id: int
    rec_type: str          # integrity | accuracy | update | qa
    idx: int
    target: str            # 대상 텍스트 스냅샷 (인덱스 변동 대비)
    generator: str = ""    # qa일 때만 의미 있음 (레인마다 답변이 다름)
    annotator: str
    label: str = ""        # 분석가가 매긴 판정 (0/1/2 또는 CORRECT/HALLUCINATION/OMISSION)
    agree: int | None = None   # judge 판정 동의 여부 (1/0)
    judge_name: str = ""
    judge_label: str = ""
    blind: int = 1
    note: str = ""
    gt_answer: str = ""   # gold_qa 검수 시 분석가가 제시하는 올바른 정답


@app.post("/api/annotations")
def add_annotation(a: AnnotationIn):
    if not a.annotator.strip():
        raise HTTPException(400, "annotator required")
    with adb() as conn:
        conn.execute(
            """INSERT INTO annotations
               (run, uuid, session_id, rec_type, idx, target, generator, annotator,
                label, agree, judge_name, judge_label, blind, note, gt_answer, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (annotator, run, uuid, session_id, rec_type, idx, generator)
               DO UPDATE SET label=excluded.label, agree=excluded.agree, judge_name=excluded.judge_name,
                 judge_label=excluded.judge_label, blind=excluded.blind, note=excluded.note,
                 gt_answer=excluded.gt_answer, created_at=excluded.created_at""",
            (a.run, a.uuid, a.session_id, a.rec_type, a.idx, a.target, a.generator,
             a.annotator.strip(), a.label, a.agree, a.judge_name, a.judge_label, a.blind, a.note,
             a.gt_answer, datetime.now(timezone.utc).isoformat()))
    return {"ok": True}


@app.get("/api/annotations")
def list_annotations(run: str | None = None, uuid: str | None = None, annotator: str | None = None):
    q, args = "SELECT * FROM annotations WHERE 1=1", []
    for col, val in (("run", run), ("uuid", uuid), ("annotator", annotator)):
        if val:
            q += f" AND {col}=?"
            args.append(val)
    with adb() as conn:
        return [dict(r) for r in conn.execute(q + " ORDER BY created_at", args).fetchall()]


@app.delete("/api/annotations/{aid}")
def delete_annotation(aid: int, annotator: str):
    with adb() as conn:
        row = conn.execute("SELECT annotator FROM annotations WHERE id=?", (aid,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such annotation")
        if row["annotator"] != annotator:
            raise HTTPException(403, "annotator mismatch")
        conn.execute("DELETE FROM annotations WHERE id=?", (aid,))
    return {"ok": True}


LABEL_FIELDS = {
    "integrity": ("memory_integrity_records", "memory_integrity_score", "memory_content"),
    "accuracy": ("memory_accuracy_records", "memory_accuracy_score", "memory_content"),
    "update": ("memory_update_records", "memory_update_type", "memory_content"),
    "qa": ("question_answering_records", "result_type", "question"),
}
# 순서가 있는 라벨: 분석가가 judge보다 관대/가혹한지(편향 방향)를 재려면 크기 비교가 가능해야 한다
ORDINAL = {"integrity": {"0": 0, "1": 1, "2": 2}, "accuracy": {"0": 0, "1": 1, "2": 2}}


def _item_target(run: str, uuid: str, rec_type: str, session_id: int, idx: int, generator: str):
    """주석이 가리키는 항목의 텍스트 키를 찾는다 (judge 레코드는 텍스트로 조인된다)."""
    for lane in [generator, "qwen4b", *gen_registry()]:
        if not lane:
            continue
        try:
            user = load_run_users(run, lane).get(uuid)
        except HTTPException:
            continue
        if user is None:
            continue
        try:
            s = user["sessions"][session_id]
        except IndexError:
            return None, None
        try:
            if rec_type in ("integrity", "update"):
                return s["memory_points"][idx]["memory_content"], lane
            if rec_type == "accuracy":
                return s["extracted_memories"][idx], lane
            if rec_type == "qa":
                return s["questions"][idx]["question"], lane
        except (IndexError, KeyError):
            return None, None
    return None, None


def judge_votes(run: str, uuid: str, rec_type: str, session_id: int, idx: int,
                generator: str, judge_name: str) -> dict | None:
    """이 항목에 대한 judge 반복 채점 결과와 다수결.

    ⚠ **같은 judge로 동일 입력을 반복 채점한 다수결**을 'judge 판정'으로 본다.
    재채점 불일치가 10~27%로 실측되므로(§16), 단일 회차와 대조하면 분석가 일치율이 judge 자기
    노이즈만큼 깎여 과소평가된다. 동률이면 합의가 없다고 보고 대조에서 제외한다.
    """
    spec = LABEL_FIELDS.get(rec_type)
    if not spec:
        return None
    field, key_name, id_field = spec
    spec_cfg = (load_registry_doc().get("judge_consensus") or {}).get("sets", {}).get(judge_name)
    if isinstance(spec_cfg, dict):
        names = list(spec_cfg.get("all") or [judge_name])
        if rec_type != "qa":     # 저장물 판정은 Stage A 입력만 쓰므로 배치가 달라도 같은 입력의 반복
            names += [n for n in (spec_cfg.get("store_only") or []) if n not in names]
    else:
        names = list(spec_cfg or [judge_name])
    target, lane = _item_target(run, uuid, rec_type, session_id, idx, generator)
    if target is None:
        return None
    # judge 세트는 generator 레인에 매달려 있다 (예: oss120-rep1은 oss120 레인 소속).
    # integrity/accuracy/update는 입력이 레인 무관 동일하므로 모든 레인을 뒤져 찾고,
    # qa만 답변 레인에 종속되므로 해당 generator 레인으로 제한한다.
    lanes = [generator] if (rec_type == "qa" and generator) else [generator, lane, *gen_registry()]
    votes = {}
    for jn in names:
        for g in lanes:
            if not g:
                continue
            jd = load_judge(run, jn, uuid, g)
            if not jd:
                continue
            for r in jd.get(field, []):
                if r.get("session_id") == session_id and r.get(id_field) == target:
                    v = r.get(key_name)
                    if v is not None:
                        votes[jn] = str(v)
                    break
            if jn in votes:
                break
    if not votes:
        return None
    tally: dict = {}
    for v in votes.values():
        tally[v] = tally.get(v, 0) + 1
    top = max(tally.values())
    winners = [k for k, c in tally.items() if c == top]
    return {"votes": votes, "tally": tally, "n_runs": len(votes),
            "consensus": winners[0] if len(winners) == 1 else None,
            "unanimous": len(tally) == 1, "tie": len(winners) > 1}


REJUDGE_DIR = ROOT / "results/mem0-classic-oss/rejudge-update"


def queue_keys() -> set:
    """판정 검토 큐에 담긴 항목 키: 분석가에게 실제로 배정된 표본."""
    if not QUEUE_PATH.exists():
        return set()
    with open(QUEUE_PATH, encoding="utf-8") as f:
        return {(x["run"], x["uuid"], x["session_id"], x["rec_type"], x["idx"])
                for x in json.load(f)["items"]}


def load_rejudge() -> dict:
    """judge 모델을 바꿔 재채점한 결과: 큐 항목만 (사람 대조가 가능한 집합).

    반환: {모델표시명: {(run,uuid,session_id,rec_type,idx): 라벨}}
    ⚠ 채점 프롬프트는 원본 judge와 동일하게 조립돼 있다 (rejudge_update.py). 모델만 다르다.
    """
    out: dict = {}
    if not REJUDGE_DIR.exists():
        return out
    for f in sorted(os.listdir(REJUDGE_DIR)):
        if not f.endswith(".json"):
            continue
        try:
            d = json.load(open(REJUDGE_DIR / f, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if d.get("scope") != "queue":
            continue
        name = f"{d['model']} ({d.get('effort') or 'none'})"
        m = out.setdefault(name, {})
        for it in d.get("items", []):
            if it.get("new_label") is None:
                continue
            m[(it["run"], it["uuid"], it["session_id"], it.get("rec_type", "update"), it["idx"])] = \
                str(it["new_label"])
    return out


def _kappa(pairs: list, ci: bool = False) -> dict:
    """Cohen's κ + 단순 일치율. pairs: [(labelA, labelB), ...]
    ci=True면 부트스트랩 95% 신뢰구간을 함께 낸다 (표본이 작을 때 κ 한 자리 비교를 막기 위함)."""
    n = len(pairs)
    if not n:
        return {"n": 0, "agree": None, "kappa": None}

    def k_of(ps):
        m = len(ps)
        if not m:
            return None
        po = sum(1 for a, b in ps if a == b) / m
        labs = {x for p in ps for x in p}
        ca = {l: sum(1 for a, _ in ps if a == l) / m for l in labs}
        cb = {l: sum(1 for _, b in ps if b == l) / m for l in labs}
        pe = sum(ca[l] * cb[l] for l in labs)
        return None if pe >= 1 else (po - pe) / (1 - pe)

    po = sum(1 for a, b in pairs if a == b) / n
    k = k_of(pairs)
    out = {"n": n, "ok": sum(1 for a, b in pairs if a == b),
           "agree": round(po * 100, 1), "kappa": None if k is None else round(k, 3)}
    if ci and n >= 10 and k is not None:
        import random
        rnd = random.Random(20260807)     # 새로고침마다 값이 흔들리지 않도록 고정 시드
        ks = sorted(x for _ in range(400)
                    if (x := k_of([pairs[rnd.randrange(n)] for _ in range(n)])) is not None)
        if len(ks) >= 40:
            out["ci"] = [round(ks[int(len(ks) * 0.025)], 3), round(ks[int(len(ks) * 0.975)], 3)]
    return out


@app.get("/api/gold-qa")
def api_gold_qa(uuid: str | None = None, generator: str = "oss120-4u", judge: str = "oss120-genoss120-4u"):
    """골든 정답 검수 결과: 벤치마크 문항 자체의 품질을 사람이 판정한 것.

    judge 판정 검토와 묻는 것이 다르다: 저쪽은 '채점이 맞았나', 이쪽은 **'문항이 채점 기준으로
    쓸 만한가'**를 묻는다. 네 라벨은 문제의 **원인**을 가르며 고치는 방법이 각각 다르다
    (근거 없음 -> 문항 제외 / 표현 자의적 -> 채점 완화 / 오답 -> 정답 수정).

    핵심 산출물은 **라벨 × 시스템 정답률 교차표**다. 분석가가 '대화에 근거 없음'으로 표시한
    문항에서 실제로 모든 시스템이 틀렸다면, 그 진단이 데이터로 확인되고 동시에
    '현재 QA 점수 중 몇 %가 벤치마크 결함 탓인지'가 정량화된다 (§13의 추정을 실측으로 대체).
    """
    rows = [r for r in list_annotations(uuid=uuid) if r["rec_type"] == "gold_qa" and r["label"]]
    if not rows:
        return {"n": 0, "annotators": [], "items": [], "by_label": {}, "runs": [], "cross": [],
                "total_q": 0, "progress": []}

    target_uuid = uuid or rows[0]["uuid"]
    rows = [r for r in rows if r["uuid"] == target_uuid]

    # 구 라벨 접기: 'ambiguous'(정답이 여럿)는 조치가 'wrong'과 같아 라벨을 합쳤다.
    # 이미 달린 주석은 지우지 않고(분석가 작업 보존) 집계에서만 접는다. 원본은 DB에 그대로 남는다.
    LEGACY = {"ambiguous": "wrong"}
    for r in rows:
        r["label"] = LEGACY.get(r["label"], r["label"])


    # 문항 총수: 이 유저의 QA 문항 (생성된 QA 세션 제외)
    total_q, qtext = 0, {}
    user = None
    # 문항 원문은 어느 런에서 읽어도 같다 (골든 질문·정답은 데이터셋 고유): 열리는 레인 아무거나 쓴다
    for cand in ["oss120b4", *[r for r in load_registry() if load_registry()[r].get("users") == 4]]:
        try:
            user = load_run_users(cand, generator).get(target_uuid)
        except Exception:
            user = None
        if user:
            break
    if user:
        for si, sess in enumerate(user["sessions"]):
            if sess.get("is_generated_qa_session"):
                continue
            for qi, q in enumerate(sess.get("questions") or []):
                total_q += 1
                qtext[(si, qi)] = {"question": q.get("question", ""), "answer": q.get("answer", ""),
                                   "n_evidence": len(q.get("evidence") or [])}

    by_item: dict = {}
    for r in rows:
        by_item.setdefault((r["session_id"], r["idx"]), {})[r["annotator"]] = {
            "label": r["label"], "gt": r.get("gt_answer") or "", "note": r.get("note") or ""}

    def consensus(d: dict):
        c: dict = {}
        for v in d.values():
            c[v["label"]] = c.get(v["label"], 0) + 1
        top = max(c.values())
        win = [k for k, v in c.items() if v == top]
        return win[0] if len(win) == 1 else None

    annotators = sorted({r["annotator"] for r in rows})
    progress = [{"annotator": a, "n": sum(1 for r in rows if r["annotator"] == a)} for a in annotators]

    # 분석가 간 일치도 (gold_qa 라벨 기준)
    pairs_out = []
    for i, a in enumerate(annotators):
        for b2 in annotators[i + 1:]:
            pr = [(v[a]["label"], v[b2]["label"]) for v in by_item.values() if a in v and b2 in v]
            st = _kappa(pr)
            if st["n"]:
                pairs_out.append({"a": a, "b": b2, **st})

    # 라벨 × 시스템 정답률: 각 런이 그 문항을 맞혔는지
    reg = load_registry()
    runs = [r for r in reg if reg[r].get("users") == 4 and not reg[r].get("oracle")]
    qa_ok: dict = {}
    for run in runs:
        # ⚠ 한 런만 레인이 없어도 resolve_lane이 404를 던져 화면 전체가 못 뜬다. 런 단위로 격리한다
        try:
            jd = load_judge(run, judge, target_uuid, generator)
            u2 = load_run_users(run, generator).get(target_uuid) if jd else None
        except Exception:
            continue
        if not jd or not u2:
            continue
        idx_of = {}
        for si, sess in enumerate(u2["sessions"]):
            for qi, q in enumerate(sess.get("questions") or []):
                idx_of[(si, q["question"])] = qi
        for rec in jd.get("question_answering_records", []):
            k = idx_of.get((rec.get("session_id"), rec.get("question")))
            if k is not None:
                qa_ok.setdefault((rec["session_id"], k), {})[run] = rec.get("result_type") == "Correct"

    live_runs = sorted({r for v in qa_ok.values() for r in v})
    cross = []
    for lab in ["valid", "wrong", "unanswerable"]:
        keys = [k for k, v in by_item.items() if consensus(v) == lab]
        if not keys:
            continue
        row = {"label": lab, "n": len(keys), "runs": {}}
        for run in live_runs:
            got = [qa_ok.get(k, {}).get(run) for k in keys]
            got = [g for g in got if g is not None]
            if got:
                row["runs"][run] = {"ok": sum(got), "n": len(got),
                                    "pct": round(sum(got) / len(got) * 100, 1)}
        # 어떤 시스템도 못 맞힌 문항 수: '원리적으로 불가'의 직접 증거
        row["none_solved"] = sum(1 for k in keys
                                 if qa_ok.get(k) and not any(qa_ok[k].values()))
        cross.append(row)

    items = []
    for (si, qi), v in sorted(by_item.items()):
        c = consensus(v)
        items.append({"session_id": si, "idx": qi, "consensus": c,
                      "labels": {a: d["label"] for a, d in v.items()},
                      "gt": next((d["gt"] for d in v.values() if d["gt"]), ""),
                      "note": next((d["note"] for d in v.values() if d["note"]), ""),
                      "solved_by": sorted(r for r, ok in (qa_ok.get((si, qi)) or {}).items() if ok),
                      "n_systems": len(qa_ok.get((si, qi)) or {}),
                      **(qtext.get((si, qi)) or {})})

    by_label: dict = {}
    for v in by_item.values():
        c = consensus(v)
        by_label[c or "동률"] = by_label.get(c or "동률", 0) + 1

    return {"uuid": target_uuid, "user_name": (user or {}).get("user_name", ""),
            "n": len(rows), "n_items": len(by_item), "total_q": total_q,
            "annotators": annotators, "progress": progress, "pairs": pairs_out,
            "by_label": by_label, "runs": live_runs, "cross": cross, "items": items}


@app.get("/api/iaa")
def api_iaa(run: str | None = None, uuid: str | None = None):
    """분석가 간(IAA) · 분석가 vs judge 합의 일치도. 항목 키는 (run,uuid,session,rec_type,idx,generator).

    'judge 판정'은 **반복 채점의 다수결**이다 (단일 채점본을 쓰지 않는다) (judge_consensus 참조).
    반복 세트가 없는 항목은 저장된 스냅샷 라벨로 폴백하고, 그 비율을 함께 보고한다.
    """
    rows = list_annotations(run=run, uuid=uuid)
    all_labeled = [r for r in rows if r["label"]]
    # ⚠ 집계는 **판정 검토 큐 항목만** 쓴다. 분석가에게 배정된 표본이 그것이고, 층화 추출이라
    #    라벨 분포가 통제돼 있다. 개별 검토(큐 밖)는 '이건 judge가 틀린 것 같다' 싶을 때 누른
    #    기회 표본이라 어려운 항목에 쏠려 있어(integrity 195건) 섞으면 지표가 왜곡된다.
    #    같은 양을 두 표가 다른 숫자로 보여주는 문제도 여기서 비롯됐다.
    qk = queue_keys()
    labeled = [r for r in all_labeled
               if (r["run"], r["uuid"], r["session_id"], r["rec_type"], r["idx"]) in qk]
    n_outside = len(all_labeled) - len(labeled)
    by_item: dict = {}
    for r in labeled:
        key = (r["run"], r["uuid"], r["session_id"], r["rec_type"], r["idx"], r["generator"])
        by_item.setdefault(key, {})[r["annotator"]] = r

    # ── judge 합의 라벨 계산 (항목 단위 1회, 주석 여러 건이 같은 항목을 공유하므로) ──
    consensus: dict = {}
    n_multi = n_unan = n_tie = n_fallback = 0
    for key in by_item:
        r0 = next(iter(by_item[key].values()))
        v = judge_votes(key[0], key[1], key[3], key[2], key[4], key[5], r0.get("judge_name") or "")
        if v and v["n_runs"] > 1:
            n_multi += 1
            n_unan += 1 if v["unanimous"] else 0
            n_tie += 1 if v["tie"] else 0
        if not v or v["consensus"] is None:
            snap = r0.get("judge_label")
            if not v and snap not in (None, ""):
                n_fallback += 1
            consensus[key] = {"label": snap or None, "n_runs": (v or {}).get("n_runs", 0),
                              "unanimous": (v or {}).get("unanimous"), "tie": bool(v and v["tie"]),
                              "tally": (v or {}).get("tally", {}), "source": "snapshot" if not v else "tie"}
        else:
            consensus[key] = {"label": v["consensus"], "n_runs": v["n_runs"],
                              "unanimous": v["unanimous"], "tie": False,
                              "tally": v["tally"], "source": "consensus"}

    def ikey(r):
        return (r["run"], r["uuid"], r["session_id"], r["rec_type"], r["idx"], r["generator"])

    def jlab(r):
        return consensus.get(ikey(r), {}).get("label")

    cmpable = [r for r in labeled if jlab(r) not in (None, "") and r["rec_type"] in LABEL_FIELDS]

    annotators = sorted({r["annotator"] for r in labeled})
    # ── 분석가 쌍별 IAA (같은 항목을 둘 다 라벨한 경우만) ──
    pairs_out = []
    for i, a in enumerate(annotators):
        for b in annotators[i + 1:]:
            shared = [v for v in by_item.values() if a in v and b in v]
            st = _kappa([(v[a]["label"], v[b]["label"]) for v in shared], ci=True)
            if st["n"]:
                by_t = {}
                for v in shared:
                    t = v[a]["rec_type"]
                    by_t.setdefault(t, []).append((v[a]["label"], v[b]["label"]))
                st["by_type"] = [{"rec_type": t, **_kappa(p)} for t, p in sorted(by_t.items())]
                pairs_out.append({"a": a, "b": b, **st})

    # ── 분석가 vs judge 합의 ──
    vs_judge = []
    for a in annotators:
        mine = [r for r in cmpable if r["annotator"] == a]
        st = _kappa([(r["label"], jlab(r)) for r in mine], ci=True)
        if not st["n"]:
            continue
        # 편향: 순서 라벨에서 분석가가 judge보다 얼마나 높게/낮게 주는가 (+면 관대)
        diffs = [ORDINAL[r["rec_type"]][r["label"]] - ORDINAL[r["rec_type"]][jlab(r)]
                 for r in mine if r["rec_type"] in ORDINAL
                 and r["label"] in ORDINAL[r["rec_type"]] and jlab(r) in ORDINAL[r["rec_type"]]]
        st["bias"] = round(sum(diffs) / len(diffs), 3) if diffs else None
        st["bias_n"] = len(diffs)
        st["done_types"] = {t: sum(1 for r in mine if r["rec_type"] == t)
                            for t in ["integrity", "accuracy", "update", "qa"]}
        vs_judge.append({"annotator": a, **st})

    # ── 레코드 타입별 (전체 분석가 합산) + judge 만장일치/분열 분해 ──
    by_type = []
    for t in ["integrity", "accuracy", "update", "qa"]:   # gold_qa는 judge 대조 대상이 아니므로 제외
        mine = [r for r in cmpable if r["rec_type"] == t]
        st = _kappa([(r["label"], jlab(r)) for r in mine], ci=True)
        if not st["n"]:
            continue
        firm = [r for r in mine if consensus[ikey(r)].get("unanimous") is True]
        split = [r for r in mine if consensus[ikey(r)].get("unanimous") is False]
        st["firm"] = _kappa([(r["label"], jlab(r)) for r in firm])
        st["split"] = _kappa([(r["label"], jlab(r)) for r in split])
        # 혼동 행렬 (분석가 라벨 → judge 합의 라벨). 누가 어느 방향으로 어긋나는지 보려고
        # 분석가 아이디까지 키에 넣는다. 합산만 보면 체계적 이견인지 개인차인지 구분이 안 된다.
        conf: dict = {}
        for r in mine:
            conf[(r["annotator"], r["label"], jlab(r))] = conf.get((r["annotator"], r["label"], jlab(r)), 0) + 1
        st["confusion"] = sorted(
            ({"annotator": w, "mine": a, "judge": j, "n": c} for (w, a, j), c in conf.items()),
            key=lambda x: (-x["n"], x["annotator"], x["mine"], x["judge"]))
        by_type.append({"rec_type": t, **st})

    # ── 큐 진척률 (분석가별) ──
    progress = None
    if QUEUE_PATH.exists():
        with open(QUEUE_PATH, encoding="utf-8") as f:
            q = json.load(f)
        qkeys = {(x["run"], x["uuid"], x["session_id"], x["rec_type"], x["idx"]) for x in q["items"]}
        qtypes: dict = {}
        for x in q["items"]:
            qtypes[x["rec_type"]] = qtypes.get(x["rec_type"], 0) + 1
        per: dict = {}
        for r in labeled:
            k = (r["run"], r["uuid"], r["session_id"], r["rec_type"], r["idx"])
            slot = per.setdefault(r["annotator"], {"in_queue": 0, "out": 0, "by_type": {}})
            if k in qkeys:
                slot["in_queue"] += 1
                slot["by_type"][r["rec_type"]] = slot["by_type"].get(r["rec_type"], 0) + 1
            else:
                slot["out"] += 1
        progress = {"queue_n": q["n"], "queue_types": qtypes,
                    "annotators": [{"annotator": a, **v} for a, v in sorted(per.items())]}

    # ── 사람 판정 vs judge (§18) ──
    # 같은 항목을 여러 judge 모델로 재채점한 결과를, **사람 쪽 기준을 바꿔가며** 대조한다.
    #   기준 '합의' : 3인 다수결 (동률 제외) : 대표값
    #   기준 '개인' : 그 분석가의 라벨 그대로: 사람마다 judge와 얼마나 맞는지
    # 기준 judge와의 비교는 **대응표본**(같은 항목)이므로 독립 CI 대신 McNemar로 판정한다.
    # 독립표본 CI로 보면 크게 겹쳐 '구분 불가'로 오판하게 된다(§18①).
    rj = load_rejudge()
    matrices: dict = {}
    if rj:
        by_key: dict = {}
        for r in labeled:
            by_key.setdefault((r["run"], r["uuid"], r["session_id"], r["rec_type"], r["idx"]), {})[
                r["annotator"]] = r["label"]

        def _consensus(d: dict):
            t: dict = {}
            for v in d.values():
                t[v] = t.get(v, 0) + 1
            top = max(t.values())
            win = [k for k, v in t.items() if v == top]
            return win[0] if len(win) == 1 else None

        # 기존 judge 라벨: 주석에 저장된 스냅샷 (재채점 파일의 base_label과 같은 값)
        base_lab = {}
        for r in labeled:
            k = (r["run"], r["uuid"], r["session_id"], r["rec_type"], r["idx"])
            if r.get("judge_label"):
                base_lab[k] = str(r["judge_label"])

        def build(href: dict) -> list:
            """href: 항목키 -> 사람 라벨. 모델별 × 유형별 일치/κ/McNemar를 낸다."""
            def mcnemar(a, b, keys):
                bo = sum(1 for k in keys if a.get(k) != href[k] and b.get(k) == href[k])
                ao = sum(1 for k in keys if a.get(k) == href[k] and b.get(k) != href[k])
                n = bo + ao
                if n == 0:
                    return bo, ao, 1.0
                import math
                mn = min(bo, ao)
                return bo, ao, min(2 * sum(math.comb(n, i) for i in range(mn + 1)) / 2 ** n, 1.0)

            # ⚠ 모든 열을 **같은 항목**에서 재야 나란히 놓을 수 있다. 재채점 모델이 커버하지 못한
            #    항목(추출 메모리가 0개라 judge.py가 LLM 없이 0점 처리하는 케이스: judge.py:127)은
            #    기준 judge만 갖고 있어, 빼지 않으면 기준 쪽 분모만 커지고 쉬운 항목이 섞인다.
            common = sorted(set(href) & set(base_lab) & set.intersection(*(set(m) for m in rj.values())))
            out = []
            for name, m in [("gpt-oss-120b (high): 기존", base_lab)] + sorted(rj.items()):
                keys = [k for k in common if k in m]
                if not keys:
                    continue
                st = _kappa([(href[k], m[k]) for k in keys], ci=True)
                st.update(model=name, by_type={})
                is_base = name.startswith("gpt-oss-120b")
                for t in ["integrity", "accuracy", "update", "qa"]:
                    kk = [k for k in keys if k[3] == t]
                    if not kk:
                        continue
                    st["by_type"][t] = _kappa([(href[k], m[k]) for k in kk])
                    if is_base:
                        # judge가 반복 채점에서 흔들리지 않은 항목 / 갈린 항목으로 나눠 본다.
                        firm = [k for k in kk if consensus.get(k + ("",), {}).get("unanimous") is True]
                        split = [k for k in kk if consensus.get(k + ("",), {}).get("unanimous") is False]
                        st["by_type"][t]["firm"] = _kappa([(href[k], m[k]) for k in firm])
                        st["by_type"][t]["split"] = _kappa([(href[k], m[k]) for k in split])
                    else:
                        bo, ao, p = mcnemar(base_lab, m, [k for k in kk if k in base_lab])
                        st["by_type"][t]["vs_base"] = {"better": bo, "worse": ao, "p": round(p, 4)}
                    # 순서 라벨에서 사람이 judge보다 얼마나 높게 주는가 (+면 사람이 관대)
                    d = [ORDINAL[t][href[k]] - ORDINAL[t][m[k]] for k in kk
                         if t in ORDINAL and href[k] in ORDINAL[t] and m[k] in ORDINAL[t]]
                    if d:
                        st["by_type"][t]["bias"] = round(sum(d) / len(d), 3)
                if not is_base:
                    shared = [k for k in keys if k in base_lab]
                    bo, ao, p = mcnemar(base_lab, m, shared)
                    st["vs_base"] = {"n": len(shared), "better": bo, "worse": ao, "p": round(p, 4)}
                out.append(st)
            return out

        matrices["합의"] = build({k: c for k in by_key if (c := _consensus(by_key[k])) is not None})
        for a in sorted({r["annotator"] for r in labeled}):
            href = {k: v[a] for k, v in by_key.items() if a in v}
            if href:
                matrices[a] = build(href)

    # 숨긴 런(custom 프롬프트 갈래)에 달린 주석은 지우지 않고 그대로 센다. 이미 보고된 수치의
    # 재현성을 지키기 위함. 대신 얼마나 섞여 있는지는 화면에 밝힌다.
    hidden_runs = set(load_registry_doc()["runs"]) - set(load_registry())
    n_hidden = sum(1 for r in labeled if r["run"] in hidden_runs)

    cfg = load_registry_doc().get("judge_consensus") or {}
    return {
        "hidden_runs": sorted(hidden_runs), "hidden_labeled": n_hidden,
        "matrices": matrices, "matrix_refs": list(matrices),
        "outside_queue": n_outside,
        "total": len(rows), "labeled": len(labeled), "annotators": annotators,
        "items": len(by_item), "overlap_items": sum(1 for v in by_item.values() if len(v) > 1),
        "comparable": len(cmpable),
        "judge_basis": {"label": cfg.get("label", ""), "note": cfg.get("note", ""),
                        "multi_items": n_multi, "unanimous": n_unan, "tie": n_tie,
                        "fallback_items": n_fallback},
        "annotator_pairs": pairs_out, "vs_judge": vs_judge, "by_type": by_type,
        "progress": progress,
        "agree_clicks": {"agree": sum(1 for r in rows if r["agree"] == 1),
                         "disagree": sum(1 for r in rows if r["agree"] == 0)},
    }


def _qa_stats_from_dir(jdir: Path, scope: str) -> dict | None:
    """judge 디렉토리에서 QA 지표만 직접 계산 (레인 등록 없이 반복 회차를 읽기 위함).
    공식 집계와 동일한 레코드 카운트 방식이라 --only qa 채점본에서도 유효하다."""
    if not jdir.exists():
        return None
    files = sorted(f for f in os.listdir(jdir) if f.endswith(".json"))
    if scope == "first4":
        f4 = set(first4_uuids())
        files = [f for f in files if f[:-5] in f4]
    elif scope != "all":
        files = [f for f in files if f[:-5] == scope]
    recs = []
    for fn in files:
        with open(jdir / fn, encoding="utf-8") as f:
            recs.extend(json.load(f).get("question_answering_records", []))
    if not recs:
        return None
    n = len(recs)
    pct = lambda t: round(sum(1 for r in recs if r.get("result_type") == t) / n * 100, 2)
    return {"qa_c": pct("Correct"), "qa_h": pct("Hallucination"), "qa_o": pct("Omission"),
            "n_users": len(files), "n_q": n}


def _qa_correct_from_dir(jdir: Path, scope: str) -> float | None:
    s = _qa_stats_from_dir(jdir, scope)
    return s["qa_c"] if s else None


@app.get("/api/oracle-ladder")
def api_oracle_ladder(scope: str = "first4"):
    """단계별 오라클 상한 사다리: 각 단계를 완벽하게 만들었을 때의 QA 상한과 구간별 기여분.
    아직 안 돌린 단계는 metrics=None으로 내려가 화면에서 '미실행'으로 표시된다."""
    doc = load_registry_doc()
    ladders = doc.get("oracle_ladders") or ([doc["oracle_ladder"]] if doc.get("oracle_ladder") else [])
    out = [_ladder_rows(cfg, scope) for cfg in ladders]
    # 첫 사다리를 rows로도 내려 기존 소비자(및 noise_floor)의 계약을 유지한다
    return {"ladders": out, "scope": scope,
            "stage_names": {"extraction": "추출", "update": "갱신", "retrieval": "저장·검색"},
            "note": out[0]["note"] if out else "", "n_repeats": out[0]["n_repeats"] if out else 0,
            "rows": out[0]["rows"] if out else []}


def _ladder_rows(cfg: dict, scope: str) -> dict:
    reg = load_registry()
    rows, prev = [], None
    for st in cfg.get("steps", []):
        run, gen, judge = st.get("run"), st.get("generator"), st.get("judge")
        m = None
        if run in reg:
            try:
                m = compute_metrics(run, judge, scope, gen)
            except HTTPException:
                m = None
        qa = m["qa_c"] if m else None
        # 반복 회차: 레인 등록 없이 경로 템플릿으로 직접 읽어 평균·표준편차 산출
        reps, rep_users = [], None
        tpl = st.get("repeat_judge")
        if tpl:
            for i in range(1, int(cfg.get("repeats", 0)) + 1):
                s = _qa_stats_from_dir(ROOT / tpl.format(i=i, run=run), scope)
                if s is not None:
                    reps.append(s["qa_c"])
                    rep_users = s["n_users"]
        mean = sd = None
        if reps:
            mean = round(sum(reps) / len(reps), 2)
            if len(reps) > 1:
                var = sum((x - mean) ** 2 for x in reps) / (len(reps) - 1)
                sd = round(var ** 0.5, 2)
            qa = mean  # 반복이 있으면 평균을 대표값으로
        rows.append({
            "repeats": reps, "mean": mean, "sd": sd,
            "key": st.get("key"), "label": st.get("label"), "stages": st.get("stages", []),
            "run": run, "run_label": st.get("run_display") or reg.get(run, {}).get("label", run),
            "generator": gen, "judge": judge, "desc": st.get("desc", ""),
            "qa_c": qa, "qa_h": m["qa_h"] if m else None, "qa_o": m["qa_o"] if m else None,
            "n_users": (m["n_users"] if m else None) or rep_users,
            "delta": None if (qa is None or prev is None) else round(qa - prev, 2),
        })
        if qa is not None:
            prev = qa
    return {"key": cfg.get("key", ""), "label": cfg.get("label", ""),
            "note": cfg.get("note", ""), "n_repeats": int(cfg.get("repeats", 0)), "rows": rows}


@app.get("/api/judge-consistency")
def api_judge_consistency(run: str = "oss120b4", generator: str = "oss120",
                          judges: str = "oss120-genoss120,oss120-rep1,oss120-rep2,oss120-rep3"):
    """동일 입력을 같은 judge로 반복 채점한 세트들의 자기 일관성.
    '집계는 안정한데 개별 판정은 흔들린다'를 화면에서 바로 확인하기 위한 요약."""
    names = [j.strip() for j in judges.split(",") if j.strip()]
    _, avail = resolve_lane(run, generator)
    names = [j for j in names if j in avail and (ROOT / avail[j]).exists()]
    if len(names) < 2:
        return {"run": run, "judges": names, "rows": [], "note": "반복 채점 세트가 2개 미만"}

    uuids = sorted({f[:-5] for j in names for f in os.listdir(ROOT / avail[j]) if f.endswith(".json")})
    specs = [("memory_integrity_records", "memory_integrity_score", "memory_content", "Integrity"),
             ("memory_accuracy_records", "memory_accuracy_score", "memory_content", "Accuracy"),
             ("memory_update_records", "memory_update_type", "memory_content", "Update"),
             ("question_answering_records", "result_type", "question", "QA")]
    rows = []
    for key, fld, idf, label in specs:
        maps = []
        for j in names:
            m = {}
            for uid in uuids:
                jd = load_judge(run, j, uid, generator)
                if jd:
                    for r in jd.get(key, []):
                        m[(uid, r["session_id"], r[idf])] = str(r.get(fld))
            maps.append(m)
        common = set.intersection(*[set(m) for m in maps]) if maps else set()
        if not common:
            continue
        unanim = dev = tot = 0
        by_major: dict = {}
        for k in common:
            vals = [m[k] for m in maps]
            cnt: dict = {}
            for v in vals:
                cnt[v] = cnt.get(v, 0) + 1
            maj = max(cnt, key=lambda x: cnt[x])
            if len(cnt) == 1:
                unanim += 1
            dev += sum(1 for v in vals if v != maj)
            tot += len(vals)
            b = by_major.setdefault(maj, [0, 0])
            b[0] += 1
            if len(cnt) > 1:
                b[1] += 1
        rows.append({
            "rec_type": label, "n": len(common), "reps": len(names),
            "unanimous": round(unanim / len(common) * 100, 1),
            "deviation": round(dev / tot * 100, 1),
            "by_major": {k: {"n": v[0], "unstable": round(v[1] / v[0] * 100, 1)}
                         for k, v in sorted(by_major.items())},
        })
    return {"run": run, "generator": generator, "judges": names, "users": len(uuids), "rows": rows}


QUEUE_PATH = DATA_DIR / "annotation_queue.json"


@app.get("/api/queue")
def api_queue(rebuild: int = 0, per_type: int = 40, judge: str = "oss120-genoss120",
              generator: str = "oss120"):
    """공유 표본 큐: 모든 분석가에게 동일한 순서로 제공돼야 IAA가 계산된다.
    층화: 레코드 타입 × judge 라벨 클래스 × 런. judge 간 불일치 항목을 우선 배치."""
    if QUEUE_PATH.exists() and not rebuild:
        with open(QUEUE_PATH, encoding="utf-8") as f:
            return json.load(f)

    reg = load_registry()
    _, judges_map = None, None
    items = []
    for run in reg:
        try:
            _, judges_map = resolve_lane(run, generator)
        except HTTPException:
            continue
        if judge not in judges_map:
            continue
        for uid in first4_uuids():
            jd = load_judge(run, judge, uid, generator)
            if not jd:
                continue
            try:
                user = load_run_users(run, generator).get(uid)
            except HTTPException:
                user = None
            if user is None:
                continue
            # 세션별 인덱스 룩업
            for si, s in enumerate(user["sessions"]):
                if s.get("is_generated_qa_session"):
                    continue
                gidx = {m["memory_content"]: i for i, m in enumerate(s.get("memory_points", []))}
                eidx = {m: i for i, m in enumerate(s.get("extracted_memories", []))}
                qidx = {q["question"]: i for i, q in enumerate(s.get("questions", []))}
                for r in jd.get("memory_integrity_records", []):
                    if r.get("session_id") == si and r["memory_content"] in gidx:
                        items.append((run, uid, si, "integrity", gidx[r["memory_content"]],
                                      r.get("memory_integrity_score")))
                for r in jd.get("memory_accuracy_records", []):
                    if r.get("session_id") == si and r["memory_content"] in eidx:
                        items.append((run, uid, si, "accuracy", eidx[r["memory_content"]],
                                      r.get("memory_accuracy_score")))
                for r in jd.get("memory_update_records", []):
                    if r.get("session_id") == si and r["memory_content"] in gidx:
                        items.append((run, uid, si, "update", gidx[r["memory_content"]],
                                      r.get("memory_update_type")))
                for r in jd.get("question_answering_records", []):
                    if r.get("session_id") == si and r["question"] in qidx:
                        items.append((run, uid, si, "qa", qidx[r["question"]], r.get("result_type")))

    # 층화 추출: 타입 × 라벨 클래스 균등, 런 순환. 결정적(정렬 후 등간 추출)이라 재생성해도 동일
    from collections import defaultdict
    buckets = defaultdict(list)
    for it in items:
        if it[5] is None:  # judge 무효 판정: 일치도 계산이 불가능하므로 큐에서 제외
            continue
        buckets[(it[3], str(it[5]))].append(it)
    picked = []
    for t in ["integrity", "accuracy", "update", "qa"]:  # gold_qa는 judge 대조 대상이 아니므로 제외
        keys = sorted(k for k in buckets if k[0] == t)
        if not keys:
            continue
        per_class = max(1, per_type // max(len(keys), 1))
        for k in keys:
            pool = sorted(buckets[k])
            step = max(1, len(pool) // per_class)
            picked += [pool[i] for i in range(0, len(pool), step)][:per_class]
    queue = [{"run": r, "uuid": u, "session_id": s, "rec_type": t, "idx": i,
              "generator": generator if t == "qa" else "", "judge": judge, "judge_label": str(lab)}
             for (r, u, s, t, i, lab) in picked]
    out = {"built_at": datetime.now(timezone.utc).isoformat(), "judge": judge,
           "generator": generator, "per_type": per_type, "n": len(queue), "items": queue}
    with open(QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


@app.get("/api/digest/{uuid}")
def digest(uuid: str):
    """한 유저에 대한 전체 런·전체 분석가의 코멘트 모음 (digest 탭)."""
    with db() as conn:
        rows = conn.execute("SELECT * FROM comments WHERE uuid=? ORDER BY run, anchor, created_at", (uuid,)).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/export/{uuid}", response_class=PlainTextResponse)
def export_md(uuid: str):
    """유저 1명 코멘트 전체를 Markdown으로 export (미팅 자료용)."""
    with db() as conn:
        rows = conn.execute("SELECT * FROM comments WHERE uuid=? ORDER BY run, anchor, created_at", (uuid,)).fetchall()
    lines = [f"# 정성분석 코멘트: user {uuid}", ""]
    cur_run = None
    for r in rows:
        if r["run"] != cur_run:
            cur_run = r["run"]
            lines += [f"## run: {cur_run}", ""]
        tag = f" `{r['tag']}`" if r["tag"] else ""
        keys = r.keys()
        ctx_parts = [f"{k}={r[k]}" for k in ("generator", "judge", "run_b") if k in keys and r[k]]
        ctx = f" [{' · '.join(ctx_parts)}]" if ctx_parts else ""
        lines.append(f"- **{r['anchor']}**: {r['author']}{tag}{ctx} ({r['created_at'][:16]})")
        if "quote" in keys and r["quote"]:
            lines.append(f"  > “{r['quote']}”")
        for bl in r["body"].splitlines():
            lines.append(f"  {bl}")
    return "\n".join(lines) + "\n"


# ---------- 정적 파일 (SPA) ----------

@app.middleware("http")
async def no_cache_static(request, call_next):
    """정적 파일 캐시 재검증 강제: 배포 후 옛 CSS/JS가 브라우저 캐시에 남아
    화면이 깨져 보이는 문제 방지 (내부 툴이라 no-cache 비용 무시 가능)."""
    resp = await call_next(request)
    if request.url.path.startswith("/static") or request.url.path == "/":
        resp.headers["Cache-Control"] = "no-cache"
    return resp



@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
