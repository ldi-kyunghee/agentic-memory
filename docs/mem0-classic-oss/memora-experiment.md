# Memora 벤치마크 실험 (mem0 classic OSS)

Memora(*From Recall to Forgetting*, ACL 2026 Findings)를 HaluMem·BEAM과 같은 스택에 얹어
재현하고 해부한 기록임. 데이터·코드 출처는 [github.com/geniesinc/Memora](https://github.com/geniesinc/Memora)
(Apache 2.0). 논문은 [aclanthology.org/2026.findings-acl.1337](https://aclanthology.org/2026.findings-acl.1337/).

HaluMem·BEAM과 나란히 놓지 않음. 벤치마크마다 자기 비교 공간을 가짐.

---

## 0. 왜 이 벤치마크인가

**HaluMem에도 BEAM에도 없는 것이 여기 있음: 삭제.**

우리가 지금까지 세운 것 중 제일 단단한 두 가지가 갱신에 관한 것임.

- 갱신 판정이 사람·LLM 양쪽에서 정렬돼 있지 않음 (사람끼리 Cohen κ 0.562, `backbone-experiment.md` §18-3)
- 무효 UPDATE가 99.5% (§14)

그런데 HaluMem은 삭제를 아예 안 재고, BEAM은 갱신 골든조차 없음. Memora는 **삭제를 19~21%
비중으로 넣고, 삭제된 것을 언급하면 점수를 깎음.** 우리 서사의 빈 칸을 정확히 메움.

| 벤치마크 | 통합 필요 세션 (평균/최대) | 갱신·삭제 (평균/최대) |
|---|---|---|
| LOCOMO | 1.3 / 15.0 | **0.0 / 0.0** |
| LongMemEval | 1.9 / 6.0 | 2.0 / 2.0 |
| PersonaMem | 1.3 / 3.0 | 1.2 / 3.0 |
| **Memora Weekly** | 5.3 / 26.0 | 2.7 / 11.0 |
| **Memora Quarterly** | **28.4 / 309.0** | **14.8 / 94.0** |

논문 Table 1임. LOCOMO는 문항의 94%가 두 세션 이하로 답이 나옴.

---

## 1. 데이터 (로컬 전수 실측)

```
data/{weekly|monthly|quarterly}/<persona>/
├── conversations/session_NNNN.json
└── evaluation_questions_<persona>.json
```

페르소나 10종은 직업군임 (`academic_researcher`, `software_engineer`, `startup_founder` 등).

| | weekly | monthly | quarterly |
|---|---|---|---|
| 페르소나 | 10 | 10 | 10 |
| 세션 | 1,551 | 6,150 | 19,913 |
| 턴/세션 중앙 | 15 | 15 | 15 |
| 문항 | 150 | 150 | 300 |
| 평가 기준 | 735 | 1,315 | 4,365 |
| λ 중앙 | 0.286 | 0.441 | 0.375 |

⚠ **논문의 기준 수(749 / 1,421 / 4,884)와 공개본이 다름.** 2~11% 적음. 공개본 차이로 보이며
수치를 인용할 때 밝혀야 함.

### 1-1. 연산 분포: 삭제가 갱신보다 많음

| | add | **delete** | update | 메모리 없는 세션 |
|---|---|---|---|---|
| weekly | 703 | **193** | 136 | 519 (33%) |
| monthly | 2,371 | **788** | 583 | 2,408 (39%) |
| quarterly | 7,434 | **2,178** | 2,102 | 8,199 (41%) |

세션의 33~41%가 메모리를 전혀 안 담는 **순수 잡음**임. 그리고 세션당 `share_memory: true`
턴이 중앙 1개임. 15턴짜리 대화에 기억할 것이 한 줄 들어 있는 구조임.

### 1-2. 스키마

**세션 파일.** `session_type`(activity/preference/goal/no_memory), `operation`(add/update/delete/null),
`operation_details`(구조화된 메모리 항목), `date`, `conversation[{turn, speaker, message, share_memory}]`.
speaker는 `user_agent` / `ai_agent`임.

**문항.** `question_id`, `question`, `question_date`, `memory_evidence`, `forgetting_evidence`,
그리고 `evaluation.evaluation_questions[]`. 기준 하나는
`{evaluation_question, expected_answer: yes|no, evaluation_type: memory_presence|forgetting_absence}`임.

**설계를 크게 단순하게 만든 실측 하나: `question_date`가 전부 기간의 마지막 날임.**
체크포인트 없이 전부 투입한 뒤 물어도 미래 정보가 새지 않음.

**추적이 됨.** `memory_evidence`·`forgetting_evidence`에 `session_id`가 붙어 있어 HaluMem
골든처럼 "어느 세션에서 들어온 사실인가"를 따라갈 수 있음.

---

## 2. FAMA

```
FAMA = max(0, MPA - λ · (1 - FAA))
  MPA = memory_presence 기준 충족 비율     (넣어야 할 것을 넣었나)
  FAA = forgetting_absence 기준 충족 비율   (빼야 할 것을 뺐나)
  λ   = N_forget / (N_presence + N_forget)   ← 문항마다 다름
```

과제 점수는 그 과제 문항 FAMA의 평균 × 100임.

**페널티가 셈.** presence 5 / absence 8 짜리 문항이면 λ=0.615라, **기억을 완벽히 하고 망각을
전부 실패하면 1.0이 0.385로 떨어짐.** 구현과 검산은 `src/memora/fama.py`.

---

## 3. 우리 레인 (이탈 대장)

**공식 수치를 재현하는 것이 목적이 아님.** 모델이 전부 다르므로 애초에 불가능함. 목적은
HaluMem·BEAM과 **같은 레인 안에서** mem0의 기억·망각 행동을 재는 것임.

어디가 왜 다른지를 구분해 적어둠. 나중에 어느 선택이 원칙이고 어느 것이 제약인지 헷갈리지
않게 하기 위함임.

| 항목 | 공식 | 우리 | 구분 |
|---|---|---|---|
| 메모리 백엔드 | Mem0 관리형 · v3 파이프라인 | **classic OSS 0.1.118** | 어쩔 수 없음 |
| 세션 시각 | `add(..., timestamp=<unix>)` | `metadata['session_date']` | **어쩔 수 없음** (OSS `Memory.add()`에 그 인자가 없음) |
| 답변 생성 모델 | gpt-4o-mini · `max_tokens=500` · `temperature=0.7` | gpt-oss-120b · effort high · **길이 제한 없음** | **우리 선택** |
| 채점 | GPT-4.1 + Claude Haiku 4.5 + Gemini 2.5 Flash 다수결 | gpt-oss-120b · effort high · 1회 | 자원 제약 |
| agent LLM | (명시 없음) | gpt-oss-120b · effort 기본(medium) | **우리 선택** |
| 투입 단위 | 세션 통째 `add()` | 동일 | 공식 따름 |
| speaker 매핑 | `user_agent`→user, `ai_agent`→assistant | 동일 | 공식 따름 |
| 검색 | top-50, 날짜 필터 없음 | 동일 | 공식 따름 |
| 답변·채점 프롬프트 | `memory_to_answer.py` 원문 | 동일 | 공식 따름 |
| 컨텍스트 포맷 | `N. {memory} (relevance: 0.87)`, **날짜 없음** | 동일 | 공식 따름 |

**우리 선택 두 개의 근거.**

전 구간 gpt-oss-120b high는 HaluMem·BEAM과 레인을 맞추기 위함임. 이 세 벤치마크의 결과를
서로 읽으려면 모델이 같아야 함.

**답변 길이 제한을 안 두는 것**은 §5에서 실측으로 정한 것임. 공식은 `max_tokens=500`으로
그냥 끊는데, 우리가 같은 짓을 하면 필요한 내용까지 날아가 전체 점수가 오히려 나빠짐.
길이는 통제할 변인이 아니라 **함께 보고할 값**으로 다룸.

> ⚠ **논문 수치와 절대 비교하지 않음.** BEAM에서 세운 규칙 그대로임. 논문 표는 맥락으로만
> 옆에 두고 비교 불가를 명시함.

---

## 4. 파이프라인

| 단계 | 스크립트 | 하는 일 |
|---|---|---|
| Stage A | `eval/mem0-classic-oss/memora/ingest_memora.py` | 세션 통째 투입 → 문항별 top-50 검색 저장 (+`--trace`) |
| Stage A′ | `.../answer_memora.py` | 검색 결과로 답변 생성 |
| Stage B | `.../judge_memora.py` | 기준별 yes/no 판정 → FAMA |
| 판독 | `src/memora/readout.py` | 수행률·과제별·페르소나별·상관·길이 |

실험 하나 = `--version` 문자열 하나. `results/mem0-classic-oss/memora-{version}/`,
`traces/mem0-classic-oss/memora-{version}/`, Qdrant 컬렉션 `memora_{version}_{persona}`로 파생됨.

⚠ **투입은 페르소나 단위 병렬임.** 페르소나 안에서는 세션이 순차라 `--max-workers`를
페르소나 수보다 크게 줘도 소용없음. 10페르소나에 워커 5면 두 파도로 끝남.

⚠ 컬렉션은 페르소나가 끝날 때마다 지움. BEAM에서 컬렉션이 180개까지 쌓여 Qdrant가 죽은 적이
있음. `MEMORA_KEEP_COLLECTIONS=1`로 남길 수 있음.

---

## 5. 스모크 판독 (2026-08-21, weekly / academic_researcher)

158세션 · 15문항 · 81기준. 투입 13분 · 답변 2분 · 채점 2분.

### 5-1. mem0는 삭제를 하긴 하는데 덜 함

| | 데이터셋 의도 | mem0 실제 |
|---|---|---|
| ADD | 71 | **183** |
| UPDATE | 11 | 39 |
| **DELETE** | **20** | **12 (60%)** |

저장 171개 = ADD 183 − DELETE 12로 정확히 맞음 (UPDATE는 개수를 안 바꿈).

추가가 2.6배인 것은 정상임. 세션에 지정된 연산은 하나지만 mem0는 15턴 대화에 섞인 부수적
사실을 전부 뽑음. **읽어야 할 값은 삭제·갱신 수행률임.**

### 5-2. 페널티가 실재함

| 과제 | FAMA | MPA | 페널티 |
|---|---|---|---|
| remembering | 70.03 | 77.00 | 6.97 |
| **recommending** | **27.33** | 63.33 | **36.00** |
| reasoning | 13.33 | 13.33 | 0.00 |
| 전체 | 36.90 | 51.22 | 14.32 |

`recommending`에서 **36점이 날아감.** `reasoning`의 페널티가 0인 것은 그 과제에
forgetting 기준이 없어서임 (FAMA = MPA).

### 5-3. 페널티는 대부분 저장소 탓임 (길이 통제 대조)

답변이 중앙 2,726자로 공식 예산(500토큰)의 1.4배였음. FAMA가 과다 포함을 벌주므로
**길이가 원인인지** 갈라야 했음.

생성을 손대지 않고 채점 직전에 500토큰(o200k_base)으로 자른 대조군을 돌림.
`--truncate-tokens 500`. effort를 낮추는 방법은 길이와 사고량이 함께 움직여 변인이 둘이 되고
HaluMem·BEAM의 high 통일도 깨지므로 쓰지 않음.

| 과제 | | FAMA | MPA | 페널티 |
|---|---|---|---|---|
| recommending | 원본 | 27.33 | 63.33 | **36.00** |
| | 500 절단 | 35.33 | 63.33 | **28.00** |
| remembering | 원본 | **70.03** | 77.00 | 6.97 |
| | 500 절단 | 54.46 | **60.00** | 5.54 |
| 전체 | 원본 | **36.90** | 51.22 | 14.32 |
| | 500 절단 | 34.38 | 45.56 | 11.18 |

**36점 중 8점만 길이 탓임. 28점은 남음.** 자르고도 MPA가 63.33 그대로인데 페널티만 줄었으니,
저장소에 무효 메모리가 실제로 남아 검색에 딸려 오는 것임. 삭제 수행률 60%와 맞물림.

그리고 `remembering`은 절단으로 **MPA가 77.0 → 60.0으로 무너짐**(15문항 중 9개가 잘림).
전체도 36.90 → 34.38로 나빠짐. **자르는 것이 이득이 아님.** §3의 "길이 제한 없음"을 대표값으로
정한 근거임.

> ⚠ 스모크는 페르소나 하나(15문항)임. **수치를 해석하지 않음.** BEAM에서 12대화 잠정값을
> 확정값으로 읽었다가 35대화에서 0.03~0.06이 내려간 적이 있음 (`beam-experiment.md` §4-1).

---

## 6. 남은 것

- **weekly·monthly·quarterly 전량.** 2026-08-21 실행 중 (27,614세션 · 600문항 · 6,415기준)
- **삭제 수행률과 forgetting 정확도의 상관.** 페르소나 단위로 재면 §5-3의 진단이 확정됨.
  `readout.py`가 계산함
- **trace 기반 삭제 실패 원인 분석.** mem0가 왜 20건 중 8건을 놓쳤는가. 검색이 대상을 못
  찾았는가, 갱신 결정 LLM이 DELETE를 안 골랐는가
- **대시보드 Memora 탭**
