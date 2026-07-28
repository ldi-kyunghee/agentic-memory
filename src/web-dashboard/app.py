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
    # additive 마이그레이션: 드래그 하이라이트 인용문 컬럼
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(comments)")]
    if "quote" not in cols:
        conn.execute("ALTER TABLE comments ADD COLUMN quote TEXT DEFAULT ''")
    return conn


class CommentIn(BaseModel):
    run: str
    uuid: str
    anchor: str  # 예: "session:12" / "session:12/mp:3" / "session:12/qa:1" / "session:12/turn:4" / "run"
    author: str
    tag: str = ""
    body: str
    quote: str = ""  # 드래그 하이라이트로 지정한 인용 텍스트 (선택)


@app.post("/api/comments")
def add_comment(c: CommentIn):
    if not c.body.strip() or not c.author.strip():
        raise HTTPException(400, "author/body required")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO comments (run, uuid, anchor, author, tag, body, quote, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (c.run, c.uuid, c.anchor, c.author.strip(), c.tag.strip(), c.body, c.quote, datetime.now(timezone.utc).isoformat()),
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
        lines.append(f"- **{r['anchor']}** — {r['author']}{tag} ({r['created_at'][:16]})")
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
