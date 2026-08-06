# 단계별 오라클 상한 사다리 — 설계·구현·실행

> 작성 2026-08-03. 관련 판독: [backbone-experiment.md](backbone-experiment.md) §17
> **목적**: mem0 파이프라인의 각 단계를 차례로 "완벽한 정답"으로 대체하며 QA 상한을 재고, **구간 차이로 각 단계의 기여분을 분해**한다.

## 1. 왜 필요한가

지금까지 "병목은 추출 커버리지"라는 결론은 간접 증거(실패 QA의 43~60%가 evidence 미저장)로만 세웠다. 단계를 하나씩 오라클로 바꿔가며 QA를 재면 **어느 단계를 고쳐야 몇 점을 얻는지가 뺄셈으로** 나온다.

| 세팅 | 오라클로 대체한 단계 | QA C | QA H | QA O | 직전 대비 | 구간이 뜻하는 것 |
|---|---|---|---|---|---|---|
| 실측 (`oss120b4`) | — | **58.54** | 23.2 | 18.3 | | |
| **E1** (`oe1`) | 추출 | **65.85** | 18.9 | 15.2 | **+7.31** | ← 추출 커버리지의 몫 |
| **E2** (`oe2`) | 추출 + 갱신 결정 | **73.17** | 17.7 | 9.2 | **+7.32** | ← 갱신 로직의 몫 |
| 추출+갱신+검색 오라클 (`genoracle`) | 추출 + 갱신 + 저장·검색 | **83.54** | 3.0 | 13.4 | +10.37 | ⚠ 검색 기여로 읽으면 안 됨 (§6-2③) |
| 이론상 만점 | + generator·문항 | 100 | | | +16.46 | ← generator + 벤치마크 결함의 몫 |

**총 격차 25.0p.** 단 앞 두 구간(추출 7.3 / 갱신 7.3)만 조건이 완전히 통제된 비교이며, 마지막 구간은 컨텍스트 조건이 달라(evidence 평균 1.4개 vs 실제 top-20) 검색 기여의 **상한**으로만 읽어야 한다 — 판독은 §6-2.

## 2. 통제 규약

- **유저**: Martin 1명 (기존 오라클 QA·judge 반복 실험과 동일 범위)
- **agent LLM**: gpt-oss-120b, **effort는 기본값(medium)** — 기존 그리드 실험과 통제를 맞추기 위함. effort=high로 올리면 오라클 효과와 effort 효과가 섞인다.
- **generator·judge**: gpt-oss-120b **high** 고정 (기존 oss120 레인과 동일)
- **임베더·top-k·Qdrant**: 전 실험 공통 고정

## 3. 오라클의 정의 (해석에 직결)

**추출 오라클**: 그 세션의 골든 메모리 포인트를 fact 추출 결과로 주입한다. **미끼(interference)는 제외** — 완벽한 추출기라면 담지 않아야 하는 항목이기 때문이다. 부작용으로 FMR이 100 근처가 되므로 **FMR 비교는 이 실험에서 무의미**하다.

**갱신 오라클**: 각 골든에 대해 `is_update=True`면 저장소에서 `original_memories`와 가장 비슷한 항목을 찾아 UPDATE, 아니면 ADD로 결정한다. 매칭은 토큰 자카드 유사도 0.45 이상.

### 3-1. 갱신 골든의 원본(`original_memories`) 실태 — 오라클의 한계를 정하는 조건

Martin 기준, 갱신 골든 142건의 `original_memories`가 **선행 세션의 골든 MP로 실재하는지** 교차 집계:

| 기재된 원본 수 ＼ 실재 수 | 0 | 1 | 2 | 3 | 합계 |
|---|---|---|---|---|---|
| 1개 기재 | 85 | 23 | – | – | 108 |
| 2개 기재 | 8 | 8 | 14 | – | 30 |
| 3개 기재 | 2 | 0 | 1 | 1 | 4 |
| **합계** | **95** | **31** | **15** | **1** | **142** |

원본 항목 단위로는 **기재 180개 중 64개(35.6%)만 실재**한다.

여기서 세 가지가 따라온다.

**① 갱신 골든의 66.9%(95건)는 원본이 저장소에 존재할 수 없다.** 기재된 원본이 선행 세션 골든에 하나도 없다는 뜻이고, 그 실체는 §17에서 확인한 **persona 프로필 필드 유래**다(예: `monthly_income "20000" → "23000"` — 이 수치는 64세션 대화에 한 번도 등장하지 않는다). 시스템은 물론이고 **오라클조차 갱신할 대상을 못 찾아 ADD로 처리**한다. 전 백본에서 Upd O가 높게 나오는 구조적 원인 중 하나다.

**② 실재 원본이 2개 이상인 16건은 현 오라클 구현에서 낡은 원본이 남는다.** mem0의 갱신 결정은 UPDATE 하나가 메모리 하나를 대체하는 구조이고, 현 구현은 가장 잘 맞는 원본 1개만 교체하므로 나머지는 저장소에 그대로 남는다(실측: oe2의 DELETE 연산 0건). 저장소 519개 중 최대 20개 안팎(≈4%)이며, 방향은 **E2를 과소평가**하는 쪽이다(낡은 원본이 top-20 검색을 오염시킬 수 있으므로).

**③ "완벽한 갱신"의 정의가 둘로 갈린다.** (a) *mem0가 할 수 있는 최선* — UPDATE 하나로 하나를 교체. 현 구현. (b) *이상적인 저장소* — 갱신 시점에 superseded 원본을 모두 제거해 저장소가 항상 "현재 진실"만 담게 함. 사다리의 취지("이 단계를 완벽히 하면 얼마나 오르나")에는 (b)가 부합하며, mem0의 갱신 결정 JSON이 `DELETE` 이벤트를 지원하므로 구현 가능하다 — 매칭된 나머지 원본을 DELETE로 함께 내보내면 된다. 두 정의의 차이 자체가 **"낡은 원본이 남는 것의 대가"** 라는 측정값이 되므로, 별도 행으로 추가해 비교할 가치가 있다.

> 러너는 `update_miss` 카운트를 출력하므로 ①의 규모를 실행 로그에서 확인할 수 있다.

## 4. 구현 (mem0 코드 무수정)

mem0 0.1.118의 `_add_to_vector_store`는 두 번의 LLM 호출로 이뤄진다.

| 호출 | messages 형태 | 반환 | 오라클 대체 |
|---|---|---|---|
| fact extraction | system + user | `{"facts": [...]}` | 세션 골든(미끼 제외) 주입 |
| update decision | user 1개 | `{"memory": [{id,text,event,old_memory}]}` | 정답 ADD/UPDATE 결정 주입 |

**system 역할의 유무로 두 호출을 구분**할 수 있다 (기존 `TracingLLM`이 이미 쓰는 방식). 따라서 LLM 래퍼 하나로 mem0 내부를 건드리지 않고 주입할 수 있다.

### 4-1. `src/mem0-classic-oss/oracle.py` (신규)

- `OracleLLM(inner, extraction=, update=)` — mem0의 LLM 인스턴스를 감싼다.
- `set_session(memory_points)` — 러너가 `memory.add` 직전 호출. 미끼 제외 후 facts 구성, `is_update` 골든의 `original_memories` 맵 구축.
- `parse_old_memories(prompt)` — update 결정 프롬프트에 파이썬 repr로 박혀 있는 기존 메모리 목록(`[{'id':'0','text':...}]`)을 백틱 블록에서 `ast.literal_eval`로 복원. 기존 메모리가 없으면 빈 목록.
- `stats` — `{sessions, facts, add, update, none, update_miss}` 조작 점검용.

### 4-2. `eval/mem0-classic-oss/eval_memzero_oss.py` (수정)

- `from oracle import OracleLLM`
- `process_user`: `memory.delete_all` 직후 env 플래그를 보고 `memory.llm = OracleLLM(memory.llm, ...)`. **TracingLLM보다 먼저 감싼다** — 그래야 `TracingLLM(OracleLLM(llm))` 순서가 되어 주입된 응답이 trace에 그대로 남고, 나중에 "무엇을 정답으로 넣었는지" 검증할 수 있다.
- 세션 루프: `memory.add` 직전에 `oracle.set_session(session["memory_points"])`
- 유저 완료 시 `[oracle:{user}] {stats}` 출력, `main` 시작 시 `oracle 단계: ...` 출력

### 4-3. 검증 결과 (실데이터 단위 테스트)

| 항목 | 결과 |
|---|---|
| 추출 오라클 — 미끼 제외 | 골든 11개(미끼 2) → 9개 주입 ✅ |
| 추출만 켰을 때 갱신 호출은 실제 LLM 통과 | ✅ |
| 실제 mem0 프롬프트 파싱 | 2건 복원 ✅ |
| 갱신 오라클 — 원본을 정확히 지목 | UPDATE id=0 ✅ |
| 빈 저장소(첫 세션) | 15건 전부 ADD ✅ |

## 5. 실행

### 5-1. 【서버】 E1 — 추출 오라클

```bash
cd ~/projects/agentic-memory && OPENAI_BASE_URL=http://localhost:8002/v1 MEM0_LLM_MODEL=openai/gpt-oss-120b MEM0_ORACLE_EXTRACTION=1 uv run python eval/mem0-classic-oss/eval_memzero_oss.py --version oe1 --user-num 1 --top-k 20 --max-workers 20 --trace
```

시작 로그에 `oracle 단계: 추출`, 종료 시 `[oracle:Martin Mark] {...}`가 찍혀야 한다.

### 5-2. 【서버】 E2 — 추출 + 갱신 오라클

```bash
cd ~/projects/agentic-memory && OPENAI_BASE_URL=http://localhost:8002/v1 MEM0_LLM_MODEL=openai/gpt-oss-120b MEM0_ORACLE_EXTRACTION=1 MEM0_ORACLE_UPDATE=1 uv run python eval/mem0-classic-oss/eval_memzero_oss.py --version oe2 --user-num 1 --top-k 20 --max-workers 20 --trace
```

시작 로그는 `oracle 단계: 추출 + 갱신결정`. LLM 호출이 사실상 사라져 매우 빠르다.

### 5-3. 【서버】 두 런의 A′ + judge (기존 oss120 레인 규약과 동일)

```bash
cd ~/projects/agentic-memory && for v in oe1 oe2; do echo "===== $v ====="; OPENAI_BASE_URL=http://localhost:8002/v1 ANSWER_MODEL=openai/gpt-oss-120b ANSWER_REASONING_EFFORT=high uv run python eval/mem0-classic-oss/gen_answers.py --results results/mem0-classic-oss/memzero-oss-$v/memzero-oss_eval_results.jsonl --user-num 1 --regen --max-workers 20 --out results/mem0-classic-oss/genoss120/$v.jsonl && OPENAI_BASE_URL=http://localhost:8002/v1 JUDGE_MODEL=openai/gpt-oss-120b JUDGE_REASONING_EFFORT=high uv run python eval/mem0-classic-oss/judge.py --results results/mem0-classic-oss/genoss120/$v.jsonl --user-num 1 --max-workers 20 --out-dir results/mem0-classic-oss/judge-oss120-genoss120-$v; done
```

### 5-4. 【로컬】 동기화

```bash
rsync -avz --include='memzero-oss-oe1/***' --include='memzero-oss-oe2/***' --include='judge-oss120-genoss120-oe1/***' --include='judge-oss120-genoss120-oe2/***' --exclude='*' Hamster:~/projects/agentic-memory/results/mem0-classic-oss/ "/Users/wonjinkim/Documents/Agentic Memory/agentic-memory/results/mem0-classic-oss/" && rsync -avz Hamster:~/projects/agentic-memory/results/mem0-classic-oss/genoss120/ "/Users/wonjinkim/Documents/Agentic Memory/agentic-memory/results/mem0-classic-oss/genoss120/" && rsync -avz --include='oe1/***' --include='oe2/***' --exclude='*' Hamster:~/projects/agentic-memory/traces/mem0-classic-oss/ "/Users/wonjinkim/Documents/Agentic Memory/agentic-memory/traces/mem0-classic-oss/"
```

## 6. 대시보드 반영

**Metrics 탭 최상단에 "단계별 오라클 상한 사다리" 카드**가 자동으로 뜬다. 각 행은 추출·갱신·저장검색 3칸이 `오라클`(보라) / `실측`으로 표시되고, QA C 막대와 **직전 단계 대비 증가분**이 붙는다. 아직 안 돌린 단계는 `미실행`으로 흐리게 표시되다가 산출물이 생기면 자동으로 채워진다.

정의는 `src/web-dashboard/runs.yaml`의 `oracle_ladder:` 섹션 한 곳에만 있고, 계산은 `/api/oracle-ladder`가 공식 집계 함수를 재사용한다. 유저 범위는 Metrics 탭의 "유저 범위" 선택을 따른다 (사다리는 Martin 1유저 기준으로 설계됨).

## 6-2. 판독 (2026-08-03)

**조작 점검 — 오라클이 실제로 걸렸다.**

| 런 | 저장 메모리 | 평균 길이 | 연산 | 골든 원문 일치 |
|---|---|---|---|---|
| 실측 `oss120b4` | 825 | 78자 | ADD 618 / UPD 207 / DEL 7 | — |
| E1 `oe1` | 486 | 122자 | ADD 399 / UPD 87 / DEL 12 | 91.4% |
| E2 `oe2` | 593 | 117자 | ADD 519 / UPD 74 / DEL 2 | **100%** |

미끼 제외 골든이 정확히 **593개**인데 E2의 저장물이 593개·전부 골든 원문과 일치한다 — 추출·갱신을 모두 오라클로 덮으면 저장소가 "정답 그 자체"가 됨이 확인된다. E1이 486개로 더 적은 것은 **실제 갱신 결정이 일부 골든을 UPDATE로 흡수(87건)하거나 DELETE(12건)했기 때문**이며, 이 손실이 곧 아래 ②의 7.3점이다.

**① 추출 오라클: +7.31 (58.54 → 65.85)** — 완벽한 추출로 얻는 몫. 예상보다 작다. §12⑤·§13에서 "실패 QA의 43~60%가 추출 누락"이라 관측했으나, **추출을 완전히 고쳐도 QA는 7점밖에 오르지 않는다.** 저장 자체는 됐어도 뒤 단계(갱신·검색)에서 다시 잃기 때문이다. "병목=추출"이라는 기존 진단은 **필요조건이지 충분조건이 아니었다**.

**② 갱신 오라클: +7.32 (65.85 → 73.17)** — 추출과 정확히 같은 크기다. 주목할 점은 이 구간에서 **QA Omission이 15.2 → 9.2로 급감(-6.0)**한다는 것. 갱신 결정이 잘못되면 답변 재료가 통째로 사라져 "누락"으로 나타난다는 뜻이며, §14에서 발견한 no-op UPDATE 문제(Qwen 계열 97%)가 QA에 미치는 실제 대가를 정량화한 값이다. **gpt-oss-120b는 no-op이 0%인데도 갱신에서 7.3점을 흘리고 있다** — 갱신 실패는 no-op만의 문제가 아니다.

**③ 검색 구간 +10.37 (73.17 → 83.54) — ⚠ 이 값은 검색의 기여로 읽으면 안 된다.** 마지막 행은 "완벽한 검색"이 아니라 **정답 근거만 남기고 나머지를 전부 제거한** 조건이다. 실측치로 확인하면:

| | 컨텍스트 항목 수 |
|---|---|
| 실제 검색 (E2 포함 전 행) | top-20 고정, 평균 **19.9개** |
| 검색 오라클 행 | evidence만, 평균 **1.42개** (0개 39문항 / 1~2개 100문항 / 3개 이상 25문항) |

즉 이 구간에는 **(a) 맞는 항목을 찾아오는 능력**과 **(b) 무관한 19개를 치워 컨텍스트를 짧고 깨끗하게 만드는 효과**가 섞여 있다. QA Hallucination이 17.7 → 3.0으로 붕괴하는 것도 (a)보다 (b)로 설명하는 편이 자연스럽다 — 잘못된 재료가 애초에 주어지지 않았으니 지어낼 거리가 없다. **따라서 +10.37은 검색 개선으로 얻을 수 있는 몫의 상한이며, 실제 검색 기여분은 이보다 작다.**

추가로 **합성 데이터셋 특유의 유리함**이 있다. 골든 정답은 바로 그 evidence로부터 만들어졌으므로, evidence를 그대로 건네는 것은 "완벽한 검색"이 아니라 **정답의 출처를 직접 넘겨주는 것**에 가깝다. 게다가 그 evidence·정답의 품질 자체가 검증 대상인 상황이다(§13: 12세팅 전멸 문항의 상당수가 정답 임의적·답변 불가). 이 행을 "달성 가능한 상한"으로 인용할 때는 이 두 겹의 낙관을 명시해야 한다.

**④ 남은 16.5p** — generator 자체의 한계와 벤치마크 결함(§13)의 몫. 오라클도 실패한 27/164 문항이 여기 해당한다.

**종합 — 안전하게 말할 수 있는 것과 없는 것**

말할 수 있는 것:
- **"병목=추출"이라는 단일 서사는 기각된다.** 추출을 완벽하게 만들어도 +7.3에 그친다.
- **추출과 갱신은 각각 7.3점으로 동등한 크기**이며, 이 둘은 조건이 완전히 통제된 비교다(같은 유저·문항·generator·judge, 컨텍스트 조건 동일 top-20). **갱신이 추출만큼 중요하다는 것이 이 실험의 가장 견고한 발견**이다.
- 갱신 구간에서 **QA Omission이 15.2 → 9.2로 급감(-6.0)** — 갱신 실패는 재료를 통째로 잃게 만든다. gpt-oss-120b는 no-op이 0%인데도 7.3점을 흘리므로, **갱신 실패는 §14의 no-op 문제만으로 환원되지 않는다.**

말할 수 없는 것:
- **"검색이 최대 병목"** — 마지막 구간은 컨텍스트 축약 효과와 합성 데이터셋의 유리함이 섞여 있어 검색 단독 기여를 분리하지 못한다.
- **총 25.0p의 세 단계 배분** — 앞 두 구간(7.3/7.3)은 통제됐으나 마지막 구간은 그렇지 않으므로, "7.3 : 7.3 : 10.4"를 나란히 놓고 비교하면 안 된다.

**⑤ 다음 실험 — 검색 기여분의 분리 측정 (미실행)**

컨텍스트 조건을 맞춘 대조군이 필요하다. E2의 저장소(정답만 들어 있는 상태)에서 **evidence + 무관 메모리를 섞어 top-20 규모로 채운 컨텍스트**를 만들어 답변시키면:
- 이 값 − E2(73.17) = **순수 검색 정확도의 기여**
- 83.54 − 이 값 = **컨텍스트 축약·정제의 기여**

`gen_answers.py`에 오라클 컨텍스트를 top-k 규모로 패딩하는 옵션(예: `ANSWER_ORACLE_PAD=20`)을 추가하면 바로 잴 수 있다.

⚠ 공통 단서: Martin 1유저 표본이며, §17-2의 judge 자기 비일관성(QA 다수결 이탈 3.8%)이 각 행에 얹혀 있다. 7.3 대 7.3의 동률은 우연일 수 있다.

## 7. 해석 시 주의

1. **QA C만 읽는다.** 오라클 행은 저장물이 골든 그 자체라 R·Acc·Target P가 100 근처로 붙어 무의미하다.
2. **FMR 비교 금지** — 오라클 추출이 미끼를 원천 제외하므로 구조적으로 100에 가깝다.
3. **E2는 백본 무관**하다. 추출·갱신 모두 오라클이면 LLM 호출이 사라져 순수한 "저장소+검색"의 상한이 된다. 백본 영향을 받는 것은 E1뿐(갱신 결정만 실제 LLM).
4. **1유저 표본**이다. §15에서 확인했듯 QA C 수 %p 차이는 노이즈일 수 있으므로, 구간 폭이 크게 벌어지는 경우에만 결론으로 삼는다.
5. **judge 자기 비일관성**(§17-2: QA 다수결 이탈 3.8%)이 각 행에 얹혀 있다. 3~4점 이내의 구간 차이는 판정 노이즈와 구분되지 않는다.
