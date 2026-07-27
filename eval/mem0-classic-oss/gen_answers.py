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
REASONING_EFFORT = os.getenv("ANSWER_REASONING_EFFORT")  # gpt-5 계열일 때만 설정 (예: minimal)


@retry(wait=wait_random_exponential(min=5, max=30), stop=stop_after_attempt(3), reraise=True)
def answer_one(qa: dict) -> dict:
    prompt = PROMPT_MEMZERO.format(context=qa["context"], question=qa["question"])
    start = time.time()
    kwargs = dict(model=MODEL, messages=[{"role": "user", "content": prompt}])
    if REASONING_EFFORT:
        # reasoning 모델: temperature 미지원, max_tokens 대신 max_completion_tokens (추론 토큰 포함 예산)
        kwargs["reasoning_effort"] = REASONING_EFFORT
        kwargs["max_completion_tokens"] = 4096
    else:
        kwargs["temperature"] = 0.0
        kwargs["max_tokens"] = 1024  # 정답은 5-6단어 지시라 넉넉함
    resp = client.chat.completions.create(**kwargs)
    qa["system_response"] = resp.choices[0].message.content
    qa["response_duration_ms"] = (time.time() - start) * 1000
    return qa


def main(results_path: str, max_workers: int, out_path: str | None = None, regen: bool = False, user_num: int | None = None):
    users = [json.loads(l) for l in open(results_path, encoding="utf-8") if l.strip()]

    if user_num:
        # 데이터셋 순서 기준 첫 N명만 (judge.py --user-num과 동일 의미론 — 병합 jsonl은 uuid 정렬이므로)
        order = []
        for i, l in enumerate(open("dataset/HaluMem-Medium.jsonl", encoding="utf-8")):
            if i >= user_num:
                break
            order.append(json.loads(l)["uuid"])
        by_uuid = {u["uuid"]: u for u in users}
        users = [by_uuid[uid] for uid in order if uid in by_uuid]
        print(f"user-num={user_num}: 데이터셋 순서 기준 {len(users)}명 선별")

    # 답변 없는 질문만 수집 (재실행 시 자동 resume). --regen이면 기존 답변 무시하고 전부 재생성
    pending = [
        qa
        for user in users
        for s in user["sessions"]
        for qa in s.get("questions", [])
        if regen or "system_response" not in qa
    ]
    print(f"답변 생성 대상: {len(pending)}개 질문")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(answer_one, qa) for qa in pending]
        for f in tqdm(as_completed(futures), total=len(futures)):
            f.result()  # 예외 있으면 여기서 드러남

    # 원자적 저장. --out 지정 시 원본은 보존하고 새 경로에 저장 (generator 교체 실험용 — 원본 답변 파괴 금지)
    dest = out_path or results_path
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp_path = dest + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        for user in users:
            f.write(json.dumps(user, ensure_ascii=False) + "\n")
    os.replace(tmp_path, dest)
    print(f"done -> {dest}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default="results/mem0-classic-oss/memzero-oss-smoke/memzero-oss_eval_results.jsonl")
    parser.add_argument("--max-workers", type=int, default=20)
    parser.add_argument("--out", default=None, help="저장 경로 (기본: --results in-place. generator 실험 시 반드시 별도 지정)")
    parser.add_argument("--regen", action="store_true", help="기존 system_response 무시하고 전부 재생성")
    parser.add_argument("--user-num", type=int, default=None, help="데이터셋 순서 기준 첫 N명만")
    args = parser.parse_args()
    main(args.results, args.max_workers, args.out, args.regen, args.user_num)