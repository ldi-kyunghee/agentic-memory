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
                    extra["completion_tokens"] = getattr(usage, "completion_tokens", None)
                    details = getattr(usage, "completion_tokens_details", None)
                    rt = getattr(details, "reasoning_tokens", None)
                    if rt:
                        extra["reasoning_tokens"] = rt
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
        has_system = any(m.get("role") == "system" for m in messages)
        purpose = "fact_extraction" if has_system else "update_decision"

        llm = {"messages": messages, "response": response, **self._last_extra}
        self._tracer.log("llm_call", purpose=purpose, duration_ms=duration, llm=llm)
        return response
    
    def __getattr__(self, name):
        return getattr(self._inner, name)



class TracingVectorStore:
    """
    Mem0-Classic-OSS의 Vector Store Wrapper.
    insert / update / delete는 memory_write event로 (Runner가 Add 반환값으로 기록함) 커버되므로 위임만 함
    """

    def __init__(self, inner, tracer: TraceLogger):
        self._inner = inner
        self._tracer = tracer

    def search(self, query, vectors, limit=5, filters=None, **kwargs):
        start = time.time()
        results = self._inner.search(query=query, vectors=vectors, limit=limit, filters=filters, **kwargs)
        duration = (time.time() - start) * 1000
        hits = [
            {
                "id": str(r.id), 
                "text": (r.payload or {}).get("data", ""), 
                "score": round(float(r.score), 4)
            }
            for r in results
        ]
        
        # retriever 종류를 trace에 남긴다 — BM25 레인과 임베딩 레인의 trace를 나중에 구분해야 한다
        method = "bm25" if type(self._inner).__name__ == "BM25Store" else "dense"
        self._tracer.log("retrieval", duration_ms=duration, retrieval={"method": method, "query": query, "limit": limit, "hits": hits})
        return results
    
    def __getattr__(self, name):
        return getattr(self._inner, name)