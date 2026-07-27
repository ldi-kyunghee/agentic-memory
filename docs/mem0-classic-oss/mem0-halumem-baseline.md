# mem0 OSS × HaluMem Baseline — 설계 노트

> Living document. 새 발견/결정은 이 문서를 업데이트한다 (문서 증식 금지).
> 마지막 업데이트: 2026-07-14 (Step 2 미니 실험까지 반영)

> Trace 스키마/추가 가이드는 [trace-schema.md](trace-schema.md) 참고 (별도 데이터 계약 문서).

## 1. 전략

논문 수치의 완전 재현은 비용상 포기 (judge만 GPT-4o ~3.5만 호출). 대신 **프로토콜 충실 재현**:

- 평가 프로토콜은 논문과 동일: 세션 시간순 투입, update top-10 / QA top-20 검색, judge 프롬프트 4종(서브모듈 `eval_tools.py`에서 import), 메트릭 정의
- 교체: mem0 platform → **OSS mem0 0.1.118** / GPT-4o → **로컬 vLLM** (`OPENAI_BASE_URL` 스위치로 OpenAI 옵션 유지)
- 결과는 논문 수치와 나란히 보고, 편차 요인 명시 (§6)
- judge 신뢰도 보정: 판정 레코드 ~200건 GPT-4o 교차 채점 (선택)

**목표 수치 (paper Table 3, Mem0 on Medium):**
R 42.91 / Weighted R 65.03 / Target P 86.26 / Acc 60.86 / FMR 56.80 / F1 57.31 · Update C 25.50, H 0.45, O 74.02 · QA C 53.02, H 19.17, O 27.81

## 2. 파이프라인 구성

원본 2단계 → 우리는 3단계 (QA 답변 생성을 분리해 배치화):

| Stage | 무엇 | 실행 형태 |
|---|---|---|
| A | mem0 메모리 연산: 세션 add → 추출 메모리·이벤트 수집, update MP top-10 검색, 질문 top-20 검색 → context 저장 | `eval/mem0-classic-oss/eval_memzero_oss.py`, vLLM **serve** (순차 의존이라 배치 불가) |
| A' | 저장된 context+question → 답변 일괄 생성 | vLLM **offline batch** (`gpu/mem0-classic-oss/` 프로젝트) |
| B | judge 4종(integrity/accuracy/update/QA) 일괄 채점 + 집계 | vLLM **offline batch** |

산출물은 원본 `memzero_eval_results.jsonl` 스키마와 호환 유지 (원본 `evaluation.py`로도 채점 가능하게) + `memory_events` 키 추가.

## 3. mem0 0.1.118 내부 구조 (소스 검증 완료)

`Memory._add_to_vector_store` (main.py:310-481), add 1회당:

1. **LLM #1 — fact 추출**: system=`FACT_RETRIEVAL_PROMPT`, `response_format=json_object` → `{"facts": [...]}`
2. **fact별 벡터검색**: 임베딩 → `vector_store.search(limit=5 하드코딩)` → 유사 기존 메모리 수집, UUID→정수 매핑(환각 방지)
3. **LLM #2 — update 결정**: `get_update_memory_messages(기존, facts)` → `{"memory": [{id, text, event: ADD|UPDATE|DELETE|NONE, old_memory}]}`
4. **이벤트 적용**: `_create/_update/_delete_memory`. 반환값에 event 포함, UPDATE는 `previous_memory` 포함

HaluMem 태스크 대응: LLM #1 = Extraction, LLM #2 = Updating, `search()` = Retrieval.

**트레이싱 지점** (포크/몽키패치 불필요, 인스턴스 속성 래핑으로 전부 커버):

- `memory.llm = TracingLLMWrapper(...)` → 추출/update 프롬프트·응답
- `memory.vector_store = TracingVectorStore(...)` → fact별 검색 후보·점수
- `add()` 반환값 → 최종 이벤트 (러너에서 원본 저장)

## 4. 실증된 사실 (Step 2 미니 실험, 2026-07-14)

- **timestamp**: OSS `add()`에 timestamp 파라미터 없음 → `metadata={"session_time": ...}`로 주입, 검색 결과 `r["metadata"]["session_time"]`으로 회수 확인. `created_at`은 실제 벽시계 시간이라 **사용 금지** (HaluMem 가상 타임라인과 무관)
- **search**: `user_id` kwarg 사용, `threshold=None` 기본 (점수 컷 없음 = 순수 top-k, 논문 프로토콜과 일치)
- **추출 특성**: OSS 기본 프롬프트는 self-contained 서술을 강제하지 않음 ("Gender is Male" 같은 무주어 fact). platform 실험의 custom_instructions와 다른 지점 — baseline 고유 특성으로 기록, custom prompt 주입은 ablation
- **텔레메트리**: `MEM0_TELEMETRY=False`로 비활성 (PostHog 외부 호출 제거). import 시점 경고는 남지만 무해

## 4b. 스모크 테스트 발견 (1 user, gpt-4o-mini, 2026-07-14)

- **mem0 기본 `max_tokens=2000` 함정 (치명)**: update 결정 LLM 응답(기존 메모리 전체 + facts별 이벤트 JSON)이 메모리 축적에 따라 길어져 ~2k 토큰에서 잘림 → json 파싱 실패를 mem0가 `{}`로 삼켜 **해당 세션 추출이 통째로 증발**. 1차 스모크에서 65세션 중 12개(18%)가 추출 0건. → llm config에 `max_tokens: 16384` 명시로 해결 (env `MEM0_LLM_MAX_TOKENS`)
- **UPDATE id 환각**: LLM #2가 존재하지 않는 메모리 id를 UPDATE 대상으로 참조하면 mem0가 해당 액션만 조용히 skip (`Error processing memory action ... KeyError`). gpt-4o-mini에서 클린 런 기준 65세션 중 2건. 모델 강도에 반비례할 것 — baseline 특성으로 기록, 서버 모델에서 빈도 재측정
- **클린 런 지표**: 이벤트 ADD 594 / UPDATE 111 / DELETE 16, 추출 705 vs 골든 718, 질문 164 전부 context 확보, update 스냅샷 142, add 23.2분/유저 (→ 20유저 순차 ~8h)
- 비용: 1유저 Stage A ≈ $0.1~0.15 (gpt-4o-mini)
- **텔레메트리 추가 발견**: `capture_event`가 호출마다 PostHog 클라이언트 신규 생성(스모크 ~370개) → atexit flush가 종료를 무기한 붙잡음. `MEM0_TELEMETRY=False`는 전송만 차단, 클라이언트 생성은 못 막음 → 러너에서 `mem0.memory.main.capture_event`를 no-op으로 패치 (버전 고정이라 안전)

## 4c. 스모크 성적표 (1 user, 전 단계 gpt-4o-mini, 공식 evaluation.py로 채점, 2026-07-15)

| | R | W-R | Target P | Acc | FMR | F1 | Upd C/H/O | QA C/H/O |
|---|---|---|---|---|---|---|---|---|
| smoke | 42.35 | 61.42 | 98.50 | 81.99 | 76.80 | 59.23 | 12.68 / 1.41 / 82.39 | 65.24 / 9.15 / 25.61 |
| paper | 42.91 | 65.03 | 86.26 | 60.86 | 56.80 | 57.31 | 25.50 / 0.45 / 74.02 | 53.02 / 19.17 / 27.81 |

- judge 유효율: integrity/accuracy/QA 100%, update 137/142 (mini의 형식 이탈 5건)
- 해석 (주의: 1유저 = 통계적 의미 없음, 파이프라인 sanity 확인 목적):
  - **R이 거의 일치** — OSS 기본 프롬프트의 추출 커버리지가 platform과 유사한 수준
  - **정밀도 계열(Target P/Acc/FMR)이 크게 높음** — 후보: ① mini judge의 관대함 ② OSS 프롬프트가 짧고 보수적인 fact 위주라 환각 여지 적음 ③ 1유저 분산. GPT-4o 교차 채점(calibration)으로 ①을 분리 진단 예정
  - **Update C 낮고 O 높음** — mini의 update 결정 품질(id 환각 포함) 영향 추정, 서버의 강한 모델에서 재측정
- 공식 evaluation.py를 심링크 2개(results 경로)로 무수정 재사용 성공 → **러너 산출물의 공식 스키마 호환 실증**
- 유저 병렬(2 workers) 검증 통과 (2026-07-15): 유저별 컬렉션+history db 격리 하에 409/락/오염 없음. Martin 추출 수 716 — 순차 런(705·741)과 동일 변동 범위 → 격리 실증. UPDATE id 환각 유실 ~13/142세션 (gpt-4o-mini)

## 4d-2. 서버 스모크 — Qwen3-4B 행동 지문 (1 user, 2026-07-16)

- 파이프라인 건강: 추출 0건 세션 0개, session_time 왕복 정상, 텍스트 품질 정상 (전 스택 vLLM: Qwen3-4B + Qwen3-Embedding-4B/2560)
- **모델별 메모리 관리 성향이 뚜렷이 다름**: 이벤트 ADD 505 / UPDATE 930 / DELETE 4, 추출 1,435 vs 골든 718 (≈2.0×) — mini(≈1.0×, UPDATE ~100)와 대조적인 **update-happy** 성향. UPDATE id 환각 21건 (시도 930 대비 실패율 ~2%)
- 풀런 해석용 가설: 밀도 2× → R↑ / Target P·Acc↓ 가능, update 시도 多 → Upd O 개선 가능. 모델별 성향 차이는 대시보드 비교 소재

## 4e. 풀런 결과 (20 users, 전 스택 Qwen3-4B + Qwen3-Embedding-4B, 2026-07-16)

| | R | W-R | Target P | Acc | FMR | F1 | Upd C/H/O | QA C/H/O |
|---|---|---|---|---|---|---|---|---|
| ours | 29.41 | 45.29 | 99.24 | 16.27 | 73.83 | 45.37 | 31.01 / 3.14 / 65.86 | 48.75 / 37.93 / 13.33 |
| paper | 42.91 | 65.03 | 86.26 | 60.86 | 56.80 | 57.31 | 25.50 / 0.45 / 74.02 | 53.02 / 19.17 / 27.81 |

- **Update C/O는 논문 대비 개선** (31.0/65.9 vs 25.5/74.0) — Qwen의 update-happy 성향(§4d-2)의 순기능 실증. 대가로 Upd H 3.1
- **Target P 99.2 vs Acc 16.3 괴리**: 골든 매칭 추출물은 정확하나 잉여 추출 대량이 0점 — 재작성 drift vs 판정자 엄격성 미분리
- **⚠ 해석 보류 항목**: 스모크(전부 mini) 대비 시스템·판정자가 동시에 바뀜 → Acc 폭락/QA H 급등/R 하락이 시스템 요인인지 judge 요인인지 분리 불가. **후속: gpt-4o-mini judge로 유저 슬라이스 재채점(calibration)** 후 확정 해석
- QA 프로파일 역전: 논문은 Omission형(27.8), 우리는 Hallucination형(37.9) — Qwen 답변 생성기가 기권 대신 추측하는 성향 (judge 요인 배제 후 확정)

### 4e-1. Judge calibration (3-user 슬라이스, Qwen judge vs gpt-4o-mini judge, 2026-07-16)

- **결론: Qwen3-4B는 judge 부적격** — 카테고리별 방향이 다른 체계적 편향: accuracy 일치율 56.8%에 극단적 엄격 (같은 레코드 Acc 17.2 vs 46.8, 0점 남발 1,701/4,659), update는 반대로 후함 (Correct 35% vs 9.7% → 풀런의 Upd C 개선은 judge 착시), QA는 Hallucination 남발 (41.6% vs 25.1%, Omission→Hallucination 재라벨 72건), integrity 다소 엄격 (R 28.1 vs 34.0)
- 시스템 요인도 실재: mini judge 기준으로도 Acc ~47 (mini-스모크의 82 대비) — Qwen backbone의 재작성 drift는 진짜 있음. 다만 judge가 과장
- **결정: 풀런을 gpt-4o-mini judge로 전체 재채점 후 수치 확정** (mini는 공식 채점기와 교차 검증된 기준 판정자). gpt-oss-120b 로컬 judge는 후속 ablation 후보
- 교훈: judge 모델은 시스템 backbone과 독립적으로 검증해야 하며, 4B급은 judge로 쓰기엔 판정 성향이 불안정

### 4e-2. Qwen3-30B-A3B judge calibration (같은 슬라이스, 2026-07-16)

- 30B는 mini에 수렴하지 않고 **제3의 기질**: integrity 관대 (R 65.0 vs mini 34.0, 일치 56.4%), update 극단적 관대 (Correct 57% vs 9.7%, 일치 43.0%), accuracy는 근접 (52.6 vs 46.8, 일치 67.9%)하나 is_included 89.5 vs 38.7
- **Upd C가 judge 선택에 따라 9.7↔35↔57%** — HaluMem judge 태스크의 rubric 해석 민감도가 매우 큼. mini 역시 GPT-4o(논문 judge)와의 일치율은 미측정 상태라 "기준"이라 부를 근거 불충분
- **[미결] judge 표준화 보류** (2026-07-16, 기능 구현 우선 결정): 풀런 수치는 "judge 미확정" 단서를 달고 잠정치로 유지. 내부 분석(linkage 등)은 무료인 30B 라벨 사용. 추후 확정 필요 시 GPT-4o 앵커 실험(층화 샘플 ~400건, ~$3)으로 mini vs 30B 중 논문 judge에 가까운 쪽 선택
- 서빙 참고 (Blackwell): MoE 모델(30B-A3B)은 FlashInfer CUTLASS MoE 백엔드가 sm120 JIT에서 사망 → TRITON MoE 백엔드 강제로 우회 (torch CUDA<12.9 문제의 연장). 혼재 GPU 서버는 `CUDA_DEVICE_ORDER=PCI_BUS_ID` 필수 (vs 공식 evaluation.py, 같은 gpt-4o-mini, 2026-07-15)

- 집계 지표 12개 중 9개 ±1.5%p 이내 (R/W-R/TargetP/F1/QA 전부). 초과 3개: Acc +4.96 / FMR −4.80 / Upd O +4.93
- **레코드 단위 조인 100%** (integrity 576/576, accuracy 705, update 142, QA 155) → build_inputs의 입력 조립이 공식과 완전 동일함을 실증
- 레코드 일치율: integrity 92.0% / accuracy 89.8% / update 90.8% / QA 96.8% — 동일 모델의 디코딩 모드 차이(json_object 강제 vs 자유생성+펜스) 수준
- **체계적 차이 1**: accuracy에서 json 모드가 관대한 방향으로 비대칭 (official 1점→ours 2점이 49건, 역방향 3건) → Acc +5%p의 원인. 판정 경계 사례들의 모드 민감성
- **체계적 차이 2 (발견)**: 공식 채점기는 update 판정에서 rubric 밖 라벨('Completely omitted', 'Partially omitted')을 뱉어 invalid 처리됨 (공식 런의 update valid 137/142가 이것). 우리 json 모드는 대부분 'Omission'으로 정규화 → **우리 쪽이 오히려 rubric 준수율 높음**
- 결론: judge.py = 공식 채점기의 충실한 강건판으로 채택. 이후 모든 비교는 judge.py 단일 채점기로 일관성 유지 (모드 차이는 판정자 정의에 흡수)

## 4f. Trace 인과 분석 결과 (full-traced 유저1, Qwen 스택, 4B judge 라벨, 2026-07-17)

`src/mem0-classic-oss/analyze_trace.py` (ⓐ 유실 정량화 / ⓑ omission 원인 / ⓒ QA 실패 전파). 매처: 임베딩 코사인(threshold 0.65) — **Jaccard는 패러프레이즈 실명으로 extraction_miss를 87%로 과대 산정** (스팟 체크로 적발) → 임베딩 매처를 표준 확정. 대시보드의 골든↔시스템 정렬도 임베딩 기반 필수.

**20유저 확정 집계** (traced full 20 users, 2026-07-17 — 3유저 예비 결과와 분포 일치, 대표성 확인):

- ⓐ 유실 액션: 766/30,793 (2.49%) — op별 분해: ADD 0/10,105 (0%) / UPDATE 760/20,512 (3.71%) / DELETE 6/176 (3.41%). id 참조가 필요한 연산(UPDATE/DELETE)만 동률로 유실되고 ADD는 정확히 0 — id 환각 메커니즘의 op 수준 검증. 취약 연산 한정 유실률 3.70%. 매처 무관 지표 (맥/서버 동일)
- ⓑ Omission 2,071건: **decision_miss 47.9% / extraction_miss 44.3%** / overwritten 7.1% / retrieval_miss 0.7% — 논문 §6.2.1("추출 누락이 주원인")의 정밀화: 추출 누락은 절반 이하이고, **최대 원인은 old memory가 검색에 잡혔는데도 update가 안 된 결정 실패**. overwritten 7.1%는 update-happy 성향이 골든 갱신 도착 전에 old를 먼저 재작성해 소실시키는 실재 현상 (분류기 v2: 복수 이전 버전 매칭 + overwritten 체크 매처 일관성 수정, 2026-07-17)
- ⓒ QA 실패 1,770건: extraction_fault 63.5% / generation_fault 30.1% / retrieval_fault 6.4% (분류기 v2에서 변동 없음 — 회귀 검증 겸함)
- 일관 결론: **retrieval은 병목이 아님** (omission 기준 0.7%) — 병목은 저장(추출) 44%와 갱신(결정) 48%, 그리고 과잉 재작성(overwritten) 7%
- **매처 표준: Qwen3-Embedding-4B + cosine 0.65 (서버)** — 임베딩 모델 교체 시 같은 threshold라도 결론 왜곡 (동일 유저에서 OpenAI 3-small 사용 시 extraction_miss 42→68). 원인 라벨은 측정 모델 종속 — 모든 분석은 이 표준 설정으로만
- 단서: 분모는 4B judge 라벨(판정 기질 영향). threshold 민감도 스윕은 외부 보고 시점에 GPT-4o 앵커와 묶어 수행 (백로그). 관찰자 효과 없음 확인: traced/untraced 풀런 성적표 차이 ±0.2%p

## 4g. 유형별 분해 (full-traced 20u, 4B judge — 상대 비교만 유효, 2026-07-20)

질문 유형별 C/H/O (%) — paper Fig 5 대응:

| 유형 | n | C | H | O |
|---|---|---|---|---|
| Memory Boundary | 828 | **88.6** | 11.4 | 0.0 |
| Memory Conflict | 769 | **61.6** | 27.7 | 10.7 |
| Basic Fact Recall | 746 | 34.9 | **52.0** | 13.1 |
| Generalization & Application | 746 | 19.6 | 51.9 | 28.6 |
| Multi-hop Inference | 198 | 18.7 | 60.1 | 21.2 |
| Dynamic Update | 180 | 25.6 | 57.8 | 16.7 |

- **강점**: 기권(Boundary)·전제 정정(Conflict) — 모르는 것을 지어내지 않고, 틀린 전제를 바로잡음
- **약점**: 기본 사실 회수조차 H 52% (추출 병목의 직접 발현 추정 — §4f의 extraction_fault 63.5%와 정합), 추론·갱신 계열 전멸
- 메모리 유형별 (paper Table 4 대응): Persona 21.8 > Event 16.1 ≈ Relationship 16.3 (integrity_acc) — "정적 인적사항 > 동적 사건·관계" 논문 발견 재현

## 4h. 논문 Table 3 확장판 — HaluMem-Medium (보고용)

논문 Table 3의 Medium 블록 전체 + 우리 행(traced full 20u) 추가. 괄호 = 추출 메모리 수.

| System | # Users | Agent & Generator | Judge | R↑ | Weighted R↑ | Acc.↑ (# mem) | Target P↑ (# mem) | FMR↑ | F1↑ | Upd C↑ | Upd H↓ | Upd O↓ | QA C↑ | QA H↓ | QA O↓ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ***Mem0-Classic*** | 20 | `GPT-4o` | `GPT-4o` | 42.91 | 65.03 | 60.86 (16,291) | 86.26 (10,556) | 56.80 | 57.31 | 25.50 | 0.45 | 74.02 | 53.02 | 19.17 | 27.81 |
| ***Mem0-Graph*** | 20 | `GPT-4o` | `GPT-4o` | 43.28 | 65.52 | **61.86 (16,230)** | 87.20 (10,567) | 55.70 | 57.85 | 24.50 | 0.26 | 75.24 | 54.66 | 19.28 | 26.06 |
| ***Memobase*** | 20 | `GPT-4o` | `GPT-4o` | 14.55 | 25.88 | 32.29 (17,081) | 92.24 (5,443) | **80.78** | 25.13 | 5.20 | 0.55 | 94.25 | 35.33 | 29.97 | 34.71 |
| ***MemOS*** | 20 | `GPT-4o` | `GPT-4o` | **74.07** | **84.81** | 59.55 (71,793) | 86.25 (45,190) | 44.94 | **79.70** | **62.11** | 0.42 | **37.48** | **67.23** | **15.17** | 17.59 |
| ***Supermemory*** | 20 | `GPT-4o` | `GPT-4o` | 41.53 | 64.76 | 60.83 (22,551) | 90.32 (14,134) | 51.77 | 56.90 | 16.37 | 1.15 | 82.47 | 54.07 | 22.24 | 23.69 |
| ***Zep*** | 20 | `GPT-4o` | `GPT-4o` | – | – | – | – | – | – | 47.28 | 0.42 | 52.31 | 55.47 | 21.92 | 22.62 |
| ***Mem0-Classic-oss*** | 20 | `Qwen3-4B` | `Qwen3-4B` | 29.57 | 45.51 | 16.52 (29,857) | **99.37 (3,885)** | 71.87 | 45.58 | 30.40 | 3.27 | 66.34 | 48.95 | 37.64 | 13.41 |
| ***Mem0-Classic-oss*** | 4 | `Qwen3-4B` | `Qwen3-4B` | 26.44 | 42.64 | 15.95 (6,065) | 99.14 (758) | 77.19 | 41.74 | 30.25 | 3.53 | 66.22 | 48.09 | 39.01 | 12.91 |
| ***Mem0-Classic-oss*** | 4 | `Qwen3-4B` | `GPT-5-Nano` | 26.65 | 49.38 | 30.12 (6,065) | 82.15 (1,583) | 63.16 | 40.25 | 8.57 | **0.17** | 88.91 | 42.84 | 29.08 | 27.94 |
| ***Mem0-oss+커스텀프롬프트***⁸ | 20 | `Qwen3-4B` | `Qwen3-4B` | 68.99 | 74.75 | 22.66 (9,878) | 96.32 (1,659) | 61.52 | 80.40 | 58.20 | 2.53 | 39.24 | 48.37 | 39.78 | 11.85 |
| ***Mem0-oss+커스텀프롬프트***⁸ | 4 | `Qwen3-4B` | `GPT-5-Nano` | 61.47 | 74.83 | 26.82 (1,734) | 90.14 (360) | 39.38 | 73.10 | 13.78 | 0.34 | 83.19 | 40.57 | 35.46 | 23.97 |
| ***Mem0-oss (nano 백본)***⁹ | 4 | `GPT-5-Nano`(agent)+`Qwen3-4B`(gen) | `GPT-5-Nano` | 22.62 | 45.84 | 42.37 (3,544) | 81.22 (1,411) | 60.23 | 35.39 | 17.14 | 0.50 | 79.66 | 49.79 | 22.84 | 27.38 |
| ***Mem0-oss (nano 백본)+커스텀***⁹ | 4 | `GPT-5-Nano`(agent)+`Qwen3-4B`(gen) | `GPT-5-Nano` | 34.82 | 44.76 | 33.98 (1,164) | 88.00 (300) | 60.82 | 49.90 | 11.76 | 0.00 | 85.71 | 39.29 | 29.22 | 31.21 |
| ***Mem0-oss (mini 백본)***⁹ | 4 | `GPT-5-Mini`(agent)+`Qwen3-4B`(gen) | `GPT-5-Nano` | 35.89 | 57.87 | 48.56 (1,950) | 83.73 (876) | 52.83 | 50.25 | 27.56 | 0.17 | 68.74 | 52.91 | 22.84 | 24.26 |
| ***Mem0-oss (mini 백본)+커스텀***⁹ | 4 | `GPT-5-Mini`(agent)+`Qwen3-4B`(gen) | `GPT-5-Nano` | 42.67 | 53.49 | 55.07 (493) | 94.14 (239) | 64.33 | 58.72 | 19.50 | 0.00 | 78.99 | 46.52 | 29.50 | 23.97 |
| ***BM25 (@10)*** | 20 | `Qwen3-4B` | `Qwen3-4B` | – | – | – | – | – | – | – | – | – | 61.14 | 28.49 | **10.35** |
| ***BM25 (@10)*** | 4 | `Qwen3-4B` | `GPT-5-Nano` | – | – | – | – | – | – | – | – | – | 미실행 | 미실행 | 미실행 |

각주 (비교 해석 시 필수):

1. **판정자 상이**: 논문 행들은 GPT-4o judge, 우리 행은 Qwen3-4B judge (미확정 — §4e-1: judge에 따라 Upd C 9.7↔35↔57% 변동 실증). **논문 행과의 직접 비교는 부적절**하며, 이 행의 용도는 동일 judge·동일 스택으로 잰 후속 시스템(BM25 naive, mem0 2.x 등)과의 내부 비교 기준선
2. 구현 상이: mem0 hosted platform(+custom_instructions) ↔ OSS 0.1.118 기본 프롬프트; embedder는 Qwen3-Embedding-4B; 답변 생성 Qwen3-4B (논문은 GPT-4o 통일)
3. 괄호 = 메모리 개수 (논문 범례와 동일). Acc. 옆 29,857은 우리 시스템이 추출한 전체 메모리 수로, 논문 Mem0(16,291)의 약 1.8배 — Qwen 백본이 메모리를 많이 만들고 자주 고치는 성향 때문. Target P 옆 3,885는 그중 judge가 "골든과 대응된다"고 인정한 개수로, 추출량이 2배인데도 논문(10,556)의 1/3에 그침 — 4B judge가 이 대응 판정을 유난히 짜게 주는 탓이 큼 (§4e-1 실측: 동일 레코드에서 mini judge 38.7% vs 4B judge 13.3% 인정). 두 괄호는 각각 시스템 성향과 judge 성향의 지표이니 액면 그대로 비교하지 말 것
4. 논문 수치는 arXiv:2511.03506 v3 Table 3 (Medium)에서 전사
5. nano 행: gpt-5-nano-2025-08-07 (`reasoning_effort=minimal`), 데이터셋 첫 4유저 (Martin/Johnson/Donald/Sarah), 9,739콜 ~12분, 무효율 0.16%. **4유저 두 행(4B vs nano)은 유저 구성이 동일하므로 그 격차가 순수 judge 효과** — 특히 Upd C 30.3→8.6, QA H 39.0→29.1은 4B judge 착시의 정정. 판정 기질이 gpt-4o-mini와 수렴함을 레코드 단위로 확인 (§4i)
6. 열 순서 주의: 이 표는 Acc.를 Target P 앞에 배치 (논문 Table 3와 반대). 굵은 값은 열별 최고치
7. BM25 행: 동료 실험(naive-mem-eval)의 top-10 검색 베이스라인 — 비에이전틱 검색이라 extraction/updating 지표 없음. **BM25 × GPT-5-Nano 행은 미실행** (BM25 산출물에 nano judge 재채점 필요 — 4유저 기준 QA 판정만이면 ~700콜, <$0.2 예상)
8. 커스텀 프롬프트 행 (2026-07-24): HaluMem 원본의 custom instructions를 OSS `custom_fact_extraction_prompt`로 이식한 런 — 추출 프롬프트 외 전 조건은 같은 judge의 default 행과 동일 (설계·상세 분석: [custom-prompt-experiment.md](custom-prompt-experiment.md)). 요지: R/F1 급등(추출 병목 해소), FMR 급락(문단이 distractor 흡수), Upd C 개선(8.57→13.78), QA 정체(병목이 검색·활용으로 이동). 표의 굵은 값은 이 행들 추가 전 기준이므로 열별 최고치 표시로 재해석하지 말 것
9. 백본 교체 행 (2026-07-24~26): memory agent LLM만 교체 (추출·update 결정 담당), 답변 생성은 Qwen3-4B 유지 — 백본(Qwen/nano/mini)×프롬프트(default/custom) 그리드 (설계·상세 분석: [backbone-experiment.md](backbone-experiment.md)). 요지: 능력 사다리 단조 개선 — default 레인 QA C 42.84→49.79→**52.91(mini, 현 1위)**, Upd C 8.57→17.14→**27.56(논문 GPT-4o 25.50 상회)**. drift는 Qwen 특이 행동 (관건은 재작성 빈도가 아니라 품질 — UPDATE-유래 Acc 20.4→40.3→42.8%). custom 프롬프트의 FMR 효과는 백본마다 부호가 다름 (Qwen 급락/nano 불변/mini 개선)

### 4h-부록. 지표 정의 (공유용)

**Memory Extraction**

- **R (Memory Recall)**: 기준 메모리 포인트별로 judge가 시스템 추출 목록과 대조해 {2: 완전 포함/논리적 함의, 1: 부분 포함, 0: 미포함/오류}로 채점, 2점 비율. 예: 골든 718개 중 2점 212개 → 29.5%
- **Weighted R**: 동일 채점을 0.5배 정규화(부분 포함 절반 인정)하고 중요도 wᵢ로 가중: Σ(0.5·sᵢ·wᵢ)/Σwᵢ
- **Acc. (Memory Accuracy)**: 전체 후보(추출) 메모리의 평균 정확도. judge가 후보를 원자 정보점 단위로 분해, 대화·골든 대비 지지 여부로 {2: 전부 지지, 1: 혼재, 0: 전부 비지지/모순} 채점 후 Σ(0.5·sⱼ)/N_extract. 괄호 = N_extract
- **Target P**: `is_included=true`(후보의 모든 원자 정보점이 골든에 대응 **필드**를 가짐 — 값·극성·수량 차이는 무시, 하나라도 필드 부재면 false)인 부분집합 한정 평균 정확도. 괄호 = N_target. Acc와의 격차 = 골든 범주 밖 잉여 추출의 규모
- **FMR**: 간섭 메모리(AI 발화, 사용자 미확정)가 integrity 채점 0점(=저장 안 됨)을 받은 비율 — 이 지표만 미포함이 목표 행동
- **F1**: 2·R·TargetP/(R+TargetP)

**Memory Updating** — 기준 갱신 쌍(m_old→m_new)마다 top-10 검색 스냅샷을 대조: **C** 갱신본 정확·완전 반영(핵심 필드 일치, m_old 대체) / **H** 관련 메모리 존재하나 내용이 정답 갱신과 불일치 / **O** 관련 신규 메모리 부재 또는 핵심 정보 결여

**Question Answering** — 질문별 top-20 검색→답변 생성, judge가 기준 정답·근거 메모리만으로 분류: **C** 의미적 동치·무모순 / **H** 모순·날조 포함 (누락과 공존 시 H 우선) / **O** 날조 없는 불완전·부당한 "모른다" (다요소 질문은 전 요소 필요)

공통: 모든 판정은 LLM judge 수행 — 절대값은 judge 종속, 비교는 동일 judge 조건에서만 유효 (실측: judge에 따라 Upd C 8.6~57%).

### 4i. gpt-5-nano judge (4유저, 2026-07-21)

- 동일 4유저 레코드에서 4B vs nano: integrity 일치 57.2% (R은 22.0 vs 21.6으로 우연히 근사), accuracy 63.5% (Acc 15.9 vs 30.1 — 4B의 과잉 엄격 완화), update 66.8% (**Correct 30.3% vs 8.6%** — 4B의 관대함 정정), QA 68.3% (H 42→31%, O 14→30% — H 남발 정정)
- **nano ≈ mini 수렴**: update Correct (8.6 vs 9.7%)와 QA 프로파일이 gpt-4o-mini 기질과 일치 — OpenAI 계열 판정 기질의 일관성 → nano를 표준 judge로 쓰는 실증 근거
- nano 기준 확정 해석: 시스템의 갱신 능력은 논문 Mem0보다 크게 열위 (Upd C 8.6 vs 25.5), Acc 30.1은 judge를 바꿔도 논문(60.9)의 절반 — **재작성 drift는 시스템 요인으로 최종 확인**
- 이후 4유저 정성분석은 `results/mem0-classic-oss/judge-gpt5nano-4u/judge/` 라벨 기준

**동일 4유저 3자 비교** (유저 구성 통제 — 두 "ours" 열의 차이는 순수 judge 효과):

| 지표 | 4B judge (4u) | nano judge (4u) | paper GPT-4o (Mem0, 20u) |
|---|---|---|---|
| R | 26.44 | 26.65 | 42.91 |
| Weighted R | 42.64 | 49.38 | 65.03 |
| Target P | 99.14 (758) | 82.15 (1,583) | 86.26 (10,556) |
| Acc | 15.95 (6,065) | 30.12 (6,065) | 60.86 (16,291) |
| FMR | 77.19 | 63.16 | 56.80 |
| F1 | 41.74 | 40.25 | 57.31 |
| Upd C/H/O | 30.25 / 3.53 / 66.22 | 8.57 / 0.17 / 88.91 | 25.50 / 0.45 / 74.02 |
| QA C/H/O | 48.09 / 39.01 / 12.91 | 42.84 / 29.08 / 27.94 | 53.02 / 19.17 / 27.81 |

- **판정 강건 지표**: R (26.44↔26.65), F1 — 집계값이 judge 교체에 불변 → paper 대비 격차는 시스템 요인으로 확정 (추출 커버리지 열위). 단 레코드 단위는 강건하지 않음 — 2점 경계에서 (1→2) 159건 vs (2→1) 192건이 상쇄된 결과
- **판정 민감 지표**: Target P(−17), Acc(+14), FMR(−14), Upd C(−22), QA H(−10)/O(+15) — 4B 착시의 방향과 크기가 정량화됨

**4B judge 착시의 해부 (동일 레코드 점수 전이)**: 공통 패턴은 "회색지대의 극단 라벨 붕괴" — ① integrity: (0→1) 309건 — 부분 포함을 미포함으로 이분화 → Weighted R만 +7pt 오른 이유 (R은 2점 개수 불변이라 정지) ② accuracy: 4B는 0점이 80%(4,869/6,056)였고 nano가 (0→1) 1,092 + **(0→2) 524건**을 복권 — 패러프레이즈 함의를 표면 어휘 불일치로 날조 취급한 것 ③ update: 주제 유사만으로 Correct 남발 — 4B Correct 180건 중 nano 생존 32건(18%), 144건이 Omission으로 강등 (육안 검증: 스냅샷에 갱신본 부재인데 Correct). H는 양 judge에서 소멸(nano 전체 1건) → **갱신 실패의 실체는 오갱신이 아니라 누락** — 구체값을 쓰지 않는 시스템은 틀릴 값도 없음 ④ QA: 모순 없는 불완전을 Hallucination으로 뭉뚱그림 — H→O 재배치 86건(일방향)이 본체, C↔H는 25 vs 21로 대칭 노이즈. 재료 부재 시의 "없다"형 단정/기권 응답군이 이 경계에 밀집 (extraction_fault 55~64%와 연결) ⑤ **Target P 99%는 자기참조**: 4B는 is_included(독립적 필드 판정이어야 함)를 자기 정확도 판정과 결합 — target 754개 중 741개가 자신이 2점 준 것 전부와 일치 → "만점짜리들의 평균"이 되어 정의상 ≈100. nano는 두 판정을 분리해 1점짜리 563개가 target에 공존 → 82.2%로 지표 본래 의미 회복. ⑥ FMR은 같은 편향의 거울상: 간섭 메모리의 부분 흡수(0→1 전이 93건/513)를 4B가 "저항 성공(0점)"으로 오판해 FMR을 +14pt 부풀림 — 부분 포함 인식 실패가 골든에선 WR을 깎고 간섭에선 FMR을 올리는 양방향 왜곡. nano 기준 미끼의 ~37%가 부분 이상 흡수됨 (interference-hijack 정성 사례와 정합). ⑦ F1의 judge 불변(41.7→40.3)은 조화평균의 성질: R≪P 레짐에서 F1은 작은 항(R)에 지배돼 P의 착시 +17pt가 F1엔 +1.5pt만 전달됐던 것. 따름정리 — **이 레짐에서 F1 개선은 R(추출 커버리지) 개선과 동치** (P를 논문 수준으로 올려도 F1 +0.5, R을 올리면 +16 → 논문과의 F1 격차는 사실상 전부 recall 격차).

**Acc의 유래별 분해 — drift의 주범은 추출이 아니라 재작성** (nano 4유저, `src/mem0-classic-oss/analyze_acc_by_origin.py`, 2026-07-21):

"후보 메모리"에는 신규 추출(ADD)과 갱신 재작성본(UPDATE)이 섞여 있음 (러너는 DELETE만 제외). memory_events와 조인한 유래별 accuracy:

| 유래 | n | 0점 | 1점 | 2점 | Acc |
|---|---|---|---|---|---|
| ADD (신규 추출) | 2,094 | 32.9% | 37.2% | 30.0% | **48.6%** |
| UPDATE (재작성본) | 3,971 | **69.9%** | 19.3% | 10.7% | **20.4%** |

- 후보의 65%가 재작성본이고, **전체 0점의 80%가 재작성본 유래** — 전체 Acc 30%를 끌어내린 건 추출이 아니라 재작성 (가중평균 검산 일치: 57.1% = 전체 0점률)
- 신규 추출만 보면 Acc 48.6% — 논문(60.9)에 못 미치지만 "절반 수준"은 아님
- **⚠ 미판별 논점**: UPDATE 0점에는 (a) 진짜 drift (재작성 중 원 정보 이탈)와 (b) **구조적 채점 불리** — 재작성본은 과거 세션 누적 내용을 담는데 accuracy 채점은 당해 세션 대화·골든과만 대조 (HaluMem의 세션 단위 채점은 "후보=당해 세션 산물"을 전제하나 mem0의 교차 세션 재작성이 이 전제를 깸) — 가 섞여 있음. (b) 비중이 크면 Acc 30%는 시스템 성능의 과소평가 → **정성분석 A에 판별 항목 추가**: UPDATE 유래 0점 메모리의 내용이 과거 세션 대화로 소급되는지 확인
- 개선 우선순위 함의: (a)가 크면 "재작성 빈도 억제"의 순위 상승 (재작성 절반 감축만으로 Acc 산술 회복 큼), (b)가 크면 고칠 것은 시스템이 아니라 해석·각주 시스템 산출물이 병합·패러프레이즈형이라 회색지대에 밀집 → 왜곡 증폭. 단, nano 기준으로도 accuracy 0점 57% — 재작성 drift는 실측된 시스템 문제
- nano 기준 paper 대비 해석: 추출 커버리지 ~62% 수준(R), 갱신 크게 열위(Upd C 8.6 vs 25.5), Acc 절반(30 vs 61 — 재작성 drift), QA는 Omission 구조 동일(27.9≈27.8)하나 Hallucination +10pt(근거 부재 시 추측하는 생성기 성향), FMR은 우위(63 vs 57)

## 5. 러너 설계 결정

| 항목 | 결정 | 근거 |
|---|---|---|
| 유저 스코핑 | `user_id=uuid`, 표시용 이름 별도 (`TEMPLATE_MEM0`에는 이름) | 이름 충돌 방지. 스코핑 키는 LLM에 안 보여 결과 무영향 |
| 컬렉션 | **유저별 분리** (`halumem_{version}_{uuid}`) | 0.1.118 `delete_all`이 filter 무시하고 컬렉션 전체 reset (main.py:819) → 공유 컬렉션 + 유저 병렬 시 타 워커 메모리 증발/409 레이스. 유저별 분리로 원천 차단 |
| 유저 병렬 | ProcessPool, 워커별 Memory 인스턴스 + 유저별 history_db_path | SQLite 공유 시 database is locked. history db가 유저별 CUD 이력 trace 부산물로도 남음 |
| 컨텍스트 타임스탬프 | `metadata.session_time` | §4 |
| add 이벤트 | `memory_events` 키로 원본 보존 | UPDATE의 previous_memory가 trace/대시보드 핵심 데이터 |
| `extracted_memories` | DELETE 이벤트 제외 (**확정 필요** — platform의 포함 여부 불명, 편차 후보) | 죽은 메모리가 Integrity/Accuracy 판정 오염 방지 |
| 재개 | 유저 tmp 파일 존재 시 skip | 수십 시간 잡 안전장치 |
| 병렬성 | 순차 시작, 필요 시 docker Qdrant로 병렬화 | 디버깅 용이 |

## 6. 논문 대비 편차 레지스터

결과 보고 시 반드시 명시할 것들:

1. 메모리 백엔드: platform(내부 미공개) → OSS 0.1.118 기본 프롬프트 (custom_instructions 미주입 포함)
2. judge/답변 모델: GPT-4o → 로컬 vLLM 모델 (calibration 서브샘플로 보정)
3. embedder: platform 내장 → 로컬 임베딩 모델
4. 내부 후보 검색 limit=5 (OSS 하드코딩, platform 값 불명)
5. `extracted_memories`의 DELETE 처리 (§5)

## 7. 환경·인프라 사실

- **uv 구조**: 루트 = `mem0ai==0.1.118` + `openai>=1.90,<1.110` (mem0 상한 때문). vLLM은 **독립 프로젝트 `gpu/mem0-classic-oss/`** (`uv init --bare --no-workspace`) — vllm≥0.25가 openai≥2.0을 요구해 한 lockfile에 공존 불가. 서버: `uv run --project gpu vllm serve ...`
- **mem0 2.0.12 주의**: 최신 OSS는 additive-only 파이프라인 (update 결정 단계 없음, 이벤트 전부 ADD, linked_memory_ids 방식). HaluMem Updating 태스크와 부정합 → baseline은 0.1.118 고정. 2.x는 추후 대시보드의 "비교 아키텍처 #2" 후보
- **Qdrant**: 서버 모드(host/port, docker) 기본. mac 스모크는 embedded `path` 모드 가능
- **llms.py 주의** (서브모듈): `RETRY_TIMES` 등 env 기본값 없이 `int(os.getenv())` — .env 누락 시 import 에러. JSON 파싱이 ```json 블록 정규식 — 로컬 모델은 vLLM structured output으로 강제 필요
- **데이터 경로**: 벤치마크는 `dataset/HaluMem-{Medium,Long}.jsonl` (`HaluMem/data/`에는 없음)
- **서버(Blackwell RTX 6000 Pro) 실전 이슈 3종** (2026-07-15, 전부 scripts/mem0-classic-oss/serve.sh에 반영):
  1. FlashInfer 샘플러 JIT이 "requires sm75+"로 죽음 — torch가 CUDA<12.9 빌드라 sm_120 capability 조회 실패 → arch 폴백 목록에 구형 arch 섞임. 우회: `VLLM_USE_FLASHINFER_SAMPLER=0` + `TORCH_CUDA_ARCH_LIST=12.0` (근본 해결은 torch cu129+ 재설치)
  2. 같은 GPU에 vLLM 서버 2개 (순차 기동 기준) — 두 번째 서버의 `gpu-memory-utilization`은 양쪽 제약의 박스 안이어야 함: **(선점 프로세스 점유 + 자기 웨이트/그래프)/전체 < util < 잔여 메모리/전체**. 낮으면 KV cache 음수(util×전체 − 총사용량), 높으면 기동 시 free-memory 검사 탈락. 동시 기동은 프로파일링 레이스라 비결정적 — 금지 (serve.sh가 순차 기동 + 윈도우 단위 생존 확인으로 강제)
     **확정 설정 (2026-07-20)**: llm 0.40 + emb 0.55 `--enforce-eager`. emb의 CUDA graph 메모리 추정(~15GiB)이 박스를 공집합으로 만들 수 있어 eager 강제 (임베딩 서버는 graph 이득 미미). llm 0.45일 땐 emb 기동 검사 탈락 (free 49.6 < 요구 52.2)
  3. vLLM이 Qwen3-Embedding-4B의 `dimensions` 파라미터를 400으로 거부 (mem0 embedder는 항상 dimensions를 보냄) — 모델은 MRL 지원이나 HF config 미선언이 원인. 해결: serve 시 `--hf-overrides '{"is_matryoshka": true}'`
  - 참고: 임베딩 모델은 별도 태스크 플래그 없이 자동 감지됨 (`Supported tasks: ['embed']`)
