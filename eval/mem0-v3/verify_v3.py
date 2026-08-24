"""v3 가 **반쪽으로 돌고 있지 않은지** 확인함. 본실험 전에 반드시 통과시킴.

v3 의 하이브리드 검색은 세 신호(임베딩 · BM25 · 엔티티)를 융합함. 그런데 셋 다
**예외 없이 조용히 꺼짐**:

  - BM25 슬롯 없는 컬렉션 -> keyword_search 가 None
  - fastembed 없음        -> 인코딩 실패 -> None
  - spaCy 없음            -> extract_entities 가 [] (경고 로그만)

셋 다 꺼진 채로 돌리면 "v3 하이브리드를 돌렸다" 고 믿으면서 임베딩 단일 검색을 돌리게 되고,
그 결과는 "v3 가 별로였다" 로 읽힘. 그래서 **완주 여부가 아니라 신호 기여를 확인함.**

실행 (서버, 리포 루트에서):
    OPENAI_BASE_URL=http://localhost:8002/v1 \
    MEM0_LLM_MODEL=openai/gpt-oss-120b \
    MEM0_EMBED_BASE_URL=http://localhost:8001/v1 \
    MEM0_EMBED_MODEL=Qwen/Qwen3-Embedding-4B MEM0_EMBED_DIMS=2560 \
    QDRANT_HOST=localhost QDRANT_PORT=6333 OPENAI_API_KEY=dummy \
    uv run --project eval/mem0-v3 python eval/mem0-v3/verify_v3.py
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

FAILS = []


def check(name, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


# 엔티티가 확실히 잡히고 키워드도 뚜렷한 문장들. 세 신호를 다 자극하려고 고름
SEEDS = [
    "Martin Kim moved from Seoul to Kyoto in March and now works at Acme Corp.",
    "He switched from coffee to genmaicha tea after a doctor visit.",
    "The quarterly research proposal deadline is the 14th.",
    "His advisor is Professor Lindgren, who prefers Tuesday meetings.",
    "He is learning to play the shakuhachi flute on weekends.",
]
QUERY = "Where does Martin Kim work now, and who is his advisor?"


def main():
    from compat import build_memory, SEARCH_DEFAULTS

    print("=== 0. 환경 ===")
    for k in ("OPENAI_BASE_URL", "MEM0_LLM_MODEL", "MEM0_EMBED_BASE_URL",
              "MEM0_EMBED_MODEL", "MEM0_EMBED_DIMS", "QDRANT_HOST"):
        print(f"  {k:22s} {os.getenv(k)}")
    print(f"  search 기본값           {SEARCH_DEFAULTS}")

    import mem0
    print(f"  mem0                   {mem0.__version__}")
    check("mem0 가 2.x 임", mem0.__version__.startswith("2."), mem0.__version__)

    print("\n=== 1. 부속 모듈이 실제로 동작하는가 (설치만으로는 부족) ===")
    from mem0.utils.entity_extraction import extract_entities
    from mem0.utils.lemmatization import lemmatize_for_bm25
    ents = extract_entities(QUERY)
    check("spaCy 엔티티 추출이 비어 있지 않음", len(ents) > 0, str(ents[:4]))
    lem = lemmatize_for_bm25(QUERY)
    check("lemmatization 이 원문과 다름 (spaCy 로드됨)", lem != QUERY.lower() and lem != QUERY, repr(lem))
    try:
        import fastembed  # noqa: F401
        check("fastembed 설치됨", True)
    except ImportError:
        check("fastembed 설치됨", False, "BM25 인코딩이 조용히 죽음")

    # ⚠ 공유 서버 함정. fastembed 기본 캐시가 $TMPDIR/fastembed_cache 라 다른 사용자가
    #    먼저 만들어 두면 우리 프로세스가 못 읽음 (실측: /tmp/fastembed_cache 가 dania
    #    소유, 내부 파일이 0600). "Ignoring corrupted tree cache" 를 뿜으며 매번
    #    우회하거나 재다운로드함. FASTEMBED_CACHE_PATH 로 우리 경로를 못 박음.
    import tempfile
    fe = os.getenv("FASTEMBED_CACHE_PATH")
    default_fe = os.path.join(tempfile.gettempdir(), "fastembed_cache")
    check("FASTEMBED_CACHE_PATH 가 지정됨 (공유 /tmp 회피)", bool(fe), fe or f"미지정 -> {default_fe}")
    if fe:
        try:
            os.makedirs(fe, exist_ok=True)
            probe = os.path.join(fe, ".write_probe")
            with open(probe, "w") as f:
                f.write("x")
            os.remove(probe)
            check("캐시 경로에 쓰기 가능", True, fe)
        except Exception as e:
            check("캐시 경로에 쓰기 가능", False, f"{type(e).__name__}: {e}")
    else:
        owner_ok = not os.path.exists(default_fe) or os.access(default_fe, os.W_OK | os.R_OK)
        check("기본 캐시 경로를 읽고 쓸 수 있음", owner_ok,
              f"{default_fe} 가 남의 것이면 매 실행마다 재다운로드함")

    col = f"v3verify_{uuid.uuid4().hex[:8]}"
    uid = "verify_user"
    print(f"\n=== 2. 투입 (컬렉션 {col}) ===")
    mem = build_memory(col, f"/tmp/{col}.db")
    try:
        check("Qdrant BM25 희소 슬롯이 생성됨",
              getattr(mem.vector_store, "_has_bm25_slot", None) is True,
              f"_has_bm25_slot={getattr(mem.vector_store, '_has_bm25_slot', '속성 없음')}")

        events = []
        for i, s in enumerate(SEEDS):
            r = mem.add([{"role": "user", "content": s}], user_id=uid,
                        metadata={"session_date": f"2026-03-{i+1:02d}", "session_id": i})
            events += [x.get("event") for x in (r.get("results") or [])]
        print(f"  투입 이벤트: {events}")
        check("이벤트가 ADD 뿐임 (v3 는 UPDATE/DELETE 를 안 냄)",
              len(events) > 0 and set(events) <= {"ADD"}, str(set(events)))

        stored = mem.get_all(user_id=uid, limit=100000)
        n = len(stored.get("results", stored) if isinstance(stored, dict) else stored)
        print(f"  저장된 메모리 {n}개")
        check("get_all 이 20 을 넘겨 셀 수 있음 (top_k 번역 확인)", n != 20 or len(SEEDS) >= 20,
              f"n={n} (기본 20 에 걸리면 의심)")
        check("메모리가 실제로 쌓임", n > 0)

        print("\n=== 3. 검색: 세 신호가 모두 기여하는가 ===")
        ks = mem.vector_store.keyword_search(query=lem, top_k=5)
        check("keyword_search 가 None 이 아님 (BM25 살아 있음)", ks is not None,
              f"{type(ks).__name__}, {len(ks) if ks else 0}건")

        found = mem.search(QUERY, user_id=uid, limit=5, explain=True)
        res = found.get("results", found) if isinstance(found, dict) else found
        print(f"  검색 결과 {len(res)}건")
        check("검색 결과가 있음", len(res) > 0)

        # 키 이름은 추측하지 않음. mem0.utils.scoring.score_and_rank 를 직접 돌려 확인한
        # 실제 키임: semantic_score · bm25_score · entity_boost · raw_score ·
        #            max_possible_score · final_score · threshold
        SIGNALS = ("semantic_score", "bm25_score", "entity_boost")
        details = [r.get("score_details") for r in res if r.get("score_details")]
        print(f"  score_details 예시: {details[0] if details else None}")
        check("score_details 가 붙어 나옴 (explain 동작)", bool(details))

        if details:
            missing = [k for k in SIGNALS if k not in details[0]]
            check("세 신호 키가 모두 존재함", not missing, f"없는 키 {missing}" if missing else "")
            for key in SIGNALS:
                n = sum(1 for d in details if (d.get(key) or 0) > 0)
                # max_possible_score 로도 교차 확인함. BM25/엔티티가 아예 꺼져 있으면
                # score_and_rank 가 그 신호를 분모에서 빼므로 값이 1.0 씩 작아짐
                check(f"{key} 가 0 이 아닌 기여", n > 0, f"{n}/{len(details)}건")
            mps = details[0].get("max_possible_score")
            check("max_possible_score 가 3신호 기준(2.5)임",
                  mps is not None and abs(mps - 2.5) < 1e-6,
                  f"max_possible_score={mps} (1.0=의미만 · 2.0=2신호 · 2.5=3신호)")

        print("\n=== 4. top_k 번역 (cutoff 실험의 전제) ===")
        big = mem.search(QUERY, user_id=uid, limit=3)
        nb = len(big.get("results", big) if isinstance(big, dict) else big)
        check("limit=3 이 실제로 3 이하로 나옴 (기본 20 이 아님)", nb <= 3, f"{nb}건")

        print("\n=== 5. OSS 에서 막힌 인자 (넘기면 안 됨) ===")
        try:
            mem.add([{"role": "user", "content": "x"}], user_id=uid, timestamp=1)
            check("add(timestamp=) 는 막혀 있어야 함", False, "예외가 안 남")
        except ValueError:
            check("add(timestamp=) 가 막혀 있음 (예상대로)", True)
    finally:
        try:
            mem.vector_store.delete_col()
            print(f"\n  컬렉션 {col} 정리함")
        except Exception as e:
            print(f"\n  ⚠ 컬렉션 정리 실패: {e}")

    print()
    if FAILS:
        print(f"✗ 실패 {len(FAILS)}건: {FAILS}")
        print("  이 상태로 본실험을 돌리면 v3 를 반쪽으로 재는 것임. 고치고 다시 돌림.")
        return 1
    print("✓ 전부 통과. v3 세 신호가 모두 살아 있음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
