# 우리 재현 대 mem0 클라우드 플랫폼: 무엇이 같고 무엇이 다른가

(2026-08-28 코드 리뷰. 대상: `eval/mem0-classic-oss/` 의 classic 레인,
`eval/mem0-v3/compat.py` 의 v3 레인, docs.mem0.ai 플랫폼 문서)

## 0. 결론 한 줄

**우리 재현 = OSS 패키지의 알고리즘 코어를 공식 코드 그대로 + 모델·임베더만 우리 레인으로
교체.** 클라우드 플랫폼과는 같지 않음 — 플랫폼은 같은 코어 위에 **비공개 최적화와 관리형
기능 층**을 더 얹은 것이고, mem0 스스로 OSS 사용자에게 "방향은 비슷하되 수치는 다를 것"
이라고 말함. 플랫폼 내부가 비공개라 "정확히 동일한지"는 원리상 검증 불가능하고,
**검증 가능한 것(OSS 코어와의 일치)은 전부 검증했음.**

## 1. 층위를 나눠야 정확함

### 층위 1 — 알고리즘 코어: OSS 와 동일 (무수정)

| | classic 레인 | v3 레인 |
|---|---|---|
| 패키지 | `mem0ai==0.1.118` | `mem0ai[nlp]==2.0.18` |
| 부르는 API | `Memory.from_config` / `.add` / `.search` 공개 API 만 | 동일 (`compat.py` 는 시그니처 번역만) |
| 투입 | **2콜**: 사실 추출 → ADD/UPDATE/DELETE/NONE 결정 | **1콜**: ADD-only 추출 |
| 검색 | 임베딩 단일 신호 | 의미 + BM25 + 엔티티 융합 (`score_details` 최대 2.5 확인) |
| 프롬프트 | 패키지 내장 그대로 (`custom_fact_extraction_prompt` 미사용) | 패키지 내장 그대로 |

알고리즘·프롬프트·융합식·가중치를 우리가 다시 짠 것이 없음. `compat.py` 가 하는 일은
classic 하네스의 `search(q, user_id=, limit=)` 호출을 v3 의
`search(q, filters=, top_k=)` 로 번역하는 것뿐임 (인자 이름이 버전 간에 바뀌었기 때문).

### 층위 2 — 우리가 갈아끼운 변인 (의도적 · 레인 통제)

| 변인 | 공식/플랫폼 | 우리 | 왜 |
|---|---|---|---|
| LLM | OpenAI 모델 (플랫폼은 선택 불가) | gpt-oss-120b (vLLM 8002) | 세 시스템 공통 레인 |
| LLM 파라미터 | (비공개) | temperature 0.0 · max_tokens 16384 · effort 미설정(medium) | |
| 임베더 | text-embedding-3-small (1536) | Qwen3-Embedding-4B (2560) | 공통 레인 |
| 벡터 저장소 | 플랫폼 관리형 | 로컬 Qdrant + 워커별 SQLite history | 재현성·병렬 |
| reasoning_effort | 해당 없음 | mem0 가 gpt-oss 에 effort 를 못 넘겨 클라이언트 훅으로 주입 가능하게 함 — **기본 미설정** | mem0 는 gpt-5/o1/o3 이름만 인식 |

→ 이 층위 때문에 **mem0 공식 발표 수치(LoCoMo +20 등)와의 절대 비교는 성립하지 않음.**
우리 결론은 전부 같은 레인 안의 상대 비교임 (`implementation-plan.md` §2-9 원칙).

### 층위 3 — 플랫폼에만 있는 것 (우리도 OSS 패키지도 없음)

mem0 문서가 플랫폼 전용으로 분류하는 기능들. **검색 랭킹이나 저장 내용을 바꾸는 것**과
운영 편의를 나눠 봐야 함.

**랭킹·내용에 영향 (플랫폼 수치가 OSS 와 달라질 수 있는 실제 원인):**
- **비공개 최적화** — 공식 스탠스가 "open-source users should expect directionally
  similar gains but not identical numbers" 임. 무엇인지는 비공개
- **Advanced retrieval**: rerank(2차 재정렬, +150~200ms, opt-in) · keyword_search ·
  filter_memories. v3 OSS 에도 rerank 인자는 있으나 reranker 를 따로 붙여야 하고
  우리는 off (classic 에 대응물이 없어 레인이 어긋남)
- **Memory decay**: 접근 이력 기반 점수 스케일링 0.3×~1.5× (opt-in, 프로젝트 단위)
- **Temporal reasoning / timestamp**: 플랫폼 `add(timestamp=)` 지원. **OSS 0.1.118 은
  ValueError** (우리가 실측으로 확인, plan §2-5) — 그래서 우리 레인은 세션 시각을
  metadata 로만 넣고, 추출 LLM 은 그것을 못 봄
- **criteria retrieval · custom categories · Dream(합성) · feedback · graph memory(유료)**

**운영 편의 (수치 무관):** async client · webhooks · memory export · multimodal ·
entity-scoped memory · audit log · 자동 스케일링

### 층위 4 — OSS 인데 조용히 꺼질 수 있는 것 (우리는 켜짐을 검증함)

v3 의 세 신호는 의존성이 빠지면 **에러 없이** 임베딩 단일 검색으로 퇴화함:
BM25 슬롯 없는 컬렉션 → `keyword_search=None` · fastembed 없음 → 동일 ·
spaCy 모델 없음 → 엔티티 부스트 0. `verify_v3.py` 가 `score_details` 의
`max_possible_score == 2.5` 로 세 신호 생존을 실측 확인함. **이 검증 없이 v3 를 돌리면
"v3 를 돌렸다" 고 믿으면서 다른 것을 돌리게 됨.**

## 2. 그래서 "정확히 동일한가" 에 대한 답

- **OSS 패키지와는**: 알고리즘 코어가 동일함 (공식 코드 무수정 호출 + 신호 생존 검증).
  다른 것은 모델·임베더·저장소이고 전부 의도적·문서화됨.
- **클라우드 플랫폼과는**: 동일하지 않고, 동일한지 검증할 수도 없음. 코어는 같은 계열이지만
  플랫폼은 비공개 최적화 + rerank/decay/temporal 등 랭킹에 영향 주는 층이 더 있음.
  mem0 스스로 수치가 다를 것이라고 말함.
- 우리 결론의 유효 범위: **"mem0 알고리즘(0.1.118/2.0.18)의 설계가 우리 레인에서 어떻게
  행동하는가"** 이지 "mem0 클라우드 서비스의 품질" 이 아님. 문서·대시보드에서 시스템
  이름을 "mem0 classic (0.1.118)" / "mem0 v3 (2.0.18)" 로 버전 명기하는 이유임.

## 3. 참고 출처

- docs.mem0.ai 플랫폼 문서 (platform/features/* — advanced-retrieval, memory-decay,
  timestamp, temporal-reasoning 등 플랫폼 전용 목록)
- `docs/mem0-v3/implementation-plan.md` §2-5(구현 가능성 실측) · §2-9(충실성)
- `eval/mem0-classic-oss/eval_memzero_oss.py` build_memory · `eval/mem0-v3/compat.py`
