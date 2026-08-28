DATASET=$2
BACKEND=$3
ONLINE=$4

if [[ $ONLINE == true ]]; then
    uv run eval/naive/qa.py --exp_num $1 --backend ${BACKEND} --llm_config ${BACKEND}_config.yaml --online --structured_outputs --retrieval_dir "results/naive/retrieval/${DATASET}"
else
    uv run eval/naive/qa.py --exp_num $1 --backend ${BACKEND} --llm_config ${BACKEND}_config.yaml --structured_outputs --retrieval_dir "results/naive/retrieval/${DATASET}"
fi
