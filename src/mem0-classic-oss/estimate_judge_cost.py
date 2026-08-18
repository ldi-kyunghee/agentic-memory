"""Judge 재채점 비용 산정 — 기존 산출물에서 실제 프롬프트를 재구성해 토큰을 세고 모델별 비용을 계산함.

사용:
    uv run python src/mem0-classic-oss/estimate_judge_cost.py \
        --results results/mem0-classic-oss/memzero-oss-full-traced/memzero-oss_eval_results.jsonl

- 프롬프트 구성은 judge.py의 build_inputs + 서브모듈 템플릿을 그대로 재사용 -> 실제 재채점과 동일한 입력임
- 토큰 근사: chars/4 (영문 기준, ±10~15%). 모델별 토크나이저 차이는 이 오차 안에 포함됨
- ⚠ PRICES 단가는 반드시 공식 가격 페이지에서 확인 후 갱신할 것 (아래 값은 2026-01 지식 기준 추정)
"""

import sys
import json
import argparse

sys.path.insert(0, "eval/mem0-classic-oss")
from judge import build_inputs  # noqa: E402
from eval_tools import (  # noqa: E402
    EVALUATION_PROMPT_FOR_MEMORY_INTEGRITY,
    EVALUATION_PROMPT_FOR_MEMORY_ACCURACY,
    EVALUATION_PROMPT_FOR_UPDATE_MEMORY,
    EVALUATION_PROMPT_FOR_QUESTION,
)

# $/1M tokens (input, output) — 2026-07 기준, 집계 사이트 교차 확인값 (⚠ 계약 전 공식 가격 페이지 재확인)
# Batch API 사용 시 양사 모두 ~50% 추가 할인 — judge는 완전 배치 가능 작업이라 적용 대상임
PRICES = {
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5-mini(legacy)": (0.25, 2.00),
    "gpt-4o-mini(legacy)": (0.15, 0.60),   # 우리 calibration 앵커 — 제공 여부 확인 필요
    "claude-haiku-4.5": (1.00, 5.00),
    "gpt-5.6-luna": (1.00, 6.00),
    "claude-sonnet-5(intro)": (2.00, 10.00),  # ~2026-08-31 프로모션가, 이후 3.00/15.00
    "gpt-5.6-terra": (2.50, 15.00),
    "claude-opus-4.8": (5.00, 25.00),
    "vllm-local": (0.0, 0.0),
}

OUT_TOKENS_PER_CALL = 150  # 판정 응답(reasoning+JSON) 가정치


def count(results_path: str) -> dict:
    users = [json.loads(l) for l in open(results_path, encoding="utf-8")]
    stats = {k: {"n": 0, "chars": 0} for k in ["integrity", "accuracy", "update", "qa"]}
    for u in users:
        integ, acc, upd, qas = build_inputs(u)
        for m, ex in integ:
            if not ex.strip():
                continue  # 추출 0건 세션은 LLM 호출 없이 0점 처리됨 (비용 0)
            p = EVALUATION_PROMPT_FOR_MEMORY_INTEGRITY.format(
                memories=ex, expected_memory_point=m["memory_content"])
            stats["integrity"]["n"] += 1
            stats["integrity"]["chars"] += len(p)
        for d, g, m in acc:
            p = EVALUATION_PROMPT_FOR_MEMORY_ACCURACY.format(
                dialogue=d, golden_memories=g, candidate_memory=m["memory_content"])
            stats["accuracy"]["n"] += 1
            stats["accuracy"]["chars"] += len(p)
        for m in upd:
            p = EVALUATION_PROMPT_FOR_UPDATE_MEMORY.format(
                memories="\n".join(m["memories_from_system"]),
                updated_memory=m["memory_content"],
                original_memory="\n".join(m["original_memories"]))
            stats["update"]["n"] += 1
            stats["update"]["chars"] += len(p)
        for q in qas:
            p = EVALUATION_PROMPT_FOR_QUESTION.format(
                question=q["question"], reference_answer=q["answer"],
                key_memory_points="\n".join(e["memory_content"] for e in q["evidence"]),
                response=q["system_response"])
            stats["qa"]["n"] += 1
            stats["qa"]["chars"] += len(p)
    return stats


def main(results_path: str):
    stats = count(results_path)
    total_n = sum(v["n"] for v in stats.values())
    total_in = sum(v["chars"] // 4 for v in stats.values())
    total_out = total_n * OUT_TOKENS_PER_CALL

    print(f'{"카테고리":<12}{"호출수":>9}{"입력토큰":>14}{"평균/콜":>9}')
    for k, v in stats.items():
        tok = v["chars"] // 4
        print(f'{k:<12}{v["n"]:>9,}{tok:>14,}{tok // max(v["n"], 1):>9,}')
    print(f'{"합계":<12}{total_n:>9,}{total_in:>14,}')
    print(f'\n출력 추정: {total_n:,} × {OUT_TOKENS_PER_CALL} = {total_out:,} 토큰\n')

    print(f'{"모델":<16}{"입력비":>9}{"출력비":>9}{"합계":>10}')
    for model, (pi, po) in PRICES.items():
        ci, co = total_in / 1e6 * pi, total_out / 1e6 * po
        print(f'{model:<16}{ci:>8.1f}${co:>8.1f}${ci + co:>9.1f}$')
    print("\n※ 단가는 추정치 — 공식 가격 확인 후 PRICES 갱신할 것")
    print("※ accuracy가 비용 대부분 — 세션 단위 프리픽스 캐싱 시 대폭 절감 가능 (모델별 캐시 단가 별도)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--results", default="results/mem0-classic-oss/memzero-oss-full-traced/memzero-oss_eval_results.jsonl")
    args = p.parse_args()
    main(args.results)
