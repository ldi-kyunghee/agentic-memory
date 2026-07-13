from vllm import LLM, SamplingParams

import bm25s
from dotenv import load_dotenv
from bm25s.hf import BM25HF
import Stemmer
import argparse
import json

import os

from utils import load_config, PROMPT

load_dotenv()

def init_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=str, default='HaluMem-Medium.jsonl')
    parser.add_argument('--level', choices=['per_persona', 'per_session'], default='per_session')
    parser.add_argument('--top_k', type=int, default=5)
    parser.add_argument('--use_llm', action='store_true', default=False)
    parser.add_argument('--llm_config', type=str, default=None)
    return parser


def load_dataset(args):
    with open(f'dataset/{args.dataset}', 'r') as file:
        dataset = [json.loads(line) for line in file.readlines()]

    return dataset

def load_vllm(**model_kwargs):
    llm = LLM(
        **model_kwargs
    )

    return llm

def generate_answers(queries: list[dict], **sampling_params):
    sampling_params = SamplingParams(
        **sampling_params
    )

    prompts = []
    for item in queries:
        query = item['query']
        documents = ""
        for doc in item['documents']:
            documents += f" - {doc}"
        prompt = PROMPT.format(documents=documents, question=query)
        prompts.append(prompt)

    request_ids = llm.enqueue(prompts, sampling_params=sampling_params)
    outputs = llm.wait_for_completion()

    answers = [output.outputs[0].text for output in outputs]

    results = []
    for item, answer in zip(queries, answers):
        results.append({
            "query": item['query'],
            "answer": answer,
            "reference": item['answer'],
            "documents": item['documents'],
            "scores": item['scores']
        })
    return results

def per_persona(persona, level, top_k):
    sessions = [session for session in persona['sessions'] if session.get("questions")]
    queries = []
    answers = []
    memories = []
    results = []
    for session in sessions:
        per_session_query = [question['question'] for question in session['questions']]
        per_session_answer = [question['answer'] for question in session['questions']]
        per_session_memory = [memory['memory_content'] for memory in session['memory_points']]
        
        if level == 'per_session':
            results += retrieve(per_session_query, per_session_answer, per_session_memory, top_k)
        else:
            queries += per_session_query
            memories += per_session_memory
            answers  += per_session_answer

    if level == 'per_persona':
        results = retrieve(queries, answers, memories, top_k)
    return results

def retrieve(queries: list, answers: list, memories: list, top_k: int = 5):
    results = []
    mem_tokenized = bm25s.tokenize(memories, stemmer=stemmer)
    retriever.index(mem_tokenized)
    queries_tokenized = bm25s.tokenize(queries, stemmer=stemmer)
    documents, document_scores = retriever.retrieve(queries_tokenized, corpus=memories, k=top_k)
    for query, answer, docs, scores in zip(queries, answers, documents, document_scores):
        results.append({
            "question": query,
            "answer": answer,
            "documents": docs,
            "scores": scores
        })

    return results

    

if __name__ == '__main__':
    parser = init_parser()
    args = parser.parse_args()

    retriever = BM25HF()
    stemmer = Stemmer.Stemmer("english")

    dataset = load_dataset(args)
    results = []
    for persona in dataset:
        per_persona_results = per_persona(persona, args.level, args.top_k)
        results.append(per_persona_results)

    results_dir = "results/"
    os.makedirs(results_dir, exist_ok=True)
    with open(results_dir + f"bm25_retrieval_top_{args.top_k}_results.json", "w") as file:
        json.dump(results, file, indent=2)

    if args.use_llm:
        model_kwargs, sampling_params = load_config(args.llm_config)
        llm: LLM = load_vllm(model_kwargs)
        llm_results = []
        for per_persona_result in results:
            per_persona_llm_results = generate_answers(per_persona_result, **sampling_params)
            llm_results.append(per_persona_llm_results)
        
        model_name = model_kwargs['model'].split('/')[-1].replace('-2507', '')
        result_file = f"{model_name}_qa_top_{args.top_k}_results.json"
        with open(results_dir + result_file, "w") as file:
            json.dump(llm_results, file, indent=2)