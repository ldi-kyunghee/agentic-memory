import argparse
import gc
import json
import os
import time

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
from qdrant_client import QdrantClient, models
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

load_dotenv()

def with_prior(string):

    if "t" in string.lower():
        return True
    elif "f" in string.lower():
        return False
    else:
        raise ValueError


def flush():
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    gc.collect()


def init_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exp_num", type=int)
    parser.add_argument("--data_path", type=str, default="dataset/")
    parser.add_argument("--dataset", type=str, default="HaluMem-Medium.jsonl")
    parser.add_argument("--n_persona", type=int, default=None)
    parser.add_argument("--top_k", type=int, default=5)
    parser.add_argument("--alpha", type=float, default=0.5)
    parser.add_argument("--use_llm", action="store_true", default=False)
    parser.add_argument("--backend", choices=["vllm", "openai"], default="vllm")
    parser.add_argument("--memory_type", choices=['bm25', 'embeddings', 'hybrid'], default='hybrid')
    parser.add_argument("--embed_config", type=str, default=None)
    parser.add_argument("--memory_with_prior_question", type=with_prior, default=False)
    return parser


def load_dataset(args):
    with open(args.data_path + args.dataset, "r") as file:
        dataset = [json.loads(line) for line in file]

    return dataset


def load_vllm(**model_kwargs):
    llm = LLM(**model_kwargs)

    return llm
        
def embed_online(queries: list, memories: list):
    model = "Qwen/Qwen3-Embedding-4B"
    open_api_key = "EMPTY"
    open_api_base = "http://localhost:8001/v1/embeddings"

    client = OpenAI(api_key=open_api_key, base_url=open_api_base)

    corpus_responses = client.embeddings.create(input=memories, model=model)

    query_responses = client.embeddings.create(input=queries, model=model)

    corpus_embeddings = [data.embedding for data in corpus_responses.data]
    query_embeddings = [data.embedding for data in query_responses.data]
    return np.array(corpus_embeddings), np.array(query_embeddings)


def embed_offline(queries: list, memories: list):
    corpus_outputs = embed_model.embed(memories)
    query_outputs = embed_model.embed(queries)

    corpus_embeddings = [output.outputs.embedding for output in corpus_outputs]
    query_embeddings = [output.outputs.embedding for output in query_outputs]

    return np.array(corpus_embeddings), np.array(query_embeddings)

def fetch_results(qas, documents, ranked_results):
    results = []
    for qa, document, scores in zip(qas, documents, ranked_results):
        res = {k: v for k, v in qa.items()}

        res["retrieved"] = []
        for doc, score in zip(document.tolist(), scores.tolist()):
            res["retrieved"].append({"memory_content": doc, "score": score})

        results.append(res)
    return results

def qdrant_store(
        qas: list[dict],
        memories: list,
        collection_name: str
):
    queries = [qa["question"] for qa in qas]
    
    if embed_model is not None:
        corpus_embeddings, query_embeddings = embed_offline(queries, memories)
    else:
        corpus_embeddings, query_embeddings = None, None
        
    ids = [i for i in range(len(memories))]

    payloads = [
        {"document": memory, "source": "HaluMem-Medium"}
        for memory in memories
    ]

    if args.memory_type == 'hybrid':
        documents = [
            {
                "bm25": models.Document(
                    text=memory,
                    model="qdrant/bm25"
                ),
                "embeddings": corpus_embedding
            }
            for memory, corpus_embedding in zip(memories, corpus_embeddings)
        ]

    elif args.memory_type == 'embeddings':
        documents = [
            {
                "embeddings": corpus_embedding
            }
            for corpus_embedding in corpus_embeddings
        ]
    else:
        documents = [
            {
                "bm25": models.Document(
                    text=memory,
                    model="qdrant/bm25"
                )
            }
            for memory in memories
        ]

    client.upsert(
        collection_name=collection_name,
        points=[
            models.PointStruct(
                id=id,
                vector=vector,
                payload=payload
            )
            for id, vector, payload in zip(ids, documents, payloads)],
    )

    return queries, query_embeddings

def qdrant_retrieve(
        queries: list,
        query_embeddings: np.ndarray,
        memories: list,
        collection_name: str,
        k: int = 5,
):

    using = args.memory_type
    
    if args.memory_type == 'hybrid':
        prefetch_queries = [
            [
                models.Prefetch(
                    query=models.Document(
                        text=query,
                        model="qdrant/bm25"
                    ),
                    using="bm25"
                ),
                models.Prefetch(
                    query=query_embedding,
                    using="embeddings"
                )
            ]
            for query, query_embedding in zip(queries, query_embeddings)
        ]

        model_queries = [models.FusionQuery(fusion=models.Fusion.RRF)] * len(prefetch_queries)
        using = None
        
    elif args.memory_type == 'embeddings':
        model_queries = [
            query_embedding
            for query_embedding in query_embeddings
        ]

        prefetch_queries = [None] * len(model_queries)
        
    else:
        model_queries = [
            models.Document(
                text=query,
                model='qdrant/bm25'
            )
            for query in queries
        ]

        prefetch_queries = [None] * len(model_queries)

    results = []
    for query, prefetch in zip(model_queries, prefetch_queries):
        query_results = client.query_points(
            collection_name=collection_name,
            prefetch=prefetch,
            query=query,
            using=using,
            limit=k
        ).points

        retrieved_memories = [query_result.payload['document'] for query_result in query_results]
        scores = [query_result.score for query_result in query_results]
        
        results.append([
            {
                "memory_content": retrieved_memory,
                "score": score
            }
            for retrieved_memory, score in zip(retrieved_memories, scores)
        ])
        
    return results

def qdrant_retrieval(
        qas: list[dict],
        memories: list,
        collection_name: str,
        k: int = 5,
):
    queries, query_embeddings = qdrant_store(qas, memories, collection_name=collection_name)
    retrieved = qdrant_retrieve(
        queries=queries,
        query_embeddings=query_embeddings,
        memories=memories,
        collection_name=collection_name,
        k=k
    )

    results = []
    for qa, docs in zip(qas, retrieved):
        result = {
            k: v
            for k, v in qa.items()
        }

        result['retrieved'] = docs
        results.append(result)
    return results
    
def run_retrieval(args, dataset):
    retrieval_results = []
    k = args.top_k if args.memory_type != 'hybrid' else None
    for i, persona in enumerate(dataset):
        qas, per_persona_memories = per_persona_dataset(
            persona, args.memory_with_prior_question
        )
        k = len(per_persona_memories) if k is None else k
        collection_name = f"{proj_name}_{i}"
        qdrant_config = {}
        
        if args.memory_type == 'hybrid':
            qdrant_config["vectors_config"] = {
                "embeddings": models.VectorParams(size=2560, distance=models.Distance.COSINE),
            }
            qdrant_config["sparse_vectors_config"] = {
                "bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)
            }
        elif args.memory_type == 'embeddings':
            qdrant_config["vectors_config"] = {
                args.memory_type: models.VectorParams(size=2560, distance=models.Distance.COSINE)
            }
        else:
            qdrant_config["sparse_vectors_config"] = {
                args.memory_type: models.SparseVectorParams(modifier=models.Modifier.IDF)
            }

        client.create_collection(
            collection_name=collection_name,
            **qdrant_config
            )
        
        per_persona_results = qdrant_retrieval(
            qas, per_persona_memories, collection_name=collection_name, k=k
        )
        retrieval_results.append(per_persona_results)
    return retrieval_results

if __name__ == "__main__":
    flush()

    parser = init_parser()
    args = parser.parse_args()
    print(args)

    exp_name = f"exp{args.exp_num}"
    dataset = load_dataset(args)

    if args.n_persona is not None:
        dataset = dataset[:args.n_persona]

    if args.memory_type != 'bm25':
        embed_kwargs = load_config(args.embed_config)
        embed_model = load_vllm(**embed_kwargs)
    else:
        embed_kwargs, embed_model = None, None

    proj_name = "naive-mem"
    client = QdrantClient(":memory:")
    
    retrieval_results = run_retrieval(args, dataset)

    if embed_model is not None:
        del embed_model
        time.sleep(3)
        flush()

    results_dir = f'results/naive/{exp_name}/retrieval/'
    dataset_name = args.dataset.split("-")[-1].split(".")[0].lower()
    results_file = f"{args.memory_type}_retrieval_{dataset_name}_top_{args.top_k}_results.json"

    os.makedirs(results_dir, exist_ok=True)
    with open(results_dir + results_file, "w") as file:
        json.dump(retrieval_results, file, indent=2)

    print(results_file)
