RESULTS_DIR=results/bm25/exp$1/question_answering/
RESULTS=$(ls $RESULTS_DIR)
BACKEND=$2

for file in ${RESULTS[@]}; do
    OPENAI_BASE_URL=http://localhost:8000/v1 OPENAI_API_KEY="" uv run eval/bm25/evaluation.py --results_dir $RESULTS_DIR --results_file ${file} --backend ${BACKEND} --config_file ${BACKEND}_config.yaml --use_online_inference;
done
