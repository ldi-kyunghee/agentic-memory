import matplotlib.pyplot as plt
import numpy as np
import json
import os

RETRIEVAL_TYPES = {"bm25": "BM25", "embeddings": "Embedding", "hybrid": "Hybrid"}
results_dir = "result_files/"

def load_results_stats(model: str, result_type: str = 'Correct'):
    if model == 'gpt-oss-120b':
        exps = {5, 6, 9, 10}
    else:
        exps = {7, 8, 11, 12}

    memory_type_len = {
        "Memory Boundary": 162,
        "Memory Conflict": 153,
        "Basic Fact Recall": 152,
        "Generalization & Application": 149,
        "Multi-hop Inference": 50,
        "Dynamic Update": 39
    }

    per_memory_type_results = {}
    for retrieval_type, retrieval_type_name in RETRIEVAL_TYPES.items():
        exps_dir = results_dir + retrieval_type
        exps_paths = os.listdir(exps_dir)
        for exp in exps_paths:
            if int(exp.removeprefix('exp')) in exps:
                result_dir = f"{exps_dir}/{exp}/"
                for memory_type in memory_type_len.keys():
                    if per_memory_type_results.get(memory_type) is None:
                        per_memory_type_results[memory_type] = {}
                    if per_memory_type_results[memory_type].get(retrieval_type_name) is None:
                        per_memory_type_results[memory_type][retrieval_type_name] = 0

                    result_path = result_dir + f"{memory_type}.json"
                    with open(result_path, 'r') as file:
                        data = json.load(file)

                    if isinstance(data[result_type], list):
                        result_count = len(data[result_type])
                    else:
                        if data[result_type] == np.nan:
                            result_count = 0
                    per_memory_type_results[memory_type][retrieval_type_name] += result_count

    per_memory_type_results = {
        key: {
            k: (v / len(list(exps))) / memory_type_len[key]
            for k, v in value.items()
        }
        for key, value in per_memory_type_results.items()
    }
    return per_memory_type_results

fig, axes = plt.subplots(2, 3, figsize=(36, 18))
for i, model in enumerate(["gpt-oss-120b", "gpt-5-mini"]):
    for j, result_label in enumerate(["Correct", "Hallucination", "Omission"]):
        per_memory_type_results_dict = load_results_stats(model, result_label)
        memory_types = list(per_memory_type_results_dict.keys())

        results = {
            retrieval_type: [per_memory_type_results_dict[memory_type][retrieval_type] for memory_type in memory_types]
            for retrieval_type in ["BM25", "Embedding", "Hybrid"]
        }

        axes[i][j].grouped_bar(results, tick_labels=memory_types)
        axes[i][j].set_title(f"{model} ({result_label})", fontsize=18)
        axes[i][j].legend()

plt.suptitle(t="Result Distribution Per Memory Type", verticalalignment='baseline', fontsize=20)
plt.tight_layout()
os.makedirs("figs", exist_ok=True)

plt.savefig("figs/per-memory-type-mean-distribution-plot.png")
print("figs/per-memory-type-mean-distribution-plot.png")
