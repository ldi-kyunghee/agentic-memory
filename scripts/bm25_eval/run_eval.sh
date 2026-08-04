RESULTS_DIR=results/bm25/exp$1/question_answering/
BACKEND=$2
RESULTS=$(ls $RESULTS_DIR)

for file in ${RESULTS[@]}; do
    uv run eval/bm25/evaluation.py --results_dir $RESULTS_DIR --results_file ${file} --backend ${BACKEND} --config_file ${BACKEND}_config.yaml;
done
