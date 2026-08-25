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

# ⚠ 출력 길이를 실제와 맞추는 것이 핵심임. 디코드가 병목이라 처리량은 req/s 가 아니라
#    **출력 tok/s** 로 결정됨. 2026-08-22 에 출력 250토큰으로 재고 "16워커면 충분"이라고
#    판단했는데, 실측 요청은 667~2,218토큰이었고 그래서 추정이 크게 빗나갔음.
#    실측은 vllm:generation_tokens_total 증분 / request_success_total 증분 으로 냄.
BODY = ("The user discussed their research schedule, mentioned preferring morning work blocks, "
        "and noted a deadline for the grant proposal. They also talked about switching from "
        "coffee to tea, planning a trip to Kyoto, and rescheduling a meeting with their advisor. ") * 6
PROMPT = ("You are a memory extraction system. Read the conversation below and extract every "
          "discrete fact worth remembering. For each fact give: the fact itself, why it matters, "
          "how confident you are, and what it supersedes if anything. Be exhaustive and verbose; "
          "cover preferences, schedule items, people, places, and any implied follow-ups. "
          "Then summarize the user's current state in detail.\n\n" + BODY)


def main(url, model, levels, n_per, max_tokens, effort=None):
    cli = OpenAI(base_url=url, api_key="dummy")

    def one(_):
        t = time.time()
        kw = {"reasoning_effort": effort} if effort else {}
        r = cli.chat.completions.create(
            model=model, messages=[{"role": "user", "content": PROMPT}],
            max_completion_tokens=max_tokens, temperature=0.0, **kw)
        return time.time() - t, (r.usage.completion_tokens if r.usage else 0)

    print(f"엔드포인트 {url} · 모델 {model} · 워커당 {n_per}건 · 출력 상한 {max_tokens}"
          f"{' · effort ' + effort if effort else ''}")
    print(f"{'동시성':>6s}{'초':>8s}{'req/s':>9s}{'출력tok/s':>11s}{'지연 중앙':>11s}{'직전 대비':>11s}{'요청당 출력':>12s}")
    prev = None
    for c in levels:
        one(0)  # 워밍업
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=c) as ex:
            # ⚠ 총 요청 수가 동시성에 비례해야 함. 고정하면 동시성이 그 수를 넘는 순간
            #    남는 워커가 놀아서 모든 레벨이 같은 값으로 나옴 (2026-08-25 에 이걸로
            #    "무릎이 12" 라고 잘못 판단했음. 12~48 이 전부 평평했던 이유임).
            res = list(ex.map(one, range(n_per * c)))
        dt = time.time() - t0
        rps = len(res) / dt
        tps = sum(r[1] for r in res) / dt
        lat = statistics.median(r[0] for r in res)
        # ⚠ '선형 대비 효율'(배수/c)로 읽으면 안 됨. 단일 요청의 고정 지연 때문에 c=2 에서
        #   이미 0.8 밑으로 떨어져 무릎을 놓침. 실제로 볼 것은 **한 단계 올렸을 때의 증가분**임
        mark = "" if prev is None else f"{100 * (rps / prev - 1):+9.0f}%"
        out_avg = sum(r[1] for r in res) / max(len(res), 1)
        print(f"{c:6d}{dt:8.1f}{rps:9.2f}{tps:11.0f}{lat:10.2f}s{mark:>11s}{out_avg:11.0f}")
        prev = rps
    print("\n읽는 법: '직전 대비' 증가가 뚝 떨어지는 지점이 무릎임. 그 앞 단계가 실용 상한임.")
    print("        투입은 페르소나 단위 병렬이라 페르소나 수(10)를 넘겨도 소용없음.")
    print("        답변·채점은 문항 단위라 그 제한이 없음.")
    print("        ⚠ '요청당 출력'이 실제 작업(667~2,218토큰)과 비슷해야 이 표를 믿을 수 있음.")
    print("        ⚠ 총 요청 = 워커당 x 동시성. 레벨마다 보낸 건수가 다르므로 '초' 는 서로 비교하지 않음.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8002/v1")
    p.add_argument("--model", default="openai/gpt-oss-120b")
    p.add_argument("--levels", default="1,2,4,8,16,24")
    p.add_argument("--n-per", type=int, default=4,
                   help="**워커당** 요청 수. 총 요청 = n_per x 동시성. "
                        "고정 총량으로 재면 동시성이 그 수를 넘는 순간 워커가 놀아 "
                        "모든 레벨이 같아짐")
    p.add_argument("--max-tokens", type=int, default=2048,
                   help="실측 요청의 출력이 667~2,218토큰이었음. 250 같은 작은 값으로 재면 "
                        "디코드 병목을 못 보고 동시성 상한을 과대평가함")
    p.add_argument("--effort", default=None, help="reasoning_effort (기본: 모델 기본값)")
    a = p.parse_args()
    main(a.url, a.model, [int(x) for x in a.levels.split(",") if x.strip()], a.n_per,
         a.max_tokens, a.effort)
