RESULTS=$(ls results)

for file in ${RESULTS[@]}; do
    if [[ ${file} == *"Qwen"* ]]; then
        uv run eval/evaluation.py --results_file ${file};
    fi
done