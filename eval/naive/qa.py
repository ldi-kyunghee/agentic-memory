import argparse
import gc
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from json import JSONDecodeError
from typing import Any

import torch
from dotenv import load_dotenv
from llms import llm_request_for_json
from openai import Client, OpenAI
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
from pydantic import BaseModel, Field
from tqdm import tqdm
from utils import (
    DEVELOPER_PROMPT,
    PROMPT,
    SYSTEM_PROMPT,
    USER_PROMPT,
    load_config,
)
from vllm import LLM, SamplingParams
from vllm.sampling_params import StructuredOutputsParams

load_dotenv()

class QA(BaseModel):
    reasoning_content: str = Field(description="Provide a concise and traceable rationale behind your answer.")
    answer: str

def flush():
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    gc.collect()

def init_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exp_num", type=int)
    parser.add_argument("--retrieval_dir", type=str, default="results/naive/retrieval")
    parser.add_argument("--backend", choices=["vllm", "openai"], default="vllm")
    parser.add_argument("--online", action='store_true', default=False)
    parser.add_argument('--structured_outputs', action='store_true', default=False)
    parser.add_argument("--llm_config", type=str, default=None)
    return parser

def load_vllm(**model_kwargs):
    llm = LLM(**model_kwargs)

    return llm

def generate_answer(query: dict, **common_params):
    documents = ""
    for doc in query['retrieved']:
        documents += f" - {doc['memory_content']}"
    prompt = PROMPT.format(context=documents, question=query['question'])

    result = llm_request_for_json(
        prompt,
        **common_params
    )

    return result

def generate_answer_online(queries: list[dict], max_workers: int = 10, **model_kwargs):
    results = []
    if args.structured_outputs:
        model_kwargs['text_format'] = QA

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for query in queries:
            future = executor.submit(
                generate_answer,
                query,
                **model_kwargs
            )

            futures[future] = query

        for future in tqdm(as_completed(futures), total=len(futures), desc="Generating..."):
            query = futures[future]
            result = future.result()
            query['generated_answer'] = result.get("answer")
            query['answer_reasoning'] = result.get("answer_reasoning")
            results.append(result)
    return results

def format_inputs_vllm(query, documents):
    prompt = PROMPT.format(context=documents, question=query)
    return [
        {
            "role": "user", "content": prompt
        }
    ]

def format_inputs_gpt_oss(query, documents):
    if args.structured_outputs:
        DEVELOPER_PROMPT += f"""

        # Response Format

        ## QA

        {QA.model_json_schema()}
        """.strip()

    prompt = Conversation.from_messages(
        [
            Message.from_role_and_content(
                Role.SYSTEM,
                SystemContent.new().with_model_identity(SYSTEM_PROMPT).with_reasoning_effort(ReasoningEffort.HIGH)
            ),
            Message.from_role_and_content(
                Role.DEVELOPER,
                DeveloperContent.new().with_instructions(DEVELOPER_PROMPT)
            ),
            Message.from_role_and_content(
                Role.USER,
                USER_PROMPT.format(
                    context=documents,
                    question=query
                )
            )
        ]
    )

    return prompt

def generate_answers_gpt_oss(queries: list[dict], llm: LLM, generation_kwargs: dict | None = None, sampling_params_config: dict[str, Any] | None = None):
    encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
    stop_token_ids = encoding.stop_tokens_for_assistant_actions()
    if sampling_params_config is not None:
        sampling_params_config['stop_token_ids'] = stop_token_ids
        sampling_params = SamplingParams(**sampling_params_config)
    else:
        sampling_params = SamplingParams(n=1)
        
    prompts = []
    for item in queries:
        query = item["question"]
        documents = ""
        for doc in item['retrieved']:
            documents += f" - {doc['memory_content']}"

        prompt = format_inputs_gpt_oss(query, documents)
        prefill_ids = encoding.render_conversation_for_completion(prompt, Role.ASSISTANT)
        prompts.append({
            "prompt_token_ids": prefill_ids,
            "prompt": encoding.decode(prefill_ids)
        })

    if generation_kwargs is not None:
        _ = llm.enqueue(prompts, sampling_params, **generation_kwargs)
    else:
        _ = llm.enqueue(prompts, sampling_params)
    outputs = llm.wait_for_completion()

    output_tokens = [output.outputs[0].token_ids for output in outputs]
    responses = [encoding.parse_messages_from_completion_tokens(tokens, Role.ASSISTANT) for tokens in output_tokens]
    answers = []
    for response in responses:
        raw_answer = response[0].content[0].text
        answer = raw_answer.splitlines()[-1].split(':')[-1]
        answers.append(answer)
    return compile_outputs(queries, answers)

def generate_answers_vllm(queries: list[dict], llm: LLM, generation_kwargs: dict | None = None, sampling_params_config: dict | None = None):
    if args.structured_outputs:
        structured_output_params = StructuredOutputsParams(json=QA.model_json_schema())
    else:
        structured_output_params = None

    if sampling_params_config is not None:
        sampling_params = SamplingParams(structured_outputs=structured_output_params, **sampling_params_config)
    else:
        sampling_params = SamplingParams(n=1, structured_outputs=structured_output_params)

    prompts = []
    for item in queries:
        query = item["question"]
        documents = ""
        for doc in item["retrieved"]:
            documents += f" - {doc['memory_content']}"

        prompt = format_inputs_vllm(query, documents)
        prompts.append(prompt)

    if generation_kwargs is not None:
        _ = llm.enqueue_chat(prompts, sampling_params, **generation_kwargs)
    else:
        _ = llm.enqueue_chat(prompts, sampling_params)
    outputs = llm.wait_for_completion()
    answers = []

    for output in outputs:
        answer = output.outputs[0].text
        if args.structured_outputs:
            try:
                parsed_answer = json.loads(answer)
                answers.append(parsed_answer)
            except JSONDecodeError:
                answers.append(answer)
        else:
            answers.append(answer)
    return compile_outputs(queries, answers)

def compile_outputs(queries: list[dict], answers: list[str] | list[dict]):
    results = []
    for item, answer in zip(queries, answers):
        if isinstance(answer, dict):
            reasoning_content = answer.pop('reasoning_content')
            answer = answer.pop('answer')
        else:
            reasoning_content = None

        result = {
            "question": item["question"],
            "generated_answer": answer,
            "reference": item["answer"],
            "retrieved": item["retrieved"],
            "evidence": item["evidence"],
            "question_type": item["question_type"],
            "difficulty": item["difficulty"],
        }

        if reasoning_content is not None:
            result.update({"answer_reasoning": reasoning_content})

        results.append(result)
    return results

def run_qa(args, retrieval_results: list[dict], llm: LLM | None = None):
    llm_results = []
    for per_persona_results in retrieval_results:
        if args.backend == "vllm":
            if args.online:
                per_persona_llm_results = generate_answer_online(per_persona_results, **model_kwargs)
            else:
                per_persona_llm_results = generate_answers_vllm(
                    per_persona_results, llm=llm, generation_kwargs=generation_kwargs, sampling_params_config=sampling_params
                )
        elif args.backend == 'openai':
            per_persona_llm_results = generate_answer_online(
                per_persona_results, **model_kwargs
            )
        llm_results.append(per_persona_llm_results)
    return llm_results

if __name__ == "__main__":
    flush()

    parser = init_parser()
    args = parser.parse_args()
    print(args)

    exp_name = f"exp{args.exp_num}"

    retrieval_results = {}
    for result_file in os.listdir(args.retrieval_dir):
        with open(f"{args.retrieval_dir}/{result_file}", 'r') as file:
            retrieval_results[result_file] = json.load(file)

    if args.backend == "vllm":
        kwargs = load_config(args.llm_config, use_online_inference=args.online)
        if args.online:
            client_params, model_kwargs = kwargs
        elif isinstance(kwargs, tuple):
            if len(kwargs) == 2:
                model_kwargs, sampling_params = kwargs
            else:
                model_kwargs, sampling_params, generation_kwargs = kwargs
        else:
            model_kwargs = kwargs
            sampling_params = {}
            generation_kwargs = {}

        if args.online:
            if client_params:
                base_url = "{base_url}:{port}/v1".format(**client_params)
            else:
                base_url = "http://localhost:8000/v1"
            os.environ["OPENAI_BASE_URL"] = base_url
            llm = None
        else:
            llm: LLM = load_vllm(**model_kwargs)
    elif args.backend == 'openai':
        llm = None
    else:
        raise ValueError(f"Invalid backend: {args.backend}")

    dataset_name = args.retrieval_dir.split('/')[-1]
    results_dir = f"results/naive/question_answering/{exp_name}/{dataset_name}/"
    os.makedirs(results_dir, exist_ok=True)
    for result_file, retrieval_result in retrieval_results.items():
        results = run_qa(args, retrieval_result, llm=llm)
        with open(results_dir + result_file, "w") as file:
            json.dump(results, file, indent=2)

        flush()
