# 실험: Memory Agent 백본 강화 (Qwen3-4B → GPT-5-Nano) — mem0-classic-oss

> 작성 2026-07-24. 배경: [custom-prompt-experiment.md](custom-prompt-experiment.md) · [mem0-halumem-baseline.md](mem0-halumem-baseline.md)
> **변인은 memory agent 백본 하나** (mem0의 추출·update 결정 LLM). 프롬프트는 default/custom 양쪽을 돌려 **백본×프롬프트 2×2**를 완성한다. Generator(답변 생성)는 Qwen3-4B 고정, judge는 gpt-5-nano(4유저) 고정.

## 1. 목적

- 병목 분석 결과 추출 커버리지(custom 프롬프트로 해소됨)와 update 디테일 보존·재작성 drift는 **백본 능력 문제**로 지목돼 왔음 — 백본을 강화하면 어떤 지표가 얼마나 움직이는지 실측
- 특히: ① update 결정 품질 (Upd C, decision_miss), ② 재작성 drift (유래별 Acc), ③ 지침 준수 (custom 프롬프트에서 문단형·"user 발화만" 준수 — Qwen은 과잉 준수+FMR 오염이 있었음)

## 2. 설계 결정

- **4유저 레인이 canonical.** 20유저 Stage A는 백본이 유료가 되면서 근거 상실 (4B judge 20u 행은 왜곡 있는 judge의 보조 비교였고, 신뢰 축은 nano judge 4u). 4유저 = QA 705·골든 1,861개로 방향 탐색엔 충분하며, 우승 조합 확정 시 그 조합만 20유저로 승격
- **코드 변경 불필요 (검증 완료)**: mem0 0.1.118 `LLMBase._get_supported_params`가 모델명 "gpt-5" 감지 시 temperature/max_tokens/top_p를 자동 필터링. `MEM0_LLM_MODEL` env 교체만으로 동작
- **reasoning effort는 기본값** (mem0 config로 전달 불가) — judge에서 쓰는 minimal이 아님. "백본 강화" 취지에는 부합하나 출력 과금이 늘어나는 요인으로 기록
- 임베더(서버 Qwen3-Embedding-4B)·Qdrant·top-k 20·데이터셋 첫 4유저 모두 기존과 동일

## 3. 비용 (trace 실측 외삽)

Stage A 20유저 기준 입력 ~11M tok + 출력 ~5M tok → 4유저는 ~20% → **런당 ~$0.6, reasoning 여유 포함 ~$1**. B-2 judge ~$1.1/런. 2런 총 **~$4 내외**.

## 4. 실행 (전부 서버, tmux)

```bash
# Run C: nano 백본 × default 프롬프트
OPENAI_BASE_URL=https://api.openai.com/v1 \
MEM0_LLM_MODEL=gpt-5-nano-2025-08-07 \
uv run python eval/mem0-classic-oss/eval_memzero_oss.py \
    --version nano4 --user-num 4 --top-k 20 --max-workers 20 --trace

# Run D: nano 백본 × custom 프롬프트 (토글 추가)
OPENAI_BASE_URL=https://api.openai.com/v1 \
MEM0_LLM_MODEL=gpt-5-nano-2025-08-07 MEM0_CUSTOM_FACT_PROMPT=halumem \
uv run python eval/mem0-classic-oss/eval_memzero_oss.py \
    --version nano4-custom --user-num 4 --top-k 20 --max-workers 20 --trace

# 이후 각 런에 대해 (버전명만 교체):
# A' — generator는 Qwen이므로 env 프리픽스 없이!
uv run python eval/mem0-classic-oss/gen_answers.py \
    --results results/mem0-classic-oss/memzero-oss-nano4/memzero-oss_eval_results.jsonl --max-workers 20
# B-2 — nano judge 4유저
OPENAI_BASE_URL=https://api.openai.com/v1 \
JUDGE_MODEL=gpt-5-nano-2025-08-07 JUDGE_REASONING_EFFORT=minimal \
uv run python eval/mem0-classic-oss/judge.py \
    --results results/mem0-classic-oss/memzero-oss-nano4/memzero-oss_eval_results.jsonl \
    --user-num 4 --max-workers 20 --out-dir results/mem0-classic-oss/judge-gpt5nano-4u-nano
# (Run D는 nano4 -> nano4-custom, out-dir -> judge-gpt5nano-4u-nano-custom)

# 로컬 동기화 후: 유래별 분해 + evidence 저장 교차검증
uv run python src/mem0-classic-oss/analyze_acc_by_origin.py \
    --artifacts results/mem0-classic-oss/memzero-oss-nano4/tmp \
    --judge results/mem0-classic-oss/judge-gpt5nano-4u-nano/judge
uv run python src/mem0-classic-oss/analyze_qa_evidence_storage.py \
    --judge results/mem0-classic-oss/judge-gpt5nano-4u-nano/judge
```

두 런 동시 실행 안전 (공유 상태 없음 — 버전별 컬렉션/디렉토리 분리, OpenAI rate limit만 공유).

## 5. 트러블 기록

- **tmux 전역 환경의 dummy 키**: 과거 `export OPENAI_API_KEY=dummy` 상태에서 tmux 서버가 떠서 새 윈도우마다 dummy를 상속 → `.env`의 진짜 키를 `load_dotenv()`가 덮지 못함 (기존 env 우선). Run D 첫 시도가 이걸로 실패. 조치: 해당 윈도우 `unset` + `tmux set-environment -g -u OPENAI_API_KEY` (전역 테이블에서 제거). 교훈: **OpenAI로 나가는 커맨드 전 `echo $OPENAI_API_KEY` 확인** (빈 값이 정상)

## 6. 결과 (실행 후 기입) — 백본×프롬프트 2×2, nano judge 4유저 고정

| Agent 백본 | Prompt | R↑ | WR↑ | Acc.↑ (# mem) | Target P↑ (# mem) | FMR↑ | F1↑ | Upd C↑ | Upd H↓ | Upd O↓ | QA C↑ | QA H↓ | QA O↓ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Qwen3-4B | default | 26.65 | 49.38 | 30.12 (6,065) | 82.15 (1,583) | 63.16 | 40.25 | 8.57 | 0.17 | 88.91 | 42.84 | 29.08 | 27.94 |
| Qwen3-4B | custom | **61.47** | **74.83** | 26.82 (1,734) | 90.14 (360) | 39.38 | **73.10** | 13.78 | 0.34 | 83.19 | 40.57 | 35.46 | 23.97 |
| GPT-5-Nano | default | 22.62 | 45.84 | **42.37** (3,544) | 81.22 (1,411) | 60.23 | 35.39 | **17.14** | 0.50 | **79.66** | **49.79** | **22.84** | 27.38 |
| GPT-5-Nano | custom | 34.82 | 44.76 | 33.98 (1,164) | 88.00 (300) | 60.82 | 49.90 | 11.76 | **0.00** | 85.71 | 39.29 | 29.22 | 31.21 |
| GPT-5-Mini | default | 35.89 | 57.87 | 48.56 (1,950) | 83.73 (876) | 52.83 | 50.25 | **27.56** | 0.17 | **68.74** | **52.91** | **22.84** | 24.26 |
| GPT-5-Mini | custom | 42.67 | 53.49 | **55.07** (493) | **94.14** (239) | 64.33 | 58.72 | 19.50 | **0.00** | 78.99 | 46.52 | 29.50 | 23.97 |
| Qwen3-30B-A3B | default | 37.51 | 58.65 | 36.44 (6,025) | 81.83 (1,978) | 52.05 | 51.44 | 15.46 | 0.50 | 80.67 | 48.79 | 27.52 | 23.69 |
| Qwen3-30B-A3B | custom | 45.57 | 58.41 | 31.73 (1,776) | 85.09 (436) | 52.05 | 59.35 | 13.78 | **0.00** | 82.02 | 44.68 | 29.22 | 26.10 |
| Qwen3-4B-Thinking | default | 12.90 | 36.81 | 46.95 (2,359) | 81.12 (1,120) | **76.80** | 22.25 | 10.08 | **0.00** | 87.39 | 45.82 | 27.94 | 26.24 |
| Qwen3-4B-Thinking | custom | 13.02 | 20.62 | 49.28 (347) | 82.45 (151) | **83.24** | 22.48 | 8.49 | **0.00** | 89.98 | 34.18 | 25.25 | 40.57 |

(2026-07-24 nano/Qwen 행, 2026-07-26 mini 행, 2026-07-28 Thinking 행. nano judge 무효율 전부 0.1% 미만. 굵은 값 = 열별 최고 — Qwen custom의 R/WR/F1은 오염 커버리지(③) 유의. **effort 표기**: nano/mini 백본=default(medium, mem0가 effort 미전달)·4B-Thinking=항상 사고·judge nano=minimal)

유래별 Acc 분해 (nano judge):

| Agent 백본 × Prompt | ADD n / Acc | UPDATE n / Acc | UPDATE 비중 |
|---|---|---|---|
| Qwen × default | 2,094 / 48.6% | 3,971 / 20.4% | 65.5% |
| Qwen × custom | 460 / 54.5% | 1,274 / 16.8% | 73.5% |
| Nano × default | 2,943 / 42.8% | 601 / **40.3%** | **17.0%** |
| Nano × custom | 1,122 / 33.6% | 42 / 44.0% | 3.6% |
| Mini × default | 984 / 54.3% | 966 / 42.8% | 49.5% |
| Mini × custom | 220 / 57.7% | 273 / **52.9%** | 55.4% |
| 30B × default | 3,513 / 47.2% | 2,512 / 21.4% | 41.7% |
| 30B × custom | 1,067 / 41.8% | 709 / 16.5% | 39.9% |
| Thinking × default | 2,045 / 47.2% | 314 / **45.5%** | 13.3% |
| Thinking × custom | 277 / 48.9% | 70 / **50.7%** | 20.2% |

실패 QA evidence 저장 교차검증 ("전부 완전저장 = 저장됐는데 못 씀" 비율): Qwen×default 11.7% / Qwen×custom 37.0% (4B judge 20u 기준 — 백본 런은 nano judge 4u 기준이라 절대값 비교 주의) / **Nano×default 7.7% / Nano×custom 9.6%**

## 7. 분석 — 셀마다 병목이 다르다

**① QA 최강 셀은 nano×default (49.79)** — Qwen×default 대비 +7.0pt, QA H는 29.08→22.84로 급감. **그런데 R은 오히려 최저(22.62)**: nano는 적게(3,544개), 짧고(76~80자) 깨끗하게 저장하고 재작성을 거의 안 하는데, 그 "적지만 깨끗한 저장소"가 검색·활용 단계에서 더 잘 먹힘. R(완전 포함 2점 비율)이 낮아도 WR 45.8로 부분 커버리지는 유지된다는 점과 합치면, **QA엔 커버리지 총량보다 저장소 청결도가 더 효과적**이라는 게 이 실험의 핵심 발견.

**② drift는 Qwen 특이 행동으로 확정.** UPDATE 비중 65.5%(Qwen) → 17.0%(nano), UPDATE-유래 Acc 20.4% → 40.3% (ADD-유래와 사실상 동급). 재작성을 덜 하고, 해도 덜 망가뜨림 → Acc 30.12→42.37 (+12.3pt). "0점의 80%가 재작성본" 문제는 백본 교체만으로 대부분 해소.

**③ FMR로 지침 준수의 백본 의존성 입증.** Qwen×custom에서 39.38로 추락했던 FMR이 nano×custom에선 60.82 — **default 수준(60.23)과 동일**. nano는 "user 발화만" 지침을 실제로 지켜서 distractor를 흡수하지 않음 (빈 세션 5~6/유저 증가도 같은 신호). Qwen custom의 R 61.47은 상당 부분 "지침 무시하고 세션 전체를 쓸어담은" 대가로 얻은 오염된 커버리지였던 것.

**④ custom 프롬프트는 nano에선 손해.** R 22.62→34.82로 오르긴 하나 Qwen custom(61.47)에 한참 못 미치고, Acc·QA 모두 default보다 나쁨 (QA C 39.29 = 최저 셀). 절제된 ~580자 문단 + 엄격한 필터링 → 후보 1,164개로 커버리지 부족. **프롬프트 효과는 백본 종속적** — Qwen에겐 커버리지 폭증(오염 동반), nano에겐 과잉 절제. 동일 개입이 백본에 따라 정반대 방향으로 작용하므로, 프롬프트 튜닝은 백본 확정 후에 해야 함.

**⑤ 병목 지도 (evidence 저장 교차검증)**: nano 두 셀 모두 실패 QA의 절반 이상이 "일부 미저장"(49.7%/60.6%)이고 "저장됐는데 못 씀"은 7.7%/9.6%뿐 — **nano 런의 병목은 다시 추출 커버리지로 회귀** (Qwen×custom의 37%와 대조적). 셀별 병목: Qwen×default = 추출+drift / Qwen×custom = 초장문 활용(생성) / nano×default·custom = 추출 커버리지.

**개선 방향 시사**: 현 최선 조합은 **nano 백본 × default 프롬프트** (QA C 49.79, Acc 42.37, H 최저). 다음 개입은 이 셀의 남은 병목인 커버리지 — "깨끗함을 유지한 채 더 많이 뽑기" (예: default 프롬프트에 커버리지 지시만 추가, 세션당 fact 수 하한, 2-pass 추출). Qwen 계열로 돌아갈 경우엔 재작성 억제가 선결 과제.

## 8. 확장: 백본 사다리 E/F/G (2026-07-26 계획)

2×2의 발견("청결도가 QA를 결정")의 원인을 가르는 3개 런. 전부 default 프롬프트 × 4유저 × nano judge, 리포 코드 변경 0 (env 교체만).

| Run | 백본 | 검증 질문 | 비용 (A + judge) | 버전명 |
|---|---|---|---|---|
| E | gpt-5-mini (API) | 능력 사다리를 더 올리면 QA 49.79가 더 오르나, nano에서 포화인가 | ~$2.5 + $1.1 | `mini4` |
| F | Qwen3-30B-A3B-Instruct **bf16** (로컬) | 청결도는 능력 문제인가 Qwen 스타일 문제인가 — 30B가 재작성을 절제하면 능력 스토리 | $0 + $1.1 | `30b4` |
| G | Qwen3-4B-Thinking-2507 (로컬) | 같은 크기에서 reasoning만 얹으면 update 결정·drift가 개선되나 | $0 + $1.1 | `think4` |

보류: 30B-Thinking (E/F/G 결과가 필요성 결정), gemma-4 계열 (family 축 — F에서 능력 스토리 기각 시 1종만), custom 프롬프트 변형 (우승 백본 확정 후).

**인프라 (GPU 3 단일, 96GB)**: emb 상시 (8001, util **0.20** — 기존 0.55는 과예약이었음, ~19GB면 충분 확인) + LLM 슬롯 (8000) 실험별 교체. emb 축소로 30B bf16(~61GB, util 0.72)이 FP8 양자화 없이 탑재 가능해짐 (교란 요인 제거). A′는 generator=4B-Instruct가 8000에 있어야 하므로 실행 동선: E(A′까지) → 30B 스왑·F-A → Thinking 스왑·G-A → 4B 복원·F/G-A′ → B-2 ×2. G는 vLLM `--reasoning-parser` 필수 (`<think>`가 content에 남으면 mem0 JSON 파싱 파손 — 기동 후 1콜 검증).

- 결과: E(mini) 완료·§6 테이블 반영. F(30B)/G(Thinking) 진행 중

### 8-1. Run E (gpt-5-mini) 판독 — 2026-07-26

- **능력 사다리 유효, 포화 아님**: default 레인에서 4B→nano→mini 단조 개선 — QA C 42.84→49.79→**52.91**, Acc 30.12→42.37→**48.56**, Upd C 8.57→17.14→**27.56** (논문 GPT-4o mem0의 25.50을 넘어섬), UPDATE-유래 Acc 20.4→40.3→42.8%
- **"청결도" 재정의**: mini는 재작성 비중이 49.5%로 낮지 않은데(nano 17%) UPDATE-유래 Acc가 42.8%로 건강 — 관건은 **재작성 빈도가 아니라 재작성 품질**. Qwen의 문제는 "많이 고침"이 아니라 "고칠 때 망가뜨림"이었던 것
- **FMR 백본×프롬프트 상호작용 3형태**: custom 프롬프트가 FMR에 미치는 효과 — Qwen 급락(63→39), nano 불변(60→61), mini **개선**(52.83→64.33). 동일 개입의 부호가 백본마다 다름. 한편 default 레인에서 FMR은 능력이 올라도 하락 추세(63→60→53) — mini가 default에서도 171~223자로 길게 쓰며 distractor를 일부 흡수하는 트레이드오프
- **mini×custom의 극단 전략**: 세션의 15~35%를 통째로 빈 세션 처리 + 평균 1,461~2,393자 초대형 요약. 그런데도 R 42.67(자기 default보다 +6.8), Acc 55.07·Target P 94.14는 **전 셀 최고** — "선별한 세션만 정밀하게"가 정확도엔 최적이나 QA(46.52)는 default에 밀림 (빈 세션의 커버리지 손실이 QA에서 청구됨, evidence 미저장 51.6%)
- 실패 QA 분포(교차검증): mini-d 미저장 40.1%/완전저장 17.8% — 추출과 활용이 반반인 중간 체제. 병목이 단일 지점에서 분산 구조로 이행
- 현 순위 (QA C 기준): **mini-d 52.91** > nano-d 49.79 > Qwen-d 42.84 > … > nano-c 39.29

### 8-2. Run F (Qwen3-30B-A3B-Instruct) 판독 — 2026-07-26: "능력 vs family" 판정

- **재작성 품질은 family 각인으로 확정**: UPDATE-유래 Acc — Qwen 계열 20.4%(4B-d)/16.8%(4B-c)/**21.4%(30B-d)**/16.5%(30B-c) vs GPT 계열 40.3~52.9%. 파라미터 7.5배에도 품질 불변 → drift는 능력 부족이 아니라 **Qwen 스타일(및 mem0 update 프롬프트와의 궁합) 문제**. 반면 **재작성 빈도는 능력 따라 완화** (65.5%→41.7%) — 빈도와 품질이 서로 다른 축에 지배됨
- **추출 스타일도 family 각인**: 30B-d 후보 6,025개 ≈ 4B(6,065) 다작형, granularity 76~81자 동일. GPT 계열의 절제형(1,950~3,544)과 대비
- **QA는 능력 따라 개선**: 42.84(4B) → 48.79(30B) — 하지만 GPT 계열 대비 Acc(36.44)가 낮은 건 재작성 오염이 발목. Upd C도 15.46로 개선되나 mini(27.56)의 절반 수준
- FMR 52.05가 **두 셀에서 동일** — 30B는 custom에서도 FMR이 안 깨짐 (4B의 39.38 추락과 대비, 지침 준수는 능력 따라 개선). 단 절대값은 낮은 편 (다작형이 distractor를 일부 흡수)
- **custom 프롬프트는 4/4 백본 전패 (QA 기준)**: 42.84>40.57, 49.79>39.29, 52.91>46.52, 48.79>44.68 — QA가 목적이면 default가 항상 우위. custom의 용도는 R/Acc 특화 세팅(mini-c의 Acc 55.07)으로 한정
- **개선 함의**: Qwen 계열을 유지하려면 백본 교체가 아니라 **update 단계 개입(재작성 억제·update 프롬프트 개편)이 선결** — 이것이 개선 실험 1순위 후보. GPT 계열은 재작성 품질이 이미 건강하므로 커버리지(R)가 과제
- ⚠ **후속 정정 (§8-3)**: "family 각인" 결론은 Run G(4B-Thinking)에서 정밀화됨 — 재작성 품질을 가르는 건 family가 아니라 **reasoning 유무**였음

### 8-3. Run G (Qwen3-4B-Thinking) 판독 — 2026-07-28: drift의 진범은 "숙고 부족"

- **§8-2의 "family 각인" 결론 정정**: UPDATE-유래 Acc가 4B-Instruct 20.4% → **4B-Thinking 45.5%** — 같은 크기·같은 family에서 thinking만 얹었는데 GPT 계열(40.3~52.9%) 수준으로 점프. 전 백본을 다시 보면 패턴이 통일됨: **재작성 품질 건강(40~53%) = reasoning 모델(nano/mini/4B-Thinking), 파괴(16~21%) = non-reasoning(4B/30B Instruct)**. 30B가 못 고친 걸 4B+thinking이 고쳤으므로 크기도 family도 아닌 **"update 결정에서의 숙고"가 관건**
- **"숙고 = 깐깐한 필터"의 양면**: FMR 76.80/83.24로 **전 셀 최고** (미끼 저항 최강), 재작성 빈도 13.3%로 최저 수준, Upd H 0.00 — 저장소 청결도의 극한. 대신 R 12.90로 **전 셀 최저** (평균 55~61자 초절제 추출 + 빈 세션 8~10/유저). Upd C 10.08이 낮은 것도 품질 문제가 아니라 갱신 자체를 잘 안 해서(갱신 요구 골든이 커버 안 됨 → O 87%)
- **청결도 명제의 극단 검증**: R 12.90(4B-Instruct의 절반)인데 QA C 45.82로 4B-Instruct(42.84)보다 높음 — 커버리지 절반을 버리고도 청결도로 만회. 단 nano(49.79)/mini(52.91)에는 못 미침 = 청결도만으로는 한계, 커버리지와의 균형이 정답
- **think4-custom = 과잉 절제의 붕괴**: 세션의 40~77%를 빈 세션 처리, 후보 347개(전 셀 최저), WR 20.62까지 추락 → QA C 34.18로 **최저 셀 갱신**. custom 프롬프트 5백본 전패 확정
- 실패 QA의 55.5%(d)/69.6%(c)가 미저장 — 병목은 순수 추출 커버리지
- **종합 함의**: 로컬 스택에서 drift를 고치는 최저비용 개입 = **백본을 4B-Thinking으로 (단 추출 커버리지 보강 필수)** 또는 4B-Instruct 유지 + update 결정만 thinking 모델로 이원화하는 하이브리드가 유망

## 9. 관측 스택 민감도 — generator/judge를 GPT-5-Mini(minimal)로 (2026-07-28)

10런 전체 × **1유저(Martin)** 로 A′(gen=mini)·B(judge=mini) 재수행 (~$14). 산출물: `genmini/{run}.jsonl`, `judge-mini-genmini-{run}/`. 무효율 0.01%. 대시보드에 **Generator 레인**으로 등록 (드롭다운 qwen4b↔mini 전환, 기본값 mini).

**핵심: judge 기질 지각변동 — mini-minimal은 nano-minimal보다 대폭 관대.** 같은 유저·같은 Stage A 산출물이므로 메모리 지표 차이는 **순수 judge 효과** (generator는 QA에만 관여):

| (Martin, nano→mini judge) | R | Acc | FMR | Upd C | QA C(혼합) |
|---|---|---|---|---|---|
| full-traced | 29.3→67.4 | 31.8→74.6 | 60.8→49.6 | 8.4→56.3 | 40.9→57.9 |
| nano4 | 28.2→70.1 | 44.6→**97.1** | 55.2→44.0 | 19.0→79.6 | 52.4→70.1 |
| mini4 | 44.8→85.4 | 49.8→84.1 | 52.8→52.0 | 26.1→79.6 | 48.8→62.8 |
| think4 | 15.7→56.3 | 47.2→**99.5** | 72.8→64.8 | 12.0→59.2 | 49.4→64.0 |

- **관대화 방향 일관**: R +25~43pt, Upd C +40~60pt, Acc는 천장(97~99)까지 — "거의 다 인정" 기질. FMR은 오히려 하락 (포함을 후하게 잡으니 미끼도 흡수로 판정) — 방향까지 정합
- judge 스펙트럼 3점 완성: **4B(가혹) → nano-minimal(중간) → mini-minimal(관대)** — 절대 수치의 judge 지배가 3중 실증됨. Acc 97~99는 판별력 상실 신호라 mini-minimal의 신뢰성도 의심 대상 → **gpt-oss-120b(medium) 레인이 캐스팅보트** (준비 완료, GPU 대여 시 실행)
- 상대 순위: 메모리 지표는 대체로 보존 (custom>default R, think 최하 등), QA 순위는 이동 (nano4가 1위로) — 단 QA는 generator 교체 혼입이라 원인 분리 불가
- 스팟 체크: mini generator가 "middle name?"에 "Mark"로 날조한 건을 mini judge가 **Hallucination으로 정확히 판정** — 관대화가 무차별 승인은 아님
- **분석 방어 원칙 재확인**: 절대 수치는 judge 종속 → 보고는 "동일 judge 내 상대 비교 + 다중 judge 교차검증"으로만
