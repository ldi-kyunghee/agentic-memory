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

(2026-07-24 실측, nano judge 무효율 0.03/0.04%)

유래별 Acc 분해 (nano judge):

| Agent 백본 × Prompt | ADD n / Acc | UPDATE n / Acc | UPDATE 비중 |
|---|---|---|---|
| Qwen × default | 2,094 / 48.6% | 3,971 / 20.4% | 65.5% |
| Qwen × custom | 460 / 54.5% | 1,274 / 16.8% | 73.5% |
| Nano × default | 2,943 / 42.8% | 601 / **40.3%** | **17.0%** |
| Nano × custom | 1,122 / 33.6% | 42 / 44.0% | 3.6% |

실패 QA evidence 저장 교차검증 ("전부 완전저장 = 저장됐는데 못 씀" 비율): Qwen×default 11.7% / Qwen×custom 37.0% (4B judge 20u 기준 — 백본 런은 nano judge 4u 기준이라 절대값 비교 주의) / **Nano×default 7.7% / Nano×custom 9.6%**

## 7. 분석 — 셀마다 병목이 다르다

**① QA 최강 셀은 nano×default (49.79)** — Qwen×default 대비 +7.0pt, QA H는 29.08→22.84로 급감. **그런데 R은 오히려 최저(22.62)**: nano는 적게(3,544개), 짧고(76~80자) 깨끗하게 저장하고 재작성을 거의 안 하는데, 그 "적지만 깨끗한 저장소"가 검색·활용 단계에서 더 잘 먹힘. R(완전 포함 2점 비율)이 낮아도 WR 45.8로 부분 커버리지는 유지된다는 점과 합치면, **QA엔 커버리지 총량보다 저장소 청결도가 더 효과적**이라는 게 이 실험의 핵심 발견.

**② drift는 Qwen 특이 행동으로 확정.** UPDATE 비중 65.5%(Qwen) → 17.0%(nano), UPDATE-유래 Acc 20.4% → 40.3% (ADD-유래와 사실상 동급). 재작성을 덜 하고, 해도 덜 망가뜨림 → Acc 30.12→42.37 (+12.3pt). "0점의 80%가 재작성본" 문제는 백본 교체만으로 대부분 해소.

**③ FMR로 지침 준수의 백본 의존성 입증.** Qwen×custom에서 39.38로 추락했던 FMR이 nano×custom에선 60.82 — **default 수준(60.23)과 동일**. nano는 "user 발화만" 지침을 실제로 지켜서 distractor를 흡수하지 않음 (빈 세션 5~6/유저 증가도 같은 신호). Qwen custom의 R 61.47은 상당 부분 "지침 무시하고 세션 전체를 쓸어담은" 대가로 얻은 오염된 커버리지였던 것.

**④ custom 프롬프트는 nano에선 손해.** R 22.62→34.82로 오르긴 하나 Qwen custom(61.47)에 한참 못 미치고, Acc·QA 모두 default보다 나쁨 (QA C 39.29 = 최저 셀). 절제된 ~580자 문단 + 엄격한 필터링 → 후보 1,164개로 커버리지 부족. **프롬프트 효과는 백본 종속적** — Qwen에겐 커버리지 폭증(오염 동반), nano에겐 과잉 절제. 동일 개입이 백본에 따라 정반대 방향으로 작용하므로, 프롬프트 튜닝은 백본 확정 후에 해야 함.

**⑤ 병목 지도 (evidence 저장 교차검증)**: nano 두 셀 모두 실패 QA의 절반 이상이 "일부 미저장"(49.7%/60.6%)이고 "저장됐는데 못 씀"은 7.7%/9.6%뿐 — **nano 런의 병목은 다시 추출 커버리지로 회귀** (Qwen×custom의 37%와 대조적). 셀별 병목: Qwen×default = 추출+drift / Qwen×custom = 초장문 활용(생성) / nano×default·custom = 추출 커버리지.

**개선 방향 시사**: 현 최선 조합은 **nano 백본 × default 프롬프트** (QA C 49.79, Acc 42.37, H 최저). 다음 개입은 이 셀의 남은 병목인 커버리지 — "깨끗함을 유지한 채 더 많이 뽑기" (예: default 프롬프트에 커버리지 지시만 추가, 세션당 fact 수 하한, 2-pass 추출). Qwen 계열로 돌아갈 경우엔 재작성 억제가 선결 과제.
