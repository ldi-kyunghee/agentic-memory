GPU=$1
EXP_NUM=$2
BACKEND=$3
DATASET=$4
ONLINE=$5
WITH_PRIOR_QUESTION=$6

CUDA_VISIBLE_DEVICES=${GPU} bash scripts/naive/run_retrieval.sh ${BACKEND} ${DATASET} ${WITH_PRIOR_QUESTION}

bash scripts/naive/run_naive.sh ${GPU} ${EXP_NUM} ${BACKEND} ${DATASET} ${ONLINE}
