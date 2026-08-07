import argparse
import datetime
import gc
import json
import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import partial
from json import JSONDecodeError

import torch
from dotenv import load_dotenv
from llms import llm_request_for_json
from tqdm import tqdm
from utils import (
    EVALUATION_PROMPT_FOR_QA,
    EVALUATION_PROMPT_FOR_QUESTION,
    QAEval,
    load_config,
)
from vllm import LLM, SamplingParams
from vllm.config import ReasoningConfig
from vllm.sampling_params import StructuredOutputsParams

load_dotenv()
torch.cuda.empty_cache()
gc.collect()

logger = logging.getLogger()

def init_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--results_dir', type=str)
    parser.add_argument('--results_file', type=str)
    parser.add_argument('--backend', choices=['vllm', 'openai'], default='vllm')
    parser.add_argument('--config_file', type=str)
    return parser

def load_vllm(model_kwargs: dict, enable_reasoning: bool = False):
    # if enable_reasoning:
    #     reasoning_config = ReasoningConfig(
    #         reasoning_start_str="<think>",
    #         reasoning_end_str="I have to answer based on my reasoning now.</think>"
    #     )
    #     model_kwargs['reasoning_config'] = reasoning_config
        
    llm = LLM(
        max_num_seqs=128,
        **model_kwargs
    )

    return llm

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

def evaluation_for_question_vllm(
    question: str,
    reference_answer: str,
    key_memory_points: str,
    response: str
):
    prompt = EVALUATION_PROMPT_FOR_QA.format(
        question=question,
        reference_answer=reference_answer,
        key_memory_points=key_memory_points,
        response=response,
        JSON_SCHEMA=QAEval.model_json_schema()
    )

    return prompt

def parse_answers(outputs):
    contents = [output.outputs[0].text for output in outputs]
    results = []
    raw_output = []
    os.makedirs("raw_outputs", exist_ok=True)
    for content in tqdm(contents, desc="Parsing answers..."):
        try:
            result = json.loads(content)
            results.append(result)
        except JSONDecodeError:
            if content is not None:
                try:
                    idx = content.lower().find("json")
                    if idx >= 0:
                        idx += len("json")
                        response = content[idx:]
                        result = json.loads(response)
                        results.append(result)
                    else:
                        raw_output.append(content)
                except JSONDecodeError:
                    logger.warning("Cannot parse json: %s", content)
                    raw_output.append(content)
            else:
                logger.error("Content is %s", content)
                raw_output.append(content)
                
    file_name = datetime.datetime.now().astimezone().isoformat()
    with open(f"raw_outputs/{file_name}.txt", "w") as file:
        file.writelines(raw_output)
    return results

def llm_judge_vllm(qa_results, llm: LLM, sampling_params: dict, generation_kwargs: dict | None = None):
    structured_outputs = StructuredOutputsParams(json=QAEval.model_json_schema())
    sampling_params = SamplingParams(structured_outputs=structured_outputs, **sampling_params)
    
    prompts = []
    for result in qa_results:
        prompt = evaluation_for_question_vllm(
            result['question'],
            result['reference'],
            '\n'.join([evidence['memory_content'] for evidence in result['evidence']]),
            result['generated_answer']
        )
        prompts.append([
            {
                "role": "user",
                "content": prompt
            }
        ])

    request_ids = llm.enqueue_chat(prompts, sampling_params, **generation_kwargs)
    outputs = llm.wait_for_completion()
    results = parse_answers(outputs)

    eval_results = []
    for i, result in enumerate(results):
        result_type = result.get("evaluation_result")
        eval_result = {
            k: v 
            for k, v in qa_results[i].items()
        }
        eval_result['result_type'] = result_type
        eval_results.append(eval_result)
    return eval_results

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
                "\n".join(evidence['memory_content'] for evidence in qa['evidence']), 
                qa["generated_answer"]
            )
            futures[future] = qa

        for future in tqdm(as_completed(futures), total=len(futures)):
            qa = futures[future]
            try:
                result = future.result()
                result_type = result.get("evaluation_result")
            except Exception:
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
    for item in eval_results['per_persona_results']:
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
    data_dir = args.results_dir
    data_file = data_dir + args.results_file
    output_dir = args.results_dir.replace('results', 'scores').replace('question_answering/', '')
    output_file = output_dir + args.results_file.replace("results", "scores")

    os.makedirs(output_dir, exist_ok=True)
    with open(data_file, "r") as file:
        data = json.load(file)

    if args.backend == 'vllm':
        model_kwargs, sampling_params, generation_kwargs = None, None, None
        enable_reasoning = False
        kwargs = load_config(args.config_file)
        if isinstance(kwargs, tuple):
            if len(kwargs) == 3:
                model_kwargs, sampling_params, generation_kwargs = kwargs
                if (generation_kwargs['chat_template_kwargs'].get('reasoning_effort') is not None) and (generation_kwargs['chat_template_kwargs'].get('reasoning_effort') != "none"):
                    enable_reasoning = True          
            else:
                model_kwargs, sampling_params = kwargs
        else:
            model_kwargs = kwargs
        llm = load_vllm(model_kwargs, enable_reasoning)
        eval_fn = partial(llm_judge_vllm, llm=llm, sampling_params=sampling_params, generation_kwargs=generation_kwargs)
    elif args.backend == 'openai':
        model_kwargs = load_config(args.config_file)
        os.environ['OPENAI_MODEL'] = model_kwargs['model']
        eval_fn = partial(llm_judge_eval, max_workers=max_workers)

    eval_results = {
        "per_persona_results": [],
        "overall_score": {}
    }

    inputs = [session for item in data for session in item]
    eval_results['per_persona_results'] = eval_fn(inputs) # [results[s] for s in slices]
    eval_results = aggregate_results(eval_results)

    with open(output_file, "w") as file:
        json.dump(eval_results, file, indent=2)

if __name__ == '__main__':
    parser = init_parser()
    args = parser.parse_args()

    main(args)
