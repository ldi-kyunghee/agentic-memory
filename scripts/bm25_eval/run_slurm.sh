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

bash scripts/bm25_eval/run_bm25.sh $1
bash scripts/bm25_eval/run_eval.sh $1

rm -r /local_datasets/halumem_dataset

exit
