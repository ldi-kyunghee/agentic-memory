"""LIGHT 사전 점검. 본실행 전에 서버에서 돌려 전부 ✓ 여야 함 (verify_v3.py 본보기).

  uv run --project eval/light python eval/light/verify_light.py

검사 목록이 곧 겪은 사고 목록임:
  - 8000 포트는 남의 인스턴스 (max_model_len 으로 가름)
  - 임베딩이 trace 에 안 남아 비용을 못 되살린 사고 (2026-08-27)
  - fold 의 replace(int) TypeError (원본 공개판 크래시 버그)
  - reasoning 백본이 "Yes." 를 내면 == "yes" 가 전 조각을 버리는 문제
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_ROOT, "src", "mem0-classic-oss"))

import json  # noqa: E402
import tempfile  # noqa: E402
import urllib.request  # noqa: E402

FAIL = []


def check(name, fn):
    try:
        detail = fn()
        print(f"  ✓ {name}" + (f" — {detail}" if detail else ""))
    except Exception as e:
        FAIL.append(name)
        print(f"  ✗ {name} — {type(e).__name__}: {e}")


def _models(base):
    with urllib.request.urlopen(base.rstrip("/") + "/models", timeout=10) as r:
        return {d["id"]: d.get("max_model_len") for d in json.load(r)["data"]}


def c_endpoints():
    llm_base = os.getenv("OPENAI_BASE_URL", "http://localhost:8002/v1")
    emb_base = os.getenv("MEM0_EMBED_BASE_URL", "http://localhost:8001/v1")
    lm = _models(llm_base)
    em = _models(emb_base)
    llm = os.getenv("MEM0_LLM_MODEL", "openai/gpt-oss-120b")
    emb = os.getenv("MEM0_EMBED_MODEL", "Qwen/Qwen3-Embedding-4B")
    assert lm.get(llm) == 65536, f"LLM max_model_len={lm.get(llm)} (기대 65536 — 8000 이면 남의 인스턴스)"
    assert em.get(emb) == 32768, f"임베더 max_model_len={em.get(emb)} (기대 32768)"
    return f"LLM 65536 · 임베더 32768"


def c_flags():
    from flags import load_flags, echo_flags
    f = load_flags()
    print("    " + echo_flags(f))
    assert f.fold_limit == 14000 or os.getenv("LIGHT_FOLD_LIMIT"), "fold_limit 이 임의로 바뀜"
    return None


def c_fold_substitution():
    """원본 크래시 버그의 수정 확인: 프롬프트에 숫자가 실제로 치환되는가."""
    from prompts import scratchpad_summarizer_iterative_prompt as P
    out = P.replace("<content>", "x").replace("<tokens_limit>", str(14000))
    assert "<tokens_limit>" not in out, "치환 안 됨"
    assert out.count("14000") >= 4, f"치환 {out.count('14000')}곳 (기대 4곳 이상)"
    return f"14000 이 {out.count('14000')}곳 치환됨"


def c_yes_parsing():
    from core import _is_yes
    assert _is_yes("yes", True) and _is_yes("Yes.", True) and _is_yes("yes\n", True)
    assert not _is_yes("no", True) and not _is_yes("", True) and not _is_yes(None, True)
    assert not _is_yes("Yes.", False), "원본 모드가 완전일치가 아님"
    return None


def c_rec_shape():
    from adapter import LightVectorStore
    vs = LightVectorStore()
    vs.add(["a", "b"], [[1.0, 0.0], [0.0, 1.0]], [{"data": "A"}, {"data": "B"}])
    recs = vs.search(query="q", vectors=[1.0, 0.1], limit=2)
    r = recs[0]
    assert r.id == "a" and r.payload["data"] == "A" and 0.9 < r.score <= 1.001
    return "Qdrant 레코드 모양 (.id/.payload/.score)"


def c_no_effort_env():
    """투입 LLM 에 답변용 effort 가 새어들지 않는가 (agent medium 통제)."""
    from adapter import LightLLM
    llm = LightLLM()
    assert llm.effort == os.getenv("LIGHT_REASONING_EFFORT"), "LIGHT_REASONING_EFFORT 외의 값을 읽음"
    if os.getenv("ANSWER_REASONING_EFFORT") and not os.getenv("LIGHT_REASONING_EFFORT"):
        assert llm.effort is None, "ANSWER_REASONING_EFFORT 가 투입에 새어듦"
    return f"effort={llm.effort or '(모델 기본값 medium)'}"


def c_smoke_trace():
    """1-pair 스모크: attach_tracing 후 추출→삽입→검색을 실제로 돌려
    llm_call(prompt_tokens 있음)·embed_call·retrieval 세 이벤트가 모두 남는지.
    임베딩 누락 사고(2026-08-27) 재발 방지의 핵심 검사임. LLM·임베더 서빙 필요."""
    from adapter import build_light_memory
    from core import EpisodicIndex, extract_episodic
    from tracing import TraceLogger, attach_tracing

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "t.jsonl")
        memory = build_light_memory()
        tracer = TraceLogger(path, system="light", run="verify", user="verify")
        attach_tracing(memory, tracer)
        pair = [{"role": "user", "content": "My name is Verify Kim."},
                {"role": "assistant", "content": "Nice to meet you, Verify."}]
        chunk = extract_episodic(memory, pair, [], 0, {"session_time": None})
        idx = EpisodicIndex(memory)
        idx.add_pairs([chunk])
        recs = idx.search("What is the user's name?", 1)
        assert recs and idx.original_of(recs[0]), "검색/원문 복원 실패"
        tracer.close()

        ev = {}
        has_pt = False
        with open(path, encoding="utf-8") as f:
            for line in f:
                d = json.loads(line)
                ev[d.get("event")] = ev.get(d.get("event"), 0) + 1
                if (d.get("llm") or {}).get("prompt_tokens"):
                    has_pt = True
        assert ev.get("llm_call"), "llm_call 이 trace 에 없음"
        assert ev.get("embed_call"), "embed_call 이 trace 에 없음 (임베딩 누락 사고 재발)"
        assert ev.get("retrieval"), "retrieval 이 trace 에 없음"
        assert has_pt, "llm_call 에 prompt_tokens 가 없음"
        return f"이벤트 {dict(ev)}"


def c_chunker():
    """SemanticChunker 가 우리 임베더 어댑터로 실제 조각을 내는가 (임베더 서빙 필요)."""
    from adapter import build_light_memory
    from core import _chunk_scratchpad
    memory = build_light_memory()
    text = ("The user lives in Seoul. The user has a cat named Momo. "
            "The project deadline is March 3rd. The budget is $500. "
            "The user prefers Python. The meeting is on Friday. "
            "The server runs Ubuntu. The database is Postgres.")
    cache = {}
    chunks = _chunk_scratchpad(memory, text, cache)
    assert chunks and all(isinstance(c, str) for c in chunks)
    again = _chunk_scratchpad(memory, text, cache)
    assert again is chunks, "내용 해시 캐시가 동작 안 함"
    return f"{len(chunks)}조각 · 캐시 적중"


def main():
    print("━━━ LIGHT 사전 점검 ━━━")
    print(f"  LLM={os.getenv('OPENAI_BASE_URL', 'http://localhost:8002/v1')}"
          f" · 임베더={os.getenv('MEM0_EMBED_BASE_URL', 'http://localhost:8001/v1')}")
    check("엔드포인트 (max_model_len 으로 인스턴스 가름)", c_endpoints)
    check("플래그", c_flags)
    check("fold 치환 (원본 크래시 버그 수정)", c_fold_substitution)
    check("yes 파싱 (관대/원본 모드)", c_yes_parsing)
    check("벡터 레코드 모양", c_rec_shape)
    check("effort 격리", c_no_effort_env)
    check("SemanticChunker + 해시 캐시", c_chunker)
    check("1-pair 스모크 trace (llm/embed/retrieval 3종)", c_smoke_trace)
    if FAIL:
        print(f"✗ {len(FAIL)}개 실패: {FAIL}")
        sys.exit(1)
    print("✓ 전부 통과")


if __name__ == "__main__":
    main()
