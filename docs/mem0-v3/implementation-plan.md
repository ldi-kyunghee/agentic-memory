# mem0 최신판(v3 알고리즘) 재현 계획

`mem0ai` **2.0.18** 을 세 벤치마크(HaluMem · BEAM · Memora)에 얹어 돌리고, 지금까지 쌓은
classic OSS **0.1.118** 결과와 비교하기 위한 계획임.

**상태: 계획만 있음. 코드 없음.** 2026-08-24 작성.

---

## 0. 먼저 확인한 것: 소스가 정말 공개돼 있는가

이 프로젝트에서 이미 `add(..., timestamp=)` 가 Cloud 전용이라 못 쓴 전례가 있어
(`memora-experiment.md` §3) **추측하지 않고 릴리스 태그의 소스를 직접 읽었음.**

| 확인 항목 | 결과 | 근거 |
|---|---|---|
| 새 알고리즘이 OSS 저장소에 있는가 | **있음** | `mem0/memory/main.py` (main 브랜치) |
| 배포판에도 들어 있는가 | **있음** | 태그 `v2.0.18` 의 `mem0/utils/scoring.py` 가 존재하고 상수까지 열려 있음 |
| 호스티드 백엔드를 부르는가 | **아님** | 임베딩·검색·엔티티 추출 전부 로컬. LLM 공급자만 외부 |
| 라이선스 | Apache 2.0 | PyPI 메타데이터 |

읽어서 확인한 상수까지 열려 있음: `ENTITY_BOOST_WEIGHT = 0.5`, BM25 파라미터가 질의
길이에 따라 적응적임(3단어 이하 midpoint 5.0 / steepness 0.7 → 긴 질의는 midpoint 12.0,
steepness 하한 0.5).

**결론: 재현에 필요한 것이 전부 공개돼 있음.** 프롬프트·가중치·융합식까지 읽을 수 있음.

---

## 1. 무엇이 바뀌는가 (두 가지임)

사용자가 "add-only" 라고 부른 변경은 실제로 **두 축**임. 이걸 뭉뚱그리면 결과 귀속이 안 됨.

### 1-1. 투입: 2콜 → 1콜, ADD 만

| | classic 0.1.118 | v3 (2.0.18) |
|---|---|---|
| LLM 호출 | 2회 (`fact_extraction` → `update_decision`) | **1회 (추출만)** |
| 이벤트 | ADD / UPDATE / DELETE / NONE | **ADD 만** |
| 기존 메모리 조회 | 사실마다 `search(limit=5)` | 없음 |

classic 의 `_add_to_vector_store` 는 추출한 사실마다 기존 메모리를 검색해 붙인 뒤
`get_update_memory_messages(...)` 로 두 번째 LLM 을 불러 ADD/UPDATE/DELETE 를 고름.
v3 는 그 단계를 통째로 없애고 전부 ADD 함.

> ⚠ `infer=False` 는 add-only 가 **아님.** 추출까지 건너뛰고 원문 메시지를 그대로 저장함.
> classic 으로 add-only 를 흉내내려면 추출은 하고 갱신 결정만 건너뛰어야 함.

### 1-2. 검색: 단일 신호 → 3신호 융합

| | classic | v3 |
|---|---|---|
| 신호 | 임베딩 코사인 하나 | **임베딩 + BM25 + 엔티티 부스트** |
| 구현 | `vector_store.search()` | `search()` + `vector_store.keyword_search()` + `_compute_entity_boosts()` |

- BM25: 질의를 `lemmatize_for_bm25` 로 정규화 후 Qdrant 희소벡터 슬롯(`using="bm25"`)으로
  검색. 인코더는 fastembed
- 엔티티: `extract_entities` 가 spaCy NER + 정규식으로 PROPER/QUOTED/TOPIC/IDENTIFIER 를
  뽑고, 엔티티 스토어와 겹치는 메모리에 가중치를 얹음
- 융합: `score_and_rank(semantic, bm25_scores, entity_boosts, ...)`

**공식 문서가 주장하는 효과**: LoCoMo +20점, LongMemEval +26점, 추출 지연 약 절반.
우리 레인에서 재현되는지가 이 실험의 질문임.

---

## 2. 비교 설계: 사다리가 필요함

v3 를 그냥 classic 과 붙이면 **투입과 검색이 동시에 바뀌어 어느 쪽이 원인인지 못 가림.**
이 프로젝트가 계속 지켜온 원칙(한 번에 한 변인)에 어긋남.

| 팔 | 투입 | 검색 | 상태 |
|---|---|---|---|
| **A. classic** | 2콜 (ADD/UPDATE/DELETE) | 임베딩 | **이미 있음** (3벤치마크 전부) |
| **B. v3 전체** | 1콜 ADD-only | 3신호 융합 | 새로 돌림 |
| **C. 귀속용 프로브** | 1콜 ADD-only | 임베딩만 | Memora 만 (제일 쌈) |

C 는 v3 의 하이브리드 검색을 끌 수 있어야 성립함. **끌 수 있는지 확인이 필요함**
(§5 미확인 목록). 못 끄면 대안은 classic 에 add-only 훅을 다는 것임. 우리 오라클 훅
(`src/mem0-classic-oss/oracle.py`)과 같은 방식으로 `get_update_memory_messages` 결과를
"전부 ADD" 로 대체하면 됨. mem0 코드는 무수정 원칙을 지킴.

C 를 Memora 로 잡은 이유: 셋 중 제일 싸고(2.8h), **삭제·망각이 이 변경의 정면 표적**이라
효과가 제일 선명함.

---

## 3. 구현

### 3-1. 격리: 별도 uv 프로젝트

`mem0ai==0.1.118` 과 `2.0.18` 은 한 venv 에 공존할 수 없음. 그리고 classic 결과의
재현성을 깨면 안 됨.

```
eval/mem0-v3/
  pyproject.toml        # mem0ai==2.0.18 고정
  ingest_halumem.py     # 기존 하네스를 v3 API 로 얇게 감쌈
  ingest_beam.py
  ingest_memora.py
```

실행은 `uv run --project eval/mem0-v3 ...`. 대시보드가 이미 쓰는 방식임.

**답변·채점 스크립트는 건드리지 않음.** 그것들은 mem0 를 안 부르고 산출물만 읽음.
같은 스크립트를 쓰는 것이 레인 통제상 옳음.

### 3-2. 하네스 재사용 범위

세 ingest 스크립트가 mem0 를 만지는 지점은 `build_memory()` 하나임
(`eval/mem0-classic-oss/eval_memzero_oss.py:65`). 나머지는 데이터 로딩·저장임.

v3 에서 그대로 쓰이는지 확인해야 하는 API:

| 호출 | 쓰는 곳 |
|---|---|
| `Memory.from_config(config)` | 생성 |
| `.add(messages, user_id=, metadata=)` | 투입 |
| `.search(query, user_id=, limit=)` | 문항별 검색 |
| `.get_all(user_id=, limit=)` | 저장물 집계 |
| `.delete_all(user_id=)` · `.vector_store.delete_col()` | 정리 |

**config 스키마가 바뀌었을 가능성이 큼** (하이브리드 검색용 키, reranker, BM25 슬롯).
§4 의 1단계가 이것을 확인하는 스모크임.

### 3-3. trace 훅

`src/mem0-classic-oss/tracing.py` 의 `TracingLLM` · `TracingVectorStore` 는 mem0 내부
객체를 감쌈. v3 에서 내부 구조가 바뀌면 안 붙을 수 있음. **붙는지부터 확인하고, 안 붙으면
v3 팔은 trace 없이 돌림** (trace 는 진단용이고 점수와 무관함).

### 3-4. 분석 코드에서 깨지는 것

v3 는 **ADD 이벤트만** 내므로 지금 분석의 일부가 의미를 잃음. 버그가 아니라 결과임.

| 지표 | classic | v3 에서 | 처리 |
|---|---|---|---|
| Memora 삭제 발생비 (§6-4) | 96.9~107.8% | **0%** (구조적) | 표에 "해당 없음"으로 두고 v3 열을 따로 씀 |
| Memora FAA · FAMA 페널티 | 측정값 | 지워진 정보가 영원히 남음 | **이 실험의 핵심 지표.** 하이브리드 검색이 그걸 눌러주는지 |
| HaluMem update 평가 (C/H/O) | 무효 UPDATE 99.5% (§14) | UPDATE 자체가 없음 | **핵심 지표.** 재작성 없이 갱신을 맞출 수 있는가 |
| 검색 `score` | 코사인 | 융합 점수 | 버전 간 비교 금지. 화면 툴팁에 명시 |

---

## 4. 실행 순서

각 단계가 통과해야 다음으로 감. `long-run-checklist.md` §0 대로 축소 실행 먼저.

| 단계 | 내용 | 시간 |
|---|---|---|
| 1 | **API 호환 스모크.** `eval/mem0-v3` 만들고 페르소나 1개로 Memora 투입. config 스키마·5개 API·Qdrant BM25 슬롯·spaCy 모델을 한 번에 확인 | 1시간 |
| 2 | **Memora 전량 (팔 B).** 3기간. 삭제·망각 지표가 어떻게 바뀌는지 | 2.8h |
| 3 | **Memora 팔 C** (하이브리드 끄고 add-only 만). 귀속 | 2.8h |
| 4 | **HaluMem 전량.** update 평가가 핵심 | 5.1h |
| 5 | **BEAM 전량.** 제일 큼 | 4.7h |
| 6 | 문서·대시보드 반영 | |

**GPU 시간 합계 약 16시간** (팔 C 포함). 2장 기준, 우리만 쓸 때.

### 비용 근거

trace 1,387세션 전수 실측으로 투입 LLM 시간의 구성을 냈음.

| 호출 | 평균 | 비중 |
|---|---|---|
| `fact_extraction` | 2.63초 | 9.9% |
| `update_decision` | 24.03초 | **90.1%** |

**v3 는 90.1% 를 없앰 → 투입이 10.1배 빨라짐.**

| | 투입 단위 | 개수 | classic 투입 | v3 투입 | 답변 | 채점 |
|---|---|---|---|---|---|---|
| HaluMem | 세션 | 1,387 | 0.8h | **0.1h** | 3.1h | 1.9h |
| BEAM | 청크 | 59,210 | 20.4h | **2.0h** | 1.4h | 1.3h |
| Memora | 세션 | 27,614 | 12.3h | **1.2h** | 0.7h | 0.9h |

투입은 3.3시간뿐이고 **나머지 10시간이 답변·채점**임. 그쪽은 버전과 무관하게 듦.

> ⚠ 답변·채점 단가는 Memora 실측(문항당 41.6초, 항목당 4.9초)을 HaluMem·BEAM 에도
> 빌려 씀. BEAM 은 컨텍스트가 길어 더 느릴 수 있음. **±40% 로 봄 (11~22시간).**

---

## 5. 미확인 · 위험

**확인해야 할 것 (1단계 스모크에서)**

- v3 의 config 스키마. 하이브리드 검색을 **끌 수 있는지** (팔 C 가 여기 달림)
- `Memory.from_config` 이하 5개 API 시그니처
- Qdrant 컬렉션의 BM25 희소 슬롯 생성. 우리 Qdrant 버전이 지원하는지
- spaCy 모델 다운로드 (서버 최초 1회, 이후 오프라인)
- trace 훅이 붙는지

**위험**

- **fastembed BM25 인코더가 CPU 에서 돎.** BEAM 청크 59,210개 × 검색마다 인코딩이면
  새 병목이 될 수 있음. 1단계에서 검색 지연을 재둠
- **Qdrant 컬렉션 누적.** BEAM 때 180개에서 죽은 적 있음 (`beam-experiment.md` §8).
  v3 도 같은 방식으로 만드므로 정리 로직을 그대로 씀
- **v3 검색이 질의당 일을 더 함** (3신호). 문항별 검색이 느려짐. 투입에서 번 시간을
  일부 반납할 수 있음
- **비교 가능성.** 답변·채점 레인(gpt-oss-120b high)과 프롬프트를 그대로 써야 함.
  여기를 건드리면 classic 과 못 붙임

---

## 6. 이 실험이 답하는 질문

1. **갱신 결정을 없애도 되는가.** classic 의 무효 UPDATE 가 99.5% 였음
   (`backbone-experiment.md` §14). 그 단계가 비용의 90% 인데 그만큼 값어치를 했는지
2. **지우지 않고 잊을 수 있는가.** Memora 는 삭제된 정보를 언급하면 깎음. v3 는 절대
   안 지움. 하이브리드 검색과 시간 메타데이터로 그걸 눌러주는지
3. **검색을 고치는 것이 저장을 고치는 것보다 나은가.** 우리가 §7-6 에서 "규모 효과의
   75% 는 검색 예산이 아니다" 를 세웠음. v3 는 검색 방식 자체를 바꿈. 그 75% 를 건드리는지
4. **공식 주장이 우리 레인에서 재현되는가.** LoCoMo +20, LongMemEval +26 은 다른
   데이터셋·다른 모델의 수치임. 우리 세 벤치마크에서 얼마가 나오는지
