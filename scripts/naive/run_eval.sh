EXP_NUM=$1
EXP_NAME=exp${EXP_NUM}
DATASET_TYPE=$2
RESULTS_DIR=results/naive/question_answering/${EXP_NAME}/${DATASET_TYPE}/
RESULTS=$(ls $RESULTS_DIR)
BACKEND=$3

echo "EXPERIMENT ${EXP_NUM}: ${DATASET_TYPE} ${BACKEND}"
for file in ${RESULTS[@]}; do
    uv run eval/naive/evaluation.py --results_dir $RESULTS_DIR --results_file ${file} --backend ${BACKEND} --config_file ${BACKEND}_config.yaml --use_online_inference;
done
