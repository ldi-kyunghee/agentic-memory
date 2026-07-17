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
프로젝트 전체 조망은 [docs/roadmap.md](docs/roadmap.md), 설계 배경·검증 기록은 [docs/mem0-halumem-baseline.md](docs/mem0-halumem-baseline.md), trace 데이터 계약은 [docs/trace-schema.md](docs/trace-schema.md) 참고.

### 구성

| 파일 | 역할 |
|---|---|
| `eval/eval_memzero_oss.py` | Stage A — 세션 투입, 메모리 추출/업데이트, 검색 context 수집 (유저 병렬 지원) |
| `eval/gen_answers.py` | Stage A' — 저장된 context로 QA 답변 일괄 생성 |
| `eval/judge.py` | Stage B — 4종 judge 채점 + 집계 (공식 evaluation.py의 structured-output 강건판) |
| `scripts/serve.sh` | Qdrant docker + vLLM 서빙 2종(LLM/임베딩) 원커맨드 기동·종료 |
| `gpu/` | vLLM 전용 독립 uv 프로젝트 (루트와 openai 버전 충돌로 분리) |

### 서버 실행 절차

```bash
# 0) 최초 1회: 클론 & 환경
git clone --recurse-submodules https://github.com/ldi-kyunghee/agentic-memory.git && cd agentic-memory
uv sync                          # 루트 (mem0 러너/judge)
(cd gpu && uv sync)              # vLLM 설치 (괄호=서브셸: 실행 후 cwd는 루트 유지)
# 리포 루트에 .env 생성 — 아래 "서버 .env 핵심값" 블록을 그대로 복사해서 저장
#   (스크립트/러너 모두 루트에서 실행하며 루트의 .env를 읽음. gpu/에는 .env 불필요)

# 1) 서빙 기동 (Qdrant + vLLM LLM/임베딩, ready까지 대기)
scripts/serve.sh <빈_GPU_번호>    # 예: scripts/serve.sh 2 / 로그: tmux attach -t vllm-serve

# 2) Stage A — 스모크(1유저) 먼저, 이상 없으면 풀런(20유저)
uv run python eval/eval_memzero_oss.py --user-num 1 --version srv-smoke 2>&1 | tee logs/srv-smoke.log
uv run python eval/eval_memzero_oss.py --max-workers 4 --version full 2>&1 | tee logs/full-run.log

# 3) Stage A' — 답변 생성
uv run python eval/gen_answers.py --results results/memzero-oss-full/memzero-oss_eval_results.jsonl --max-workers 32

# 4) Stage B — 채점 + 집계
uv run python eval/judge.py --results results/memzero-oss-full/memzero-oss_eval_results.jsonl --max-workers 32
# 결과: results/memzero-oss-full/eval_stat_result.json (overall_score)

# 5) GPU 반납
scripts/serve.sh stop            # Qdrant는 유지됨. 내리려면: docker stop qdrant
```

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

- 재실행: 유저별 tmp 캐시로 자동 resume. 처음부터 다시 돌리려면 `results/memzero-oss-<version>/` 삭제
- 실패 유저 확인: `ls results/memzero-oss-<version>/tmp/*_error.log`
- 유실 UPDATE 집계: `grep -c "Error processing memory action" logs/full-run.log`
