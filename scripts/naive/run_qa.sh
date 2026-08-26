BACKEND=$2

uv run eval/naive/qa.py --exp_num $1 --backend ${BACKEND} --llm_config ${BACKEND}_config.yaml --online --structured_outputs --retrieval_dir "results/naive/retrieval/medium"

uv run eval/naive/qa.py --exp_num $1 --backend ${BACKEND} --llm_config ${BACKEND}_config.yaml --online --structured_outputs --retrieval_dir "results/naive/retrieval/long"
