RESULTS_DIR=results/bm25/exp$1/question_answering/
RESULTS=$(ls $RESULTS_DIR)

for file in ${RESULTS[@]}; do
    if [[ ${file} == *"Qwen"* ]]; then
        uv run eval/bm25/evaluation.py --results_dir $RESULTS_DIR --results_file ${file} --backend openai --config_file openai_config.yaml;
    fi
done
