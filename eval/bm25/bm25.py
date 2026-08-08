import argparse
import gc
import json
import os
import time
from functools import partial

import bm25s
import faiss
import numpy as np
import Stemmer
import torch
from bm25s.hf import BM25HF
from dotenv import load_dotenv
from openai import Client, OpenAI
from openai_harmony import (
    Conversation,
    DeveloperContent,
    HarmonyEncodingName,
    Message,
    ReasoningEffort,
    RenderConversationConfig,
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
    parser.add_argument("--llm_config", type=str, default=None)
    parser.add_argument("--hybrid", action="store_true", default=False)
    parser.add_argument("--embed_config", type=str, default=None)
    parser.add_argument("--memory_with_prior_question", type=with_prior, default=False)
    parser.add_argument("--vector_db", type=str, default="qdrant")
    return parser


def load_dataset(args):
    with open(args.data_path + args.dataset, "r") as file:
        dataset = [json.loads(line) for line in file]

    return dataset


def load_vllm(**model_kwargs):
    llm = LLM(**model_kwargs)

    return llm

def generate_answer_openai(queries: list[dict], **model_kwargs):
    results = []
    for item in tqdm(queries, desc="Generating..."):
        query = item["question"]
        documents = ""
        for doc in item["retrieved"]:
            documents += f" - {doc['memory_content']}"
        prompt = PROMPT.format(context=documents, question=query)

        response = llm.responses.create(
            input=prompt,
            **model_kwargs
        )

        answer = response.output_text
        results.append({
            "question": item["question"],
            "generated_answer": answer,
            "reference": item["answer"],
            "retrieved": item["retrieved"],
            "evidence": item["evidence"],
            "question_type": item["question_type"],
            "difficulty": item["difficulty"],
        })
    return results

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


def sort_documents_original(I: np.ndarray, vector_scores: np.ndarray):
    distances = [
        {i.item(): d.item() for i, d in zip(I[j], vector_scores[j])}
        for j in range(I.shape[0])
    ]

    sorted_Ds = [[distance[i] for i in range(I.shape[1])] for distance in distances]
    return np.array(sorted_Ds)


def vector_retrieval(queries: list, memories: list):
    if embed_model is None:
        corpus_embeddings, query_embeddings = embed_online(queries, memories)
    else:
        corpus_embeddings, query_embeddings = embed_offline(queries, memories)

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
    documents, document_scores = retriever.retrieve(
        queries_tokenized, corpus=memories, k=k, sorted=False
    )
    return documents, document_scores

def format_inputs_vllm(query, documents):
    prompt = PROMPT.format(context=documents, question=query)
    return [
        {
            "role": "user", "content": prompt
        }
    ]

def format_inputs_gpt_oss(query, documents):
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

def generate_answers(queries: list[dict], generation_kwargs: dict = {}, sampling_params: dict = {}):
    if "openai" in model_kwargs['model']:
        encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
        stop_token_ids = encoding.stop_tokens_for_assistant_actions()
        sampling_params['stop_token_ids'] = stop_token_ids
        generate = partial(llm.enqueue, **generation_kwargs)
    else:
        encoding = None
        generate = partial(llm.enqueue_chat, **generation_kwargs)
    sampling_params = SamplingParams(**sampling_params)

    prompts = []
    prompt_token_ids = []
    for item in queries:
        query = item["question"]
        documents = ""
        for doc in item["retrieved"]:
            documents += f" - {doc['memory_content']}"
        
        if encoding is not None:
            prompt = format_inputs_gpt_oss(query, documents)
            prefill_ids = encoding.render_conversation_for_completion(prompt, Role.ASSISTANT, config=RenderConversationConfig())
            prompt_token_ids.append(prefill_ids)
        else:
            prompt = format_inputs_vllm(query, documents)
            prompts.append(prompt)

    if not prompts:
        prompts = [{"prompt_token_ids": prompt_token_ids}]
        
    _ = generate(prompts, sampling_params=sampling_params)
    outputs = llm.wait_for_completion()

    if encoding is not None:
        output_tokens = [output.outputs[0].token_ids for output in outputs]
        responses = [encoding.parse_messages_from_completion_tokens(tokens, Role.ASSISTANT) for tokens in output_tokens]
        answers = [response.content[0].text for response in responses]
    else: 
        answers = [output.outputs[0].text for output in outputs]

    results = []
    for item, answer in zip(queries, answers):
        results.append(
            {
                "question": item["question"],
                "generated_answer": answer,
                "reference": item["answer"],
                "retrieved": item["retrieved"],
                "evidence": item["evidence"],
                "question_type": item["question_type"],
                "difficulty": item["difficulty"],
            }
        )

    return results


def fetch_results(qas, documents, ranked_results):
    results = []
    for qa, document, scores in zip(qas, documents, ranked_results):
        res = {k: v for k, v in qa.items()}

        res["retrieved"] = []
        for doc, score in zip(document.tolist(), scores.tolist()):
            res["retrieved"].append({"memory_content": doc, "score": score})

        results.append(res)
    return results


def faiss_retrieval(
    qas: list[dict],
    memories: list,
    k: int = 5,
    alpha: float = 0.5,
    sorted: bool = False,
):
    queries = [qa["question"] for qa in qas]
    documents, bm25_scores = bm25_retrieval(queries, memories, k, sorted)
    if sorted:
        return fetch_results(qas, documents, bm25_scores)

    vector_scores = vector_retrieval(queries, memories)
    hybrid_scores = alpha * bm25_scores + (1 - alpha) * vector_scores
    ranked_results = sorted(
        zip(range(len(memories)), hybrid_scores), key=lambda x: x[1], reverse=True
    )

    return fetch_results(qas, documents, ranked_results)

def qdrant_store(
        qas: list[dict],
        memories: list,
        collection_name: str
):
    queries = [qa["question"] for qa in qas]
    
    if embed_model is None:
        corpus_embeddings, query_embeddings = embed_online(queries, memories)
    else:
        corpus_embeddings, query_embeddings = embed_offline(queries, memories)

    ids = [i for i in range(len(memories))]

    payloads = [
        {"document": memory, "source": "HaluMem-Medium"}
        for memory in memories
    ]
    
    documents = [
        {
            "bm25": models.Document(
                text=memory,
                model="qdrant/bm25"
            ),
            "embeds": corpus_embedding
        }
        for memory, corpus_embedding in zip(memories, corpus_embeddings)
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
                using="embeds"
            )
        ]
        for query, query_embedding in zip(queries, query_embeddings)
    ]

    results = []
    for prefetch in prefetch_queries:
        query_results = client.query_points(
            collection_name=collection_name,
            prefetch=prefetch,
            query=models.FusionQuery(fusion=models.Fusion.RRF),
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
    k = args.top_k if not args.hybrid else None
    for i, persona in enumerate(dataset):
        qas, per_persona_memories = per_persona_dataset(
            persona, args.memory_with_prior_question
        )
        k = len(per_persona_memories) if k is None else k
        if client is not None:
            collection_name = f"{proj_name}_{i}"
            client.create_collection(
                collection_name=collection_name,
                vectors_config={
                    "embeds": models.VectorParams(size=2560, distance=models.Distance.COSINE),
                },
                sparse_vectors_config={
                    "bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)
                },
            )
            per_persona_results = qdrant_retrieval(
                qas, per_persona_memories, collection_name=collection_name, k=k
            )
        else:
            per_persona_results = faiss_retrieval(
                qas, per_persona_memories, k, args.alpha, args.hybrid
            )

        retrieval_results.append(per_persona_results)

    return retrieval_results

def run_qa(args, dataset, retrieval_results):
    llm_results = []
    for per_persona_results in retrieval_results:
        if args.backend == "vllm":
            per_persona_llm_results = generate_answers(
                per_persona_results, generation_kwargs, sampling_params
            )
        else:
            per_persona_llm_results = generate_answer_openai(
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
    
    retriever = BM25HF()
    stemmer = Stemmer.Stemmer("english")
    dataset = load_dataset(args)

    if args.n_persona is not None:
        dataset = dataset[:args.n_persona]

    if args.embed_config:
        embed_kwargs = load_config(args.embed_config)
        embed_model = load_vllm(**embed_kwargs)
        if args.vector_db == "qdrant":
            proj_name = "naive-mem"
            client = QdrantClient(":memory:")
        else:
            collection_name = None
    else:
        embed_model = None
        client = None
        proj_name = None

    retrieval_results = run_retrieval(args, dataset)

    if embed_model is not None:
        del embed_model
        time.sleep(3)
        flush()
        
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
        llm: LLM = load_vllm(**model_kwargs)
    else:
        model_kwargs = load_config(args.llm_config)
        llm: Client = OpenAI()
        
    llm_results = run_qa(args, dataset, retrieval_results)
    del llm
    time.sleep(3)
    flush()

    results_dir = f"results/bm25/{exp_name}/"
    bm25_results_dir = results_dir + "retrieval/"
    dataset_name = args.dataset.split("-")[-1].split(".")[0].lower()
    bm25_results_file = f"bm25_retrieval_{dataset_name}_top_{args.top_k}_results.json"

    os.makedirs(bm25_results_dir, exist_ok=True)
    with open(bm25_results_dir + bm25_results_file, "w") as file:
        json.dump(retrieval_results, file, indent=2)

    model_name = model_kwargs["model"].split("/")[-1].replace("-2507", "")
    result_file = f"{model_name}_{dataset_name}_qa_top_{args.top_k}_results.json"

    qa_results_dir = results_dir + "question_answering/"
    os.makedirs(qa_results_dir, exist_ok=True)
    with open(qa_results_dir + result_file, "w") as file:
        json.dump(llm_results, file, indent=2)
