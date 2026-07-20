import os
import sys
import json
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from tqdm import tqdm
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_random_exponential

load_dotenv()

# 서브모듈의 QA 프롬프트를 그대로 import (프로토콜 충실성 — 복사본 만들지 않음)
sys.path.insert(0, "HaluMem/eval")
from prompts import PROMPT_MEMZERO

client = OpenAI()  # OPENAI_API_KEY / OPENAI_BASE_URL env 자동 사용
MODEL = os.getenv("ANSWER_MODEL", os.getenv("OPENAI_MODEL"))


@retry(wait=wait_random_exponential(min=5, max=30), stop=stop_after_attempt(3), reraise=True)
def answer_one(qa: dict) -> dict:
    prompt = PROMPT_MEMZERO.format(context=qa["context"], question=qa["question"])
    start = time.time()
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=1024,  # 정답은 5-6단어 지시라 넉넉함
    )
    qa["system_response"] = resp.choices[0].message.content
    qa["response_duration_ms"] = (time.time() - start) * 1000
    return qa


def main(results_path: str, max_workers: int):
    users = [json.loads(l) for l in open(results_path, encoding="utf-8") if l.strip()]

    # 답변 없는 질문만 수집 (재실행 시 자동 resume 됨)
    pending = [
        qa
        for user in users
        for s in user["sessions"]
        for qa in s.get("questions", [])
        if "system_response" not in qa
    ]
    print(f"답변 생성 대상: {len(pending)}개 질문")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(answer_one, qa) for qa in pending]
        for f in tqdm(as_completed(futures), total=len(futures)):
            f.result()  # 예외 있으면 여기서 드러남

    # 원자적 저장: 임시 파일에 다 쓰고 rename (도중에 죽어도 원본 보존됨)
    tmp_path = results_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        for user in users:
            f.write(json.dumps(user, ensure_ascii=False) + "\n")
    os.replace(tmp_path, results_path)
    print(f"done -> {results_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default="results/mem0-classic-oss/memzero-oss-smoke/memzero-oss_eval_results.jsonl")
    parser.add_argument("--max-workers", type=int, default=8)
    args = parser.parse_args()
    main(args.results, args.max_workers)