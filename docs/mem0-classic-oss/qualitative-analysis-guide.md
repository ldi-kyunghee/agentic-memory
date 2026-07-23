# 정성분석 가이드 — mem0-classic-oss × HaluMem-Medium

> 목적: **에이전트 메모리의 강점 / 약점 / 병목 식별.**
> 정량 분석은 "실패가 어느 단계에서 얼마나 나는지"까지 밝혔다 (아래 §2). 정성분석의 몫은 그 **카테고리 안쪽의 패턴** — 어떤 종류의 정보가, 왜, 어떤 양상으로 실패하는가 — 를 사람 눈으로 찾는 것이다.
> 작성 2026-07-17. 배경 문서: [roadmap.md](roadmap.md) · [mem0-halumem-baseline.md](mem0-halumem-baseline.md) · [trace-schema.md](trace-schema.md)

## 1. 실험이 무엇이었나 (1분 요약)

- **벤치마크**: HaluMem-Medium (arXiv:2511.03506) — 가상 유저 20명, 유저당 세션 ~69개(총 30k 대화 턴), 골든 메모리 포인트 15k개, QA 3.5k개. 메모리 시스템을 추출/업데이트/QA **단계별로** 채점하는 벤치마크
- **시스템**: mem0 OSS 0.1.118 (classic CUD 파이프라인: LLM이 fact 추출 → 유사 메모리 검색 → ADD/UPDATE/DELETE 결정)
- **모델**: Qwen3-4B-Instruct-2507이 메모리 에이전트 LLM·답변 생성·judge를 전담, 임베딩은 Qwen3-Embedding-4B (전부 로컬 vLLM)
- **파이프라인**: 세션을 시간순 투입(Stage A) → 질문별 top-20 검색 context로 답변 생성(A') → LLM judge 채점(B). 3유저는 내부 동작 전체를 trace로 기록

## 2. 정량 분석이 이미 밝힌 것 (정성분석의 출발점)

3유저 인과 분석 집계 (임베딩 매칭 기준, 상세 baseline §4f):

- **Update Omission의 원인**: 추출 누락 49% / **결정 실패 39%** (old memory가 검색에 잡혔는데도 갱신 안 됨) / 검색 실패 11%
- **QA 오답의 원인**: 추출 누락 58% / **생성 실패 37%** (재료가 context에 다 있었는데 오답) / 검색 실패 4.5%
- **검색(retrieval)은 주 병목이 아님** — 병목은 저장(추출)과 갱신(결정)
- 시스템 내부 유실: update 결정의 1.85%가 id 환각으로 조용히 증발
- ⚠ judge가 4B라 라벨 신뢰도에 한계 있음 (모델별 판정 기질 편차 큼) → 라벨은 "사례를 찾는 필터"로 쓰고, 최종 판단은 사람이 할 것

## 3. 산출물 맵 — 무엇을 보면 되나

| 파일 | 내용 | 쓰임 |
|---|---|---|
| `results/mem0-classic-oss/memzero-oss-full/memzero-oss_eval_results.jsonl` | 유저 1명 = 1줄. 세션별: 대화 원문, 골든 메모리(`memory_points`), 시스템 추출 메모리(`extracted_memories`), 이벤트 원본(`memory_events`, prev_text 포함), update 검색 스냅샷(`memories_from_system`), 질문별 context+시스템 답변 | **모든 정성분석의 원본** (20유저) |
| `results/mem0-classic-oss/memzero-oss-full/judge-qwen4b/{uuid}.json` | 레코드별 judge 라벨: integrity 0/1/2, accuracy 0/1/2+is_included, update C/H/O, QA C/H/O | 사례 필터링 |
| `traces/mem0-classic-oss/full-traced/{uuid}.jsonl` | 내부 동작 전체(3유저): 추출 LLM 프롬프트/응답 전문, fact별 후보 검색 hits, update 결정 프롬프트/응답, probe/QA 검색 hits+score | "왜 그랬나"의 최종 증거 (스키마: trace-schema.md) |
| `reports/mem0-classic-oss/trace_analysis_*.json` | 원인 라벨이 붙은 사례 목록 (`cases` 배열: omission별 원인, QA 실패별 원인) | **각 분석 태스크의 시작 목록** — 여기서 사례를 골라 원본으로 들어가면 됨 |

## 4. 분석 태스크 (연구원별 분담 단위)

각 태스크: **(시작 데이터 → 하는 일 → 기록할 것)**. 공통 기록 양식은 §5.

### T1. 추출 실패 유형학 — 최대 병목의 해부 ★핵심

- 시작: `reports/*.json`의 `omission_linkage.cases`에서 `cause: extraction_miss` + QA `extraction_fault` 사례
- 하는 일: 해당 세션의 **대화 원문**과 골든 메모리, 시스템 추출 목록을 나란히 놓고 "이 정보는 왜 안 뽑혔나" 유형화. 가설 후보: 관계(3자) 정보인가? 시간·조건부 정보인가? 대화 후반부인가? 명시 발화가 아니라 암시인가? assistant 발화에만 있었나? 여러 fact가 한 문장에 뭉쳐 있었나?
- 기록: 실패 유형 태그(자유 명명 후 수렴), 대표 인용, 유형별 빈도 감각

### T2. Update 결정 실패 양상 — 숨은 공동 주범 (trace 3유저 한정)

- 시작: `cases`에서 `cause: decision_miss`
- 하는 일: trace에서 그 세션의 `llm_call(purpose=update_decision)` **프롬프트/응답 원문**을 읽고, old memory가 후보 목록에 있었는데 무슨 결정이 났는지 확인: NONE 처리? UPDATE 대신 중복 ADD? 엉뚱한 id 참조(환각)? 갱신했지만 핵심 필드 누락?
- 기록: 결정 실패 하위 유형, LLM 응답 인용

### T3. 생성 실패 + judge 검증 (이중 목적)

- 시작: `cases`에서 QA `generation_fault` (context에 재료가 전부 있었는데 오답 판정)
- 하는 일: context 원문 ↔ 시스템 답변 ↔ 골든 답변 3자 대조. 두 갈래로 갈림: (a) 진짜 생성 실패 — 시간 계산 실패? 다중 근거 종합 실패? 최신 정보 선택 실패? (b) **judge 오판** — 답이 사실상 맞는데 표현 차이로 오답 처리
- 기록: (a)/(b) 판별 + 하위 유형. (b)의 비율은 그 자체로 judge 신뢰도 데이터가 됨

### T4. Judge 라벨 스팟 검증 (전 카테고리)

- 시작: judge 파일에서 카테고리별(integrity/accuracy/update/QA) 무작위 20~30건씩
- 하는 일: 라벨 안 보고 사람이 먼저 판정 → 4B 라벨과 대조
- 기록: 사람↔judge 일치율, 불일치의 방향성. **비용 0의 human anchor** — 잠정 정량 지표의 신뢰구간을 정하는 근거가 됨

### T5. 질문 유형별 실패 패턴

- 시작: QA 오답을 질문 유형(`question_type`: Basic Fact Recall / Multi-hop / Dynamic Update / Memory Boundary / Memory Conflict / Generalization)별로 그룹핑
- 하는 일: 유형별 특징적 실패 양상 수집 (논문 Fig 5의 유형별 성능 차이에 대한 "왜"를 채우는 것)
- 기록: 유형 × 실패 양상 매트릭스

### T6. 강점 수집 — 실패만 보지 말 것

- 시작: judge `Correct` 중 난도 높은 사례 — Multi-hop 성공, Conflict에서 잘못된 전제를 정정한 답변, Boundary에서 정확히 기권한 답변, 여러 세션에 걸친 정보를 올바르게 갱신·회수한 체인
- 하는 일: "에이전트 메모리가 잘하는 것"의 구체 서사 수집 — 강점/약점 보고서의 균형추
- 기록: 성공 사례 + 성공 요인 추정

## 5. 공통 기록 양식 (공유 시트 권장)

| 필드 | 예 |
|---|---|
| 사례 ID | `user=2f1f..., session=12, mp_index=7` 또는 `question="..."` |
| 태스크 | T1~T6 |
| 관찰 | old memory가 후보 목록 3위에 있었으나 LLM이 NONE 판정 |
| 유형 태그 | `decision/none-판정`, `extraction/관계정보`, `judge/표현차-오판` 등 |
| 인용 | 대화/trace/답변에서 핵심 1-2줄 |
| 시사점 | 강점/약점/병목 중 무엇의 증거인지 한 줄 |

## 6. 표준 열람 절차 — 유저 단위, judge → tmp → trace 순

**분석 단위는 유저 1명.** 파생 파일이나 스크립트 없이 파일 3개를 정해진 순서로 직접 연다 (에디터의 JSON 포맷팅 + 접기면 충분).

**Step 1 — QA 실패 훑기: `judge/{uuid}.json` 하나로 완결**

`question_answering_records`를 스크롤하며 `result_type`이 Hallucination/Omission인 레코드에서 정지. **QA 레코드는 자기완결**이라 (question, answer=골든 정답, system_response, evidence, context, question_type이 모두 안에 있음) 다른 파일 없이 레코드 안에서 4자 대조한다:

1. 질문·골든 정답 확인 → 2. 시스템 답변 대조 → 3. evidence(근거 골든) 확인 → 4. **context에서 evidence가 있는지, 있으면 몇 번째인지, 오답의 출처가 된 다른 메모리는 무엇인지** 관찰

⚠ `tmp/{uuid}.json`의 questions에는 `system_response`가 **없다** (답변은 병합 jsonl에만 기록됨) — QA 분석은 반드시 judge 파일에서 할 것.

**Step 2 — 의심 메모리의 정체 확인: `tmp/{uuid}.json`** (필요할 때만)

context의 수상한 메모리(오답 출처 등)를 해당 세션에서 추적: `memory_points`에서 비슷한 골든을 찾아 **`memory_source` 확인** (`interference`면 미끼 흡수 사례), `dialogue`에서 원 발화 확인 (유저 발화 vs assistant 제안), `extracted_memories`와 골든 대조.

**Step 3 — 시스템 내부 결정 확인: `traces/.../{uuid}.jsonl`** (필요할 때만)

해당 `session` 번호로 검색해 `purpose: fact_extraction`(그 정보를 아예 안 뽑았나) / `update_decision`(뽑았는데 결정에서 버렸나) 응답 원문을 읽는다.

**Step 4 — update 실패 훑기** (QA 후): judge 파일의 `memory_update_records`에서 `Omission`을 골라 `original_memories`(옛 버전) ↔ `memories_from_system`(top-10 스냅샷) 비교 — 옛 버전이 스냅샷에 보이면 결정 실패, 안 보이면 Step 2·3으로 소급.

- **분배**: 연구원 1명당 유저 2~3명 (uuid 단위)
- 열람의 80%는 Step 1에서 끝난다. 원인 라벨(`reports/.../trace_analysis_full20.json`)은 참고용 — 오분류 발견도 기록 대상 (§7)
- 같은 유형 태그가 3번 반복되면 그것이 발견이다. 체계적 표집·자동 조인은 대시보드(기둥 3)의 몫

## 7. 주의사항

- 데이터는 전부 **가상 인물의 생성 대화** (개인정보 아님) — 다만 리포 밖 유출은 자제 (HaluMem 라이선스 CC-BY-NC-ND)
- 4B judge 라벨은 **필터이지 정답이 아님** — 이상하면 의심하고 T4에 기록
- trace는 3유저(`2f1f…`, `6106…`, `8ece…`)만 존재 — T2는 이 범위에서
- 원인 라벨(`extraction_miss` 등)도 임베딩 매칭(threshold 0.65) 기반 자동 분류라 경계 사례는 오분류 가능 — 발견 시 기록해주면 매처 개선에 반영됨
