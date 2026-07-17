uv run eval/bm25/bm25.py --top_k 5 --use_llm --llm_config vllm_config.yaml
uv run eval/bm25/bm25.py --top_k 10 --use_llm --llm_config vllm_config.yaml

uv run eval/bm25/bm25.py --dataset HaluMem-Long.jsonl --top_k 5 --use_llm --llm_config vllm_config.yaml
uv run eval/bm25/bm25.py --dataset HaluMem-Long.jsonl --top_k 10 --use_llm --llm_config vllm_config.yaml