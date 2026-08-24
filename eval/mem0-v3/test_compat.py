"""어댑터 단위 검사. 서버·GPU 없이 돎.

하네스가 mem0 를 만지는 **모든 지점**을 가짜 Memory 로 재현해서, 번역이 맞는지와
흘려보내기가 되는지를 봄. 실제 ingest 를 돌리기 전에 여기서 잡는 것이 목적임.

    uv run --project eval/mem0-v3 python eval/mem0-v3/test_compat.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class FakeVectorStore:
    def __init__(self):
        self.deleted_col = False

    def delete_col(self):
        self.deleted_col = True


class FakeMemory:
    """v3 Memory 의 호출 규약을 흉내냄. classic 규약으로 부르면 터지게 해둠."""

    def __init__(self):
        self.calls = []
        self.vector_store = FakeVectorStore()
        self.llm = "ORIGINAL_LLM"

    def add(self, messages, *, user_id=None, agent_id=None, run_id=None,
            metadata=None, timestamp=None, expiration_date=None, infer=True,
            memory_type=None, prompt=None):
        if timestamp is not None:
            raise ValueError("The timestamp parameter is not supported by the OSS Memory SDK.")
        self.calls.append(("add", {"user_id": user_id, "metadata": metadata}))
        return {"results": [{"id": "1", "memory": "m", "event": "ADD"}]}

    def search(self, query, *, top_k=20, filters=None, threshold=0.1, rerank=False,
               explain=False, reference_date=None, show_expired=False, **kwargs):
        if "user_id" in kwargs:
            raise TypeError("user_id 를 top-level 로 주면 v3 가 거부함")
        if reference_date is not None:
            raise ValueError("reference_date is not supported by the OSS Memory SDK.")
        self.calls.append(("search", {"top_k": top_k, "filters": filters,
                                      "threshold": threshold, "rerank": rerank,
                                      "explain": explain, "extra": kwargs}))
        return {"results": [{"id": str(i), "memory": f"m{i}"} for i in range(top_k)]}

    def get_all(self, *, filters=None, top_k=20, show_expired=False, **kwargs):
        if "user_id" in kwargs:
            raise TypeError("user_id 를 top-level 로 주면 v3 가 거부함")
        self.calls.append(("get_all", {"top_k": top_k, "filters": filters}))
        return {"results": [{"id": str(i)} for i in range(top_k)]}

    def delete_all(self, user_id=None, agent_id=None, run_id=None):
        self.calls.append(("delete_all", {"user_id": user_id}))


def run():
    from compat import V3MemoryAdapter

    fails = []

    def check(name, cond, detail=""):
        print(f"  {'OK  ' if cond else 'FAIL'} {name}{('  ' + detail) if detail else ''}")
        if not cond:
            fails.append(name)

    inner = FakeMemory()
    m = V3MemoryAdapter(inner)

    print("=== 1. search: limit -> top_k, user_id -> filters ===")
    m.search("q", user_id="u1", limit=800)
    kind, a = inner.calls[-1]
    check("top_k 로 800 이 전달됨 (기본 20 으로 안 떨어짐)", a["top_k"] == 800, f"top_k={a['top_k']}")
    check("filters 에 user_id 가 들어감", a["filters"] == {"user_id": "u1"}, str(a["filters"]))
    check("user_id 가 top-level 로 새지 않음", "user_id" not in a["extra"])
    check("threshold 기본 0.1 이 붙음", a["threshold"] == 0.1)

    print("\n=== 2. get_all: limit -> top_k ===")
    m.get_all(user_id="u1", limit=100000)
    kind, a = inner.calls[-1]
    check("top_k 로 100000 이 전달됨", a["top_k"] == 100000, f"top_k={a['top_k']}")
    check("filters 에 user_id", a["filters"] == {"user_id": "u1"})

    print("\n=== 3. add: 규약 그대로, timestamp 는 안 넘김 ===")
    m.add([{"role": "user", "content": "x"}], user_id="u1",
          metadata={"session_date": "2026-01-01", "session_id": 3})
    kind, a = inner.calls[-1]
    check("user_id 전달", a["user_id"] == "u1")
    check("metadata 전달 (session_date 로 시각 유지)",
          a["metadata"] == {"session_date": "2026-01-01", "session_id": 3})

    print("\n=== 4. 흘려보내기: delete_all · vector_store.delete_col ===")
    m.delete_all(user_id="u1")
    check("delete_all 이 내부로 감", inner.calls[-1] == ("delete_all", {"user_id": "u1"}))
    m.vector_store.delete_col()
    check("vector_store 접근이 내부로 감", inner.vector_store.deleted_col)

    print("\n=== 5. 대입 흘려보내기 (tracing 훅이 이걸 씀) ===")
    m.llm = "TRACING_LLM"
    check("memory.llm 대입이 내부에 반영됨", inner.llm == "TRACING_LLM", f"inner.llm={inner.llm}")
    sentinel = FakeVectorStore()
    m.vector_store = sentinel
    check("memory.vector_store 대입이 내부에 반영됨", inner.vector_store is sentinel)

    print("\n=== 6. 반환 형태가 classic 과 같은지 ===")
    r = m.search("q", user_id="u1", limit=3)
    check("search 반환이 {'results': [...]}", isinstance(r, dict) and "results" in r and len(r["results"]) == 3)
    r = m.add([{"role": "user", "content": "x"}], user_id="u1", metadata={})
    check("add 반환이 {'results': [...]}", isinstance(r, dict) and "results" in r)

    print("\n=== 7. 잘못 부르면 조용히 넘어가지 않는지 ===")
    try:
        m.search("q", limit=50)
        check("user_id 없이 search 하면 예외", False, "예외가 안 남")
    except ValueError:
        check("user_id 없이 search 하면 예외", True)
    try:
        m.get_all(limit=50)
        check("user_id 없이 get_all 하면 예외", False, "예외가 안 남")
    except ValueError:
        check("user_id 없이 get_all 하면 예외", True)

    print("\n=== 8. explain 을 넘길 수 있는지 (신호 검증에 필요) ===")
    m.search("q", user_id="u1", limit=5, explain=True)
    check("explain=True 가 전달됨", inner.calls[-1][1]["explain"] is True)

    print()
    if fails:
        print(f"✗ 실패 {len(fails)}건: {fails}")
        return 1
    print("✓ 전부 통과")
    return 0


if __name__ == "__main__":
    sys.exit(run())
