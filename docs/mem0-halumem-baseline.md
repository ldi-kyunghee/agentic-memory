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
| A | mem0 메모리 연산: 세션 add → 추출 메모리·이벤트 수집, update MP top-10 검색, 질문 top-20 검색 → context 저장 | `src/runner_mem0_oss.py`, vLLM **serve** (순차 의존이라 배치 불가) |
| A' | 저장된 context+question → 답변 일괄 생성 | vLLM **offline batch** (`gpu/` 프로젝트) |
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

`src/analyze_trace.py` (ⓐ 유실 정량화 / ⓑ omission 원인 / ⓒ QA 실패 전파). 매처: 임베딩 코사인(threshold 0.65) — **Jaccard는 패러프레이즈 실명으로 extraction_miss를 87%로 과대 산정** (스팟 체크로 적발) → 임베딩 매처를 표준 확정. 대시보드의 골든↔시스템 정렬도 임베딩 기반 필수.

**20유저 확정 집계** (traced full 20 users, 2026-07-17 — 3유저 예비 결과와 분포 일치, 대표성 확인):

- ⓐ 유실 UPDATE: 766/30,793 (**2.49%**) — mini 스택(0.66%)의 ~4배. 매처 무관 지표라 맥/서버 동일 (교차 검증)
- ⓑ Omission 2,071건 (분류기 픽스 ce858cd 반영: 복수 이전 버전 대응 + overwritten 검사 매처 일관화): **decision_miss 47.9% / extraction_miss 44.3% / overwritten 7.1% / retrieval_miss 0.7%**
  - 논문 §6.2.1("추출 누락이 주원인")의 정밀화를 넘어 역전: **최대 단일 원인은 결정 실패** — old memory가 검색에 잡혔는데도 update가 안 됨 (집계 관점에선 불가시)
  - 픽스 전 "retrieval_miss 9.3%"의 대부분은 실제로는 **overwritten** (시스템이 old를 스스로 갱신/삭제했으나 골든 새 버전과 다른 내용 — 어휘 매칭이 prev_text를 못 알아봐 오분류됐던 것). overwritten은 사실상 "갱신은 했으나 틀리게"라는 결정 품질의 아종
- ⓒ QA 실패 1,770건: extraction_fault 63.5% / generation_fault 30.1% / retrieval_fault 6.4% (이번 픽스 무관 경로, 불변)
- 일관 결론 (더 강해짐): **retrieval은 무죄에 가까움 (0.7%)** — 병목은 갱신(결정+overwritten ≈ 55%)과 저장(추출 44%)
- **매처 표준 확정: Qwen3-Embedding-4B + cosine 0.65 (서버)** — 임베딩 모델을 바꾸면 같은 threshold라도 결론이 왜곡됨을 실측 (동일 유저에서 OpenAI 3-small 사용 시 extraction_miss 42→68로 부풀어). 원인 라벨은 측정 모델 종속 — 모든 분석은 이 표준 설정으로만
- 단서: 분모는 4B judge 라벨(판정 기질 영향). threshold 민감도 스윕은 외부 보고 시점에 GPT-4o 앵커와 묶어 수행 (백로그). 관찰자 효과 없음 확인: traced/untraced 풀런 성적표 차이 ±0.2%p

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

- **uv 구조**: 루트 = `mem0ai==0.1.118` + `openai>=1.90,<1.110` (mem0 상한 때문). vLLM은 **독립 프로젝트 `gpu/`** (`uv init --bare --no-workspace`) — vllm≥0.25가 openai≥2.0을 요구해 한 lockfile에 공존 불가. 서버: `uv run --project gpu vllm serve ...`
- **mem0 2.0.12 주의**: 최신 OSS는 additive-only 파이프라인 (update 결정 단계 없음, 이벤트 전부 ADD, linked_memory_ids 방식). HaluMem Updating 태스크와 부정합 → baseline은 0.1.118 고정. 2.x는 추후 대시보드의 "비교 아키텍처 #2" 후보
- **Qdrant**: 서버 모드(host/port, docker) 기본. mac 스모크는 embedded `path` 모드 가능
- **llms.py 주의** (서브모듈): `RETRY_TIMES` 등 env 기본값 없이 `int(os.getenv())` — .env 누락 시 import 에러. JSON 파싱이 ```json 블록 정규식 — 로컬 모델은 vLLM structured output으로 강제 필요
- **데이터 경로**: 벤치마크는 `dataset/HaluMem-{Medium,Long}.jsonl` (`HaluMem/data/`에는 없음)
- **서버(Blackwell RTX 6000 Pro) 실전 이슈 3종** (2026-07-15, 전부 scripts/serve.sh에 반영):
  1. FlashInfer 샘플러 JIT이 "requires sm75+"로 죽음 — torch가 CUDA<12.9 빌드라 sm_120 capability 조회 실패 → arch 폴백 목록에 구형 arch 섞임. 우회: `VLLM_USE_FLASHINFER_SAMPLER=0` + `TORCH_CUDA_ARCH_LIST=12.0` (근본 해결은 torch cu129+ 재설치)
  2. 같은 GPU에 vLLM 서버 2개 (순차 기동 기준) — 두 번째 서버의 `gpu-memory-utilization`은 양쪽 제약의 박스 안이어야 함: **(선점 프로세스 점유 + 자기 웨이트/그래프)/전체 < util < 잔여 메모리/전체**. 낮으면 KV cache 음수(util×전체 − 총사용량), 높으면 기동 시 free-memory 검사 탈락. LLM(0.45, ~36GB 점유) 뒤의 emb는 0.49~0.61 범위 → **0.55 채택**. 동시 기동은 프로파일링 레이스라 비결정적 — 금지
  3. vLLM이 Qwen3-Embedding-4B의 `dimensions` 파라미터를 400으로 거부 (mem0 embedder는 항상 dimensions를 보냄) — 모델은 MRL 지원이나 HF config 미선언이 원인. 해결: serve 시 `--hf-overrides '{"is_matryoshka": true}'`
  - 참고: 임베딩 모델은 별도 태스크 플래그 없이 자동 감지됨 (`Supported tasks: ['embed']`)
