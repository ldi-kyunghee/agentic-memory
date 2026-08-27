"""LIGHT · HaluMem Stage A. 유일한 구조적 난점: **중간 시점 질의** (65세션 중 47개에
질문이 있고, 그 세션까지 투입한 시점의 메모리로 답해야 함).

스냅샷 = 복사가 아니라 순차 삽입 + 세션 경계에서 읽기:
  1) 전 pair 의 추출 2P 콜을 먼저 병렬로 뽑음 (fold 는 note 순서에만 의존하므로 안전)
  2) 세션 순서대로 재생: note feed(fold 콜은 그 자리) → episodic 삽입 → working 추가
     → 경계에서 update_probe(top-10)·질문(top-200 + noise filter + 조립) 수행

산출물은 judge.py 가 읽는 필드를 전부 보존함 (memory_points+memories_from_system ·
dialogue 원본 · extracted_memories=그 세션 KV들 · qa 원본 필드 + context 문자열).
gen_answers.py / judge.py 무수정 통과가 목표.

실행 (서버):
  uv run --project eval/light python eval/light/ingest_halumem_light.py \
      --data dataset/HaluMem-Medium.jsonl --version 20u-light --max-workers 10 --trace
"""
import argparse
import copy
import json
import os
import re
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_ROOT, "src", "mem0-classic-oss"))

from adapter import build_light_memory                     # noqa: E402
from core import (EpisodicIndex, WorkingMemory, assemble_context,  # noqa: E402
                  noise_filter, pair_text_working)
from flags import load_flags, echo_flags, flags_dict       # noqa: E402
from pairing import pairs_halumem                          # noqa: E402
from runner import ingest_pairs, replay_fold               # noqa: E402
from tracing import TraceLogger, attach_tracing            # noqa: E402

_NAME_RE = re.compile(r"Name:\s*(.*?); Gender:")   # eval_memzero_oss 와 동일 규약


def process_user(user_data: dict, save_path: str, run_name: str,
                 trace_dir: str | None, top_k: int) -> str:
    uuid = user_data["uuid"]
    m = _NAME_RE.search(user_data.get("persona_info") or "")
    user_name = m.group(1) if m else uuid
    tmp_file = os.path.join(save_path, "tmp", f"{uuid}.json")
    if os.path.exists(tmp_file):
        return f"skip {user_name} (cached)"

    flags = load_flags()
    tracer = None
    try:
        memory = build_light_memory()
        if trace_dir:
            tracer = TraceLogger(os.path.join(trace_dir, f"{uuid}.jsonl"),
                                 system="light", run=run_name, user=uuid)
            attach_tracing(memory, tracer)

        sessions = user_data["sessions"]
        pairs = pairs_halumem(sessions)
        episodic, notes = ingest_pairs(
            memory, pairs, flags,
            tmp_prefix=os.path.join(save_path, "tmp", uuid),
            progress_key=user_name, tracer=tracer)

        # 세션별로 묶음 (재생 루프의 단위)
        ep_by_session: dict[int, list] = {}
        gid_session = {p["gid"]: p["session_idx"] for p in pairs}
        for c in episodic:
            ep_by_session.setdefault(gid_session[c["gid"]], []).append(c)
        max_gid_of_session = {si: max(c["gid"] for c in cs)
                              for si, cs in ep_by_session.items()}
        pair_by_gid = {p["gid"]: p for p in pairs}

        index = EpisodicIndex(memory)
        wm = WorkingMemory(flags)
        folder = None
        fed_upto = -1
        chunk_cache: dict = {}
        out = {"uuid": uuid, "user_name": user_name, "sessions": []}

        for si, session in enumerate(sessions):
            new_session = {
                "memory_points": copy.deepcopy(session.get("memory_points") or []),
                "dialogue": session.get("dialogue") or [],   # judge accuracy 가 원본을 씀
                "is_generated_qa_session": session.get("is_generated_qa_session", False),
                "add_dialogue_duration_ms": 0,               # LIGHT 는 선행 병렬 추출이라 세션 단위 시간이 없음
            }
            t_add = time.time()
            # ① 이 세션의 note 를 fold 에 feed (그 시점 scratchpad)
            if si in max_gid_of_session:
                folder, fed_upto = replay_fold(
                    memory, notes, flags, tracer=tracer,
                    upto_gid=max_gid_of_session[si], folder=folder, fed_upto=fed_upto)
            elif folder is None:
                folder, fed_upto = replay_fold(memory, [], flags, folder=None)
            # ② episodic 삽입 · working 추가
            ses_chunks = ep_by_session.get(si, [])
            if tracer:
                tracer.set_context(session=si, stage="index", ref=None)
            index.add_pairs(ses_chunks)
            for c in sorted(ses_chunks, key=lambda x: x["gid"]):
                p = pair_by_gid[c["gid"]]
                wm.append(pair_text_working(p["pair"], p.get("time_prefix")))
            new_session["add_dialogue_duration_ms"] = (time.time() - t_add) * 1000
            new_session["extracted_memories"] = [c["text"] for c in ses_chunks]
            new_session["memory_events"] = [
                {"event": "ADD", "memory": c["text"], "id": c["metadata"]["id"]}
                for c in ses_chunks]

            if new_session["is_generated_qa_session"]:
                out["sessions"].append(new_session)
                continue

            scratchpad_now = folder.content if folder else ""

            # ③ update_probe: 그 시점 top-10 스냅샷 (judge 의 update C/H/O 입력)
            #    KV 추출물을 줌 — update 채점은 "저장물이 갱신을 반영했는가" 라서
            #    mem0 쪽 등가물(추출 메모리)과 같은 층위여야 함. 원문 pair 를 주면
            #    저장물 평가가 아니라 대화 검색 평가가 됨.
            for mp in new_session["memory_points"]:
                if mp.get("is_update") == "True" and mp.get("original_memories"):
                    if tracer:
                        tracer.set_context(session=si, stage="update_probe",
                                           ref={"mp_index": mp.get("index")})
                    recs = index.search(mp["memory_content"], 10)
                    mp["memories_from_system"] = [
                        f"{r.payload.get('session_time') or 'unknown_time'}: {r.payload['data']}"
                        for r in recs]

            # ④ 질문: 그 시점 스냅샷으로 top-200 검색 + noise filter + 조립
            qs = []
            for qa in (session.get("questions") or []):
                q = copy.deepcopy(qa)
                if tracer:
                    tracer.set_context(session=si, stage="qa_retrieval",
                                       ref={"q": (q.get("question") or "")[:60]})
                t0 = time.time()
                recs = index.search(q["question"], top_k)
                originals = [index.original_of(r) or "" for r in recs]
                if tracer:
                    tracer.set_context(session=si, stage="noise_filter", ref=None)
                nf = noise_filter(memory, scratchpad_now, q["question"],
                                  chunk_cache, flags)
                ctx, budget = assemble_context(
                    originals[:flags.halumem_cutoff], wm.snapshot(), nf["kept"],
                    reader_max_tokens=flags.reader_max_tokens,
                    wm_recent_first=flags.wm_recent_first,
                    scratchpad_budget=flags.scratchpad_budget)
                q["context"] = ctx
                q["light"] = {"n_chunks": nf["n_chunks"], "n_kept": nf["n_kept"],
                              "n_bad": nf["n_bad"], **budget,
                              "episodic_ids": [[str(r.id), round(float(r.score), 6)]
                                               for r in recs]}
                q["search_duration_ms"] = (time.time() - t0) * 1000
                qs.append(q)
            new_session["questions"] = qs
            out["sessions"].append(new_session)
            print(f"[{user_name}] {si + 1}/{len(sessions)} sessions done", flush=True)

        out["light"] = {"stored_memories": index.size,
                        "n_folds": folder.n_folds if folder else 0,
                        "scratchpad_final_len": len(folder.content) if folder else 0,
                        "flags": flags_dict(flags)}
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        return f"saved {user_name} -> {tmp_file}"
    except Exception:
        err = os.path.join(save_path, "tmp", f"{uuid}_error.log")
        with open(err, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        return f"FAILED {user_name} -> {err}"
    finally:
        if tracer:
            tracer.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="dataset/HaluMem-Medium.jsonl")
    ap.add_argument("--version", required=True)
    ap.add_argument("--top-k", type=int, default=200)
    ap.add_argument("--user-num", type=int, default=None,
                    help="데이터셋 순서 기준 앞 N명 (기존 규약)")
    ap.add_argument("--max-workers", type=int, default=10)
    ap.add_argument("--trace", action="store_true")
    a = ap.parse_args()

    flags = load_flags()
    save_path = os.path.join("results", "light", f"memzero-{a.version}")
    os.makedirs(os.path.join(save_path, "tmp"), exist_ok=True)
    trace_dir = None
    if a.trace:
        trace_dir = os.path.join("traces", "light", f"memzero-{a.version}")
        os.makedirs(trace_dir, exist_ok=True)

    users = []
    with open(a.data, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                users.append(json.loads(line))
    if a.user_num:
        users = users[:a.user_num]

    print("구현: LIGHT (BEAM 3e12035 이식)")
    print(f"유저 {len(users)}명 · 검색 top-{a.top_k} · 조립 top-{flags.halumem_cutoff}")
    print(echo_flags(flags), flush=True)

    with ProcessPoolExecutor(max_workers=a.max_workers) as ex:
        futures = {ex.submit(process_user, u, save_path,
                             f"memzero-{a.version}", trace_dir, a.top_k): u["uuid"]
                   for u in users}
        done = 0
        for fut in as_completed(futures):
            done += 1
            print(f"[{done}/{len(users)}] {fut.result()}", flush=True)

    out_path = os.path.join(save_path, "memzero-oss_eval_results.jsonl")
    with open(out_path, "w", encoding="utf-8") as fo:
        for fn in sorted(os.listdir(os.path.join(save_path, "tmp"))):
            if fn.endswith(".json") and "_error" not in fn:
                with open(os.path.join(save_path, "tmp", fn), encoding="utf-8") as fi:
                    fo.write(json.dumps(json.load(fi), ensure_ascii=False) + "\n")
    print(f"done -> {out_path}")


if __name__ == "__main__":
    main()
