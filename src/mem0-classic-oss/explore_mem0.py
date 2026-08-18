import json
from dotenv import load_dotenv

load_dotenv()

from mem0 import Memory

# ── mem0 구성 ──────────────────────────────────────────
# llm/embedder 둘 다 OPENAI_BASE_URL env를 자동으로 읽음
# → 서버에서는 .env만 바꾸면 vLLM으로 전환됨
config = {
    "llm": {
        "provider": "openai",
        "config": {"model": "gpt-4o-mini", "temperature": 0.0},
    },
    "embedder": {
        "provider": "openai",
        "config": {"model": "text-embedding-3-small"},  # dims 1536 기본값임
    },
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "collection_name": "explore",
            "path": "traces/qdrant_local",  # 로컬 embedded 모드, docker 불필요함
        },
    },
}

memory = Memory.from_config(config)

# ── HaluMem 첫 유저의 첫 세션 로드 ─────────────────────
with open("dataset/HaluMem-Medium.jsonl", encoding="utf-8") as f:
    user = json.loads(f.readline())

session = user["sessions"][0]
dialogue = [{"role": t["role"], "content": t["content"]} for t in session["dialogue"]]

print(f"유저: {user['persona_info'][:80]}...")
print(f"세션 턴 수: {len(dialogue)}, 골든 메모리 수: {len(session['memory_points'])}")

# ── add: timestamp는 metadata로 우회 (OSS엔 파라미터 없음) ──
result = memory.add(
    dialogue,
    user_id="explore_user",
    metadata={"session_time": session["start_time"]},
)

print("\n=== add() 반환 이벤트 ===")
print(json.dumps(result, indent=2, ensure_ascii=False))

# ── search: 반환 스키마 실측이 목적 ────────────────────
q = "What is the user's name and where do they live?"
found = memory.search(q, user_id="explore_user", limit=5)

print("\n=== search() 결과 원본 (스키마 확인용) ===")
print(json.dumps(found, indent=2, ensure_ascii=False))