CUDA_VISIBLE_DEVICES=$1 bash scripts/bm25_eval/run_bm25.sh $2 $3 $4 $5
CUDA_VISIBLE_DEVICES=$1 bash scripts/bm25_eval/run_eval.sh $2 $3
