#!/usr/bin/env bash
# vLLM 서빙 원커맨드 기동/종료 스크립트
# 사용법:
#   scripts/serve.sh <gpu_id>   # 예: scripts/serve.sh 2  -> tmux 세션 vllm-serve에 LLM+임베딩 서빙 기동
#   scripts/serve.sh stop       # 세션 종료 + GPU 반납
set -euo pipefail
cd "$(dirname "$0")/.."   # 어디서 실행해도 리포 루트 기준으로 동작함

SESSION=vllm-serve
LLM_PORT=8000
EMB_PORT=8001

if [[ "${1:-}" == "stop" ]]; then
    tmux kill-session -t "$SESSION" 2>/dev/null && echo "serving stopped" || echo "no session to stop"
    exit 0
fi

GPU="${1:?GPU id 필요함. 예: scripts/serve.sh 2}"

set -a; source .env; set +a   # OPENAI_MODEL / MEM0_EMBED_MODEL을 단일 출처(.env)에서 읽음

ensure_qdrant() {
    local port="${QDRANT_PORT:-6333}"
    # 이미 응답하면 그대로 사용 (컨테이너든 뭐든 불문)
    if curl -sf "localhost:$port" > /dev/null; then
        echo "qdrant already running (:$port)"
        return
    fi
    if docker ps -a --format '{{.Names}}' | grep -qx qdrant; then
        docker start qdrant > /dev/null   # 멈춰있던 기존 컨테이너 재기동 (데이터 유지됨)
    else
        docker run -d --name qdrant \
            -p "$port:6333" \
            -v qdrant_storage:/qdrant/storage \
            qdrant/qdrant > /dev/null     # 최초 1회 생성. named volume이라 컨테이너 재생성에도 데이터 살아남음
    fi
    until curl -sf "localhost:$port" > /dev/null; do sleep 2; done
    echo "qdrant ready (:$port)"
}

ensure_qdrant

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "이미 $SESSION 세션이 떠 있음. scripts/serve.sh stop 먼저 할 것"
    exit 1
fi

mkdir -p logs

tmux new-session -d -s "$SESSION" -n llm \
    "VLLM_USE_FLASHINFER_SAMPLER=0 TORCH_CUDA_ARCH_LIST=12.0 CUDA_VISIBLE_DEVICES=$GPU uv run --project gpu vllm serve $OPENAI_MODEL \
        --port $LLM_PORT --gpu-memory-utilization 0.45 --max-model-len 32768 \
        2>&1 | tee logs/vllm-llm.log"

tmux new-window -t "$SESSION" -n emb \
    "TORCH_CUDA_ARCH_LIST=12.0 CUDA_VISIBLE_DEVICES=$GPU uv run --project gpu vllm serve $MEM0_EMBED_MODEL \
        --hf-overrides '{\"is_matryoshka\": true}' \
        --port $EMB_PORT --gpu-memory-utilization 0.30 \
        2>&1 | tee logs/vllm-emb.log"

echo "서버 기동 대기 중... (첫 실행이면 모델 다운로드 때문에 오래 걸림. tmux attach -t $SESSION 으로 로그 확인 가능)"
for port in $LLM_PORT $EMB_PORT; do
    until curl -sf "localhost:$port/v1/models" > /dev/null; do
        sleep 5
        # 서빙 프로세스가 죽었으면 무한 대기하지 않고 탈출함
        tmux has-session -t "$SESSION" 2>/dev/null || { echo "❌ 서빙 세션이 죽음. logs/vllm-*.log 확인"; exit 1; }
    done
    echo "  port $port ready"
done
echo "✅ GPU $GPU 에서 서빙 중 (LLM :$LLM_PORT / EMB :$EMB_PORT). 종료: scripts/serve.sh stop"