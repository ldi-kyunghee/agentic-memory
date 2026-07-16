# Memory System Trace Schema (v1)

> 메모리 시스템의 내부 동작(LLM 호출·검색·상태 변화)을 단계별로 기록하는 trace JSONL의 명세.
> **모든 메모리 시스템 구현체와 trace 뷰어 대시보드가 공유하는 데이터 계약**이다.
> 새 메모리 시스템(BM25 naive, 기타 agentic memory 등)에 tracing을 붙일 때 이 문서만 보고 작업할 수 있어야 한다.
>
> 상태: **v1 확정 + mem0-classic-oss 참조 구현 검증 완료** (2026-07-16, 1-user trace smoke: 이벤트 1,279건, ref 부착 100%, omission 역추적 리허설 통과). `event` enum 변경은 이 문서 개정을 통해서만.

## 1. 설계 원칙 — 2층 구조

확장성의 핵심은 **닫힌 층과 열린 층의 분리**다:

- **닫힌 층** (`stage`, `event`): 소수 고정값. 대시보드 공통 뷰는 이것만 알고 렌더링한다.
  어떤 메모리 시스템이든 하는 일은 결국 ① LLM을 부르고(`llm_call`) ② 저장소를 조회하고(`retrieval`) ③ 메모리 상태를 바꾸는(`memory_write`) 것이라는 관찰이 근거.
- **열린 층** (`purpose`, `detail`): 시스템별 자유. 시스템 고유 연산 이름·부가정보는 여기 싣는다.
  대시보드 공통 뷰는 읽지 않고, drill-down 화면에서만 노출된다.

새 시스템을 추가할 때 **`event` 값을 새로 만들면 안 된다** — 필요하다고 느껴지면 대부분 `purpose`로 표현 가능하다. 정말 새 연산 범주가 필요하면 이 문서를 개정하고 대시보드 영향을 검토한다.

## 2. 레코드 명세

trace 한 줄(JSONL) = 이벤트 하나.

### 공통 envelope (모든 이벤트 필수)

| 필드 | 타입 | 설명 |
|---|---|---|
| `v` | int | 스키마 버전. 현재 `1` |
| `system` | str | 아키텍처 식별자. 예: `mem0-classic-oss`, `bm25-naive`, `mem0-additive` |
| `run` | str | 실행 버전 태그 (러너의 `--version` 등) |
| `user` | str | HaluMem 유저 uuid |
| `session` | int | 세션 인덱스 (0-base) |
| `seq` | int | 유저 내 이벤트 순번 (1-base, 단조증가) |
| `ts` | str | ISO8601 UTC |
| `stage` | enum | 프로토콜 단계. §3 참고 |
| `event` | enum | `llm_call` \| `retrieval` \| `memory_write` |
| `purpose` | str? | 시스템별 자유 라벨. 예: `fact_extraction`, `update_decision`, `chunk_store` |
| `duration_ms` | float? | 해당 연산 소요 시간 |
| `ref` | obj? | 골든 데이터 연결. `{"mp_index": 7}` (update_probe) 또는 `{"question": "..."}` (qa_retrieval) |
| `detail` | obj? | 시스템 고유 확장. 스키마 자유 |

### 이벤트별 페이로드 (event 값에 따라 정확히 하나)

**`llm_call`** — 시스템 내부의 모든 LLM 호출:

```jsonc
"llm": {"messages": [{"role": "...", "content": "..."}], "response": "..."}
```

프롬프트/응답은 **전문 저장** (대시보드 drill-down 원료. 자르지 말 것).

**`retrieval`** — 저장소 조회 (방식 불문):

```jsonc
"retrieval": {
  "method": "dense",        // dense | bm25 | graph | ... (score 해석 기준)
  "query": "...", "limit": 5,
  "hits": [{"id": "...", "text": "...", "score": 0.83}]
}
```

**`memory_write`** — 메모리 상태 변화 (정규화):

```jsonc
"writes": [
  {"op": "ADD",    "id": "...", "text": "...", "prev_text": null},
  {"op": "UPDATE", "id": "...", "text": "...", "prev_text": "이전 내용"},
  {"op": "DELETE", "id": "...", "text": "...", "prev_text": null}
]
```

시스템 API의 반환 형식이 무엇이든 이 형태로 변환해서 기록한다 (mem0의 event 리스트, BM25의 청크 저장 등).

## 3. `stage` — HaluMem 프로토콜 층

시스템 내부 연산이 아니라 **벤치마크 러너가 지금 뭘 하는 중인지**를 나타낸다. 러너가 컨텍스트로 주입하며, 시스템 무관하게 동일하다:

| stage | 의미 |
|---|---|
| `ingest` | 세션 대화를 시스템에 투입 (Add Dialogue). 이 동안 발생하는 모든 내부 연산에 붙음 |
| `update_probe` | 골든 update MP에 대한 top-10 검색 스냅샷 (벤치마크의 관측 행위) |
| `qa_retrieval` | 질문에 대한 top-20 검색 (답변 생성용 context 수집) |

## 4. 파일 배치

- 경로: `traces/{run}/{user_uuid}.jsonl` — 유저당 1파일 (병렬 워커 격리 목적)
- gitignore 영역 (용량 큼). 서버↔로컬 이동은 rsync/scp
- 크래시 대비 라인 단위 flush 권장

## 5. 새 시스템에 tracing 추가하기 (협업자 가이드)

참조 구현: `src/tracing.py` (TraceLogger + 래퍼) 및 `eval/eval_memzero_oss.py`의 통합 지점.

1. **TraceLogger는 그대로 재사용** — `system=` 식별자만 자기 것으로.
2. **관측 지점 매핑표부터 작성**: 내 시스템의 어떤 함수 호출이 `llm_call` / `retrieval` / `memory_write`에 해당하는가. 예시:

   | | mem0-classic | mem0 2.x (additive) | BM25 naive |
   |---|---|---|---|
   | llm_call | fact 추출, update 결정 (2회/ingest) | additive 추출 (1회) | 없음 (0건도 유효) |
   | retrieval | dense 후보검색 + probe/QA 검색 | dense | method=`bm25` |
   | memory_write | ADD/UPDATE/DELETE | ADD (+`detail.linked_memory_ids`) | ADD (purpose=`chunk_store`) |

3. **구현 패턴은 인스턴스 래핑 권장**: 시스템 라이브러리를 포크/수정하지 말고, 인스턴스의 llm/저장소 속성을 위임 래퍼로 교체 (`__getattr__`로 나머지 위임). 라이브러리 버전 변화에 결합되지 않는다.
4. **stage/ref는 러너가 주입**: 러너의 세션 루프·probe 루프·질문 루프 진입 시 `tracer.set_context(...)`. 래퍼는 stage를 몰라도 된다.
5. **검증 체크리스트**:
   - [ ] ingest 1회의 이벤트 시퀀스가 시스템 설계와 일치 (예: mem0-classic은 llm→retrieval×N→llm→write)
   - [ ] `purpose` 라벨이 매핑표와 일치, `ref`가 update_probe/qa_retrieval에 붙음
   - [ ] update 실패 사례 하나를 trace만으로 재구성 가능 (probe hits에 old memory 부재 → ingest 추출 누락 소급)
   - [ ] 대시보드 공통 뷰 필드(envelope + retrieval.hits + writes)만으로 타임라인이 그려짐 (`detail` 없이)

## 6. 경계: judge/답변 생성은 trace 대상이 아님

trace는 **메모리 시스템 자신의 동작**만 담는다. 채점(judge)과 QA 답변 생성은 시스템 밖의 평가 인프라이므로 trace하지 않고, 분석/대시보드에서 **3층 조인**으로 결합한다:

| 층 | 위치 | 조인 키 |
|---|---|---|
| trace | `traces/{run}/{uuid}.jsonl` | `user`, `session`, `ref.mp_index`/`ref.question` |
| 평가 산출물 (context/답변/이벤트) | `results/.../*_eval_results.jsonl` | uuid, 세션, mp index, question |
| judge 판정 | `results/.../judge/{uuid}.json` | `uuid`, `session_id`, `index`/`question` |

이 경계 덕에 judge를 교체·재채점해도 trace는 불변이고, 시스템 간 trace 비교가 공정해진다.
(답변 생성을 trace에 포함하고 싶어지면 `stage=qa_answer` + `purpose=answer_generation`으로 스키마 변경 없이 수용 가능 — 현재는 보류.)

## 7. 대시보드 계약

공통 뷰가 읽는 것: **envelope 전체 + `retrieval.hits` + `writes`**. 이것만 올바르면 새 시스템은 코드 수정 없이 대시보드에 나타난다. `llm.messages/response`와 `detail`은 drill-down 전용.
