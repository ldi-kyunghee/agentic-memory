"""LIGHT · BEAM Stage A. 기존 ingest_beam.py 와 같은 실행 규약(tmp 캐시·병합·--conversations
범위·에러 격리)을 따르되, 메모리 시스템만 LIGHT 임.

산출물: results/light/beam-{version}/beam_eval_results.jsonl
  기존 스키마 + stored_memories=index.size
  + 대화 레벨 light: {working: [...], scratchpad_raw: str, n_folds}
  + questions[].retrieved = episodic top-200 (원문 pair, score 순)
  + questions[].light = {scratchpad(필터 통과분), n_chunks, n_kept, n_bad}

실행 (서버):
  uv run --project eval/light python eval/light/ingest_beam_light.py \
      --chats BEAM/chats/100K --version 100k-light --top-k 200 --max-workers 10 --trace
"""
import argparse
import json
import os
import re
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_ROOT, "src", "mem0-classic-oss"))

from adapter import build_light_memory                     # noqa: E402
from core import noise_filter                              # noqa: E402
from flags import load_flags, echo_flags, flags_dict       # noqa: E402
from pairing import pairs_beam                             # noqa: E402
from runner import ingest_pairs, replay_fold               # noqa: E402
from core import EpisodicIndex, WorkingMemory, pair_text_working  # noqa: E402
from tracing import TraceLogger, attach_tracing            # noqa: E402

# 정답 필드 이름이 능력마다 다름 (기존 ingest_beam.load_questions 와 동일 규약)
_REF_KEYS = ("answer", "ideal_answer", "ideal_response", "ideal_summary")


def load_questions(conv_dir: str) -> list[dict]:
    path = os.path.join(conv_dir, "probing_questions", "probing_questions.json")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    out = []
    for ability, items in raw.items():
        for idx, it in enumerate(items):
            ref = next((it[k] for k in _REF_KEYS if k in it), None)
            out.append({"ability": ability, "idx": idx,
                        "question": it.get("question"),
                        "rubric": it.get("rubric") or [],
                        "reference": ref,
                        "difficulty": it.get("difficulty")})
    return out


def process_conversation(conv_dir: str, bucket: str, top_k: int,
                         save_path: str, run_name: str, trace_dir: str | None) -> str:
    conv_id = os.path.basename(conv_dir)
    key = f"{bucket}_{conv_id}"
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

        with open(os.path.join(conv_dir, "chat.json"), encoding="utf-8") as f:
            chat = json.load(f)
        with open(os.path.join(conv_dir, "topic.json"), encoding="utf-8") as f:
            topic = json.load(f)

        pairs = pairs_beam(chat)
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
        meta_by_gid = {p["gid"]: p for p in pairs}
        for g in sorted(meta_by_gid):
            p = meta_by_gid[g]
            wm.append(pair_text_working(p["pair"], p.get("time_prefix")))
        working = wm.snapshot()

        # ---- 질의: 전량 투입 후 (기존 하네스와 동일 시점) ----
        anchor_by_gid = {c["gid"]: c["metadata"].get("session_time") for c in episodic}
        questions = load_questions(conv_dir)
        chunk_cache: dict = {}
        import time as _t
        # 검색·필터는 투입이 아니라 질의 비용 — 계측 단계를 가름 (C-probe 에서 필터가
        # 투입으로 찍히는 것을 확인함, 2026-08-28)
        os.environ["COST_STAGE"] = "query"
        for q in questions:
            if tracer:
                tracer.set_context(stage="qa_retrieval",
                                   ref={"ability": q["ability"], "idx": q["idx"]})
            t0 = _t.time()
            recs = index.search(q["question"], top_k)
            q["retrieved"] = [
                {"memory": index.original_of(r) or "",
                 "score": round(float(r.score), 6),
                 "session_time": anchor_by_gid.get(int(str(r.id)))
                 if str(r.id).isdigit() else None,
                 "pair_id": str(r.id)}
                for r in recs]
            if tracer:
                tracer.set_context(stage="noise_filter",
                                   ref={"ability": q["ability"], "idx": q["idx"]})
            nf = noise_filter(memory, scratchpad, q["question"], chunk_cache, flags)
            q["light"] = {"scratchpad": "\n\n".join(nf["kept"]),
                          "n_chunks": nf["n_chunks"], "n_kept": nf["n_kept"],
                          "n_bad": nf["n_bad"]}
            q["search_duration_ms"] = (_t.time() - t0) * 1000

        os.environ["COST_STAGE"] = "ingest"
        out = {
            "conv_id": key, "bucket": bucket, "user_id": f"light_{key}",
            "category": topic.get("category"), "title": topic.get("title"),
            "batches": len(chat),
            "ingest": [{"gid": c["gid"], "kv_len": len(c["text"])} for c in episodic],
            "stored_memories": index.size,
            "light": {"working": working, "scratchpad_raw": scratchpad,
                      "n_folds": folder.n_folds, "flags": flags_dict(flags)},
            "questions": questions,
        }
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        return f"saved {key} ({len(pairs)} chunks, {len(questions)} questions)"
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
    ap.add_argument("--chats", required=True, help="예: BEAM/chats/100K")
    ap.add_argument("--version", required=True)
    ap.add_argument("--top-k", type=int, default=200)
    ap.add_argument("--max-workers", type=int, default=10)
    ap.add_argument("--conversations", default=None,
                    help="정렬 후 인덱스 범위(끝 포함). 예: 0-4 또는 3 (기존 ingest_beam 과 동일)")
    ap.add_argument("--trace", action="store_true")
    a = ap.parse_args()

    flags = load_flags()
    bucket = os.path.basename(a.chats.rstrip("/"))
    save_path = os.path.join("results", "light", f"beam-{a.version}")
    os.makedirs(os.path.join(save_path, "tmp"), exist_ok=True)
    trace_dir = None
    if a.trace:
        trace_dir = os.path.join("traces", "light", f"beam-{a.version}")
        os.makedirs(trace_dir, exist_ok=True)

    dirs = sorted(
        (os.path.join(a.chats, d) for d in os.listdir(a.chats)
         if os.path.isdir(os.path.join(a.chats, d))),
        key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0))
    if a.conversations:
        lo, _, hi = a.conversations.partition("-")
        dirs = dirs[int(lo): int(hi) + 1 if hi else int(lo) + 1]

    print(f"구현: LIGHT (BEAM 3e12035 이식)")
    print(f"버킷 {bucket} · 대화 {len(dirs)}개 · top-k {a.top_k}")
    print(echo_flags(flags), flush=True)

    with ProcessPoolExecutor(max_workers=a.max_workers) as ex:
        futures = {ex.submit(process_conversation, d, bucket, a.top_k,
                             save_path, f"beam-{a.version}", trace_dir): d
                   for d in dirs}
        done = 0
        for fut in as_completed(futures):
            done += 1
            print(f"[{done}/{len(dirs)}] {fut.result()}", flush=True)

    # tmp 전체를 파일명 정렬로 병합 (기존 규약: 부분 실행 여러 번 해도 최종 jsonl 은 tmp 전체)
    out_path = os.path.join(save_path, "beam_eval_results.jsonl")
    with open(out_path, "w", encoding="utf-8") as fo:
        for fn in sorted(os.listdir(os.path.join(save_path, "tmp"))):
            if fn.endswith(".json") and "_error" not in fn and "_episodic" not in fn \
                    and "_notes" not in fn:
                with open(os.path.join(save_path, "tmp", fn), encoding="utf-8") as fi:
                    fo.write(json.dumps(json.load(fi), ensure_ascii=False) + "\n")
    print(f"done -> {out_path}")


if __name__ == "__main__":
    main()
