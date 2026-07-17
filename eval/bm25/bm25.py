from vllm import LLM, SamplingParams

import bm25s
from dotenv import load_dotenv
from bm25s.hf import BM25HF
import Stemmer
import argparse
import json

import os

from utils import load_config, per_persona_dataset, PROMPT

load_dotenv()

def with_prior(string):
    if 't' in string:
        return True
    elif 'f' in string:
        return False
    else:
        raise ValueError

def init_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exp_num', type=int)
    parser.add_argument('--data_path', type=str, default='dataset/')
    parser.add_argument('--dataset', type=str, default='HaluMem-Medium.jsonl')
    parser.add_argument('--top_k', type=int, default=5)
    parser.add_argument('--use_llm', action='store_true', default=False)
    parser.add_argument('--llm_config', type=str, default=None)
    parser.add_argument('--memory_with_prior_question', type=with_prior, default=False)
    return parser


def load_dataset(args):
    with open(args.data_path + args.dataset, 'r') as file:
        dataset = [json.loads(line) for line in file.readlines()]

    return dataset

def load_vllm(**model_kwargs):
    model_kwargs.pop('max_tokens')
    llm = LLM(
        max_tokens=128,
        **model_kwargs
    )

    return llm

def generate_answers(queries: list[dict], **sampling_params):
    sampling_params = SamplingParams(
        **sampling_params
    )

    prompts = []
    for item in queries:
        query = item['question']
        documents = ""
        for doc in item['retrieved']:
            documents += f" - {doc['memory_content']}"
        prompt = PROMPT.format(context=documents, question=query)
        prompts.append(prompt)

    request_ids = llm.enqueue(prompts, sampling_params=sampling_params)
    outputs = llm.wait_for_completion()

    answers = [output.outputs[0].text for output in outputs]

    results = []
    for item, answer in zip(queries, answers):
        results.append({
            "question": item['question'],
            "generated_answer": answer,
            "reference": item['answer'],
            "retrieved": item['retrieved'],
            "evidence": item['evidence'],
            "question_type": item['question_type'],
            "difficulty": item['difficulty']
        })

    return results

def retrieve(qas: list[dict], memories: list, top_k: int = 5):
    results = []
    queries = [qa['question'] for qa in qas]
    mem_tokenized = bm25s.tokenize(memories, stemmer=stemmer)
    retriever.index(mem_tokenized)
    queries_tokenized = bm25s.tokenize(queries, stemmer=stemmer)
    documents, document_scores = retriever.retrieve(queries_tokenized, corpus=memories, k=top_k)
    for qa, document, scores in zip(qas, documents, document_scores):
        res = {
            k: v
            for k, v in qa.items()
        }

        res['retrieved'] = []
        for doc, score in zip(document.tolist(), scores.tolist()):
            res['retrieved'].append({
                'memory_content': doc,
                'score': score
            })

        results.append(res)
    return results

def main(args):
    dataset = load_dataset(args)
    retrieval_results = []
    llm_results = []
    for persona in dataset:
        qas, per_persona_memories = per_persona_dataset(persona, args.memory_with_prior_question)
        per_persona_results = retrieve(qas, per_persona_memories, args.top_k)
        per_persona_llm_results = generate_answers(per_persona_results, **sampling_params)

        retrieval_results.append(per_persona_results)
        llm_results.append(per_persona_llm_results)
    
    results_dir = "results/bm25/exp%d" % args.exp_num
    bm25_results_dir = results_dir + "retrieval/"
    dataset_name = args.dataset.split('-')[-1].split('.')[0].lower()
    bm25_results_file = f"bm25_retrieval_{dataset_name}_top_{args.top_k}_results.json"
    
    os.makedirs(bm25_results_dir, exist_ok=True)
    with open(bm25_results_dir + bm25_results_file, "w") as file:
        json.dump(retrieval_results, file, indent=2)
        
    model_name = model_kwargs['model'].split('/')[-1].replace('-2507', '')
    result_file = f"{model_name}_{dataset_name}_qa_top_{args.top_k}_results.json"
    
    qa_results_dir = results_dir + "question_answering/"
    os.makedirs(qa_results_dir, exist_ok=True)
    with open(qa_results_dir + result_file, "w") as file:
        json.dump(llm_results, file, indent=2)

if __name__ == '__main__':
    parser = init_parser()
    args = parser.parse_args()
    print(args)

    retriever = BM25HF()
    stemmer = Stemmer.Stemmer("english")

    model_kwargs, sampling_params = load_config(args.llm_config)
    llm: LLM = load_vllm(**model_kwargs)

    main(args)