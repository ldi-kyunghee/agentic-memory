BACKEND=$2
# uv run eval/bm25/bm25.py --exp_num $1 --top_k 5 --use_llm --llm_config vllm_config.yaml --memory_with_prior_question $2
uv run eval/bm25/bm25.py --exp_num $1 --top_k 10 --use_llm --backend ${BACKEND} --llm_config ${BACKEND}_config.yaml --memory_with_prior_question $3 --hybrid --embed_config embed_config.yaml --n_persona 4
