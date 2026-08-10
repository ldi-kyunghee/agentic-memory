CUDA_VISIBLE_DEVICES=$1 bash scripts/bm25_eval/run_bm25.sh $2 $3 $4 $5

if [[ $3 == "vllm" ]]; then
    CUDA_VISIBLE_DEVICES=$1 uv run vllm serve \
			    openai/gpt-oss-120b \
			    --port 8000 \
			    --quantization mxfp4 \
			    > "gpt-oss-120b.log" 2>&1 &
    VLLM_PID=$!
else
    VLLM_PID=""
fi

CUDA_VISIBLE_DEVICES=$1 bash scripts/bm25_eval/run_eval.sh $2 $3

if [[ $VLLM_PID != "" ]]; then
    kill -15 $VLLM_PID
fi
