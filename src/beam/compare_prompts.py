"""두 BEAM 채점본을 문항 단위로 짝지어 비교함.

같은 투입·같은 검색 결과 위에서 답변 생성 규약만 바꾼 두 벌을 대조하라고 만든 것임.
그래서 차이가 전부 그 규약의 몫이 됨. 다른 용도로 쓰려면 두 채점본이 같은 검색 결과에서
나왔는지 먼저 확인할 것.

짝짓기 키는 (conv, ability, idx, cutoff) 네 개임. 한쪽에만 있는 문항은 비교에서 빼고
개수를 보고함. 조용히 버리면 표가 거짓말을 함.

검정은 대응표본 Wilcoxon 부호순위임. 같은 문항을 두 규약이 각각 푸는 구조라 독립표본이
아님. 점수가 0/0.5/1 눈금이라 정규성도 기대할 수 없음.

사용:
  uv run python src/beam/compare_prompts.py \
    --a results/mem0-classic-oss/beam-judge-oss120-100k \
    --b results/mem0-classic-oss/beam-judge-oss120-100k-beamprompt \
    --label-a "mem0 프롬프트" --label-b "BEAM 공식"
"""
import os
import json
import glob
import argparse
from collections import defaultdict

from scipy.stats import wilcoxon


def load(judge_dir: str) -> dict:
    """채점본 디렉토리 -> {(conv, ability, idx, cutoff): record}"""
    out = {}
    files = sorted(glob.glob(os.path.join(judge_dir, "*.json")))
    if not files:
        raise SystemExit(f"채점본이 없음: {judge_dir}")
    for f in files:
        if os.path.basename(f).endswith("_error.json"):
            continue
        d = json.load(open(f, encoding="utf-8"))
        for r in d.get("records", []):
            out[(r["conv"], r["ability"], r["idx"], r["cutoff"])] = r
    return out


def fmt_delta(x: float | None) -> str:
    if x is None:
        return "     –"
    return f"{x:+6.3f}"


def stars(p: float | None) -> str:
    if p is None:
        return "   "
    return "***" if p < 0.001 else ("** " if p < 0.01 else ("*  " if p < 0.05 else "   "))


def paired_p(pairs: list[tuple[float, float]]) -> float | None:
    """차이가 전부 0이면 Wilcoxon이 예외를 냄. 그 경우는 p를 매기지 않음"""
    diffs = [b - a for a, b in pairs]
    if not diffs or all(abs(d) < 1e-12 for d in diffs):
        return None
    try:
        return float(wilcoxon([a for a, _ in pairs], [b for _, b in pairs]).pvalue)
    except ValueError:
        return None


def main(dir_a: str, dir_b: str, label_a: str, label_b: str):
    A, B = load(dir_a), load(dir_b)
    keys = sorted(set(A) & set(B))
    only_a, only_b = len(set(A) - set(B)), len(set(B) - set(A))

    print(f"A = {label_a}  ({dir_a})")
    print(f"B = {label_b}  ({dir_b})")
    print(f"짝지은 문항 {len(keys)}건 · A에만 {only_a}건 · B에만 {only_b}건")
    if only_a or only_b:
        print("  ⚠ 한쪽에만 있는 문항은 비교에서 뺐음. 개수가 크면 두 벌의 범위가 다른 것임")
    if not keys:
        raise SystemExit("겹치는 문항이 없음")

    cutoffs = sorted({k[3] for k in keys})
    abilities = sorted({k[1] for k in keys})

    # ---- cutoff별 전체 ----
    print(f"\n{'':22s}" + "".join(f"{f'top-{c}':>16s}" for c in cutoffs))
    print("-" * (22 + 16 * len(cutoffs)))
    for name, pick in ((label_a, 0), (label_b, 1)):
        row = []
        for c in cutoffs:
            ks = [k for k in keys if k[3] == c]
            v = [(A[k]["score"], B[k]["score"])[pick] for k in ks]
            row.append(f"{sum(v)/len(v):16.3f}")
        print(f"{name:22s}" + "".join(row))
    row, prow = [], []
    for c in cutoffs:
        ks = [k for k in keys if k[3] == c]
        pairs = [(A[k]["score"], B[k]["score"]) for k in ks]
        d = sum(b - a for a, b in pairs) / len(pairs)
        row.append(f"{fmt_delta(d):>13s}{stars(paired_p(pairs))}")
    print(f"{'차이 (B-A)':22s}" + "".join(row))

    # ---- 능력 × cutoff 차이 ----
    print(f"\n능력별 차이 (B-A). * p<0.05  ** p<0.01  *** p<0.001")
    print(f"{'':26s}" + "".join(f"{f'top-{c}':>13s}" for c in cutoffs) + f"{'전체':>15s}")
    print("-" * (26 + 13 * len(cutoffs) + 15))
    rows = []
    for ab in abilities:
        cells = []
        for c in cutoffs:
            ks = [k for k in keys if k[1] == ab and k[3] == c]
            if not ks:
                cells.append(f"{'–':>13s}")
                continue
            pairs = [(A[k]["score"], B[k]["score"]) for k in ks]
            cells.append(f"{fmt_delta(sum(b-a for a, b in pairs)/len(pairs)):>13s}")
        ks = [k for k in keys if k[1] == ab]
        pairs = [(A[k]["score"], B[k]["score"]) for k in ks]
        tot = sum(b - a for a, b in pairs) / len(pairs)
        rows.append((tot, ab, cells, paired_p(pairs)))
    for tot, ab, cells, p in sorted(rows, reverse=True):   # 많이 오른 능력부터
        print(f"{ab:26s}" + "".join(cells) + f"{fmt_delta(tot):>12s}{stars(p)}")

    # ---- 답변 길이 ----
    print(f"\n답변 길이 (문자 수, 중앙값)")
    print(f"{'':26s}{label_a:>14s}{label_b:>14s}{'배율':>10s}")
    print("-" * 64)

    def med(vals):
        v = sorted(vals)
        n = len(v)
        return (v[n//2] if n % 2 else (v[n//2-1] + v[n//2]) / 2) if v else 0

    for ab in ["(전체)"] + abilities:
        ks = keys if ab == "(전체)" else [k for k in keys if k[1] == ab]
        la = med([len(A[k].get("system_response") or "") for k in ks])
        lb = med([len(B[k].get("system_response") or "") for k in ks])
        ratio = f"{lb/la:9.2f}x" if la else "        –"
        print(f"{ab:26s}{la:14.0f}{lb:14.0f}{ratio}")

    # ---- 뒤집힌 문항 ----
    up = sum(1 for k in keys if B[k]["score"] > A[k]["score"])
    dn = sum(1 for k in keys if B[k]["score"] < A[k]["score"])
    same = len(keys) - up - dn
    print(f"\n문항 단위: 오름 {up} · 내림 {dn} · 동일 {same}  (총 {len(keys)})")

    # ---- event_ordering 부속 지표 ----
    eo = [k for k in keys if A[k].get("event_ordering") or B[k].get("event_ordering")]
    if eo:
        print(f"\nevent_ordering 부속 지표 ({len(eo)}건)")
        for field in ("tau_norm", "f1", "final_score"):
            va = [(A[k].get("event_ordering") or {}).get(field) for k in eo]
            vb = [(B[k].get("event_ordering") or {}).get(field) for k in eo]
            pair = [(a, b) for a, b in zip(va, vb) if a is not None and b is not None]
            if not pair:
                continue
            ma = sum(a for a, _ in pair) / len(pair)
            mb = sum(b for _, b in pair) / len(pair)
            zero_a = sum(1 for a, _ in pair if a == 0)
            zero_b = sum(1 for _, b in pair if b == 0)
            print(f"  {field:12s} {ma:6.3f} -> {mb:6.3f}  ({fmt_delta(mb-ma)})"
                  f"   0점 {zero_a} -> {zero_b}건")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--a", required=True, help="기준이 되는 채점본 디렉토리")
    p.add_argument("--b", required=True, help="비교할 채점본 디렉토리")
    p.add_argument("--label-a", default="A")
    p.add_argument("--label-b", default="B")
    a = p.parse_args()
    main(a.a, a.b, a.label_a, a.label_b)
