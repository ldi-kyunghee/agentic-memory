import os
import re
import sys
import json
import time
import argparse
import traceback
import logging

from concurrent.futures import ProcessPoolExecutor, as_completed

from tenacity import retry, stop_after_attempt, wait_random_exponential, before_sleep_log
from tqdm import tqdm
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("runner")
logging.basicConfig(level=logging.WARNING)  # tenacity 재시도 경고를 콘솔로
logging.getLogger("mem0.vector_stores.qdrant").setLevel(logging.ERROR)  # Resetting Index 관련 Warning 숨기는 목적

sys.path.insert(0, "src/mem0-classic-oss")  # tracing.py import를 위해 src를 sys.path에 추가
from tracing import TraceLogger, TracingLLM, TracingVectorStore
from custom_prompt import CUSTOM_FACT_EXTRACTION_PROMPT

from mem0 import Memory
# mem0 0.1.118 텔레메트리 무력화:
# capture_event가 호출마다 PostHog 클라이언트를 새로 만들고 (스모크 기준 ~370개),
# 각각의 atexit flush가 종료를 붙잡아 프로세스가 안 끝나는 원인임.
# MEM0_TELEMETRY=False는 전송만 막고 클라이언트 생성은 못 막아서 no-op 패치가 필요함
import mem0.memory.main as _m0_main
_m0_main.capture_event = lambda *args, **kwargs: None

# 원본 eval_memzero.py의 TEMPLATE_MEM0와 동일 포멧 유지
TEMPLATE_MEM0 = """Memories for user {user_id}:

    {memories}
"""

@retry(
        wait=wait_random_exponential(min=5, max=30),
        stop=stop_after_attempt(5),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,  # 5회 실패 시 원본 exception을 올려 user 단위 격리로 넘김
)
def add_with_retry(memory, dialogue, user_id, metadata):
    start = time.time()
    result = memory.add(dialogue, user_id=user_id, metadata=metadata)
    return result, (time.time() - start) * 1000  # 시간 측정은 retry 함수 내에만 둠 -> 성공한 시도의 소요 시간만 기록 (대기시간 제외)


@retry(
        wait=wait_random_exponential(min=5, max=30),
        stop=stop_after_attempt(5),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,  # 5회 실패 시 원본 exception을 올려 user 단위 격리로 넘김
)
def search_with_retry(memory, query, user_id, top_k):
    start = time.time()
    found = memory.search(query, user_id=user_id, limit=top_k)
    return found, (time.time() - start) * 1000  # 시간 측정은 retry 함수 내에만 둠 -> 성공한 시도의 소요 시간만 기록 (대기시간 제외)


def build_memory(collection_name: str, history_db_path: str) -> Memory:
    # LLM/Embedder는 OPENAI_BASE_URL / OPENAI_API_KEY env를 자동으로 읽음
    # -> .env 교체만으로 OPENAI API <-> vLLM 전환 가능하도록
    config = {
        "llm": {
            "provider": "openai",
            "config": {
                "model": os.getenv("MEM0_LLM_MODEL", os.getenv("OPENAI_MODEL")),
                "temperature": 0.0,
                "max_tokens": int(os.getenv("MEM0_LLM_MAX_TOKENS", 16384)),
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": os.getenv("MEM0_EMBED_MODEL", "text-embedding-3-small"),
                "openai_base_url": os.getenv("MEM0_EMBED_BASE_URL"),
                "embedding_dims": int(os.getenv("MEM0_EMBED_DIMS", 1536)),
            },
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": collection_name,
                "host": os.getenv("QDRANT_HOST"),
                "port": int(os.getenv("QDRANT_PORT", "6333")),
                "embedding_model_dims": int(os.getenv("MEM0_EMBED_DIMS", 1536)),
            },
        },
        "history_db_path": history_db_path,  # 워커(프로세스)마다 SQLite 히스토리 파일을 분리함 -> 여러 프로세스가 ~/.mem0/history.db 하나에 쓰면 database is locked 터짐
    }
    if os.getenv("MEM0_CUSTOM_FACT_PROMPT") == "halumem":
        config["custom_fact_extraction_prompt"] = CUSTOM_FACT_EXTRACTION_PROMPT
    return Memory.from_config(config)


def extract_user_name(persona_info: str) -> str:
    # 원본 eval_memzero.py와 동일 정규식 사용
    match = re.search(r"Name:\s*(.*?); Gender:", persona_info)
    if not match:
        raise ValueError("No name found.")
    return match.group(1).strip()


def iter_jsonl(path: str):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


# search 결과 -> context 포멧팅
def format_memories(results: list) -> list[str]:
    # created_at은 실제 벽시계 시간임. HaluMem 데이터셋의 가상 timeline과 무관하므로 사용 불가.
    formatted = []
    for r in results:
        ts = (r.get("metadata") or {}).get("session_time", "unknown_time")
        formatted.append(f"{ts}: {r['memory']}")
    return formatted


def search_memory(memory: Memory, query: str, user_id: str, user_name: str, top_k: int):
    found, duration_ms = search_with_retry(memory, query, user_id, top_k)
    memories = format_memories(found["results"])
    context = TEMPLATE_MEM0.format(user_id=user_name, memories=json.dumps(memories, indent=4))
    return context, memories, duration_ms


# user 처리 - 원본 process_user의 OSS 이식본
def process_user(user_data: dict, top_k: int, save_path: str, collection_name: str, use_tqdm: bool = True, trace_dir: str | None = None) -> str:
    uuid = user_data["uuid"]
    user_name = extract_user_name(user_data["persona_info"])
    user_id = uuid

    tmp_file = os.path.join(save_path, "tmp", f"{uuid}.json")
    if os.path.exists(tmp_file):
        return f"skip {user_name} (cached)"
    
    tracer = None
    try:
        # Process별 instance 생성 (상태는 Qdrant에 있으므로, 인스턴스 공유 불필요)
        # History DB는 User 단위로 분리 -> Lock 방지 + User별로 ADD/UPDATE/DELETE 이력이 Trace 부산물로 남음
        memory = build_memory(
            collection_name=f"{collection_name}_{uuid}",
            history_db_path=os.path.join(save_path, "tmp", f"history_{uuid}.db"),
        )
        memory.delete_all(user_id=user_id)  # 재실행 대비 초기화 작업임

        if trace_dir:
            tracer  = TraceLogger(
                os.path.join(trace_dir, f"{uuid}.jsonl"),
                system="mem0-classic-oss",
                run=collection_name,
                user=uuid
            )
            # instance 속성 교체로 mem0 내부 llm/retrieval 트래픽을 trace에 기록함
            memory.llm = TracingLLM(memory.llm, tracer)
            memory.vector_store = TracingVectorStore(memory.vector_store, tracer)

        out = {"uuid": uuid, "user_name": user_name, "sessions": []}

        sessions = user_data["sessions"]
        iterator = tqdm(sessions, desc=f"processing user [{user_name}]") if use_tqdm else sessions

        for i, session in enumerate(iterator, 1):
            if tracer:
                tracer.set_context(session=i - 1, stage="ingest", ref=None)
            new_session = {
                "memory_points": session["memory_points"],
                "dialogue": session["dialogue"],
            }

            dialogue = [{"role": t["role"], "content": t["content"]} for t in session["dialogue"]]

            result, duration_ms = add_with_retry(
                memory,
                dialogue,
                user_id=user_id,
                metadata={"session_time": session["start_time"]},
            )
            new_session["add_dialogue_duration_ms"] = duration_ms  # 처리 소요 시간 기록

            if session.get("is_generated_qa_session", False):
                # Long 버전 데이터셋에만 해당; 무관대화 대응 세션: add만 하고 평가에는 제외함
                out["sessions"].append({
                    "is_generated_qa_session": True,
                    "add_dialogue_duration_ms": new_session["add_dialogue_duration_ms"],
                })
                continue

            if not result["results"]:  # silent failure 감지 -> mem0의 update 결정 실패를 내부에서 조용히 삼킬 경우 빈 결과가 옴 -> 재시도 불가하므로 기록만 남기기
                print(f"Warning: No results found for user {user_name}, session_time: {session['start_time']}")

            new_session["extracted_memories"] = [r["memory"] for r in result["results"] if r["event"] != "DELETE"]
            new_session["memory_events"] = result["results"]  # event 원본 보존 (trace/dashboard 목적)
            if tracer:
                tracer.log(
                    "memory_write", 
                    purpose="reconcile", 
                    writes=[
                        {
                            "op": r["event"],
                            "id": r["id"],
                            "text": r["memory"],
                            "prev_text": r.get("previous_memory"),
                        }
                        for r in result["results"]
                    ]
                )

            # update 대상 gt mp : top-10 검색 스냅샷
            for mp in new_session["memory_points"]:
                if mp["is_update"] == "False" or not mp["original_memories"]:  # update 대상이 아닌 경우
                    continue
                if tracer:
                    tracer.set_context(stage="update_probe", ref={"mp_index": mp["index"]})
                _, mems, _ = search_memory(memory, mp["memory_content"], user_id, user_name, top_k=10)
                mp["memories_from_system"] = mems

            # 질문: top-20 검색 -> context 저장만 (답변 생성은 Stage A'에서 배치처리로)
            if "questions" in session:
                new_session["questions"] = []
                for qa in session["questions"]:
                    if tracer:
                        tracer.set_context(stage="qa_retrieval", ref={"question": qa["question"]})
                    context, _, dur = search_memory(memory, qa["question"], user_id, user_name, top_k=top_k)
                    new_qa = dict(qa)
                    new_qa["context"] = context
                    new_qa["search_duration_ms"] = dur
                    new_session["questions"].append(new_qa)
            out["sessions"].append(new_session)

            if not use_tqdm and i % 10 == 0:
                print(f"[{user_name}] {i}/{len(sessions)} sessions done", flush=True)

        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        return f"saved {user_name} -> {tmp_file}"
    except Exception:
        err_path = os.path.join(save_path, "tmp", f"{uuid}_error.log")
        with open(err_path, "w", encoding='utf-8') as f:
            f.write(traceback.format_exc())
        return f"FAILED {user_name} -> {err_path}"
    finally:
        if tracer:
            tracer.close()

def main(data_path: str, version: str, top_k: int=20, user_num: int | None = None, max_workers: int = 1, trace: bool = False):
    save_path = f"results/mem0-classic-oss/memzero-oss-{version}/"
    os.makedirs(os.path.join(save_path, "tmp"), exist_ok=True)
    collection_name = f"halumem_{version}"
    print(f"fact extraction prompt: {'halumem-custom' if os.getenv('MEM0_CUSTOM_FACT_PROMPT') == 'halumem' else 'mem0-default'}")
    trace_dir = None
    if trace:
        trace_dir = f"traces/mem0-classic-oss/{version}/"
        os.makedirs(trace_dir, exist_ok=True)

    # 각 User별로 병렬 처리하여 Process_User 소요시간 단축
    users = []
    for idx, user_data in enumerate(iter_jsonl(data_path), 1):
        if user_num and idx > user_num:
            break
        users.append(user_data)

    if max_workers <= 1:
        for u in users:
            print(process_user(u, top_k, save_path, collection_name, use_tqdm=True, trace_dir=trace_dir))
    else:
        with ProcessPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(process_user, u, top_k, save_path, collection_name, use_tqdm=False, trace_dir=trace_dir): u["uuid"] for u in users}
            for i, fut in enumerate(as_completed(futures), 1):
                print(f"[{i}/{len(futures)}] {fut.result()}")

    # user별 tmp를 최종 jsonl로 병합
    output = os.path.join(save_path, "memzero-oss_eval_results.jsonl")
    with open(output, "w", encoding="utf-8") as f_out:
        for file in sorted(os.listdir(os.path.join(save_path, "tmp"))):
            if file.endswith(".json"):
                with open(os.path.join(save_path, "tmp", file), encoding="utf-8") as f_in:
                    f_out.write(json.dumps(json.load(f_in), ensure_ascii=False) + "\n")
    print(f"done -> {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="dataset/HaluMem-Medium.jsonl")
    parser.add_argument("--version", default="dev")
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--user-num", type=int, default=None, help="앞에서 N명만 처리함 (스모크용)")
    parser.add_argument("--max-workers", type=int, default=1, help="병렬 처리를 위한 최대 워커 수 (1=순차처리)")
    parser.add_argument("--trace", action="store_true", help="memory system 내부 호출을 TraceLogger로 기록")
    args = parser.parse_args()
    main(args.data, args.version, args.top_k, args.user_num, args.max_workers, args.trace)