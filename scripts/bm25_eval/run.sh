uv run eval/bm25.py --level per_persona --top_k 5 --use_llm --llm_config vllm_config.yaml
uv run eval/bm25.py --level per_session --top_k 5 --use_llm --llm_config vllm_config.yaml
uv run eval/bm25.py --level per_persona --top_k 10 --use_llm --llm_config vllm_config.yaml
# uv run eval/bm25.py --level per_session --top_k 10 --use_llm --llm_config vllm_config.yaml


uv run eval/bm25.py --dataset HaluMem-Long.jsonl --level per_persona --top_k 5 --use_llm --llm_config vllm_config.yaml
uv run eval/bm25.py --dataset HaluMem-Long.jsonl --level per_session --top_k 5 --use_llm --llm_config vllm_config.yaml
uv run eval/bm25.py --dataset HaluMem-Long.jsonl --level per_persona --top_k 10 --use_llm --llm_config vllm_config.yaml