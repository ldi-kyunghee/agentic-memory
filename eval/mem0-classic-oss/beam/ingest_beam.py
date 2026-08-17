"""
BEAM Stage A: 대화를 mem0에 투입하고 문항별 검색 결과를 저장한다.

HaluMem용 eval_memzero_oss.py와 뼈대는 같고 세 가지가 다르다.
  1. 투입 단위가 세션이 아니라 2메시지 청크다 (mem0 팀의 BEAM 하네스 방식).
  2. 시각 정보가 배치당 하나뿐이라(time_anchor) 그 배치의 모든 청크에 같은 값을 준다.
  3. 검색 결과를 문자열로 굳히지 않고 top-200 원본 리스트로 저장한다.
     Stage A'에서 20/50/200으로 잘라 쓰기 때문이다.

BEAM에는 골든 메모리가 없어 R/Acc/Update 지표를 잴 수 없다. QA만 평가한다.
"""
import os
import re
import sys
import json
import argparse
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

from tqdm import tqdm
from dotenv import load_dotenv

load_dotenv()

# 기존 Stage A의 mem0 설정/재시도 래퍼 그대로 재사용
sys.path.insert(0, "eval/mem0-classic-oss")
sys.path.insert(0, "src/mem0-classic-oss")
from eval_memzero_oss import build_memory, add_with_retry, search_with_retry
from tracing import TraceLogger, TracingLLM, TracingVectorStore
from bm25_store import build_bm25_store

CHUNK_SIZE = 2  # 한 번에 넣는 message 수, mem0 팀의 하네스와 동일하게 유지

def parse_chat(chat: list) -> list[tuple[str | None, list[dict]]]:
    """
    chat.json -> [(time_anchor, [msg...]), ...] 배치 단위로 펼침
    구조 : [{batch_number, turns: [[msg, msg, ...], ...]}, ...]
    time_anchor는 배치 안 어느 msg에 붙어있을지 모르니 먼저 나오는 것을 사용
    """
    out = []
    for batch in chat:
        msgs = [m for group in batch["turns"] for m in group]
        anchor = next((m["time_anchor"] for m in msgs if m.get("time_anchor")), None)
        out.append((anchor, msgs))
    return out

def to_chunks(msgs: list[dict], size: int = CHUNK_SIZE) -> list[list[dict]]:
    """msg를 role/content만 남기고 size개씩 자름"""
    clean = [
        {"role": m["role"] if m.get("role") in ("user", "assistant") else "user",
         "content": m.get("content", "")}
        for m in msgs if m.get("content")
    ]
    return [clean[i:i+size] for i in range(0, len(clean), size)]

def load_questions(conv_dir: str) -> list[dict]:
    """
    probing_questions.json -> flat list 변환함.
    평가 능력마다 정답 필드의 이름이 다름 (answer / ideal_answer / ideal_response / ideal_summary).
    instruction_following, preference_following은 정답 필드가 부재함.
    채점은 rubric만 사용하므로 정답은 참고용으로만 담음
    """
    path = os.path.join(conv_dir, "probing_questions", "probing_questions.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    ANS_KEYS = ("answer", "ideal_answer", "ideal_response", "ideal_summary")
    out = []
    for ability, items in data.items():
        for i, it in enumerate(items):
            out.append({
                "ability": ability,
                "idx": i,
                "question": it["question"],
                "rubric": it.get("rubric") or [],
                "reference": next((it[k] for k in ANS_KEYS if k in it), None),
                "difficulty": it.get("difficulty"),
            })
    return out

def process_conversation(conv_dir: str, bucket: str, top_k: int, save_path: str,
                         collection_name: str, use_tqdm: bool = True,
                         trace_dir: str | None = None) -> str:
    conv_id = os.path.basename(conv_dir)
    key = f"{bucket}_{conv_id}"
    user_id = f"beam_{key}"

    tmp_file = os.path.join(save_path, "tmp", f"{key}.json")
    if os.path.exists(tmp_file):
        return f"skip {key} (cached)"

    tracer = None
    try:
        memory = build_memory(
            collection_name=f"{collection_name}_{conv_id}",
            history_db_path=os.path.join(save_path, "tmp", f"history_{key}.db"),
        )
        # retriever 교체는 delete_all보다 선행해야 새 store가 초기화됨
        if os.getenv("MEM0_RETRIEVER") == "bm25":
            memory.vector_store = build_bm25_store(f"{collection_name}_{conv_id}")
        memory.delete_all(user_id=user_id)

        if trace_dir:
            tracer = TraceLogger(os.path.join(trace_dir, f"{key}.jsonl"),
                                 system="mem0-classic-oss", run=collection_name, user=key)
            memory.llm = TracingLLM(memory.llm, tracer)
            memory.vector_store = TracingVectorStore(memory.vector_store, tracer)

        with open(os.path.join(conv_dir, "chat.json"), encoding="utf-8") as f:
            batches = parse_chat(json.load(f))
        with open(os.path.join(conv_dir, "topic.json"), encoding="utf-8") as f:
            topic = json.load(f)

        out = {"conv_id": key, "bucket": bucket, "user_id": user_id, 
               "category": topic.get("category"), "title": topic.get("title"), 
               "batches": len(batches), "ingest": []}

        # ------ ingestion (투입) ------
        total = sum(len(to_chunks(m)) for _, m in batches)
        bar = tqdm(total=total, desc=f"ingest {key}") if use_tqdm else None
        done = 0
        for bi, (anchor, msgs) in enumerate(batches):
            chunks = to_chunks(msgs)
            for ci, chunk in enumerate(chunks):
                if tracer:
                    tracer.set_context(session=bi, stage="ingest", ref={"chunk": ci})
                result, dur = add_with_retry(
                    memory, chunk, user_id=user_id,
                    metadata={"session_time": anchor or "unknown_time", "batch": bi},
                )
                out["ingest"].append({
                    "batch": bi,
                    "chunk": ci,
                    "time_anchor": anchor,
                    "duration_ms": dur,
                    "events": [{"op": r["event"], "text": r["memory"]} for r in result["results"]],
                })
                done += 1
                if bar:
                    bar.update(1)
                elif done % 50 == 0:
                    print(f"[{key}] {done}/{total} chunks", flush=True)
        if bar:
            bar.close()


        # ------ 문항별 검색 (top-200 한 번, 자르기는 Stage A'에서) ------
        out["questions"] = []
        for q in load_questions(conv_dir):
            if tracer:
                tracer.set_context(stage="qa_retrieval", ref={"question": q["question"]})
            found, dur = search_with_retry(memory, q["question"], user_id, top_k)
            q["retrieved"] = [
                {
                    "memory": r["memory"], "score": r.get("score"), "created_at": r.get("created_at"), 
                    "session_time": (r.get("metadata") or {}).get("session_time")
                }
                for r in found["results"]
            ]
            q["search_duration_ms"] = dur
            out["questions"].append(q)

        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        return f"saved {key} ({total} chunks, {len(out['questions'])} questions)"
    except Exception:
        err = os.path.join(save_path, "tmp", f"{key}_error.log")
        with open(err, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        return f"FAILED {key} -> {err}"
    finally:
        if tracer:
            tracer.close()


def main(chats_dir: str, version: str, top_k: int, conversations: str | None,
         max_workers: int, trace: bool):
    bucket = os.path.basename(chats_dir.rstrip("/"))
    save_path = f"results/mem0-classic-oss/beam-{version}/"
    os.makedirs(os.path.join(save_path, "tmp"), exist_ok=True)
    collection_name = f"beam_{version}"

    print(f"버킷: {bucket}, top_k: {top_k}")
    print(f"retriever: {'BM25 (Qdrant sparse + IDF)' if os.getenv('MEM0_RETRIEVER') == 'bm25' else '임베딩 (Qdrant dense)'}")
    print(f"reasoning effort override: {os.getenv('MEM0_REASONING_EFFORT') or '없음 (모델 기본값)'}")

    trace_dir = None
    if trace:
        trace_dir = f"traces/mem0-classic-oss/beam-{version}/"
        os.makedirs(trace_dir, exist_ok=True)

    dirs = sorted((d.path for d in os.scandir(chats_dir) if d.is_dir()),
                  key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0))
    if conversations:
        lo, _, hi = conversations.partition("-")
        dirs = dirs[int(lo): int(hi) + 1 if hi else int(lo) + 1]
    print(f"대화 {len(dirs)}개: {[os.path.basename(d) for d in dirs]}")

    if max_workers <= 1:
        for d in dirs:
            print(process_conversation(d, bucket, top_k, save_path, collection_name, True, trace_dir))
    else:
        with ProcessPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(process_conversation, d, bucket, top_k, save_path, collection_name, False, trace_dir): d for d in dirs}
            for i, future in enumerate(as_completed(futures), 1):
                print(f"[{i}/{len(futures)}] {future.result()}", flush=True)

    output = os.path.join(save_path, "beam_eval_results.jsonl")
    with open(output, "w", encoding="utf-8") as f_out:
        for file in sorted(os.listdir(os.path.join(save_path, "tmp"))):
            if file.endswith(".json"):
                with open(os.path.join(save_path, "tmp", file), encoding="utf-8") as f_in:
                    f_out.write(json.dumps(json.load(f_in), ensure_ascii=False) + "\n")
    print(f"done -> {output}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--chats", default="BEAM/chats/100K")
    p.add_argument("--version", default="dev")
    p.add_argument("--top-k", type=int, default=200, help="BEAM 기준 세팅. A'에서 20/50/200으로 자른다")
    p.add_argument("--conversations", default=None, help="인덱스 범위 (예: 0-4). 미지정이면 전체")
    p.add_argument("--max-workers", type=int, default=1)
    p.add_argument("--trace", action="store_true")
    a = p.parse_args()
    main(a.chats, a.version, a.top_k, a.conversations, a.max_workers, a.trace)