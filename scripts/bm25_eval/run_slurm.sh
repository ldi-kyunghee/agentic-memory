#!/usr/bin/bash

#SBATCH -J agentic-memory
#SBATCH --gres=gpu:2
#SBATCH --cpus-per-gpu=8
#SBATCH --mem=64G
#SBATCH -p batch_grad
#SBATCH -w ariel-v4
#SBATCH -t 1-0
#SBATCH -o ./logs/slurm-%A.out
#SBATCH -e ./logs/slurm-err-%A.out

tar -xvf dataset/halumem_dataset.tar.gz -C /local_datasets/

uv run eval/bm25.py --data_path /local_datasets/ --top_k 5 --use_llm --llm_config vllm_config.yaml
uv run eval/bm25.py --data_path /local_datasets/ --top_k 10 --use_llm --llm_config vllm_config.yaml

uv run eval/bm25.py --data_path /local_datasets/ --dataset HaluMem-Long.jsonl --top_k 5 --use_llm --llm_config vllm_config.yaml
uv run eval/bm25.py --data_path /local_datasets/ --dataset HaluMem-Long.jsonl --top_k 10 --use_llm --llm_config vllm_config.yaml

rm -r 

exit
