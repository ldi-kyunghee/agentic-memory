BACKEND=$2
DATASET=$4

uv run eval/bm25/bm25.py --exp_num $1 --top_k 5 --use_llm --backend ${BACKEND} --llm_config ${BACKEND}_config.yaml --memory_with_prior_question $3 --n_persona 4 --dataset ${DATASET} --memory_type bm25

uv run eval/bm25/bm25.py --exp_num $1 --top_k 5 --use_llm --backend ${BACKEND} --llm_config ${BACKEND}_config.yaml --memory_with_prior_question $3 --embed_config embed_config.yaml --n_persona 4 --dataset ${DATASET} --memory_type embeddings

uv run eval/bm25/bm25.py --exp_num $1 --top_k 10 --use_llm --backend ${BACKEND} --llm_config ${BACKEND}_config.yaml --memory_with_prior_question $3 --memory_type hybrid --embed_config embed_config.yaml --n_persona 4 --dataset ${DATASET}
