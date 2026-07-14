import os
import json
import time
import copy
import argparse
from tqdm import tqdm
from concurrent.futures import ProcessPoolExecutor, as_completed
from dotenv import load_dotenv

from llms import llm_request_for_json
from utils import EVALUATION_PROMPT_FOR_QUESTION

load_dotenv()

def init_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--results_file', type=str)
    return parser

def evaluation_for_question(
    question: str,
    reference_answer: str,
    key_memory_points: str,
    response: str
):
    """
    Question-Answering Evaluation
    question: The question string to be evaluated.
    reference_answer: The reference (gold-standard) answer.
    key_memory_points: The memory points used to derive the reference answer.
    response: The answer produced by the memory system.
    """
    prompt = EVALUATION_PROMPT_FOR_QUESTION.format(
        question=question,
        reference_answer=reference_answer,
        key_memory_points=key_memory_points,
        response=response
    )

    result = llm_request_for_json(prompt)

    return result

def compute_f1(precision: float, recall: float) -> float:
    """
    Compute the F1-score from precision and recall.

    Args:
        precision (float): Precision value (0~1)
        recall (float): Recall value (0~1)

    Returns:
        float: F1-score
    """
    if precision + recall == 0:
        return 0.0
    return 2 * (precision * recall) / (precision + recall)

def llm_judge_eval(qa_results, max_workers: int = 10):
    # Question-Answering Evaluation
    eval_results = []
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for qa in qa_results:
            future = executor.submit(
                evaluation_for_question,
                qa["question"], 
                qa["reference"], 
                "\n".join(qa['evidence']), 
                qa["answer"]
            )
            futures[future] = qa

        for future in tqdm(as_completed(futures), total=len(futures)):
            qa = futures[future]
            try:
                result = future.result()
                result_type = result.get("evaluation_result")
            except Exception as e:
                result_type = None
            qa["result_type"] = result_type
            eval_results.append(qa)

    return eval_results

def aggregate_results(eval_results):
    correct_qa_num = 0
    hallucination_qa_num = 0
    omission_qa_num = 0
    qa_num = 0
    qa_valid_num = 0
    for item in eval_results["question_answering_records"]:
        item["is_valid"] = True
        qa_num += 1

        if item["result_type"] not in ["Correct", "Hallucination", "Omission"]:
            item["is_valid"] = False
            continue

        if item["result_type"] == "Correct":
            correct_qa_num += 1
        elif item["result_type"] == "Hallucination":
            hallucination_qa_num += 1
        elif item["result_type"] == "Omission":
            omission_qa_num += 1

        qa_valid_num += 1

    eval_results["overall_score"]["correct_qa_ratio(all)"] = correct_qa_num / qa_num
    eval_results["overall_score"]["correct_qa_ratio(valid)"] = correct_qa_num / qa_valid_num
    eval_results["overall_score"]["hallucination_qa_ratio(all)"] = hallucination_qa_num / qa_num
    eval_results["overall_score"]["hallucination_qa_ratio(valid)"] = hallucination_qa_num / qa_valid_num
    eval_results["overall_score"]["omission_qa_ratio(all)"] = omission_qa_num / qa_num
    eval_results["overall_score"]["omission_qa_ratio(valid)"] = omission_qa_num / qa_valid_num
    eval_results["overall_score"]["qa_valid_num"] = qa_valid_num
    eval_results["overall_score"]["qa_num"] = qa_num

    return eval_results

def main(args, max_workers: int = 10):
    data_dir = "results/"
    data_file = data_dir + args.results_file
    output_dir = "scores/"
    output_file = output_dir + args.results_file.replace("results", "scores")

    with open(data_file, "r") as file:
        data = json.load(file)

    eval_results = llm_judge_eval(data, max_workers)
    results_aggregated = aggregate_results(eval_results)

    with open(output_file, "w") as file:
        json.dump(results_aggregated, file, indent=2)

if __name__ == '__main__':
    parser = init_parser()
    args = parser.parse_args()

    main(args)