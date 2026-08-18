# Agentic Memory Management

### 참고 사항

1. 레포 clone 시 `--recurse-submodules` 플래그를 붙어 실행하세요

```git
git clone --recurse-submodules https://github.com/ldi-kyunghee/agentic-memory.git
```

2. HaluMem 레포는 전체 데이터셋이 포함되지 않아 허깅페이스 레포를 다운받아 `dataset/`에 놓았습니다

---

## mem0-classic-oss

OSS mem0(0.1.118) × 로컬 vLLM으로 HaluMem 평가를 프로토콜 충실하게 수행하는 파이프라인.
프로젝트 전체 조망은 [docs/mem0-classic-oss/roadmap.md](docs/mem0-classic-oss/roadmap.md), 설계 배경·검증 기록은 [docs/mem0-classic-oss/mem0-halumem-baseline.md](docs/mem0-classic-oss/mem0-halumem-baseline.md), trace 데이터 계약은 [docs/mem0-classic-oss/trace-schema.md](docs/mem0-classic-oss/trace-schema.md), 정성분석 안내는 [docs/mem0-classic-oss/qualitative-analysis-guide.md](docs/mem0-classic-oss/qualitative-analysis-guide.md) 참고.

### 파이프라인 단계

HaluMem 원본은 2단계(시스템 구동 → 채점)인데, 우리는 답변 생성을 분리해 3단계로 재구성했다 (배치화·재채점 자유 목적):

| 단계 | 이름 | 하는 일 | 산출 |
|---|---|---|---|
| **Stage A** | 메모리 구동 | 세션 대화를 시간순으로 mem0에 투입 (추출→갱신 발생), 골든 update MP별 top-10 검색 스냅샷과 질문별 top-20 검색 context를 수집 | 평가 산출물 jsonl (+`--trace` 시 trace) |
| **Stage A'** | 답변 생성 | A가 저장해둔 context+질문으로 QA 답변을 일괄 생성해 같은 jsonl에 채움 | system_response |
| **Stage B** | 채점 | LLM judge가 4개 과제(integrity/accuracy/update/QA)를 레코드 단위 판정 후 집계 | judge 라벨 + 성적표 |

### 구성

| 파일 | 역할 |
|---|---|
| `eval/mem0-classic-oss/eval_memzero_oss.py` | Stage A: 세션 투입, 메모리 추출/업데이트, 검색 context 수집 (유저 병렬, `--trace` 지원) |
| `eval/mem0-classic-oss/gen_answers.py` | Stage A': 저장된 context로 QA 답변 일괄 생성 |
| `eval/mem0-classic-oss/judge.py` | Stage B: 4종 judge 채점 + 집계 (공식 evaluation.py의 structured-output 강건판) |
| `src/mem0-classic-oss/tracing.py` | trace 기록 계층: mem0 내부 LLM/검색/상태변화를 스키마 v1로 기록 |
| `src/mem0-classic-oss/analyze_trace.py` | trace 소비 계층: 유실 액션 정량화, Omission/QA 실패 원인 분류 (다유저 집계) |
| `scripts/mem0-classic-oss/serve.sh` | Qdrant docker + vLLM 서빙 2종(LLM/임베딩) 원커맨드 기동·종료 |
| `gpu/mem0-classic-oss/` | vLLM 전용 독립 uv 프로젝트 (루트와 openai 버전 충돌로 분리) |

### 서버 실행 절차

```bash
# 0) 최초 1회: 클론 & 환경
git clone --recurse-submodules https://github.com/ldi-kyunghee/agentic-memory.git && cd agentic-memory
uv sync                          # 루트 (mem0 러너/judge)
(cd gpu/mem0-classic-oss && uv sync)              # vLLM 설치 (괄호=서브셸: 실행 후 cwd는 루트 유지)
# 리포 루트에 .env 생성: 아래 "서버 .env 핵심값" 블록을 그대로 복사해서 저장
#   (스크립트/러너 모두 루트에서 실행하며 루트의 .env를 읽음. gpu/에는 .env 불필요)

# 1) 서빙 기동 (Qdrant + vLLM LLM/임베딩, ready까지 대기)
scripts/mem0-classic-oss/serve.sh <빈_GPU_번호>    # 예: scripts/mem0-classic-oss/serve.sh 2 / 로그: tmux attach -t vllm-serve

# 2) Stage A: 스모크(1유저) 먼저, 이상 없으면 풀런(20유저). trace가 필요하면 --trace
uv run python eval/mem0-classic-oss/eval_memzero_oss.py --user-num 1 --version srv-smoke 2>&1 | tee logs/srv-smoke.log
uv run python eval/mem0-classic-oss/eval_memzero_oss.py --max-workers 10 --version full --trace 2>&1 | tee logs/full-run.log

# 3) Stage A': 답변 생성
uv run python eval/mem0-classic-oss/gen_answers.py --results results/mem0-classic-oss/memzero-oss-full/memzero-oss_eval_results.jsonl --max-workers 32

# 4) Stage B: 채점 + 집계
uv run python eval/mem0-classic-oss/judge.py --results results/mem0-classic-oss/memzero-oss-full/memzero-oss_eval_results.jsonl --max-workers 32

# 5) (선택) trace 인과 분석: 임베딩 서버(:8001)가 떠 있어야 함
uv run python src/mem0-classic-oss/analyze_trace.py --trace traces/mem0-classic-oss/full --judge results/mem0-classic-oss/memzero-oss-full/judge \
  --matcher embed --out reports/mem0-classic-oss/trace_analysis_full.json

# 6) GPU 반납
scripts/mem0-classic-oss/serve.sh stop            # Qdrant는 유지됨. 내리려면: docker stop qdrant
```

### 산출물 구조

`--version full` 기준 (다른 버전은 이름만 바뀜):

```
agentic-memory/
├── dataset/HaluMem-Medium.jsonl          # 입력 (벤치마크 원본: 골든의 원출처)
│
├── results/mem0-classic-oss/memzero-oss-full/             # Stage A/A'/B 산출물 (gitignore)
│   ├── tmp/
│   │   ├── {uuid}.json                   #   유저별 중간 산출물 = resume 캐시
│   │   ├── {uuid}_error.log              #   실패 유저 traceback (없어야 정상)
│   │   └── history_{uuid}.db             #   mem0 내부 SQLite 이벤트 이력 (부산물)
│   ├── memzero-oss_eval_results.jsonl    #   ★ 평가 산출물 본체 (tmp 병합; A'가 답변을 in-place 추가)
│   │                                     #     대화 원문·골든 MP·추출 메모리·이벤트·검색 스냅샷·QA context/답변
│   ├── judge/{uuid}.json                 #   ★ judge 레코드별 판정 (유저 캐시 겸용)
│   └── eval_stat_result.json             #   집계 성적표 (12개 지표)
│
├── traces/mem0-classic-oss/full/                          # ★ trace: --trace 켰을 때만 (gitignore)
│   └── {uuid}.jsonl                      #   내부 동작 기록 (docs/mem0-classic-oss/trace-schema.md 스키마 v1)
│
├── reports/mem0-classic-oss/trace_analysis_*.json         # 인과 분석 결과 (git 커밋 대상)
├── logs/*.log                            # 실행 로그 (gitignore)
└── (docker) qdrant_storage 볼륨          # 벡터 저장소 실체: 컬렉션 halumem_full_{uuid}
```

데이터 흐름:

```
dataset ─Stage A─> results/.../tmp/{uuid}.json ─병합─> *_eval_results.jsonl ─A'─> (답변 추가) ─B─> judge/ + eval_stat_result.json
             └─(--trace)─> traces/mem0-classic-oss/{run}/{uuid}.jsonl
traces + judge + eval_results ─analyze_trace─> reports/mem0-classic-oss/trace_analysis_*.json
```

분석(인과 분류·대시보드)이 조인하는 3층 데이터와 실제 위치:

| 층 | 내용 | 위치 | 만든 주체 |
|---|---|---|---|
| trace | 시스템 내부 동작의 확정적 기록 (LLM 프롬프트/응답, 검색 hits, 상태 변화) | `traces/mem0-classic-oss/{run}/{uuid}.jsonl` | Stage A의 `--trace` |
| 평가 산출물 | 대화 원문 + 골든(메모리·이전버전·evidence) + 시스템 추출/이벤트/검색/답변 | `results/.../memzero-oss_eval_results.jsonl` | Stage A 생성, A'가 답변 추가 |
| judge 라벨 | 레코드별 판정 (실패 케이스 선정 필터) | `results/.../judge/{uuid}.json` | Stage B |

조인 키: `user(uuid)` + `session` + (`mp index` 또는 `question`): 상세는 [docs/mem0-classic-oss/trace-schema.md](docs/mem0-classic-oss/trace-schema.md) §6.

- 골든 데이터(memory_points, original_memories, evidence)는 dataset에서 `*_eval_results.jsonl` 안으로 복사돼 내장됨. 분석 단계에서 dataset을 다시 읽을 필요 없음
- git 커밋 대상은 `reports/`와 `docs/`뿐. `results/`·`traces/`·`logs/`는 gitignore이며 서버↔로컬 이동은 scp
- 서버에서 생성한 리포트를 scp로 가져와 로컬에서 커밋한 뒤에는, 서버 쪽 사본을 `git checkout -- reports/...`로 원복해둘 것 (다음 pull 충돌 방지)

### 서버 .env 핵심값

```bash
OPENAI_API_KEY=dummy
OPENAI_BASE_URL=http://localhost:8000/v1
OPENAI_MODEL=Qwen/Qwen3-4B-Instruct-2507     # serve.sh가 이 값으로 서빙함
MEM0_LLM_MAX_TOKENS=16384
MEM0_EMBED_BASE_URL=http://localhost:8001/v1
MEM0_EMBED_MODEL=Qwen/Qwen3-Embedding-4B     # serve.sh가 이 값으로 서빙함
MEM0_EMBED_DIMS=2560                          # Qwen3-Embedding-4B 차원 (Qdrant 컬렉션과 일치해야 함)
MEM0_TELEMETRY=False
QDRANT_HOST=localhost
QDRANT_PORT=6333
RETRY_TIMES=3
WAIT_TIME_LOWER=10
WAIT_TIME_UPPER=30
```

### 주의

- 재실행: 유저별 tmp 캐시로 자동 resume. 처음부터 다시 돌리려면 `results/mem0-classic-oss/memzero-oss-<version>/` 삭제
- 실패 유저 확인: `ls results/mem0-classic-oss/memzero-oss-<version>/tmp/*_error.log`
- 유실 액션(내부 id 환각) 집계: `reports/mem0-classic-oss/trace_analysis_*.json`의 `lost_updates` (op별 분모 포함)

