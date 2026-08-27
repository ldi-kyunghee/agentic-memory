import os
import json
import time
from datetime import datetime, timezone


class TraceLogger:
    """
    User 1명 분량의 Trace를 jsonl로 기록하는 클래스.
    Context (Session / Stage / Ref)는 Runner가 갱신함.
    """

    def __init__(self, path: str, system: str, run: str, user: str):
        self.f = open(path, "a", encoding="utf-8")
        self.system = system
        self.run = run
        self.user = user
        self.seq = 0
        self.ctx = {"session": None, "stage": None, "ref": None}

    def set_context(self, **kwargs):
        # session / stage / ref 중 넘어온 것만 갱신함
        self.ctx.update(kwargs)

    def log(self, event: str, purpose: str | None = None, duration_ms: float | None = None, **payload):
        self.seq += 1
        record = {
            "v": 1,
            "system": self.system,
            "run": self.run,
            "user": self.user,
            "session": self.ctx["session"],
            "seq": self.seq,
            "ts": datetime.now(timezone.utc).isoformat(),
            "stage": self.ctx["stage"],
            "event": event,
            "purpose": purpose,
            "duration_ms": duration_ms,
        }
        if self.ctx["ref"]:
            record["ref"] = self.ctx["ref"]
        record.update(payload)
        self.f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.f.flush()  # crash 나기 전까지 보존되도록 매 줄마다 flush함 (LLM Call 대비 비용 무시가능한 수준)

    def close(self):
        self.f.close()



class TracingLLM:
    """
    Mem0-Classic-OSS의 LLM Instance Wrapper.
    generate_response만 가로채고 나머지 속성은 위임함
    """
    
    def __init__(self, inner, tracer: TraceLogger):
        self._inner = inner
        self._tracer = tracer
        self._last_extra = {}
        self._hook_client()

    def _hook_client(self):
        # mem0의 generate_response는 message.content(문자열)만 돌려주므로 reasoning 모델의
        # 사고 과정이 상위에서는 이미 유실된다 (vLLM --reasoning-parser는 message.reasoning으로 분리 반환).
        # -> OpenAI 클라이언트 레벨에서 원 응답을 가로채 사고 과정·토큰 사용량을 따로 보관한다.
        comp = getattr(getattr(getattr(self._inner, "client", None), "chat", None), "completions", None)
        if comp is None:
            return
        orig_create = comp.create

        def create(*args, **kwargs):
            resp = orig_create(*args, **kwargs)
            extra = {}
            try:
                choice = resp.choices[0]
                msg = choice.message
                for key in ("reasoning", "reasoning_content"):  # vLLM/OpenAI 양쪽 표기 대응
                    val = getattr(msg, key, None)
                    if val:
                        extra["reasoning"] = val
                        break
                extra["finish_reason"] = choice.finish_reason
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    # ⚠ 예전에는 completion_tokens 만 남겼다. 이 파이프라인들은 **입력 토큰이
                    #   비용의 대부분**이라 그것 없이는 사후에 비용을 못 센다 (2026-08-26에 겪음).
                    extra["prompt_tokens"] = getattr(usage, "prompt_tokens", None)
                    extra["completion_tokens"] = getattr(usage, "completion_tokens", None)
                    extra["total_tokens"] = getattr(usage, "total_tokens", None)
                    details = getattr(usage, "completion_tokens_details", None)
                    rt = getattr(details, "reasoning_tokens", None)
                    if rt:
                        extra["reasoning_tokens"] = rt
                extra["model"] = getattr(resp, "model", None) or kwargs.get("model")
            except Exception:
                pass  # trace 부가정보 실패가 본 파이프라인을 막지 않도록
            self._last_extra = extra
            return resp

        comp.create = create

    def generate_response(self, messages, **kwargs):
        start = time.time()
        self._last_extra = {}
        response = self._inner.generate_response(messages, **kwargs)
        duration = (time.time() - start) * 1000

        # mem0 0.1.118: extraction call은 [system, user] 2개, update decision은 [user] 1개
        # -> system role의 유무가 purpose를 결정함
        # ⚠ 2.0.18(v3)은 ADD-only라 호출이 하나뿐이고 항상 system을 낀다. 같은 규칙을 쓰면
        #   전부 fact_extraction으로 찍혀 classic의 추출 호출과 구분이 안 된다.
        if os.getenv("MEM0_IMPL") == "v3":
            purpose = "additive_extraction"
        else:
            has_system = any(m.get("role") == "system" for m in messages)
            purpose = "fact_extraction" if has_system else "update_decision"

        llm = {"messages": messages, "response": response, **self._last_extra}
        self._tracer.log("llm_call", purpose=purpose, duration_ms=duration, llm=llm)
        return response
    
    def __getattr__(self, name):
        return getattr(self._inner, name)



class TracingEmbedder:
    """임베딩 호출을 전부 남긴다.

    ⚠ 왜 필요한가: 예전 tracer 는 LLM 만 감쌌다. 그래서 임베딩 호출이 trace 에 한 줄도 없었고,
      나중에 비용을 되살릴 때 **임베딩만 통째로 비었다.** 2026-08-27에 `retrieval` 이벤트와
      쓰기 이벤트로 역산해봤으나 실측과 2.1배 어긋났다 (쓰기 하나가 호출 하나가 아니다).
      추측할 수 있는 값이 아니므로 그 자리에서 남긴다.

    mem0 0.1.118 은 `embed` 만, 2.0.18 은 `embed_batch` 도 있다. 있는 것만 감싼다.
    """

    def __init__(self, inner, tracer: TraceLogger):
        self._inner = inner
        self._tracer = tracer
        self._last = {}
        self._hook_client()

    def _hook_client(self):
        comp = getattr(getattr(self._inner, "client", None), "embeddings", None)
        if comp is None:
            return
        orig = comp.create

        def create(*args, **kwargs):
            resp = orig(*args, **kwargs)
            info = {"model": kwargs.get("model")}
            try:
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    info["prompt_tokens"] = getattr(usage, "prompt_tokens", None)
                    info["total_tokens"] = getattr(usage, "total_tokens", None)
                inp = kwargs.get("input")
                info["n_input"] = len(inp) if isinstance(inp, list) else 1
            except Exception:
                pass
            self._last = info
            return resp

        comp.create = create

    def _log(self, kind, memory_action, n_items, duration, sample):
        self._tracer.log("embed_call", purpose=memory_action or kind,
                         duration_ms=duration,
                         embed={"kind": kind, "n_items": n_items,
                                "text_sample": (sample or "")[:200], **self._last})

    def embed(self, text, memory_action=None, *a, **kw):
        start = time.time()
        self._last = {}
        out = self._inner.embed(text, memory_action, *a, **kw)
        self._log("embed", memory_action, 1, (time.time() - start) * 1000,
                  text if isinstance(text, str) else None)
        return out

    def embed_batch(self, texts, memory_action=None, *a, **kw):
        start = time.time()
        self._last = {}
        out = self._inner.embed_batch(texts, memory_action, *a, **kw)
        self._log("embed_batch", memory_action, len(texts or []), (time.time() - start) * 1000,
                  (texts or [None])[0] if texts else None)
        return out

    def __getattr__(self, name):
        return getattr(self._inner, name)


def attach_tracing(memory, tracer: TraceLogger):
    """메모리 객체의 **모든 외부 호출 지점**에 tracer 를 건다.

    ⚠ 새 메모리 시스템을 붙일 때도 이 함수를 쓴다. 호출부마다 직접 감싸면 하나씩 빠지고,
      빠진 것은 사후에 되살릴 방법이 없다 (임베딩이 실제로 그렇게 빠져 있었다).
    """
    if getattr(memory, "llm", None) is not None:
        memory.llm = TracingLLM(memory.llm, tracer)
    if getattr(memory, "embedding_model", None) is not None:
        memory.embedding_model = TracingEmbedder(memory.embedding_model, tracer)
    if getattr(memory, "vector_store", None) is not None:
        memory.vector_store = TracingVectorStore(memory.vector_store, tracer)
    return memory


class TracingVectorStore:
    """
    Mem0-Classic-OSS의 Vector Store Wrapper.
    insert / update / delete는 memory_write event로 (Runner가 Add 반환값으로 기록함) 커버되므로 위임만 함
    """

    def __init__(self, inner, tracer: TraceLogger):
        self._inner = inner
        self._tracer = tracer

    def search(self, *args, **kwargs):
        """인자를 그대로 넘긴다. **시그니처를 고정하면 안 된다.**

        ⚠ 검색 예산 인자 이름이 mem0 버전마다 다르다: classic 0.1.118 은 `limit`,
          2.0.18 은 `top_k`. 예전에는 여기서 `limit=5` 를 기본값으로 받아 무조건
          다시 넘겼는데, 그러면 v3 의 Qdrant.search 가 `limit` 을 모르는 인자로 보고
          TypeError 로 죽는다 (2026-08-26 BEAM v3 투입이 이걸로 전멸했음).
          그래서 그대로 통과시키고, 기록할 값만 있는 쪽에서 읽는다.
        """
        start = time.time()
        results = self._inner.search(*args, **kwargs)
        duration = (time.time() - start) * 1000

        def _hit(r):
            payload = getattr(r, "payload", None) or {}
            score = getattr(r, "score", None)
            return {"id": str(getattr(r, "id", "")),
                    "text": payload.get("data", ""),
                    "score": round(float(score), 4) if score is not None else None}

        hits = [_hit(r) for r in (results or [])]

        # 버전마다 다른 이름 중 있는 것을 쓴다. 위치 인자로 온 경우도 대비한다.
        budget = kwargs.get("limit", kwargs.get("top_k"))
        query = kwargs.get("query", args[0] if args else None)

        # retriever 종류를 trace에 남긴다 - BM25 레인과 임베딩 레인의 trace를 나중에 구분해야 한다
        method = "bm25" if type(self._inner).__name__ == "BM25Store" else "dense"
        self._tracer.log("retrieval", duration_ms=duration,
                         retrieval={"method": method, "query": query, "limit": budget, "hits": hits})
        return results
    
    def __getattr__(self, name):
        return getattr(self._inner, name)