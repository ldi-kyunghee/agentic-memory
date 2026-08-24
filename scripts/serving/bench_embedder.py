"""임베더 지연·처리량 측정. TP=1 대 TP=2 를 가르기 위한 것임.

v3 는 classic 보다 임베더를 훨씬 많이 씀:
  - 입력 대화 블록 전체 (실측 중앙 900~1,000토큰, 최대 20,351)
  - 추출된 사실마다 (중앙 151자)
  - 엔티티마다 (매우 짧음)
  - 문항 검색 질의

그래서 **짧은 것과 긴 것을 따로** 잼. TP 는 작은 요청의 지연에 불리하므로 짧은 쪽이 관건임.

사용 (서버, 리포 루트에서):
    OPENAI_API_KEY=dummy uv run python scripts/serving/bench_embedder.py
    OPENAI_API_KEY=dummy uv run python scripts/serving/bench_embedder.py --url http://localhost:8001/v1
"""
import time
import argparse
import statistics
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI

# 실측 분포에 맞춘 표본
SHORT = "User switched from coffee to genmaicha tea after a doctor visit in March."      # 사실 (~150자)
ENTITY = "Martin Kim"                                                                     # 엔티티
WORD = ("The user discussed their research schedule and mentioned preferring morning "
        "work blocks before the grant proposal deadline. ")


def make(tokens):
    """대략 tokens 개가 되도록 반복 (1단어 ~1.3토큰 가정)."""
    n = max(1, int(tokens / (len(WORD.split()) * 1.3)))
    return WORD * n


def main(url, model, n_per, levels):
    cli = OpenAI(base_url=url, api_key="dummy")

    def one(text):
        t = time.time()
        cli.embeddings.create(model=model, input=text)
        return time.time() - t

    print(f"엔드포인트 {url} · 모델 {model}")
    print(f"\n{'입력':>16s}{'건수':>6s}{'지연 중앙':>11s}{'지연 p90':>10s}")
    cases = [("엔티티 (2단어)", ENTITY), ("사실 (~20토큰)", SHORT),
             ("블록 1k토큰", make(1000)), ("블록 8k토큰", make(8000))]
    for name, text in cases:
        one(text)  # 워밍업
        lat = sorted(one(text) for _ in range(n_per))
        print(f"{name:>16s}{len(lat):6d}{lat[len(lat)//2]:10.3f}s{lat[int(len(lat)*0.9)]:9.3f}s")

    print(f"\n동시 처리량 (사실 크기, 실제 투입에서 제일 잦음)")
    print(f"{'동시성':>8s}{'req/s':>10s}{'지연 중앙':>11s}{'직전 대비':>11s}")
    prev = None
    for c in levels:
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=c) as ex:
            lat = list(ex.map(lambda _: one(SHORT), range(n_per * 2)))
        dt = time.time() - t0
        rps = len(lat) / dt
        mark = "" if prev is None else f"{100*(rps/prev-1):+9.0f}%"
        print(f"{c:8d}{rps:10.1f}{statistics.median(lat):10.3f}s{mark:>11s}")
        prev = rps


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8001/v1")
    p.add_argument("--model", default="Qwen/Qwen3-Embedding-4B")
    p.add_argument("--n-per", type=int, default=12)
    p.add_argument("--levels", default="1,4,8,16")
    a = p.parse_args()
    main(a.url, a.model, a.n_per, [int(x) for x in a.levels.split(",") if x.strip()])
