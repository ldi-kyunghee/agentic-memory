"""동시성별 처리량 측정. `--max-workers` 를 추측이 아니라 실측으로 정하기 위함임.

mem0 agent LLM 호출을 흉내냄: 세션 본문(실측 중앙 312토큰) + 추출 프롬프트, 출력 250토큰.
동시성을 올려도 req/s 가 안 오르면 concurrency 가 아니라 GPU 연산이 한계임.

⚠ 서버를 다른 프로젝트와 함께 쓰는 동안에는 돌리지 않음. 그쪽 요청을 밀어냄.
   엔드포인트가 우리 것만일 때 재서빙 직후에 한 번 잼.

사용 (서버, 리포 루트에서):
    OPENAI_API_KEY=dummy uv run python scripts/serving/bench_concurrency.py
    OPENAI_API_KEY=dummy uv run python scripts/serving/bench_concurrency.py --levels 1,4,8,16,24,32
"""
import time
import argparse
import statistics
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI

BODY = ("The user discussed their research schedule, mentioned preferring morning work blocks, "
        "and noted a deadline for the grant proposal. They also talked about switching from "
        "coffee to tea, planning a trip to Kyoto, and rescheduling a meeting with their advisor. ") * 6
PROMPT = ("You are a memory extraction system. Extract discrete facts from the conversation below "
          "as a JSON list. Return only facts that would be useful to remember later.\n\n" + BODY)


def main(url, model, levels, n_per, max_tokens):
    cli = OpenAI(base_url=url, api_key="dummy")

    def one(_):
        t = time.time()
        r = cli.chat.completions.create(
            model=model, messages=[{"role": "user", "content": PROMPT}],
            max_completion_tokens=max_tokens, temperature=0.0)
        return time.time() - t, (r.usage.completion_tokens if r.usage else 0)

    print(f"엔드포인트 {url} · 모델 {model} · 레벨당 {n_per}건")
    print(f"{'동시성':>6s}{'초':>8s}{'req/s':>9s}{'출력tok/s':>11s}{'지연 중앙':>11s}{'직전 대비':>11s}")
    prev = None
    for c in levels:
        one(0)  # 워밍업
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=c) as ex:
            res = list(ex.map(one, range(n_per)))
        dt = time.time() - t0
        rps = len(res) / dt
        tps = sum(r[1] for r in res) / dt
        lat = statistics.median(r[0] for r in res)
        # ⚠ '선형 대비 효율'(배수/c)로 읽으면 안 됨. 단일 요청의 고정 지연 때문에 c=2 에서
        #   이미 0.8 밑으로 떨어져 무릎을 놓침. 실제로 볼 것은 **한 단계 올렸을 때의 증가분**임
        mark = "" if prev is None else f"{100 * (rps / prev - 1):+9.0f}%"
        print(f"{c:6d}{dt:8.1f}{rps:9.2f}{tps:11.0f}{lat:10.2f}s{mark:>11s}")
        prev = rps
    print("\n읽는 법: '직전 대비' 증가가 뚝 떨어지는 지점이 무릎임. 그 앞 단계가 실용 상한임.")
    print("        투입은 페르소나 단위 병렬이라 페르소나 수(10)를 넘겨도 소용없음.")
    print("        답변·채점은 문항 단위라 그 제한이 없음.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8002/v1")
    p.add_argument("--model", default="openai/gpt-oss-120b")
    p.add_argument("--levels", default="1,2,4,8,16,24")
    p.add_argument("--n-per", type=int, default=16)
    p.add_argument("--max-tokens", type=int, default=250)
    a = p.parse_args()
    main(a.url, a.model, [int(x) for x in a.levels.split(",") if x.strip()], a.n_per, a.max_tokens)
