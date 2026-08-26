BACKEND=$2
DATASET=$4

uv run eval/naive/naive_memory.py --exp_num $1 --top_k 5 --memory_with_prior_question $3 --n_persona 4 --dataset ${DATASET} --memory_type bm25

uv run eval/naive/naive_memory.py --exp_num $1 --top_k 5 --memory_with_prior_question $3 --embed_config embed_config.yaml --n_persona 4 --dataset ${DATASET} --memory_type embeddings

uv run eval/naive/naive_memory.py --exp_num $1 --top_k 5 --memory_with_prior_question $3 --memory_type hybrid --embed_config embed_config.yaml --n_persona 4 --dataset ${DATASET}

uv run eval/naive/naive_memory.py --exp_num $1 --top_k 10 --memory_with_prior_question $3 --n_persona 4 --dataset ${DATASET} --memory_type bm25

uv run eval/naive/naive_memory.py --exp_num $1 --top_k 10 --memory_with_prior_question $3 --embed_config embed_config.yaml --n_persona 4 --dataset ${DATASET} --memory_type embeddings

uv run eval/naive/naive_memory.py --exp_num $1 --top_k 10 --memory_with_prior_question $3 --memory_type hybrid --embed_config embed_config.yaml --n_persona 4 --dataset ${DATASET}
