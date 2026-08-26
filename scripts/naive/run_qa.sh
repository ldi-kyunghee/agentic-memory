DATASET=$2
BACKEND=$3

uv run eval/naive/qa.py --exp_num $1 --backend ${BACKEND} --llm_config ${BACKEND}_config.yaml --online --structured_outputs --retrieval_dir "results/naive/retrieval/${DATASET}"
