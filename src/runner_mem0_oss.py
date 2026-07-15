import os
import re
import json
import time
import argparse
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

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

def build_memory(collection_name: str) -> Memory:
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
            },
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": collection_name,
                "host": os.getenv("QDRANT_HOST"),
                "port": int(os.getenv("QDRANT_PORT", "6333")),
            },
        },
    }
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
    start = time.time()
    found = memory.search(query, user_id=user_id, limit=top_k)
    memories = format_memories(found["results"])
    context = TEMPLATE_MEM0.format(user_id=user_name, memories=json.dumps(memories, indent=4))
    return context, memories, (time.time() - start) * 1000


# user 처리 - 원본 process_user의 OSS 이식본
def process_user(user_data: dict, memory: Memory, top_k: int, save_path: str):
    uuid = user_data["uuid"]
    user_name = extract_user_name(user_data["persona_info"])
    user_id = uuid

    tmp_file = os.path.join(save_path, "tmp", f"{uuid}.json")
    if os.path.exists(tmp_file):
        print(f"skip {user_name} (cached)")
        return
    
    memory.delete_all(user_id=user_id)  # 재실행 대비 초기화 작업임

    out = {"uuid": uuid, "user_name": user_name, "sessions": []}

    for session in tqdm(user_data["sessions"], desc=f"user {user_name}"):
        new_session = {
            "memory_points": session["memory_points"],
            "dialogue": session["dialogue"],
        }

        dialogue = [{"role": t["role"], "content": t["content"]} for t in session["dialogue"]]

        start = time.time()
        result = memory.add(
            dialogue,
            user_id=user_id,
            metadata={"session_time": session["start_time"]},
        )
        new_session["add_dialogue_duration_ms"] = (time.time() - start) * 1000  # 처리 소요 시간 기록

        if session.get("is_generated_qa_session", False):
            # Long 버전 데이터셋에만 해당; 무관대화 대응 세션: add만 하고 평가에는 제외함
            out["sessions"].append({
                "is_generated_qa_session": True,
                "add_dialogue_duration_ms": new_session["add_dialogue_duration_ms"],
            })
            continue

        new_session["extracted_memories"] = [r["memory"] for r in result["results"] if r["event"] != "DELETE"]
        new_session["memory_events"] = result["results"]  # event 원본 보존 (trace/dashboard 목적)

        # update 대상 gt mp : top-10 검색 스냅샷
        for mp in new_session["memory_points"]:
            if mp["is_update"] == "False" or not mp["original_memories"]:  # update 대상이 아닌 경우
                continue
            _, mems, _ = search_memory(memory, mp["memory_content"], user_id, user_name, top_k=10)
            mp["memories_from_system"] = mems

        # 질문: top-20 검색 -> context 저장만 (답변 생성은 Stage A'에서 배치처리로)
        if "questions" in session:
            new_session["questions"] = []
            for qa in session["questions"]:
                context, _, dur = search_memory(memory, qa["question"], user_id, user_name, top_k=top_k)
                new_qa = dict(qa)
                new_qa["context"] = context
                new_qa["search_duration_ms"] = dur
                new_session["questions"].append(new_qa)
        out["sessions"].append(new_session)

    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"saved {user_name} -> {tmp_file}")



def main(data_path: str, version: str, top_k: int=20, user_num: int | None = None):
    save_path = f"results/memzero-oss-{version}/"
    os.makedirs(os.path.join(save_path, "tmp"), exist_ok=True)

    memory = build_memory(collection_name=f"halumem_{version}")

    for idx, user_data in enumerate(iter_jsonl(data_path), 1):
        if user_num and idx > user_num:
            break
        process_user(user_data, memory, top_k, save_path)

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
    args = parser.parse_args()
    main(args.data, args.version, args.top_k, args.user_num)