"""LIGHT · Memora Stage A. 기존 ingest_memora.py 와 같은 데이터 규약(날짜순 세션·질문
dict 전체 보존·tmp 캐시)을 따르되 메모리 시스템만 LIGHT 임.

질문은 전량 투입 후 단일 스냅샷 — Memora 문항의 question_date 는 전부 기간의 마지막
날이라 미래 정보 누출이 없음 (기존 하네스와 같은 근거).

산출물: results/light/memora-{version}/memora_eval_results.jsonl
  (persona/period/... 기존 스키마 + questions[].retrieved + questions[].light + light 블록)

실행 (서버):
  uv run --project eval/light python eval/light/ingest_memora_light.py \
      --data Memora/data/weekly --version weekly-light --top-k 200 --max-workers 10 --trace
"""
import argparse
import glob
import json
import os
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_ROOT, "src", "mem0-classic-oss"))

from adapter import build_light_memory                     # noqa: E402
from core import (EpisodicIndex, WorkingMemory, noise_filter,   # noqa: E402
                  pair_text_working)
from flags import load_flags, echo_flags, flags_dict       # noqa: E402
from pairing import pairs_memora                           # noqa: E402
from runner import ingest_pairs, replay_fold               # noqa: E402
from tracing import TraceLogger, attach_tracing            # noqa: E402


def load_sessions(persona_dir: str) -> list[dict]:
    """(날짜, session_id) 이중 정렬 — 기존 ingest_memora.load_sessions 와 동일 규약."""
    out = []
    for f in sorted(glob.glob(os.path.join(persona_dir, "conversations", "session_*.json"))):
        with open(f, encoding="utf-8") as fh:
            out.append(json.load(fh))
    out.sort(key=lambda d: (d.get("date") or "", d.get("session_id") or 0))
    return out


def load_questions(persona_dir: str, persona: str) -> tuple[dict, list[dict]]:
    """질문 dict 전체 보존 (criteria 가 채점의 유일한 근거 — 필드 누락 금지)."""
    path = os.path.join(persona_dir, f"evaluation_questions_{persona}.json")
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    out = []
    for task, items in (doc.get("questions") or {}).items():
        for it in items:
            q = dict(it)
            q["task"] = task
            ev = (it.get("evaluation") or {}).get("evaluation_questions") or []
            q["criteria"] = [{"id": e.get("evaluation_question_id"),
                              "text": e.get("evaluation_question"),
                              "expected": e.get("expected_answer"),
                              "type": e.get("evaluation_type")} for e in ev]
            out.append(q)
    return doc, out


def process_persona(persona_dir: str, period: str, top_k: int,
                    save_path: str, run_name: str, trace_dir: str | None) -> str:
    persona = os.path.basename(persona_dir.rstrip("/"))
    key = f"{period}_{persona}"
    tmp_file = os.path.join(save_path, "tmp", f"{key}.json")
    if os.path.exists(tmp_file):
        return f"skip {key} (cached)"

    flags = load_flags()
    tracer = None
    try:
        memory = build_light_memory()
        if trace_dir:
            tracer = TraceLogger(os.path.join(trace_dir, f"{key}.jsonl"),
                                 system="light", run=run_name, user=key)
            attach_tracing(memory, tracer)

        sessions = load_sessions(persona_dir)
        qdoc, questions = load_questions(persona_dir, persona)

        pairs = pairs_memora(sessions)
        episodic, notes = ingest_pairs(
            memory, pairs, flags,
            tmp_prefix=os.path.join(save_path, "tmp", key),
            progress_key=key, tracer=tracer)

        folder, _ = replay_fold(memory, notes, flags, tracer=tracer)
        scratchpad = folder.finalize()

        index = EpisodicIndex(memory)
        if tracer:
            tracer.set_context(stage="index", ref=None)
        index.add_pairs(episodic)

        wm = WorkingMemory(flags)
        by_gid = {p["gid"]: p for p in pairs}
        for g in sorted(by_gid):
            wm.append(pair_text_working(by_gid[g]["pair"]))
        working = wm.snapshot()

        date_by_gid = {c["gid"]: c["metadata"].get("session_date") for c in episodic}
        sid_by_gid = {c["gid"]: c["metadata"].get("session_id") for c in episodic}
        chunk_cache: dict = {}
        os.environ["COST_STAGE"] = "query"   # 검색·필터는 질의 비용
        for q in questions:
            if tracer:
                tracer.set_context(stage="qa_retrieval", ref={"qid": q.get("question_id")})
            t0 = time.time()
            recs = index.search(q["question"], top_k)
            q["retrieved"] = [
                {"memory": index.original_of(r) or "",
                 "score": round(float(r.score), 6),
                 "session_date": date_by_gid.get(int(str(r.id)))
                 if str(r.id).isdigit() else None,
                 "session_id": sid_by_gid.get(int(str(r.id)))
                 if str(r.id).isdigit() else None,
                 "pair_id": str(r.id)}
                for r in recs]
            if tracer:
                tracer.set_context(stage="noise_filter", ref={"qid": q.get("question_id")})
            nf = noise_filter(memory, scratchpad, q["question"], chunk_cache, flags)
            q["light"] = {"scratchpad": "\n\n".join(nf["kept"]),
                          "n_chunks": nf["n_chunks"], "n_kept": nf["n_kept"],
                          "n_bad": nf["n_bad"]}
            q["search_duration_ms"] = (time.time() - t0) * 1000

        os.environ["COST_STAGE"] = "ingest"
        out = {
            "persona": persona, "period": period,
            "user_id": f"light_{key}",
            "date_range": qdoc.get("date_range"),
            "n_sessions": len(sessions),
            "ingest": [{"idx": i, "session_id": s.get("session_id"),
                        "date": s.get("date"), "session_type": s.get("session_type"),
                        "operation": s.get("operation"),
                        "operation_details": s.get("operation_details"),
                        "n_turns": len(s.get("conversation") or []),
                        # LIGHT 는 ADD/UPDATE/DELETE 개념이 없음 — 연산 발생비 표에서
                        # '해당 없음' 으로 읽히도록 events 를 비워 둠
                        "events": []}
                       for i, s in enumerate(sessions)],
            "stored_memories": index.size,
            "light": {"working": working, "scratchpad_raw": scratchpad,
                      "n_folds": folder.n_folds, "flags": flags_dict(flags)},
            "questions": questions,
        }
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        return f"saved {key} ({len(sessions)} sessions, {len(questions)} questions)"
    except Exception:
        err = os.path.join(save_path, "tmp", f"{key}_error.log")
        with open(err, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        return f"FAILED {key} -> {err}"
    finally:
        if tracer:
            tracer.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="예: Memora/data/weekly")
    ap.add_argument("--version", required=True)
    ap.add_argument("--top-k", type=int, default=200)
    ap.add_argument("--max-workers", type=int, default=10)
    ap.add_argument("--personas", default=None, help="쉼표 구분 이름. 없으면 전부")
    ap.add_argument("--trace", action="store_true")
    a = ap.parse_args()

    flags = load_flags()
    period = os.path.basename(a.data.rstrip("/"))
    save_path = os.path.join("results", "light", f"memora-{a.version}")
    os.makedirs(os.path.join(save_path, "tmp"), exist_ok=True)
    trace_dir = None
    if a.trace:
        trace_dir = os.path.join("traces", "light", f"memora-{a.version}")
        os.makedirs(trace_dir, exist_ok=True)

    dirs = sorted(d for d in glob.glob(os.path.join(a.data, "*"))
                  if os.path.isdir(d))
    if a.personas:
        want = {p.strip() for p in a.personas.split(",")}
        dirs = [d for d in dirs if os.path.basename(d.rstrip("/")) in want]

    print("구현: LIGHT (BEAM 3e12035 이식)")
    print(f"기간 {period} · 페르소나 {len(dirs)}개 · top-k {a.top_k}")
    print(echo_flags(flags), flush=True)

    with ProcessPoolExecutor(max_workers=a.max_workers) as ex:
        futures = {ex.submit(process_persona, d, period, a.top_k,
                             save_path, f"memora-{a.version}", trace_dir): d
                   for d in dirs}
        done = 0
        for fut in as_completed(futures):
            done += 1
            print(f"[{done}/{len(dirs)}] {fut.result()}", flush=True)

    out_path = os.path.join(save_path, "memora_eval_results.jsonl")
    with open(out_path, "w", encoding="utf-8") as fo:
        for fn in sorted(os.listdir(os.path.join(save_path, "tmp"))):
            if fn.endswith(".json") and "_error" not in fn:
                with open(os.path.join(save_path, "tmp", fn), encoding="utf-8") as fi:
                    fo.write(json.dumps(json.load(fi), ensure_ascii=False) + "\n")
    print(f"done -> {out_path}")


if __name__ == "__main__":
    main()
