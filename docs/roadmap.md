# Roadmap — mem0 × HaluMem × Trace Dashboard

> 프로젝트 전체 조망. 상세 설계·검증 기록은 [mem0-halumem-baseline.md](mem0-halumem-baseline.md), trace 데이터 계약은 [trace-schema.md](trace-schema.md).
> 마지막 갱신: 2026-07-16

## 최종 목표

특정 입력에 대해 다양한 메모리 아키텍처가 정보를 어떻게 **분해/추출 → 저장/업데이트 → 회수 → 답변**하는지 시각적으로 탐색하는 **web demo dashboard**. (GitHub 부모 이슈 #3)

## 3개 기둥

### 기둥 1 — 평가 파이프라인 ✅ 완료

"시스템이 얼마나 잘하나"를 만드는 인프라. HaluMem 프로토콜 충실 재현.

| 구성 | 파일 | 상태 |
|---|---|---|
| Stage A: 세션 투입·추출·검색 수집 | `eval/eval_memzero_oss.py` | ✅ 유저 병렬, 서버 검증 완료 |
| Stage A': 답변 일괄 생성 | `eval/gen_answers.py` | ✅ |
| Stage B: judge 채점·집계 | `eval/judge.py` | ✅ 공식 채점기와 교차검증 |
| 서빙 자동화 | `scripts/serve.sh` | ✅ Blackwell 이슈 3종 해결 |
| 20유저 풀런 (Qwen3-4B 스택) | `results/memzero-oss-full/` | ✅ 산출물 확보 |

**[미결] judge 표준화**: mini/4B/30B가 서로 다른 채점 기질 (Upd C 9.7↔35↔57%). 풀런 수치는 잠정치. 확정 필요 시 GPT-4o 앵커 실험(~$3). 내부 분석은 30B 라벨 사용.

### 기둥 2 — Traceability 🔄 진행 중 (서브이슈 #8 — analyze_trace 포함, 체크리스트 마지막 항목)

"왜 그 숫자가 나오나"를 담는 데이터 계층. 스키마 v1 확정 ([trace-schema.md](trace-schema.md)).

| 구성 | 파일 | 상태 |
|---|---|---|
| 기록 계층: LLM/검색/상태변화 trace | `src/tracing.py` + 러너 `--trace` | ✅ 1유저 스모크 검증 (이벤트 1,279건) |
| 소비 계층: 인과 분석 | `src/analyze_trace.py` | ✅ ⓐⓑⓒ 검증 완료 + 임베딩 매처 표준화 (baseline §4f: extraction_miss 46% / decision_miss 48%, retrieval 병목 아님) |
| Qwen 스택 traced 데이터 (3유저) | 서버 `full-traced` 런 | ✅ traces + 4B judge 라벨 확보 |

**analyze_trace.py의 존재 이유** (= 대시보드에서 UI를 뺀 것):
1. 대시보드 착수 전 trace 데이터의 유효성 검증 (최저비용 리허설)
2. 대시보드 백엔드 로직의 프로토타입 — ⓐ 유실 UPDATE 정량화(LLM 의도 vs 실제 적용 대조), ⓑ Omission 원인 분류(`decision_miss / extraction_miss / overwritten / retrieval_miss` — 3층 조인)가 곧 대시보드 drill-down 기능의 코어
3. 연구 산출물 — "Omission의 X%는 추출 누락" 인과 정량화 (논문은 집계 수준 추정만 함)

### 기둥 3 — Web Dashboard ⬜ 미착수 (서브이슈: dashboard)

3층 데이터(trace / 평가 산출물 / judge 판정)를 조인해 탐색하는 UI.
핵심 시나리오: 틀린 QA 클릭 → 답변·판정·근거 확인 → context에 정보가 있었나 → 검색 랭킹 문제였나 → 원 세션 추출 누락까지 소급. 다중 아키텍처 비교(BM25 naive, mem0 2.x 등)는 trace-schema의 2층 설계로 수용.

## 당면 작업 순서

1. (맥) `eval/analyze_trace.py` 작성 → ⓐ를 trace-smoke로 무료 검증 → push
2. (서버) pull → `full-traced` 3유저 런 + gen_answers + 30B judge (전부 무료)
3. (맥) traces·판정 회수 → ⓑ 검증 + linkage 통계 → **기둥 2 완료**
4. 대시보드 설계 착수 (요구사항 → 스택 선정 → 프로토타입)

## 미래 확장 (기둥 3 이후)

- 비교 아키텍처 추가: BM25 naive (동료 작업과 합류), mem0 2.x additive, backbone ablation (4B→8B/30B)
- judge 표준화 확정 + 논문 대비 최종 보고
- HaluMem-Long 확장
