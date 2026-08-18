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
    parser.add_argument("--llm_config", type=str, default=None)
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

def generate_answer_openai(queries: list[dict], **model_kwargs):
    answers = []
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
        answers.append(answer)
    return compile_outputs(queries, answers)
        
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

def run_qa(args, dataset, retrieval_results):
    llm_results = []
    for per_persona_results in retrieval_results:
        if args.backend == "vllm":
            if "openai" in model_kwargs['model']:
                per_persona_llm_results = generate_answers_gpt_oss(
                    per_persona_results, generation_kwargs, sampling_params
                )
            else:
                per_persona_llm_results = generate_answers_vllm(
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

    results_dir = f"results/naive/{exp_name}/"
    naive_results_dir = results_dir + "retrieval/"
    dataset_name = args.dataset.split("-")[-1].split(".")[0].lower()
    naive_results_file = f"{args.memory_type}_retrieval_{dataset_name}_top_{args.top_k}_results.json"

    os.makedirs(naive_results_dir, exist_ok=True)
    with open(naive_results_dir + naive_results_file, "w") as file:
        json.dump(retrieval_results, file, indent=2)

    model_name = model_kwargs["model"].split("/")[-1].replace("-2507", "")
    result_file = f"{model_name}_{dataset_name}_{args.memory_type}_qa_top_{args.top_k}_results.json"

    qa_results_dir = results_dir + "question_answering/"
    os.makedirs(qa_results_dir, exist_ok=True)
    with open(qa_results_dir + result_file, "w") as file:
        json.dump(llm_results, file, indent=2)


