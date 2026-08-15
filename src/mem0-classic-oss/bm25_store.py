"""BM25 retriever — mem0의 vector_store를 통째로 갈아끼우는 어댑터 (mem0 코드 무수정).

왜 갈아끼우기만 하면 되는가
---------------------------
mem0가 vector_store에 요구하는 계약이 **텍스트만으로 충족된다**:

  * `search(query, vectors, ...)` — 원문 `query`가 항상 함께 온다 (main.py:378, 722)
  * `insert(vectors, payloads, ids)` — `payloads[i]["data"]`에 메모리 원문이 있다 (main.py:853)
  * 결과 소비 계약은 `.id` / `.score` / `.payload` 세 필드뿐 (main.py:735~755)

따라서 `vectors` 인자를 전부 무시하고 payload 텍스트로 색인·검색하면 된다.
러너에서 `memory.vector_store = BM25Store(...)` 한 줄로 교체한다 — OracleLLM과 같은 패턴.

⚠ mem0는 여전히 `embedding_model.embed()`를 호출한다(main.py:376/721/846). 결과는 이 스토어가
버리므로 **지표에는 영향이 없고 시간만 든다**. ingest 시간을 임베딩 레인과 비교할 때는
"임베딩 호출이 포함된 값"임을 명시할 것.

구현 방식
---------
Qdrant의 sparse vector + `Modifier.IDF`를 쓴다 (공동연구자의 naive-memory 실험과 동일 방식이라
결과를 직접 비교할 수 있다). 토큰화·스톱워드·스테밍은 `qdrant/bm25` 모델(fastembed) 기본값을
그대로 따른다 — 우리 취향대로 바꾸면 비교 가능성이 깨진다.

⚠ naive 실험과 다른 점: 저쪽은 골든 전체를 한 번에 색인해 IDF가 고정이지만, 메모리 시스템은
세션마다 증분 색인하므로 **IDF 통계가 시간에 따라 변한다**. 회피 불가능한 구조적 차이이며
해석에 명시해야 한다.
"""

from __future__ import annotations

import logging
import os

from qdrant_client import QdrantClient, models
from mem0.vector_stores.base import VectorStoreBase

logger = logging.getLogger(__name__)

BM25_MODEL = "qdrant/bm25"   # fastembed 로컬 추론 (uv add fastembed 필요)
VECTOR_NAME = "bm25"


class BM25Store(VectorStoreBase):
    """mem0 vector_store 계약을 만족하는 BM25(sparse) 스토어."""

    def __init__(self, collection_name: str, host: str | None = None, port: int | None = None,
                 path: str | None = None):
        self.collection_name = collection_name
        if path:                                   # 테스트용 로컬 모드
            self.client = QdrantClient(path=path)
        elif host:
            self.client = QdrantClient(host=host, port=int(port or 6333))
        else:
            self.client = QdrantClient(":memory:")
        self.create_col()

    # ---------- 컬렉션 ----------

    def create_col(self, name=None, vector_size=None, distance=None):
        """sparse 전용 컬렉션. 인자들은 mem0 시그니처 호환용이며 BM25에서는 의미가 없다."""
        if self.client.collection_exists(self.collection_name):
            # ⚠ 같은 이름으로 dense 컬렉션이 이미 있으면(임베딩 레인을 돌린 흔적) upsert가
            #    조용히 실패한다. sparse 설정이 없으면 지우고 다시 만든다.
            try:
                cfg = self.client.get_collection(self.collection_name).config.params
                if getattr(cfg, "sparse_vectors", None) and VECTOR_NAME in cfg.sparse_vectors:
                    return
                logger.warning("컬렉션 %s에 sparse 설정이 없어 재생성한다", self.collection_name)
            except Exception:
                logger.warning("컬렉션 %s 설정 확인 실패 — 재생성한다", self.collection_name)
            self.client.delete_collection(collection_name=self.collection_name)
        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config={},   # dense 벡터 없음 — mem0가 넘기는 임베딩은 버린다
            sparse_vectors_config={
                # IDF를 Qdrant가 서버측에서 계산하게 한다 (BM25의 정석 구성)
                VECTOR_NAME: models.SparseVectorParams(modifier=models.Modifier.IDF)
            },
        )

    def list_cols(self):
        return self.client.get_collections()

    def delete_col(self):
        self.client.delete_collection(collection_name=self.collection_name)

    def col_info(self):
        return self.client.get_collection(collection_name=self.collection_name)

    def reset(self):
        """delete_all 마지막에 호출된다 — 컬렉션을 비우고 다시 만든다."""
        logger.warning("BM25Store 초기화: %s", self.collection_name)
        self.delete_col()
        self.create_col()

    # ---------- 필터 ----------

    def _filter(self, filters: dict | None):
        """mem0의 Qdrant 구현과 동일한 의미론 (user_id 등 payload 완전일치)."""
        if not filters:
            return None
        conds = []
        for key, value in filters.items():
            if isinstance(value, dict) and "gte" in value and "lte" in value:
                conds.append(models.FieldCondition(
                    key=key, range=models.Range(gte=value["gte"], lte=value["lte"])))
            else:
                conds.append(models.FieldCondition(key=key, match=models.MatchValue(value=value)))
        return models.Filter(must=conds) if conds else None

    @staticmethod
    def _doc(payload: dict | None):
        """payload에서 색인 대상 텍스트를 꺼내 BM25 Document로 만든다."""
        text = (payload or {}).get("data") or ""
        return {VECTOR_NAME: models.Document(text=text, model=BM25_MODEL)}

    # ---------- CRUD ----------

    def insert(self, vectors: list, payloads: list = None, ids: list = None):
        """⚠ `vectors`(임베딩)는 의도적으로 무시한다 — 색인은 payload['data'] 텍스트로 한다."""
        payloads = payloads or [{} for _ in vectors]
        ids = ids or [None] * len(payloads)
        points = [
            models.PointStruct(id=pid, vector=self._doc(payload), payload=payload)
            for pid, payload in zip(ids, payloads)
        ]
        self.client.upsert(collection_name=self.collection_name, points=points)

    def search(self, query: str, vectors: list = None, limit: int = 5, filters: dict = None) -> list:
        """원문 `query`로 BM25 검색. `vectors`는 무시.

        반환은 Qdrant의 ScoredPoint 그대로 — mem0가 쓰는 `.id`/`.score`/`.payload`를 이미 만족한다.
        ⚠ BM25 점수는 코사인 유사도와 스케일이 다르다. 러너는 threshold를 넘기지 않으므로
        (main.py:653 기본 None) 점수 컷오프에 걸릴 일은 없지만, 점수 절대값을 임베딩 레인과
        비교해서는 안 된다.
        """
        hits = self.client.query_points(
            collection_name=self.collection_name,
            query=models.Document(text=query or "", model=BM25_MODEL),
            using=VECTOR_NAME,
            query_filter=self._filter(filters),
            limit=limit,
            with_payload=True,
        )
        return hits.points

    def update(self, vector_id, vector: list = None, payload: dict = None):
        """`vector`는 무시하고 payload 텍스트로 재색인한다."""
        if payload is None:                       # 텍스트가 없으면 payload만 갱신
            self.client.set_payload(collection_name=self.collection_name,
                                    payload={}, points=[vector_id])
            return
        self.client.upsert(
            collection_name=self.collection_name,
            points=[models.PointStruct(id=vector_id, vector=self._doc(payload), payload=payload)],
        )

    def get(self, vector_id):
        result = self.client.retrieve(collection_name=self.collection_name,
                                      ids=[vector_id], with_payload=True)
        return result[0] if result else None

    def delete(self, vector_id):
        self.client.delete(collection_name=self.collection_name,
                           points_selector=models.PointIdsList(points=[vector_id]))

    def list(self, filters: dict = None, limit: int = 100):
        """⚠ 반드시 (points, next_offset) 튜플을 반환해야 한다 —
        mem0의 delete_all이 `list(filters=...)[0]`으로 첫 원소를 꺼내 쓴다 (main.py:816)."""
        return self.client.scroll(
            collection_name=self.collection_name,
            scroll_filter=self._filter(filters),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )


def build_bm25_store(collection_name: str) -> BM25Store:
    """러너에서 쓰는 팩토리 — Qdrant 접속 정보는 기존 실험과 동일한 env를 따른다."""
    return BM25Store(collection_name=collection_name,
                     host=os.getenv("QDRANT_HOST"),
                     port=os.getenv("QDRANT_PORT", "6333"))
