from vllm import LLM, SamplingParams
from openai import OpenAI
import torch
import faiss
import bm25s
from dotenv import load_dotenv
from bm25s.hf import BM25HF
import Stemmer
import argparse
import json

import numpy as np
import os, gc

from utils import load_config, per_persona_dataset, PROMPT

load_dotenv()

def with_prior(string):
    if 't' in string:
        return True
    elif 'f' in string:
        return False
    else:
        raise ValueError
    
def flush():
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    gc.collect()

def init_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exp_num', type=int)
    parser.add_argument('--data_path', type=str, default='dataset/')
    parser.add_argument('--dataset', type=str, default='HaluMem-Medium.jsonl')
    parser.add_argument('--top_k', type=int, default=5)
    parser.add_argument('--alpha', type=float, default=0.5)
    parser.add_argument('--use_llm', action='store_true', default=False)
    parser.add_argument('--llm_config', type=str, default=None)
    parser.add_argument('--hybrid', action='store_true', default=False)
    parser.add_argument('--embed_config', type=str, default=None)
    parser.add_argument('--memory_with_prior_question', type=with_prior, default=False)
    return parser


def load_dataset(args):
    with open(args.data_path + args.dataset, 'r') as file:
        dataset = [json.loads(line) for line in file.readlines()]

    return dataset

def load_vllm(**model_kwargs):
    llm = LLM(
        **model_kwargs
    )

    return llm

def embed_online(queries: list, memories: list):
    model = "Qwen/Qwen3-4B-Embedding"
    open_api_key = "EMPTY"
    open_api_base = "http://localhost:8001/v1/embeddings"

    client = OpenAI(
        api_key=open_api_key,
        base_url=open_api_base
    )

    corpus_responses = client.embeddings.create(
        input=memories,
        model=model
    )

    query_responses = client.embeddings.create(
        input=queries,
        model=model
    )

    corpus_embeddings = [data.embedding for data in corpus_responses.data]
    query_embeddings = [data.embedding for data in query_responses.data]
    return np.array(corpus_embeddings), np.array(query_embeddings)

def embed_offline(queries: list, memories: list, **model_kwargs):
    llm = load_vllm(**model_kwargs)
    return

def sort_documents_original(I: np.ndarray, vector_scores: np.ndarray):
    distances = [
        {
            i.item(): d.item()
            for i, d in zip(I[j], vector_scores[j])
        }
        for j in range(I.shape[0])
    ]

    sorted_Ds = [[distance[i] for i in range(I.shape[1])] for distance in distances]
    return np.array(sorted_Ds)

def vector_retrieval(queries: list, memories: list):
    corpus_embeddings, query_embeddings = embed_online(queries, memories)
    index = faiss.IndexFlatIP(corpus_embeddings.shape[1])
    index.add(corpus_embeddings)
    k = corpus_embeddings.shape[0]
    D, I = index.search(np.expand_dims(query_embeddings, axis=0), k=k)
    vector_scores = sort_documents_original(I, 1 - D)
    return vector_scores

def bm25_retrieval(queries, memories, k: int = 5, sorted: bool = False):
    mem_tokenized = bm25s.tokenize(memories, stemmer=stemmer)
    retriever.index(mem_tokenized)
    queries_tokenized = bm25s.tokenize(queries, stemmer=stemmer)
    k = len(memories)
    documents, document_scores = retriever.retrieve(queries_tokenized, corpus=memories, k=k, sorted=False)
    return documents, document_scores

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
        prompts.append([dict(role='user', content=prompt)])

    request_ids = llm.enqueue_chat(prompts, sampling_params=sampling_params)
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

def fetch_results(qas, documents, ranked_results):
    results = []
    for qa, document, scores in zip(qas, documents, ranked_results):
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

def retrieval(qas: list[dict], memories: list, k: int = 5, alpha: float = 0.5, sorted: bool = False):
    queries = [qa['question'] for qa in qas]
    documents, bm25_scores = bm25_retrieval(queries, memories, k, sorted)
    if sorted:
        return fetch_results(qas, documents, bm25_scores)
    
    vector_scores = vector_retrieval(queries, memories)
    hybrid_scores = alpha * bm25_scores + (1 - alpha) * vector_scores
    ranked_results = sorted(zip(range(len(memories)), hybrid_scores), key=lambda x: x[1], reverse=True)

    return fetch_results(qas, documents, ranked_results)

def main(args):
    dataset = load_dataset(args)
    retrieval_results = []
    llm_results = []
    k = args.top_k if args.hybrid else None
    for persona in dataset:
        qas, per_persona_memories = per_persona_dataset(persona, args.memory_with_prior_question)
        k = len(per_persona_memories) if k is None else k
        per_persona_results = retrieval(qas, per_persona_memories, k, args.alpha, args.hybrid)
        per_persona_llm_results = generate_answers(per_persona_results, **sampling_params)

        retrieval_results.append(per_persona_results)
        llm_results.append(per_persona_llm_results)
    
    results_dir = "results/bm25/exp%d/" % args.exp_num
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
    flush()

    parser = init_parser()
    args = parser.parse_args()
    print(args)

    retriever = BM25HF()
    stemmer = Stemmer.Stemmer("english")

    model_kwargs, sampling_params = load_config(args.llm_config)
    llm: LLM = load_vllm(**model_kwargs)

    main(args)
