"""BM25Store 계약 검증 — mem0가 실제로 부르는 경로만 실데이터 모양으로 확인한다.

실행: uv run python src/mem0-classic-oss/test_bm25_store.py
Qdrant 서버 없이 로컬 모드로 돈다 (계약 검증이 목적이라 서버 왕복이 필요 없다).
"""

import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bm25_store import BM25Store  # noqa: E402

UID = "martin"
PASS, FAIL = "  ✅", "  ❌"
fails = []


def check(name, cond, detail=""):
    print(f"{PASS if cond else FAIL} {name}{('  — ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)


def payload(text, user=UID):
    # mem0의 _create_memory가 만드는 payload 모양 (main.py:848~852)
    return {"data": text, "hash": "x", "created_at": "2025-09-04T18:42:18", "user_id": user}


with tempfile.TemporaryDirectory() as td:
    S = BM25Store(collection_name="t", path=td)

    MEMS = [
        "Martin Mark's monthly income is 20000 yuan",
        "Martin Mark dislikes sugary sodas",
        "Martin Mark prefers low-sugar carbonated drinks as of 2031",
        "Martin Mark lives in Columbus",
        "Martin Mark's partner status: no_relationship",
    ]
    # mem0는 point id로 str(uuid.uuid4())를 쓴다 (main.py:848) — Qdrant가 UUID/정수만 받으므로 동일하게 맞춘다
    ids = [str(uuid.uuid4()) for _ in MEMS]
    # mem0는 insert(vectors=[emb], ids=[...], payloads=[...]) 형태로 부른다 — 벡터는 버려져야 한다
    S.insert(vectors=[[0.0] * 4] * len(MEMS), ids=ids, payloads=[payload(m) for m in MEMS])

    # 1) 왕복: 자기 자신이 1위
    hit = S.search(query=MEMS[3], vectors=[0.0] * 4, limit=5, filters={"user_id": UID})
    check("insert→search 왕복 (자기 자신 1위)", hit and hit[0].payload["data"] == MEMS[3],
          hit[0].payload["data"][:40] if hit else "결과 없음")

    # 2) 소비 계약: .id / .score / .payload
    check("결과 객체가 .id/.score/.payload 보유",
          all(hasattr(h, a) for h in hit for a in ("id", "score", "payload")))

    # 3) ⚠ 핵심 가설 — 시점·수치 토큰이 스테밍에 살아남는가
    y = S.search(query="2031 low sugar drinks", vectors=None, limit=3, filters={"user_id": UID})
    check("연도 토큰(2031)으로 해당 메모리 검색", y and "2031" in y[0].payload["data"],
          y[0].payload["data"][:52] if y else "결과 없음")
    n = S.search(query="20000 yuan income", vectors=None, limit=3, filters={"user_id": UID})
    check("수치 토큰(20000)으로 해당 메모리 검색", n and "20000" in n[0].payload["data"],
          n[0].payload["data"][:52] if n else "결과 없음")

    # 4) 필터 격리 — 다른 유저 문서가 안 섞여야 한다
    S.insert(vectors=[[0.0] * 4], ids=[str(uuid.uuid4())], payloads=[payload("Sarah Garcia lives in Columbus", "sarah")])
    iso = S.search(query="lives in Columbus", vectors=None, limit=10, filters={"user_id": UID})
    check("filters(user_id) 격리", all(h.payload["user_id"] == UID for h in iso),
          f"{len(iso)}건 모두 {UID}")

    # 5) update — 텍스트 재색인이 검색에 반영되는가
    S.update(ids[1], vector=[0.0] * 4, payload=payload("Martin Mark now enjoys espresso every morning"))
    up = S.search(query="espresso morning", vectors=None, limit=3, filters={"user_id": UID})
    check("update 후 새 텍스트로 검색됨", up and "espresso" in up[0].payload["data"],
          up[0].payload["data"][:44] if up else "결과 없음")
    check("update가 옛 텍스트를 남기지 않음",
          not any("sugary sodas" in h.payload["data"] for h in S.list(limit=100)[0]))

    # 6) get / delete
    check("get(vector_id)", (g := S.get(ids[3])) is not None and g.payload["data"] == MEMS[3])
    S.delete(ids[3])
    check("delete 후 get이 None", S.get(ids[3]) is None)

    # 7) list 반환 형태 — delete_all이 list(...)[0]으로 꺼내 쓴다 (main.py:816)
    lst = S.list(filters={"user_id": UID}, limit=100)
    check("list가 (points, next_offset) 튜플 반환",
          isinstance(lst, tuple) and isinstance(lst[0], list), f"{len(lst[0])}건")

    # 8) limit — 실험은 top-20으로 돌린다
    S.insert(vectors=[[0.0] * 4] * 30, ids=[str(uuid.uuid4()) for _ in range(30)],
             payloads=[payload(f"Martin Mark event number {i} happened in Columbus") for i in range(30)])
    check("top-20 요청 시 20건 반환",
          len(S.search(query="Columbus event", vectors=None, limit=20, filters={"user_id": UID})) == 20)

    # 9) reset — delete_all 마지막 단계
    S.reset()
    check("reset 후 컬렉션이 비어 있음", len(S.list(limit=100)[0]) == 0)
    S.insert(vectors=[[0.0] * 4], ids=[str(uuid.uuid4())], payloads=[payload("Martin Mark returned to Columbus")])
    check("reset 후에도 재사용 가능", len(S.search(query="Columbus", vectors=None, limit=5)) == 1)

print()
if fails:
    print(f"실패 {len(fails)}건: {fails}")
    sys.exit(1)
print("전체 통과 — mem0 vector_store 계약 충족")
