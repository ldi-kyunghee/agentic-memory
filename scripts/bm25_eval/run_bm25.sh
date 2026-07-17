uv run eval/bm25/bm25.py --exp_num $1 --top_k 5 --use_llm --llm_config vllm_config.yaml
uv run eval/bm25/bm25.py --exp_num $1 --top_k 10 --use_llm --llm_config vllm_config.yaml