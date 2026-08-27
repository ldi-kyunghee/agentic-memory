import gc
import argparse
import json
import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import partial
from json import JSONDecodeError

import torch
from dotenv import load_dotenv
from llms import llm_request_for_json
from openai_harmony import (
    Conversation,
    DeveloperContent,
    HarmonyEncodingName,
    Message,
    ReasoningEffort,
    Role,
    SystemContent,
    load_harmony_encoding,
)
from tqdm import tqdm
from utils import (
    EVALUATION_DEVELOPER_PROMPT_FOR_QA,
    EVALUATION_PROMPT_FOR_QUESTION,
    EVALUATION_SYSTEM_PROMPT_FOR_QA,
    EVALUATION_USER_PROMPT_FOR_QA,
    QAEval,
    load_config,
)
from vllm import LLM, SamplingParams
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
    parser.add_argument('--use_online_inference', action='store_true', default=False)
    parser.add_argument('--config_file', type=str)
    return parser

def load_vllm(model_kwargs: dict, enable_reasoning: bool = False):
    llm = LLM(
        max_num_seqs=128,
        **model_kwargs
    )

    return llm

def evaluation_for_question(
    question: str,
    reference_answer: str,
    key_memory_points: str,
    response: str,
    model: str,
    **common_params
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

    result = llm_request_for_json(prompt, model=model, **common_params)

    return result

def evaluation_for_question_vllm(
    question: str,
    reference_answer: str,
    key_memory_points: str,
    response: str
):
    prompt = EVALUATION_PROMPT_FOR_QUESTION.format(
        question=question,
        reference_answer=reference_answer,
        key_memory_points=key_memory_points,
        response=response
    )
    return prompt

def parse_answers(outputs):
    results = []
    for output in tqdm(outputs, desc="Parsing answers..."):
        content = output.outputs[0].text
        result = json.loads(content)
        results.append(result)
    return results

def format_inputs_for_gps_oss(prompt: str):
    system_prompt = SystemContent.new().with_model_identity(
        EVALUATION_SYSTEM_PROMPT_FOR_QA
    ).with_reasoning_effort(ReasoningEffort.HIGH)

    developer_prompt = DeveloperContent.new().with_instructions(
                        EVALUATION_DEVELOPER_PROMPT_FOR_QA
                    )

    convo = Conversation.from_messages(
        [
            Message.from_role_and_content(
                Role.SYSTEM,
                system_prompt
            ),
            Message.from_role_and_content(
                Role.DEVELOPER,
                developer_prompt,
            ),
            Message.from_role_and_content(
                Role.USER,
                prompt,
            )
        ]
    )
    return convo

def llm_judge_vllm_gpt_oss(qa_results, llm: LLM, sampling_params: dict, generation_kwargs: dict | None = None):
    encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
    stop_token_ids = encoding.stop_tokens_for_assistant_actions()
    structured_outputs_params = StructuredOutputsParams(json=QAEval.model_json_schema())

    sampling_params = SamplingParams(
        stop_token_ids=stop_token_ids,
        structured_outputs=structured_outputs_params,
        **sampling_params
    )

    prompts = []
    for result in qa_results:
        prompt = EVALUATION_USER_PROMPT_FOR_QA.format(
            question=result['question'],
            reference_answer=result['reference'],
            key_memory_points='\n'.join([evidence['memory_content'] for evidence in result['evidence']]),
            response=result['generated_answer']
        )

        convo = format_inputs_for_gps_oss(prompt)
        prefill_ids = encoding.render_conversation_for_completion(convo, Role.ASSISTANT)
        input_prompt = encoding.decode(prefill_ids)
        prompts.append({
            "prompt": input_prompt,
            "prompt_token_ids": prefill_ids
        })

    outputs = llm.generate(prompts, sampling_params=sampling_params)
    output_tokens = [output.outputs[0].token_ids for output in outputs]
    results = [encoding.parse_messages_from_completion_tokens(tokens, Role.ASSISTANT) for tokens in output_tokens]

    eval_results = []
    for i, result in enumerate(results):
        response = result[0].content[0].text
        try:
            result = json.loads(response)
            result_type = result.get("evaluation_result")
            eval_result = {
                k: v
                for k, v in qa_results[i].items()
            }
            eval_result['result_type'] = result_type
            eval_results.append(eval_result)
        except JSONDecodeError:
            logger.error("Failed to parse JSON from: %s", response)
    return eval_results

def llm_judge_vllm(qa_results, llm: LLM, sampling_params: dict, generation_kwargs: dict | None = None):
    structured_outputs_params = StructuredOutputsParams(json=QAEval.model_json_schema())
    sampling_params = SamplingParams(structured_outputs=structured_outputs_params, **sampling_params)

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
                "role": "user", "content": prompt
            }
        ])

    _ = llm.enqueue_chat(prompts, sampling_params, **generation_kwargs)
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

def llm_judge_eval(qa_results, model: str, max_workers: int = 10, **common_params):
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
                qa["generated_answer"],
                model,
                **common_params
            )
            futures[future] = qa

        for future in tqdm(as_completed(futures), total=len(futures)):
            qa = futures[future]
            result = future.result()
            result_type = result.get("evaluation_result")
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

def vllm_online_inference(model, common_params, max_workers=10):
    return partial(
        llm_judge_eval,
        model=model,
        max_workers=max_workers,
        **common_params
    )

def vllm_offline_inference(llm, model_kwargs, sampling_params, generation_kwargs, max_workers=10):
    if "gpt" in model_kwargs['model']:
        return partial(
            llm_judge_vllm_gpt_oss,
            llm=llm,
            sampling_params=sampling_params,
            generation_kwargs=generation_kwargs
        )
    else:
        return partial(
            llm_judge_vllm,
            llm=llm,
            sampling_params=sampling_params,
            generation_kwargs=generation_kwargs
        )

def main(args, max_workers: int = 10):
    data_dir = args.results_dir
    data_file = data_dir + args.results_file
    output_dir = '/'.join(args.results_dir.replace('results', 'scores').replace('question_answering', '').split('/')).strip()

    if not output_dir.endswith('/'):
        output_dir += '/'

    output_file = output_dir + args.results_file.replace("results", "scores")

    os.makedirs(output_dir, exist_ok=True)
    with open(data_file, "r") as file:
        data = json.load(file)

    kwargs = load_config(args.config_file, args.use_online_inference)
    if args.backend == 'vllm':
        if args.use_online_inference:
            model_kwargs, client_params, common_params = kwargs
            if os.environ.get('OPENAI_BASE_URL'):
                print(os.environ['OPENAI_BASE_URL'])
            else:
                os.environ['OPENAI_BASE_URL'] = '{base_url}:{port}/v1'.format(**client_params)
            if common_params.get('model'):
                _ = common_params.pop('model')
            eval_fn = vllm_online_inference(model_kwargs['model'], common_params)
        else:
            model_kwargs, sampling_params, generation_kwargs = kwargs
            llm = load_vllm(model_kwargs)
            eval_fn = vllm_offline_inference(
                llm,
                model_kwargs=model_kwargs,
                sampling_params=sampling_params,
                generation_kwargs=generation_kwargs,
            )

    elif args.backend == 'openai':
        model_kwargs, online_kwargs = load_config(args.config_file, use_online_inference=True)
        os.environ['OPENAI_MODEL'] = model_kwargs['model']
        eval_fn = partial(llm_judge_eval, model=model_kwargs['model'], max_workers=max_workers, **online_kwargs)

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
