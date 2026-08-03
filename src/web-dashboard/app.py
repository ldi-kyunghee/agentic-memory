"""정성분석 웹앱 백엔드 — 산출물 4종(러너/A'/trace/judge)을 유저 단위로 조인해 API로 제공.

원칙: results/·traces/는 읽기 전용. 쓰기는 src/web-dashboard/data/ 안에서만 (comments.sqlite3).
실행: uv run --project src/web-dashboard uvicorn app:app --app-dir src/web-dashboard --port 8501
"""

import os
import json
import sqlite3
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

HERE = Path(__file__).parent
ROOT = HERE.parent.parent  # 리포 루트 — runs.yaml의 상대경로 기준
DATA_DIR = HERE / "data"
DATA_DIR.mkdir(exist_ok=True)

app = FastAPI(title="mem0-halumem qualitative dashboard")


# ---------- 레지스트리 / 사전 ----------

def load_registry_doc() -> dict:
    with open(HERE / "runs.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_registry() -> dict:
    return load_registry_doc()["runs"]


def gen_registry() -> dict:
    """generator 레인 정의 (전역 섹션). base 레인은 각 런의 기본 results/judges를 사용."""
    return load_registry_doc().get("generators", {"qwen4b": {"label": "Qwen3-4B", "base": True}})


def resolve_lane(run: str, generator: str):
    """run×generator -> (results_path, judges{name: dir상대경로}). 미지의 run/generator면 404."""
    reg = load_registry()
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
#    이후 파일이 생겨도(동기화 완료 등) 회색 '–' 라벨로 남는 버그가 됨 — 성공한 로드만 캐싱한다.
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
        return None  # 실패는 캐싱하지 않음 — 다음 요청에서 재시도
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
def api_runs():
    reg = load_registry()
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
    reg = load_registry()
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


# ---------- 지표 테이블 (공식 집계 함수 재사용 — 문서 테이블과 수치 일치 보장) ----------

import sys as _sys
_sys.path.insert(0, str(ROOT / "HaluMem" / "eval"))
from evaluation import aggregate_eval_results  # noqa: E402
# judge.py가 쓰는 것과 동일한 프롬프트 템플릿 — 분석가에게 judge와 똑같은 입력을 재현해 보여주기 위함
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
    elif rec_type == "qa":
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
    """모든 실험이 공유하는 데이터셋 첫 4유저 — 4u 런의 judge 디렉토리 파일 목록에서 확보."""
    reg = load_registry()
    for r in reg.values():
        if r.get("users") == 4:
            d = ROOT / list(r["judges"].values())[0]
            if d.exists():
                return tuple(sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json")))
    return ()


_metrics_cache: dict = {}


def compute_metrics(run: str, judge_name: str, scope: str, generator: str = "qwen4b") -> dict | None:
    """scope: 'first4' | 'all' | <uuid>. 선택 범위의 judge 레코드를 모아 공식 집계로 지표 산출.
    성공 결과만 캐싱 (None 캐싱 금지 — load_judge와 동일 이유)."""
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
    o = aggregate_eval_results(skeleton)["overall_score"]
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
    """서버측 캐시 전체 무효화 — 새 런 동기화·judge 재채점 후 강제 재로딩용."""
    _judge_cache.clear()
    _metrics_cache.clear()
    load_run_users.cache_clear()
    first4_uuids.cache_clear()
    return {"ok": True}


@app.get("/api/first4")
def api_first4():
    """nano judge가 라벨을 보유한 데이터셋 첫 4유저 uuid — UI의 ★ 표시용."""
    return list(first4_uuids())


def compute_latency(run: str, scope: str) -> dict | None:
    """Stage A가 기록한 시간 실측 — 세션 투입(mem0.add: LLM 콜 포함)과 질문 검색. 백본 속도 비교용."""
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


@app.get("/api/metrics")
def api_metrics(judge: str = "nano", scope: str = "first4", generator: str = "qwen4b"):
    reg = load_registry()
    rows = []
    for name, r in reg.items():
        if not (ROOT / r["results"]).exists():
            continue
        rows.append({
            "run": name, "label": r.get("label", name),
            "backbone": r.get("backbone"), "prompt": r.get("prompt"),
            "backbone_effort": r.get("backbone_effort"),
            "note": r.get("note", ""),
            "metrics": compute_metrics(name, judge, scope, generator),
            "latency": compute_latency(name, scope),  # Stage A 실측이라 generator 무관 (base 레인 기준)
        })
    return {"judge": judge, "scope": scope, "generator": generator, "first4": list(first4_uuids()), "rows": rows}


# ---------- 코멘트 ----------

DB_PATH = DATA_DIR / "comments.sqlite3"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run TEXT NOT NULL, uuid TEXT NOT NULL, anchor TEXT NOT NULL,
        author TEXT NOT NULL, tag TEXT DEFAULT '', body TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    # additive 마이그레이션 (기존 레코드 보존 — 새 컬럼은 빈 값)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(comments)")]
    if "quote" not in cols:
        conn.execute("ALTER TABLE comments ADD COLUMN quote TEXT DEFAULT ''")
    # 코멘트 작성 당시의 관측 세팅 (어떤 generator/judge 라벨을 보며 단 코멘트인지 재구성용)
    for col in ("generator", "judge", "run_b"):
        if col not in cols:
            conn.execute(f"ALTER TABLE comments ADD COLUMN {col} TEXT DEFAULT ''")
    return conn


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
    run_b: str = ""      # 작성 당시 비교(B) 런 — extb 앵커의 대상 식별용


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
    # 인증 없는 내부 툴 — 본인 이름이 일치할 때만 삭제 허용 (실수 방지 수준)
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
    blind INTEGER DEFAULT 1, note TEXT DEFAULT '', created_at TEXT NOT NULL"""


def adb():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(f"CREATE TABLE IF NOT EXISTS annotations ({ANN_COLS})")
    # 한 분석가가 같은 항목을 두 번 라벨하면 갱신되도록 (QA는 generator 레인 종속이라 키에 포함)
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ann_key
        ON annotations (annotator, run, uuid, session_id, rec_type, idx, generator)""")
    return conn


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


@app.post("/api/annotations")
def add_annotation(a: AnnotationIn):
    if not a.annotator.strip():
        raise HTTPException(400, "annotator required")
    with adb() as conn:
        conn.execute(
            """INSERT INTO annotations
               (run, uuid, session_id, rec_type, idx, target, generator, annotator,
                label, agree, judge_name, judge_label, blind, note, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (annotator, run, uuid, session_id, rec_type, idx, generator)
               DO UPDATE SET label=excluded.label, agree=excluded.agree, judge_name=excluded.judge_name,
                 judge_label=excluded.judge_label, blind=excluded.blind, note=excluded.note,
                 created_at=excluded.created_at""",
            (a.run, a.uuid, a.session_id, a.rec_type, a.idx, a.target, a.generator,
             a.annotator.strip(), a.label, a.agree, a.judge_name, a.judge_label, a.blind, a.note,
             datetime.now(timezone.utc).isoformat()))
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


def _kappa(pairs: list) -> dict:
    """Cohen's κ + 단순 일치율. pairs: [(labelA, labelB), ...]"""
    n = len(pairs)
    if not n:
        return {"n": 0, "agree": None, "kappa": None}
    po = sum(1 for a, b in pairs if a == b) / n
    labs = {x for p in pairs for x in p}
    ca = {l: sum(1 for a, _ in pairs if a == l) / n for l in labs}
    cb = {l: sum(1 for _, b in pairs if b == l) / n for l in labs}
    pe = sum(ca[l] * cb[l] for l in labs)
    k = None if pe >= 1 else round((po - pe) / (1 - pe), 3)
    return {"n": n, "agree": round(po * 100, 1), "kappa": k}


@app.get("/api/iaa")
def api_iaa(run: str | None = None, uuid: str | None = None):
    """분석가 간(IAA) · 분석가 vs judge 일치도. 항목 키는 (run,uuid,session,rec_type,idx,generator)."""
    rows = list_annotations(run=run, uuid=uuid)
    labeled = [r for r in rows if r["label"]]
    by_item: dict = {}
    for r in labeled:
        key = (r["run"], r["uuid"], r["session_id"], r["rec_type"], r["idx"], r["generator"])
        by_item.setdefault(key, {})[r["annotator"]] = r

    annotators = sorted({r["annotator"] for r in labeled})
    # 분석가 쌍별 (같은 항목을 둘 다 라벨한 경우만)
    pairs_out = []
    for i, a in enumerate(annotators):
        for b in annotators[i + 1:]:
            pr = [(v[a]["label"], v[b]["label"]) for v in by_item.values() if a in v and b in v]
            st = _kappa(pr)
            if st["n"]:
                pairs_out.append({"a": a, "b": b, **st})
    # 분석가 vs judge (라벨 스냅샷이 있는 항목만)
    vs_judge = []
    for a in annotators:
        pr = [(r["label"], r["judge_label"]) for r in labeled
              if r["annotator"] == a and r["judge_label"] not in (None, "")]
        st = _kappa(pr)
        if st["n"]:
            vs_judge.append({"annotator": a, **st})
    # 레코드 타입별 분석가 vs judge
    by_type = []
    for t in ["integrity", "accuracy", "update", "qa"]:
        pr = [(r["label"], r["judge_label"]) for r in labeled
              if r["rec_type"] == t and r["judge_label"] not in (None, "")]
        st = _kappa(pr)
        if st["n"]:
            by_type.append({"rec_type": t, **st})
    return {
        "total": len(rows), "labeled": len(labeled), "annotators": annotators,
        "items": len(by_item), "overlap_items": sum(1 for v in by_item.values() if len(v) > 1),
        "annotator_pairs": pairs_out, "vs_judge": vs_judge, "by_type": by_type,
        "agree_clicks": {"agree": sum(1 for r in rows if r["agree"] == 1),
                         "disagree": sum(1 for r in rows if r["agree"] == 0)},
    }


QUEUE_PATH = DATA_DIR / "annotation_queue.json"


@app.get("/api/queue")
def api_queue(rebuild: int = 0, per_type: int = 40, judge: str = "oss120-genoss120",
              generator: str = "oss120"):
    """공유 표본 큐 — 모든 분석가에게 동일한 순서로 제공돼야 IAA가 계산된다.
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
        if it[5] is None:  # judge 무효 판정 — 일치도 계산이 불가능하므로 큐에서 제외
            continue
        buckets[(it[3], str(it[5]))].append(it)
    picked = []
    for t in ["integrity", "accuracy", "update", "qa"]:
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
    lines = [f"# 정성분석 코멘트 — user {uuid}", ""]
    cur_run = None
    for r in rows:
        if r["run"] != cur_run:
            cur_run = r["run"]
            lines += [f"## run: {cur_run}", ""]
        tag = f" `{r['tag']}`" if r["tag"] else ""
        keys = r.keys()
        ctx_parts = [f"{k}={r[k]}" for k in ("generator", "judge", "run_b") if k in keys and r[k]]
        ctx = f" [{' · '.join(ctx_parts)}]" if ctx_parts else ""
        lines.append(f"- **{r['anchor']}** — {r['author']}{tag}{ctx} ({r['created_at'][:16]})")
        if "quote" in keys and r["quote"]:
            lines.append(f"  > “{r['quote']}”")
        for bl in r["body"].splitlines():
            lines.append(f"  {bl}")
    return "\n".join(lines) + "\n"


# ---------- 정적 파일 (SPA) ----------

@app.middleware("http")
async def no_cache_static(request, call_next):
    """정적 파일 캐시 재검증 강제 — 배포 후 옛 CSS/JS가 브라우저 캐시에 남아
    화면이 깨져 보이는 문제 방지 (내부 툴이라 no-cache 비용 무시 가능)."""
    resp = await call_next(request)
    if request.url.path.startswith("/static") or request.url.path == "/":
        resp.headers["Cache-Control"] = "no-cache"
    return resp



@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
