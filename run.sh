bash scripts/bm25_eval/run.sh 0 5 vllm
bash scripts/bm25_eval/run.sh 0 6 vllm

git add .
git commit -m "update scores"
git push
