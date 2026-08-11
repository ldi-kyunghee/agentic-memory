# CUDA_VISIBLE_DEVICES=$1 bash scripts/bm25_eval/run_bm25.sh $2 $3 $4 $5
t=$5

HEALTH_TIMEOUT=300
HEALTH_INTERVAL=5

wait_for_server() {
    local port=$1
    local name=$2
    local elapsed=0
    echo "Waiting for $name on port $port ..."
    while [ $elapsed -lt $HEALTH_TIMEOUT ]; do
        if curl -s "http://localhost:$port/health" >/dev/null 2>&1; then
            echo "$name is ready on port $port (took ${elapsed}s)"
            return 0
        fi
        if curl -s "http://localhost:$port/v1/models" >/dev/null 2>&1; then
            echo "$name is ready on port $port via /v1/models (took ${elapsed}s)"
            return 0
        fi
        sleep $HEALTH_INTERVAL
        elapsed=$((elapsed + HEALTH_INTERVAL))
    done
    echo "ERROR: $name failed to start on port $port after ${HEALTH_TIMEOUT}s"
    return 1
}

if [[ $3 == "vllm" ]]; then
    CUDA_VISIBLE_DEVICES=$1 uv run vllm serve \
			    openai/gpt-oss-120b \
			    --port 8000 \
			    --quantization mxfp4 \
			    > "gpt-oss-120b.log" 2>&1 &
    VLLM_PID=$!

    if wait_for_server 8000 "gpt-oss-120b"; then
	OPENAI_BASE_URL="http://localhost:8000/v1" CUDA_VISIBLE_DEVICES=$1 bash scripts/bm25_eval/run_eval.sh $2 $3;
    fi

    kill -15 $VLLM_PID
    
else
    CUDA_VISIBLE_DEVICES=$1 bash scripts/bm25_eval/run_eval.sh $2 $3
fi
