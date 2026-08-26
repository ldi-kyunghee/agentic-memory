# LIGHT (BEAM 논문) 리뷰: 구현 전 이해

출처: Tavakoli et al., *Beyond a Million Tokens: Benchmarking and Enhancing Long-Term Memory in LLMs* (ICLR 2026).
논문 arXiv:2510.27246 · 코드 github.com/mohammadtavakoli78/BEAM (커밋 3e12035, 이미 `BEAM/` 에 받아둠)

**별도 저장소는 없다.** LIGHT 구현은 BEAM 벤치마크 저장소 안에 있고, 파일 하나다:
`BEAM/src/answer_probing_questions/light.py` (542줄) + `long_term_memory_methods.py` 의 검색·조립부.

---

## 0. 한 줄 요약, 그리고 왜 이게 중요한가

**LIGHT 는 mem0 같은 메모리 저장소가 아니라 추론 시점 파이프라인이다.**

mem0 는 `add(대화) → 저장소 갱신`, `search(질문) → top-k` 의 온라인 API 다. LIGHT 는 대화 전체를
미리 훑어 **세 가지 산출물**을 만들어 두고, 질문마다 그 셋을 합쳐 컨텍스트를 조립한다.
저장소를 갱신한다는 개념 자체가 없다 (ADD/UPDATE/DELETE 가 없다).

이 차이가 우리 하네스에 그대로 걸린다. §5 참조.

---

## 1. 세 가지 기억 (+ 논문이 안 세는 네 번째)

### 1-1. Episodic memory (장기)

`create_episodic_memory()` · 대화의 **user/assistant 짝(pair)마다** LLM 을 한 번 불러
`Key: Value` 목록과 한 줄 요약을 뽑는다. 직전 대화 이력을 함께 넣어 문맥을 준다.

- 뽑은 key-value 텍스트를 **임베딩해 색인**하고, 값으로는 **원문 짝**을 들고 있는다
- 질문이 오면 key-value 색인에서 top-k 를 찾고, **원문 짝을 꺼내 쓴다** (색인은 key-value, 반환은 원문)
- 입력 절단: assistant 발화가 24,000 토큰을 넘으면 70,000자에서 자른다. 이력은 27,000 토큰에 맞춰 뒤에서 자른다

> mem0 의 "사실 추출" 과 겉모습이 비슷하지만, **추출물로 검색하고 원문을 돌려준다**는 점이 다르다.
> mem0 는 추출물 자체를 돌려준다.

### 1-2. Working memory (단기)

`create_working_memory()` · **가장 최근 짝 100개를 가공 없이 그대로** 들고 있는다
(`chunks[-100:]`). LLM 호출이 없다. 검색도 안 한다. 무조건 컨텍스트에 들어간다(예산 안에서).

### 1-3. Scratchpad (누적 요약)

`create_scratch_pad()` · 짝마다 LLM 을 한 번 불러 "사실·지시·갱신" 을 구조화해 뽑고,
그것들을 **순서대로 이어붙이다가 길이가 넘으면 요약해 접는다**.

- 접는 기준: 누적 길이가 `tokens_limit*2` = **28,000 토큰**을 넘으면 → **14,000 토큰**으로 압축
- 압축 프롬프트가 요구하는 출력 골격이 고정돼 있다: 핵심 개체·결정/선호·절차·사용자 선호·
  사용자 지시·중요 날짜·핵심 맥락·해야 할 일
- 모드가 둘인데 (`all_at_once` / `iterative`) 코드는 **`iterative` 로 고정**돼 있다

### 1-4. Noise filtering (질의 시점 선별)

논문 abstract 는 셋만 말하지만 **ablation 에는 네 번째로 들어 있고, 실제로 비용이 제일 크다.**

질문이 올 때마다:
1. scratchpad 를 **SemanticChunker** 로 쪼갠다 (percentile 80 기준)
2. **쪼갠 조각마다 LLM 에 yes/no 를 묻는다** ("이 조각이 이 질문에 필요한가")
3. yes 인 것만 이어붙여 scratchpad 자리에 넣는다

`noise_handling_type` 이 1/2/3 세 갈래인데 코드는 **1 로 고정**이다.

---

## 2. 컨텍스트 조립 순서

`noise_filtering()` 이 마지막에 만드는 문자열 순서가 이렇다. **예산 14,000 토큰**(`reader_max_tokens`).

```
[episodic 검색 결과 원문들]   ← 예산 넘으면 거기서 중단
[working memory 최근 100짝]   ← 남은 예산 안에서
SCRATCH PAD: [필터 통과한 조각들]  ← 예산 검사 없이 항상 붙음
```

⚠ **scratchpad 는 예산 검사를 안 받는다.** 앞의 둘만 14K 로 잘리고 scratchpad 는 그 뒤에 통째로
붙는다. 실제 컨텍스트는 14K 를 넘을 수 있다.

⚠ 코드에 `reader_max_tokens = 14000` 옆에 `# reader_max_tokens = 28000` 이 주석으로 남아 있다.
논문 수치가 어느 쪽으로 나온 것인지 코드만으로는 못 가른다.

---

## 3. 쓰는 모델

| 역할 | 모델 | 어디서 |
|---|---|---|
| episodic key-value 추출 | Qwen2.5-32B-AWQ | 짝마다 1콜 |
| scratchpad 추출 | Qwen2.5-32B-AWQ | 짝마다 1콜 |
| scratchpad 압축 | GPT-4.1-nano (논문) / **gpt-4.1-mini (코드 기본값)** | 28K 넘을 때마다 |
| noise filtering yes/no | Qwen2.5-32B-AWQ | 질문 × scratchpad 조각 |
| 최종 답변 | 실험 변인 (GPT-4.1-nano, Gemini 2.0-flash, Qwen2.5, Llama-4-Maverick) | 질문마다 1콜 |
| 임베딩 | BAAI/bge-small-en-v1.5 (dense 갈래) | FAISS |

⚠ 코드에는 임베더가 **두 개** 나온다. 검색은 `bge-small-en-v1.5`, scratchpad 의 SemanticChunker 는
`bge-large-en-v1.5` 다. 논문은 small 하나만 말한다.

---

## 4. 논문 대 코드 불일치 (재현 전에 정해야 할 것)

| 항목 | 논문 | 코드 | 우리가 정할 것 |
|---|---|---|---|
| scratchpad 압축 임계 | 30K → 15K | **28K → 14K** | 코드를 따른다 (실행된 쪽) |
| scratchpad 압축 모델 | GPT-4.1-nano | **gpt-4.1-mini** | 우리 레인은 gpt-oss-120b 로 통일 |
| 검색 k | **15가 최적**이라 보고 | 기본값 **5** | 15 로 맞춘다 (논문 결론) |
| working memory 크기 | "최근 z 짝" (z 미명시) | **100** | 100 |
| reader 예산 | 명시 없음 | 14,000 (28,000이 주석) | 14,000 으로 시작 |
| 검색 임베더 | bge-small | bge-small(검색) + bge-large(청커) | 우리는 Qwen3-Embedding-4B 하나로 통일 |

---

## 5. 우리 하네스에 안 맞는 지점 (제일 중요)

**우리 세 벤치마크 하네스는 전부 `build_memory()` → `add()` → `search()` 를 전제로 짜여 있다.**
`MEM0_IMPL` 스위치도 그 계약 위에 있다 (`eval/mem0-v3/compat.py` 가 v3 API 를 그 모양으로 번역함).

LIGHT 는 그 계약에 안 맞는다.

1. **`add()` 가 없다.** 대화 전체를 미리 다 보고 scratchpad 를 순서대로 접어야 한다.
   세션을 하나씩 넣는 온라인 갱신이 아니다.
2. **`search()` 가 질문마다 LLM 을 여러 번 부른다.** 우리 하네스는 search 를 순수 검색으로 보고
   비용을 안 센다. LIGHT 는 여기가 제일 비싸다.
3. **투입 단위가 벤치마크마다 mem0 와 어긋난다.** §5-1 에서 수치로 본다.

→ **LIGHT 는 `compat.py` 같은 얇은 어댑터로 못 감싼다.** 별도 갈래가 필요하다.

### 5-1. 투입 단위: BEAM 에서만 두 시스템이 맞는다 (2026-08-26 실측)

LIGHT 의 처리 단위는 **user/assistant 짝(pair)** 이다. 우리 mem0 하네스의 단위는 벤치마크마다 다르다.

| 벤치마크 | mem0 투입 단위 | 단위 수 | mem0 투입 콜 | LIGHT 짝 수 | LIGHT 투입 콜 | 배수 |
|---|---|---|---|---|---|---|
| **BEAM 100K** | 청크(발화 2개) | 2,866 | 5,732 | 2,866 | 5,732 | **1.0배** |
| **BEAM 500K** | 청크(발화 2개) | 19,029 | 38,058 | 19,029 | 38,058 | **1.0배** |
| HaluMem | 세션 | 1,387 | 2,774 | 30,073 | 60,146 | **21.7배** |
| Memora weekly | 세션 | 1,551 | 3,102 | 12,226 | 24,452 | 7.9배 |
| Memora monthly | 세션 | 6,150 | 12,300 | 47,972 | 95,944 | 7.8배 |
| Memora quarterly | 세션 | 19,913 | 39,826 | 155,859 | 311,718 | 7.8배 |

(mem0 classic 은 단위마다 2콜, LIGHT 는 짝마다 2콜(episodic 1 + scratchpad 1)로 잡음)

**BEAM 은 `ingest_beam.py` 의 `CHUNK_SIZE = 2` 라 우리 청크가 곧 LIGHT 의 짝이다.**
그 값은 mem0 팀 하네스에 맞춘 것인데, 우연히 LIGHT 의 단위와도 같다.
**두 시스템이 글자 그대로 같은 조각을 같은 순서로 받는다.**

HaluMem·Memora 는 우리가 세션을 통째로 `add()` 한다. LIGHT 를 그 단위로 돌리면
스펙에서 벗어나고(짝 단위 추출이 세션 단위 요약이 됨), 짝 단위로 돌리면 mem0 와 투입 단위가
달라진다. **어느 쪽을 골라도 설명이 붙는 선택지지 그냥 되는 일이 아니다.**

> 단위를 짝으로 맞추는 쪽이 각 시스템의 레퍼런스 구성을 지키는 것이라 더 방어하기 쉽다.
> 대신 HaluMem 투입 비용이 21.7배가 된다.

### 5-2. k 축은 검색 예산만 같고 총 컨텍스트는 같지 않다

우리 cutoff 그리드(20/50/100/200)를 LIGHT 에도 그대로 걸 수 있다. 다만 **LIGHT 의 k 는
episodic 검색에만 걸린다.** working memory(짝 100개)와 scratchpad 는 k 와 무관하게 항상 들어간다.

→ 같은 k 에서 **LIGHT 쪽 컨텍스트가 항상 더 크다.** 표에 k 를 나란히 놓되
"검색 예산은 같고 총 컨텍스트는 LIGHT 가 크다" 를 각주로 박는다.

---

## 6. 비용 감각 (실행 전에 반드시 볼 것)

호출 수를 짝(pair) 수 `P`, 질문 수 `Q`, scratchpad 조각 수 `C` 로 두면

```
투입:  2P (episodic 1 + scratchpad 1)  +  압축 (28K 넘을 때마다 1)
질의:  Q × (C + 1)      ← C 는 scratchpad 를 semantic chunk 로 쪼갠 개수
```

**질의 쪽이 문제다.** scratchpad 14,000 토큰을 percentile-80 으로 쪼개면 조각이 수십 개 나온다.
조각당 yes/no 를 LLM 에 물으므로 질문 하나에 LLM 호출이 수십 번이다.

BEAM 100K 를 우리 규모(20대화 400문항)로 잡고 조각을 30개로 가정하면

| 단계 | 호출 수 |
|---|---|
| 투입 | 2 × (짝 수) |
| 질의 필터 | 400 × 30 = **12,000** |
| 답변 | 400 |

⚠ **이 추정은 조각 수를 가정한 것이다.** 실제 조각 수는 대화마다 다르므로
**대화 한 개로 축소 실행해 `C` 를 먼저 재고** 전체 비용을 다시 계산한다
(`docs/mem0-classic-oss/long-run-checklist.md` §0).

---

## 7. 논문이 보고한 수치

BEAM 전체 평균 (10능력 평균). 우리 표와 견줄 때는 **컨텍스트 예산이 다르다는 점을 먼저 본다** ·
`docs/mem0-classic-oss/beam-experiment.md` §5 참조.

| 규모 | GPT vanilla | GPT RAG | **GPT LIGHT** |
|---|---|---|---|
| 100K | 0.239 | 0.309 | **0.345** |
| 500K | 0.194 | 0.314 | **0.335** |
| 1M | 0.191 | 0.302 | **0.336** |
| 10M | 0.109 | 0.218 | **0.226** |

ablation 은 **10M 에서 모든 구성요소가 커진다**고 보고한다 (검색 −8.5%, noise filtering −8.3%,
working memory −5.7%, scratchpad −3.7%). 100K 에서는 검색 제거가 오히려 +0.28% 로,
**작은 규모에서는 세 기억의 값어치가 거의 없다**는 뜻이다.

> 우리 실험은 100K 버킷만 돌렸다. 이 표대로면 **LIGHT 의 이점이 가장 안 보이는 구간**이다.
> LIGHT 를 붙일 때 500K 를 함께 돌릴지 정해야 한다.

---

## 8. 붙일 때 정할 것 (구현 전 결정 목록)

1. **어느 벤치마크부터**: BEAM 100K 가 자연스럽다 (LIGHT 가 그 자료구조로 짜여 있음).
   HaluMem·Memora 는 chunking 을 새로 써야 한다.
2. **500K 도 돌릴지**: §7 대로면 100K 만으로는 LIGHT 의 값어치가 안 드러난다.
3. **모델 통일**: 우리 레인은 agent·답변·채점 전부 gpt-oss-120b(high) 다. LIGHT 의 Qwen2.5-32B·
   GPT-4.1-mini 자리를 전부 gpt-oss-120b 로 바꾼다. **그래야 mem0 두 벌과 같은 표에 놓인다.**
4. **임베더 통일**: bge-small/bge-large 대신 Qwen3-Embedding-4B.
5. **k**: 논문 결론인 15 로 간다 (코드 기본값 5 아님).
6. **noise filtering 을 켤지**: 비용의 대부분이 여기다. 끄면 싸지지만 논문 구성이 아니게 된다.
   **끄지 않는다.** 대신 축소 실행으로 비용을 먼저 잰다.

