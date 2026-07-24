# 실험: HaluMem 커스텀 추출 프롬프트 적용 — mem0-classic-oss

> 작성 2026-07-24. 배경 문서: [mem0-halumem-baseline.md](mem0-halumem-baseline.md) (§4h 테이블, §4i judge 분석)
> **변인은 fact 추출 프롬프트 하나** (mem0 기본 → HaluMem 원본 custom instructions). 모델·top-k·유저 수·tracing·judge 세팅 등 나머지는 전부 기존 full run과 동일하게 고정한다.

## 1. 동기

- 지금까지의 모든 수치(§4h)는 mem0 **기본** 추출 프롬프트로 뽑은 것. 그러나 HaluMem 원본의 Mem0 실험은 **커스텀 프롬프트**를 줬으므로, 논문 수치와의 격차 중 프롬프트 몫이 섞여 있다
- nano judge 분석(§4i)의 병목 1순위가 추출 커버리지(R 26 vs 논문 42, F1 격차의 사실상 전부)였고, 커스텀 프롬프트는 정확히 추출 단계를 겨냥하는 개입이다
- 부수 관심사: 커스텀 지침이 "구체 디테일 유지"를 강제하므로 UPDATE 재작성 drift(유래별 분해, §4i)와 Upd C에 주는 영향

## 2. 사실관계 — 원본과 OSS의 주입 방식 차이

| | HaluMem 원본 | 우리 (OSS 0.1.118) |
|---|---|---|
| 시스템 | mem0 **Platform** (`MemoryClient`, 관리형 API) | mem0 OSS classic |
| 주입 방법 | `client.project.update(custom_instructions=...)` (`HaluMem/eval/eval_memzero.py:27-53, 66`) | `MemoryConfig.custom_fact_extraction_prompt` |
| 의미론 | 관리형 추출 파이프라인에 지침 주입 (내부 비공개) | 기본 추출 프롬프트 **전체 대체** (`mem0/memory/main.py` `_add_to_vector_store`): few-shot 예시·`{"facts": [...]}` JSON 포맷 지시·오늘 날짜 주입이 전부 사라지고, user 메시지는 `Input:\n{대화}`로 바뀜 |

→ **완전 동치 재현은 불가능** (Platform 내부가 비공개). 우리의 재현 규약: 지침 원문을 verbatim으로 쓰되, OSS 파서(`json.loads(response)["facts"]`)가 요구하는 **최소한의 JSON 포맷 푸터만 덧붙인다**. 푸터가 없으면 파싱 실패로 추출이 조용히 0건이 된다 (초기 스모크의 "Invalid JSON" 사태와 동일 기전).

update 결정 프롬프트(`custom_update_memory_prompt`)는 **건드리지 않는다** — 변인 통제.

## 3. 프롬프트 지침 요약 (원문은 `eval/mem0-classic-oss/custom_prompt.py`)

1. 자기완결 메모리 (이름 사용, "user" 금지, 맥락·날짜 포함)
2. 개인 서사 중심 (정체성/가족/취미/정신건강/커리어/이정표)
3. 일반 진술 대신 구체 디테일 (정확한 날짜, 구체적 활동명, 감정 맥락)
4. **user 발화에서만 추출, assistant 응답 미반영**
5. 문단형(paragraph) 서사 구조

## 4. 가설 — 지표별 예상 방향

- **R/WR**: 지침 3(디테일 강제)으로 상승 기대가 기본 시나리오. 단 지침 4가 역풍 가능 — assistant 발화에만 있던 골든 정보는 원천 차단됨
- **FMR**: 상승(개선) 기대 — distractor는 정의상 "AI 발화에 있고 user가 확정 안 한 내용"이라 지침 4가 정면으로 차단
- **Acc / 유래별 분해**: 문단형 메모리는 채점 단위가 커져 "일부 비지지"(1점)로 갈리기 쉬움. ADD/UPDATE 구성비 변화와 UPDATE-유래 0점 비중(기존 80%)이 움직이는지 확인
- **Upd C**: 디테일 보존 지침이 Qwen의 "추상화 병"(실패의 99%가 Omission)을 완화하는지 — 이 실험의 두 번째 관전 포인트
- **주의**: 문단형이면 후보 메모리 수 자체가 줄 수 있음 → judge 비용 구조 변화 (호출 수 ↓, 콜당 길이 ↑) — Stage B 전에 비용 재산정 필수

## 5. 변경 파일 (이것 외 변경 없음)

| 파일 | 변경 |
|---|---|
| `eval/mem0-classic-oss/custom_prompt.py` | **신설** — 원문 verbatim 상수 + JSON 푸터 |
| `eval/mem0-classic-oss/eval_memzero_oss.py` | import 1줄 + `build_memory()`에 env 토글 2줄 + 시작 시 프롬프트 모드 출력 1줄 |

토글은 env `MEM0_CUSTOM_FACT_PROMPT=halumem` (기존 config가 전부 env-driven인 것과 일관, 워커 프로세스로의 인자 배관 불필요). 미설정 시 기본 프롬프트 → 기존 실험 재현성 보존.

**사후 검증 경로**: trace의 `llm_call(purpose=fact_extraction)` 레코드에 system prompt 전문이 남으므로, 커스텀 적용 여부를 산출물에서 사후 확인 가능.

## 6. 실행 절차

버전명 `full-custom` → 산출물 `results/mem0-classic-oss/memzero-oss-full-custom/`, trace `traces/mem0-classic-oss/full-custom/`. Qdrant 컬렉션은 `halumem_full-custom_{uuid}`로 기존과 충돌 없음.

```bash
# 0) 로컬 스모크 (OpenAI .env, 로컬 qdrant docker 필요) — 문단형 메모리가 나오는지 + JSON 파싱 확인
MEM0_CUSTOM_FACT_PROMPT=halumem uv run python eval/mem0-classic-oss/eval_memzero_oss.py \
    --version smoke-custom --user-num 1 --top-k 20 --trace

# 1) [서버, tmux] Stage A — 기존 full run과 동일 인자 + env 토글만 추가
MEM0_CUSTOM_FACT_PROMPT=halumem uv run python eval/mem0-classic-oss/eval_memzero_oss.py \
    --version full-custom --user-num 20 --top-k 20 --max-workers 4 --trace

# 2) [서버] Stage A' — 답변 생성
uv run python eval/mem0-classic-oss/gen_answers.py \
    --results results/mem0-classic-oss/memzero-oss-full-custom/memzero-oss_eval_results.jsonl

# 3) [서버] Stage B-1 — Qwen3-4B judge, 20유저 (기존 20u-4B 행과 비교용)
uv run python eval/mem0-classic-oss/judge.py \
    --results results/mem0-classic-oss/memzero-oss-full-custom/memzero-oss_eval_results.jsonl \
    --max-workers 8

# 4) [로컬] judge 비용 재산정 (문단형이라 기존 산정치 무효)
uv run python src/mem0-classic-oss/estimate_judge_cost.py \
    --results results/mem0-classic-oss/memzero-oss-full-custom/memzero-oss_eval_results.jsonl

# 5) [로컬, OpenAI .env] Stage B-2 — gpt-5-nano judge, 첫 4유저 (기존 4u-nano 행과 비교용)
JUDGE_MODEL=gpt-5-nano-2025-08-07 JUDGE_REASONING_EFFORT=minimal \
uv run python eval/mem0-classic-oss/judge.py \
    --results results/mem0-classic-oss/memzero-oss-full-custom/memzero-oss_eval_results.jsonl \
    --user-num 4 --max-workers 8 \
    --out-dir results/mem0-classic-oss/judge-gpt5nano-4u-custom

# 6) [로컬] Acc 유래별 분해 재실행 (custom 런 대상)
uv run python src/mem0-classic-oss/analyze_acc_by_origin.py \
    --artifacts results/mem0-classic-oss/memzero-oss-full-custom/tmp \
    --judge results/mem0-classic-oss/judge-gpt5nano-4u-custom/judge
```

주의:
- 서버 실행 시 env 토글을 빼먹으면 조용히 기본 프롬프트로 돈다 — 시작 로그의 `fact extraction prompt: halumem-custom` 출력 확인할 것
- 3)과 5)의 순서는 무관 (같은 jsonl을 읽기만 함). 5)는 4)의 비용 확인 후 실행

## 6-1. 스모크 관찰 (2026-07-24, 유저 1명 · 백본 gpt-4o-mini)

- 커스텀 프롬프트 적용 확인: trace `fact_extraction`의 system prompt = 지침 원문 ✓
- JSON 파싱 실패 0건 (포맷 푸터 유효) — "No results found" 경고는 모델이 정당하게 `{"facts": []}`를 반환한 빈 세션 (지침 4의 assistant 발화 차단 효과로 기본 프롬프트 대비 증가 예상됨, 러너의 기록용 경고일 뿐 유실 아님)
- 문단형 확인: fact 평균 ~256자 (기본 프롬프트 대비 3~5배), 이름 사용 ✓
- 관찰: 일부 fact가 1인칭("I reflected...")으로 생성됨 — 지침 위반은 아니나 백본별 지침 준수도 편차가 실험 변수 (서버 Qwen 런에서 재확인, 정성분석 항목)
- 완주 결과 (유저 2f1f..., 65세션, JSON 실패 0): 빈 세션 5, 후보 362개(평균 264자), 이벤트 ADD 329 / UPDATE 33 / DELETE 1
- **동일 유저 default 런 대비 (⚠ 백본 상이: Qwen vs 4o-mini — 프롬프트·백본 효과 미분리)**: 후보 1,286→362 (1/3.5), UPDATE 801→**33**. 문단형 자기완결 메모리는 기존 메모리와 부분 중복이 덜 잡혀 재작성 대신 ADD로 가는 구조 → 커스텀 프롬프트는 추출 개입이자 사실상 **재작성(drift) 억제 개입**. 유래별 분해(0점의 80%가 UPDATE 유래)와 직결되므로 full run에서 Acc 변화 주시

## 6-2. full run 조작 점검 (2026-07-24, 서버 Qwen3-4B 백본, Stage A 직후)

- 20유저 전원 문단형 준수 (유저 평균 366~2,186자), 빈 세션 총 4개 — **처치 성립 확인 후 A′ 진행**
- 총 후보 9,878개 = default 런(29,857)의 **1/3** — 스모크의 축소 비율(1/3.5)이 Qwen 백본에서도 재현. judge 콜 수가 비례 감소하므로 Stage B 비용도 크게 하락 예상
- Qwen은 mini(264자)보다 훨씬 장황하고 유저 간 편차 큼 (Martin Mark 평균 2,186자, Christopher Anderson 1,777자 = 세션 요약문 수준) — **"문단형 과잉 준수"**. accuracy 채점 단위가 커져 원자 단위 일부 오류로 1점 강등되기 쉬운 방향 → 정성분석 관전 포인트
- 초장문 유저의 A′ 프롬프트는 top-20 × ~2,000자 ≈ 1만 토큰 수준까지 가능하나 vLLM `--max-model-len 32768` 이내라 문제 없음

## 7. 결과 (실행 후 기입)

§4h 확장판 테이블에 행 2개 추가 예정 (20u·4B judge / 4u·nano judge, `Prompt=custom` 표기). 상세 분석은 이 문서에.

| System | # Users | Prompt | Judge | R↑ | Weighted R↑ | Acc.↑ (# mem) | Target P↑ (# mem) | FMR↑ | F1↑ | Upd C↑ | Upd H↓ | Upd O↓ | QA C↑ | QA H↓ | QA O↓ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Mem0-Classic-oss | 20 | default | `Qwen3-4B` | 29.57 | 45.51 | 16.52 (29,857) | 99.37 (3,885) | 71.87 | 45.58 | 30.40 | 3.27 | 66.34 | 48.95 | 37.64 | 13.41 |
| Mem0-Classic-oss | 20 | custom | `Qwen3-4B` | **68.99** | **74.75** | 22.66 (9,878) | 96.32 (1,659) | 61.52 | **80.40** | **58.20** | 2.53 | **39.24** | 48.37 | 39.78 | **11.85** |
| Mem0-Classic-oss | 4 | default | `GPT-5-Nano` | 26.65 | 49.38 | 30.12 (6,065) | 82.15 (1,583) | 63.16 | 40.25 | 8.57 | 0.17 | 88.91 | 42.84 | 29.08 | 27.94 |
| Mem0-Classic-oss | 4 | custom | `GPT-5-Nano` | **61.47** | **74.83** | 26.82 (1,734) | 90.14 (360) | **39.38** | **73.10** | 13.78 | 0.34 | 83.19 | 40.57 | 35.46 | 23.97 |
| *(참고)* Mem0-Classic | 20 | platform custom | `GPT-4o` | 42.91 | 65.03 | 60.86 (16,291) | 86.26 (10,556) | 56.80 | 57.31 | 25.50 | 0.45 | 74.02 | 53.02 | 19.17 | 27.81 |

(2026-07-24 실측. Upd/QA는 official 집계의 all-비율. nano 4u update에는 라벨 변형 무효 16건 존재 — valid 기준 C 14.16/O 85.49로 결론 불변. nano judge 무효율 전체 0.02%)

유래별 분해 (nano 4u):

| 유래 | default n / Acc (0점률) | custom n / Acc (0점률) |
|---|---|---|
| ADD | 2,094 / 48.6% (32.9%) | 460 / **54.5%** (29.3%) |
| UPDATE | 3,971 / 20.4% (69.9%) | 1,274 / **16.8%** (75.7%) |
| UPDATE 비중 | 65.5% | **73.5%** |

## 8. 분석 — 병목이 이동했다

**① 추출 커버리지: 대폭 개선 (가설 적중).** R 26.65→61.47 (nano, +34.8pt), F1 40.25→73.10. 논문 GPT-4o mem0(R 42.91)를 크게 상회, MemOS(74.07)에 근접. 기존 병목 1순위(추출 커버리지 — "F1 개선 ≡ R 개선")가 프롬프트 하나로 사실상 해소됨. 두 judge가 방향 일치(4B 68.99)라 judge 착시 아님.

**② FMR: 급락 39.38 ← 63.16 (가설 반박).** "user 발화만 추출" 지침으로 distractor 저항 상승을 기대했으나 정반대. 세션 요약문 수준의 문단이 assistant 제안(미확정 내용)까지 흡수하는 것으로 보임 — Qwen이 지침 4를 안 지키는지, distractor가 user 발화에 재언급된 것인지는 정성분석 필요. **커버리지↑ ↔ 오염↑ 트레이드오프가 새로 생김.**

**③ Acc: 소폭 하락 26.82 ← 30.12, 유래별로 정반대 방향.** ADD-유래는 48.6→54.5로 개선됐으나, UPDATE-유래는 20.4→16.8로 악화 + UPDATE 비중이 65.5→73.5%로 오히려 증가. ⚠ 스모크(mini 백본)의 "UPDATE 붕괴(801→33)"는 **Qwen 백본에서 재현되지 않음** — §6-1의 해당 추정은 백본 특이 현상이었음. Qwen은 문단끼리도 적극적으로 재작성하며, 긴 문단 재작성본은 과거 세션 내용을 더 많이 누적하므로 세션 단위 채점의 구조적 불리(§4i 유래별 분해의 (b))가 가중됨. 재작성 억제 과제는 그대로 유효, 오히려 더 중요해짐.

**④ Update C: 8.57→13.78 개선, 여전히 낮음.** 디테일 보존 지침의 방향은 맞았으나(Omission 88.9→83.2) 근본 해소엔 못 미침.

**⑤ QA: 정체 — 이 실험의 핵심 역설.** R이 2.3배가 됐는데 QA C는 42.84→40.57로 오히려 소폭 하락, H는 29.08→35.46으로 상승. **저장은 됐는데 검색·활용이 안 되는 상태로 병목이 이동한 것.** 유력 가설: (a) 문단 하나에 여러 주제가 뭉개져 임베딩 판별력 저하 → top-20에 정답 문단이 못 들어옴, (b) 들어와도 top-20 × 초장문 context에서 4B generator가 근거를 못 찾음, (c) 문단 속 오염(②)이 H 상승으로 발현. Target P 모수 급감(1,583→360)도 문단화가 필드 단위 대응을 깨뜨린 부작용. → **trace의 qa_retrieval hits로 (a)/(b) 분리 가능** (analyze_trace.py QA 실패 트리 재실행이 다음 단계).

**개선 우선순위 갱신**: 1) 저장 단위와 검색 단위의 불일치 해소 (문단 저장 + 원자 단위 검색 — 청킹/하이브리드), 2) 재작성 빈도·범위 억제, 3) distractor 오염 필터. 추출 프롬프트는 이미 승부가 났고, 다음 개입 지점은 검색·갱신이다.
