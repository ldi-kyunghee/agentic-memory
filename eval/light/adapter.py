"""LIGHT 의 모델·임베더·인덱스 어댑터.

설계 제약은 하나임: `src/mem0-classic-oss/tracing.py` 의 `attach_tracing(memory, tracer)` 가
**무수정으로** 걸려야 함. 그래서 mem0 와 같은 속성 모양을 노출함:
  .llm              generate_response(messages, **kw) + .client.chat.completions.create 경로
  .embedding_model  embed(text, memory_action) / embed_batch(texts, ...) + .client.embeddings.create
  .vector_store     search(query=, vectors=, limit=) 가 Qdrant 레코드 모양(.id/.payload["data"]/.score)

⚠ 임베딩·검색을 tracer 밖에서 직접 부르면 2026-08-27 임베딩 누락 사고가 재발함.
  모든 외부 호출은 이 세 속성을 **attach_tracing 이후에** 통해서만 나감.
"""
import os
from types import SimpleNamespace

import numpy as np
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_random_exponential


class LightLLM:
    """추출·fold·noise filter 전부 이 하나를 씀 (원본의 qwen_llm/gpt_llm 두 자리 통일).

    ⚠ reasoning effort 는 LIGHT_REASONING_EFFORT 가 있을 때만 넣음. 기본 미설정 =
      모델 기본값(medium) — mem0 레인의 agent 통제와 동일함. ANSWER_REASONING_EFFORT 가
      투입에 새어들면 안 되므로 그 env 는 여기서 읽지 않음.
    """

    def __init__(self):
        self.client = OpenAI(
            base_url=os.getenv("OPENAI_BASE_URL", "http://localhost:8002/v1"),
            api_key=os.getenv("OPENAI_API_KEY", "dummy"),
        )
        self.model = os.getenv("MEM0_LLM_MODEL", "openai/gpt-oss-120b")
        self.max_tokens = int(os.getenv("LIGHT_LLM_MAX_TOKENS", "16384"))
        self.effort = os.getenv("LIGHT_REASONING_EFFORT")  # 없으면 미주입

    @retry(wait=wait_random_exponential(min=5, max=30), stop=stop_after_attempt(5), reraise=True)
    def generate_response(self, messages, **kwargs):
        kw = {"model": self.model, "messages": messages,
              "max_tokens": kwargs.pop("max_tokens", self.max_tokens),
              "temperature": kwargs.pop("temperature", 0)}
        if self.effort:
            kw["extra_body"] = {"reasoning_effort": self.effort}
        resp = self.client.chat.completions.create(**kw)
        return resp.choices[0].message.content or ""


class LightEmbedder:
    """서버 vLLM 임베더(8001) 하나로 통일. 원본의 bge-small(검색)/bge-large(청킹) 2종 대체."""

    BATCH = 64  # ⚠ 수천 건을 한 요청에 보내면 vLLM 요청 크기를 넘음. 나눠 보냄

    def __init__(self):
        self.client = OpenAI(
            base_url=os.getenv("MEM0_EMBED_BASE_URL", "http://localhost:8001/v1"),
            api_key=os.getenv("OPENAI_API_KEY", "dummy"),
        )
        self.model = os.getenv("MEM0_EMBED_MODEL", "Qwen/Qwen3-Embedding-4B")
        self.dims = int(os.getenv("MEM0_EMBED_DIMS", "2560"))

    @retry(wait=wait_random_exponential(min=2, max=20), stop=stop_after_attempt(5), reraise=True)
    def _create(self, inputs: list):
        return self.client.embeddings.create(
            input=inputs, model=self.model, dimensions=self.dims)

    def embed(self, text, memory_action=None):
        text = (text or " ").replace("\n", " ")
        return self._create([text]).data[0].embedding

    def embed_batch(self, texts, memory_action=None):
        out = []
        clean = [(t or " ").replace("\n", " ") for t in texts]
        for i in range(0, len(clean), self.BATCH):
            resp = self._create(clean[i:i + self.BATCH])
            out.extend(d.embedding for d in resp.data)
        return out


class LightVectorStore:
    """in-process 코사인 인덱스.

    원본은 FAISS 지만 결과는 정규화 코사인 top-k 로 동일함. Qdrant 를 안 쓰는 이유:
    LIGHT 는 대화/유저 단위 일회성 인덱스라 컬렉션 누적 사고(2026-08-18, 55개에서 서버
    사망)의 위험을 애초에 만들지 않는 쪽이 나음.

    search 반환은 Qdrant 레코드 모양(SimpleNamespace) — TracingVectorStore 가
    .id/.payload["data"]/.score 를 읽음.
    """

    def __init__(self):
        self._vecs = None          # (N, D) 정규화됨
        self._payloads = []        # [{"data": kv_text, "pair_id": ..., ...}]
        self._ids = []

    @staticmethod
    def _norm(m):
        m = np.asarray(m, dtype=np.float32)
        n = np.linalg.norm(m, axis=-1, keepdims=True)
        n[n == 0] = 1.0
        return m / n

    def add(self, ids, vectors, payloads):
        v = self._norm(vectors)
        self._vecs = v if self._vecs is None else np.vstack([self._vecs, v])
        self._ids.extend(ids)
        self._payloads.extend(payloads)

    def search(self, query=None, vectors=None, limit=5, filters=None, **kw):
        if self._vecs is None or not len(self._ids):
            return []
        q = self._norm(vectors)
        scores = self._vecs @ q
        k = min(int(limit or 5), len(self._ids))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [SimpleNamespace(id=self._ids[i], score=float(scores[i]),
                                payload=self._payloads[i]) for i in top]

    @property
    def size(self):
        return len(self._ids)

    def delete_col(self):
        # in-process 라 지울 것이 없음. 기존 하네스의 finally 호환용
        self._vecs, self._payloads, self._ids = None, [], []


class LightMemory:
    """attach_tracing 이 감싸는 세 속성의 컨테이너."""

    def __init__(self):
        self.llm = LightLLM()
        self.embedding_model = LightEmbedder()
        self.vector_store = LightVectorStore()


def build_light_memory() -> LightMemory:
    return LightMemory()


class LCEmbeddingsAdapter:
    """SemanticChunker 용 langchain Embeddings 어댑터.

    ⚠ 반드시 attach_tracing **이후의** memory 로 만들 것. 그래야 청킹 임베딩도
      trace(embed_call)와 비용 계측에 남음. tracer 이전 객체를 잡으면 조용히 빠짐.
    """

    def __init__(self, memory):
        self._m = memory

    def embed_documents(self, texts):
        return self._m.embedding_model.embed_batch(list(texts), "search")

    def embed_query(self, text):
        return self._m.embedding_model.embed(text, "search")
