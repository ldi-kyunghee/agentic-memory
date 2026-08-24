"""v3 Memory 를 classic 0.1.118 의 호출 규약으로 감싸는 어댑터.

**왜 프록시인가.** 세 벤치마크의 ingest 하네스(각 250~400줄)를 복사하면 A(classic)와
B(v3)의 로직이 나중에 갈라짐. 여기서 호출 규약만 번역하면 **같은 하네스가 두 팔을 다 돔.**
알고리즘은 한 줄도 안 짬. mem0 소스도 안 고침.

바뀐 곳은 둘뿐임 (v2.0.18 실측):

    classic  memory.search(q, user_id=u, limit=k)
    v3       memory.search(q, filters={"user_id": u}, top_k=k)

    classic  memory.get_all(user_id=u, limit=n)
    v3       memory.get_all(filters={"user_id": u}, top_k=n)

⚠ v3 의 search 는 user_id 를 top-level 로 주면 **거부**함(예외). 그건 안전함.
   문제는 `limit` 임. 거부되지 않고 조용히 무시되어 기본 top_k=20 이 쓰임.
   get_all 도 같음. 안 고치면 저장물을 20개로 세고 검색을 20개만 함.

⚠ OSS 에서 막힌 인자: `add(timestamp=)`, `search(reference_date=)`.
   시그니처에는 있으나 넘기면 ValueError 임. 시간 정보는 classic 과 똑같이
   metadata['session_date'] 로 넣음.
"""
import os

from mem0 import Memory

__all__ = ["build_memory", "V3MemoryAdapter", "SEARCH_DEFAULTS"]

# v3 에만 있는 손잡이. 기본값을 그대로 쓰는 것도 선택이므로 한곳에 모아 명시함.
#   threshold : 융합 점수 하한. 공식 기본 0.1 을 유지함
#   rerank    : reranker 를 따로 설정하지 않으므로 켜도 무의미. False 유지
#   explain   : 진단용. verify_v3.py 에서만 True 로 씀
SEARCH_DEFAULTS = {
    "threshold": float(os.getenv("MEM0V3_THRESHOLD", "0.1")),
    "rerank": os.getenv("MEM0V3_RERANK") == "1",
}


class V3MemoryAdapter:
    """classic 규약 -> v3 규약 번역기.

    번역하는 것만 명시적으로 정의하고 나머지(`vector_store`, `llm`, `delete_all`,
    `vector_store.delete_col()` …)는 그대로 흘려보냄. 흘려보내기가 중요한 이유는
    tracing 훅이 `memory.llm = ...` · `memory.vector_store = ...` 로 **대입**하기
    때문임. __setattr__ 도 같이 넘겨야 함.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner: Memory):
        object.__setattr__(self, "_inner", inner)

    # ---- 번역이 필요한 둘 ----
    def search(self, query, user_id=None, limit=20, **kw):
        if user_id is None:
            raise ValueError("V3MemoryAdapter.search: user_id 를 반드시 줌")
        opts = {**SEARCH_DEFAULTS, **kw}
        return self._inner.search(query, filters={"user_id": user_id}, top_k=limit, **opts)

    def get_all(self, user_id=None, limit=20, **kw):
        if user_id is None:
            raise ValueError("V3MemoryAdapter.get_all: user_id 를 반드시 줌")
        return self._inner.get_all(filters={"user_id": user_id}, top_k=limit, **kw)

    # ---- 나머지는 그대로 ----
    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_inner"), name, value)

    def __repr__(self):
        return f"V3MemoryAdapter({self._inner!r})"


def build_memory(collection_name: str, history_db_path: str) -> V3MemoryAdapter:
    """classic 의 build_memory 와 **같은 시그니처·같은 env** 를 씀.

    config 스키마는 v2.0.18 에서 무수정으로 통과하는 것을 확인했음
    (docs/mem0-v3/implementation-plan.md §2-5).
    """
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
        "history_db_path": history_db_path,
    }
    memory = Memory.from_config(config)

    # classic 과 동일: gpt-oss 계열은 mem0 가 reasoning_effort 를 못 넘겨서 extra_body 로 주입
    effort = os.getenv("MEM0_REASONING_EFFORT")
    if effort:
        _orig = memory.llm.client.chat.completions.create

        def _create(*args, **kwargs):
            kwargs["extra_body"] = {**(kwargs.get("extra_body") or {}), "reasoning_effort": effort}
            return _orig(*args, **kwargs)

        memory.llm.client.chat.completions.create = _create

    return V3MemoryAdapter(memory)
