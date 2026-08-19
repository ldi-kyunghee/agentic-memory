import pandas as pd
import random
import json
import os

results_dir = "result_files/"

RETRIEVAL_TYPES = {"bm25": "BM25", "embeddings": "Embedding", "hybrid": "Hybrid"}
MEMORY_TYPES = [
        "Memory Boundary",
        "Memory Conflict",
        "Basic Fact Recall",
        "Generalization & Application",
        "Multi-hop Inference",
        "Dynamic Update"
]

def load_per_memory_type_results(
                memory_type: str,
                exp_dir: str = 'exp5/',
                result_label: str = 'Hallucination'
):
        random.seed(42)
        sampled_results = {}
        if memory_type.islower():
              memory_type = " ".join([item.upper() for item in memory_type.split(' ')])
        for retrieval_dir, retrieval_type in RETRIEVAL_TYPES.items():
              retrieval_dir = f"{retrieval_dir}/"
              result_path = results_dir + retrieval_dir + exp_dir + f"{memory_type}.json"
              with open(result_path, 'r') as file:
                      data = json.load(file)

              k = min(len(data[result_label]), 10)
              samples = random.sample(data[result_label], k)
              for i, sample in enumerate(samples):
                      generated_answer = sample.pop('generated_answer')
                      final_answer = generated_answer.splitlines()[-1]
                      reasoning_content = '\n'.join(generated_answer.splitlines()[:-1])
                      sample['reasoning_content'] = reasoning_content
                      sample['model_answer'] = final_answer
                      samples[i] = sample
                      sampled_results[retrieval_type] = samples

        os.makedirs(f"samples/{exp_dir}", exist_ok=True)
        with open(f"samples/{exp_dir}" + f"{memory_type}.json", 'w') as file:
              json.dump(sampled_results, file, indent=2)
        return sampled_results

for exp_dir in ["exp5/", "exp7/"]:
        for memory_type in MEMORY_TYPES:
              for result_label in ["Correct", "Hallucination", "Omission"]:
                      samples = load_per_memory_type_results(
                                memory_type=f'{memory_type}',
                                exp_dir=f'{exp_dir}',
                                result_label=f'{result_label}'
                        )
