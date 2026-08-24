# mem0 최신판(v3 알고리즘) 평가

`mem0ai[nlp]==2.0.18` 을 쓰는 **별도 uv 프로젝트**임. classic `0.1.118` 과 한 venv 에
못 살아서 분리했음. 계획·근거는 `docs/mem0-v3/implementation-plan.md`.

| 파일 | 하는 일 |
|---|---|
| `compat.py` | v3 `Memory` 를 classic 호출 규약으로 감싸는 어댑터. **하네스를 복사하지 않기 위한 것** |
| `test_compat.py` | 어댑터 단위 검사. 서버·GPU 없이 돎 |
| `verify_v3.py` | 서버에 붙여 **세 신호가 살아 있는지** 확인. 본실험 전 필수 |

## 왜 어댑터인가

세 벤치마크 ingest 하네스를 복사하면 A(classic)와 B(v3)의 로직이 갈라짐. 호출 규약만
번역하면 **같은 하네스가 두 팔을 다 돎.** 알고리즘은 한 줄도 안 짜고 mem0 소스도 안 고침.

바뀐 곳은 둘뿐임.

```
classic  memory.search(q, user_id=u, limit=k)     v3  search(q, filters={"user_id":u}, top_k=k)
classic  memory.get_all(user_id=u, limit=n)       v3  get_all(filters={"user_id":u}, top_k=n)
```

⚠ `search` 는 `user_id` 를 거부해서 예외가 남(안전). **`limit` 은 거부되지 않고 무시되어
기본 `top_k=20` 이 쓰임.** `get_all` 도 같음. 안 고치면 20개만 검색하고 저장물을 20개로 셈.

## 파이썬 3.12 고정

`.python-version` 과 `requires-python = ">=3.12,<3.14"` 로 못 박았음.
**spaCy 3.8.x 는 cp312 / cp313 휠만 있어 3.14 에서 설치가 실패함.** 서버 기본 파이썬이
3.14 라 막지 않으면 uv 가 3.14 를 골라 죽음. classic 쪽은 spaCy 를 안 써서 3.14 로도
돌아가므로 이 프로젝트만 다름.

## ⚠ 공유 서버: `FASTEMBED_CACHE_PATH` 를 반드시 지정

fastembed 기본 캐시가 `$TMPDIR/fastembed_cache` 임. Hamster 에서는 그 경로가 이미
**다른 사용자(dania) 소유**이고 내부 파일이 0600 이라 우리가 못 읽음. 지정하지 않으면
매 실행마다 `Ignoring corrupted tree cache ... Permission denied` 를 뿜으며 우회함.
13시간짜리 실행에서는 재다운로드 반복이나 실패로 번질 수 있음.

```
FASTEMBED_CACHE_PATH=~/projects/agentic-memory/.cache/fastembed
```

## 순서

```bash
# 1. 어댑터 검사 (로컬, 서버 불필요)
uv run --project eval/mem0-v3 python eval/mem0-v3/test_compat.py

# 2. 신호 생존 확인 (서버)
uv run --project eval/mem0-v3 python eval/mem0-v3/verify_v3.py
```

2 가 통과해야 본실험을 시작함. **"완주" 로 통과시키지 않음** — v3 의 세 신호는 전부
예외 없이 조용히 꺼지고, 꺼진 채로 돌리면 "v3 가 별로였다" 는 틀린 결론이 나옴.

`verify_v3.py` 가 보는 것: spaCy 엔티티 · lemmatization · fastembed · Qdrant BM25 슬롯 ·
`score_details` 의 `semantic_score`/`bm25_score`/`entity_boost` 기여 ·
`max_possible_score == 2.5`(3신호 기준) · `top_k` 번역 · ADD 이벤트만 나오는지.
