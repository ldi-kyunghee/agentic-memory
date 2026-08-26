RESULTS_DIR_PREFIX=results/naive/question_answering/exp$1
BACKEND=$2
DATASET_TYPES=(
    "medium"
    "long"
)

for dataset in ${DATASET_TYPES[@]}; do
    RESULTS_DIR="${RESULTS_DIR_PREFIX}/${dataset}/"
    RESULTS=$(ls $RESULTS_DIR)
    for file in ${RESULTS[@]}; do
	uv run eval/naive/evaluation.py --results_dir $RESULTS_DIR --results_file ${file} --backend ${BACKEND} --config_file ${BACKEND}_config.yaml --use_online_inference;
    done
done
