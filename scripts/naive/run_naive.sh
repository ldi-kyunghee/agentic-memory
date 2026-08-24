BACKEND=$2
DATASET=$4

uv run eval/naive/naive_memory.py --exp_num $1 --top_k 5 --memory_with_prior_question $3 --n_persona 4 --dataset ${DATASET} --memory_type bm25

uv run eval/naive/naive_memory.py --exp_num $1 --top_k 5 --memory_with_prior_question $3 --embed_config embed_config.yaml --n_persona 4 --dataset ${DATASET} --memory_type embeddings

uv run eval/naive/naive_memory.py --exp_num $1 --top_k 5 --memory_with_prior_question $3 --memory_type hybrid --embed_config embed_config.yaml --n_persona 4 --dataset ${DATASET}

uv run eval/naive/naive_memory.py --exp_num $1 --top_k 10 --memory_with_prior_question $3 --n_persona 4 --dataset ${DATASET} --memory_type bm25

uv run eval/naive/naive_memory.py --exp_num $1 --top_k 10 --memory_with_prior_question $3 --embed_config embed_config.yaml --n_persona 4 --dataset ${DATASET} --memory_type embeddings

uv run eval/naive/naive_memory.py --exp_num $1 --top_k 10 --memory_with_prior_question $3 --memory_type hybrid --embed_config embed_config.yaml --n_persona 4 --dataset ${DATASET}

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
	OPENAI_BASE_URL="http://localhost:8000/v1" bash scripts/naive/run_qa.sh $1 $2;
    fi

    kill -15 $VLLM_PID
    
else
    bash scripts/naive/run_qa.sh $1 $2
fi
