import argparse
import gc
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import torch
from dotenv import load_dotenv
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
    per_persona_dataset,
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
    parser.add_argument("--result_dir", type=str, default="results/naive/")
    parser.add_argument("--backend", choices=["vllm", "openai"], default="vllm")
    parser.add_argument("--online", action='store_true', default=False)
    parser.add_argument('--structured_outputs', action='store_true', default=False)
    parser.add_argument("--llm_config", type=str, default=None)
    return parser

def load_vllm(**model_kwargs):
    llm = LLM(**model_kwargs)

    return llm

def generate_answer_online(queries: list[dict], **model_kwargs):
    answers = []
    if args.structured_outputs:
        model_kwargs['text_format'] = QA
        
    for item in queries:
        query = item["question"]
        documents = ""
        for doc in item["retrieved"]:
            documents += f" - {doc['memory_content']}"
        prompt = PROMPT.format(context=documents, question=query)

        response = llm.responses.parse(
            input=prompt,
            **model_kwargs
        )

        answer = response.output_parsed.model_dump_json()
        if isinstance(answer, str):
            answer = json.loads(answer)
        answers.append(answer)

    return compile_outputs(queries, answers)

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

def generate_answers_gpt_oss(queries: list[dict], generation_kwargs: dict = {}, sampling_params: dict = {}):
    encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
    stop_token_ids = encoding.stop_tokens_for_assistant_actions()
    sampling_params['stop_token_ids'] = stop_token_ids
    sampling_params = SamplingParams(**sampling_params)

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

    _ = llm.enqueue(prompts, sampling_params, **generation_kwargs)
    outputs = llm.wait_for_completion()

    output_tokens = [output.outputs[0].token_ids for output in outputs]
    responses = [encoding.parse_messages_from_completion_tokens(tokens, Role.ASSISTANT) for tokens in output_tokens]
    answers = []
    for response in responses:
        raw_answer = response[0].content[0].text
        answer = raw_answer.splitlines()[-1].split(':')[-1]
        answers.append(answer)
    return compile_outputs(queries, answers)

def generate_answers_vllm(queries: list[dict], generation_kwargs: dict = {}, sampling_params: dict = {}):
    sampling_params = SamplingParams(**sampling_params)

    prompts = []
    for item in queries:
        query = item["question"]
        documents = ""
        for doc in item["retrieved"]:
            documents += f" - {doc['memory_content']}"

        prompt = format_inputs_vllm(query, documents)
        prompts.append(prompt)

    _ = llm.enqueue_chat(prompts, sampling_params, **generation_kwargs)
    outputs = llm.wait_for_completion()
    answers = [output.outputs[0].text for output in outputs]

    return compile_outputs(queries, answers)

def compile_outputs(queries: list[dict], answers: list[str]):
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

        if reasoning_content:
            result.update({"answer_reasoning": reasoning_content})

        results.append(result)
    return results

def run_qa(args, retrieval_results):
    llm_results = []
    for per_persona_results in retrieval_results:
        if args.backend == "vllm":
            if "openai" in model_kwargs['model']:
                if args.online:
                    generation_kwargs['model'] = model_kwargs.pop('model')
                    per_persona_llm_results = generate_answer_online(per_persona_results, **generation_kwargs)
                else:
                    per_persona_llm_results = generate_answers_gpt_oss(
                        per_persona_results, generation_kwargs, sampling_params
                    )
            else:
                per_persona_llm_results = generate_answers_vllm(
                    per_persona_results, generation_kwargs, sampling_params
                )
        else:
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

    retrieval_dir = f"{args.result_dir}/{exp_name}/retrieval/"
    retrieval_results = {}
    for result_file in os.listdir(retrieval_dir):
        with open(f"{retrieval_dir}/{result_file}", 'r') as file:
            retrieval_results[result_file] = json.load(file)

    if args.backend == "vllm":
        kwargs = load_config(args.llm_config)
        if isinstance(kwargs, tuple):
            if len(kwargs) == 2:
                model_kwargs, sampling_params = kwargs
            else:
                model_kwargs, sampling_params, generation_kwargs = kwargs
        else:
            model_kwargs = kwargs
            sampling_params = {}
            generation_kwargs = {}

        if args.online:
            llm = OpenAI(api_key="EMPTY", base_url="http://localhost:8001/v1")
        else:
            llm: LLM = load_vllm(**model_kwargs)
    else:
        model_kwargs = load_config(args.llm_config)
        llm: Client = OpenAI()

    results_dir = f"results/naive/{exp_name}/question_answering/"
    os.makedirs(results_dir, exist_ok=True)
    for result_file, retrieval_result in retrieval_results.items():
        results = run_qa(args, retrieval_result)
        with open(results_dir + result_file, "w") as file:
            json.dump(results, file, indent=2)

        flush()
