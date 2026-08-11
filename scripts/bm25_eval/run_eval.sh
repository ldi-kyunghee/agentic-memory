RESULTS_DIR=results/bm25/exp$1/question_answering/
RESULTS=$(ls $RESULTS_DIR)
BACKEND=$2

for file in ${RESULTS[@]}; do
    uv run eval/bm25/evaluation.py --results_dir $RESULTS_DIR --results_file ${file} --backend ${BACKEND} --config_file ${BACKEND}_config.yaml --use_online_inference;
done
