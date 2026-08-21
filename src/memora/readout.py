"""Memora 한 기간(weekly/monthly/quarterly)의 산출물을 한 번에 판독함.

BEAM의 compare_prompts.py 자리임. 다만 Memora는 프롬프트 대조가 아니라 **저장소 행동**이
관심사라 보는 것이 다름.

내는 것
  1. 투입 요약 — 세션·저장 메모리, 데이터셋이 의도한 연산 대 mem0가 실제로 한 연산
  2. 삭제 수행률 — 이 벤치마크의 핵심. HaluMem에는 삭제가 없어 못 보던 갈래임
  3. 과제별 FAMA / MPA / 페널티
  4. 페르소나별 편차 — 순위를 말하기 전에 노이즈 바닥을 확인함 (BEAM에서 데인 것)
  5. 삭제 수행률과 forgetting 정확도의 상관 — 페널티가 저장소 탓인지 보는 직접 검정
  6. 답변 길이 — FAMA가 과다 포함을 벌주므로 길이를 함께 봐야 함

사용:
  uv run python src/memora/readout.py --period weekly
  uv run python src/memora/readout.py --period weekly --judge results/.../다른채점본
"""
import os
import sys
import json
import glob
import argparse
import statistics
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fama import aggregate   # noqa: E402

R = "results/mem0-classic-oss"


def load_ingest(path: str) -> list[dict]:
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def load_judge(d: str) -> list[dict]:
    recs = []
    for f in sorted(glob.glob(os.path.join(d, "*.json"))):
        if f.endswith("_error.json"):
            continue
        with open(f, encoding="utf-8") as fh:
            recs += json.load(fh).get("records", [])
    return recs


def spearman(xs, ys):
    """scipy 없이도 돌게 순위 상관을 직접 계산함 (동점은 평균 순위)."""
    n = len(xs)
    if n < 3:
        return None, None
    def rank(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    if not den:
        return None, None
    rho = num / den
    # n이 작아 정확한 p는 안 냄. |rho| 와 n 만 보고 판단하도록 n 을 함께 돌려줌
    return rho, n


def main(period: str, ingest_path: str, answers_path: str, judge_dir: str):
    print(f"━━━ Memora {period} ━━━\n")

    # ---------- 1. 투입 ----------
    convs = load_ingest(ingest_path)
    intent, actual = Counter(), Counter()
    per_persona = {}
    for c in convs:
        pi, pa = Counter(), Counter()
        for s in c["ingest"]:
            pi[str(s.get("operation"))] += 1
            for e in s["events"]:
                pa[e["op"]] += 1
        intent += pi
        actual += pa
        per_persona[c["persona"]] = {"intent": pi, "actual": pa,
                                     "stored": c.get("stored_memories"),
                                     "sessions": c.get("n_sessions")}
    n_sess = sum(v["sessions"] or 0 for v in per_persona.values())
    stored = [v["stored"] for v in per_persona.values() if v["stored"] is not None]
    print(f"페르소나 {len(convs)} · 세션 {n_sess:,} · 저장 메모리 {sum(stored):,} "
          f"(페르소나당 {min(stored)}~{max(stored)})")
    print(f"  데이터셋 의도: {dict(intent.most_common())}")
    print(f"  mem0 실제    : {dict(actual.most_common())}")

    # ---------- 2. 삭제 수행률 ----------
    di, da = intent.get("delete", 0), actual.get("DELETE", 0)
    ui, ua = intent.get("update", 0), actual.get("UPDATE", 0)
    print()
    print("=== 연산 수행률 (데이터셋 의도 대비) ===")
    print(f"  삭제  의도 {di:5,} → mem0 {da:5,}  ({100 * da / di if di else 0:5.1f}%)")
    print(f"  갱신  의도 {ui:5,} → mem0 {ua:5,}  ({100 * ua / ui if ui else 0:5.1f}%)")
    print(f"  추가  의도 {intent.get('add', 0):5,} → mem0 {actual.get('ADD', 0):5,}"
          f"  ({100 * actual.get('ADD', 0) / intent.get('add', 1):5.1f}%)")
    print("  ⚠ 추가가 100%를 크게 넘는 것은 정상임. 세션에 지정된 연산은 하나지만 mem0는")
    print("    대화에 섞인 부수적 사실도 전부 뽑음. 삭제·갱신 수행률이 읽을 값임.")

    # ---------- 3. 채점 ----------
    recs = load_judge(judge_dir)
    if not recs:
        print(f"\n채점본이 없음: {judge_dir}")
        return
    print()
    print("=== 과제별 ===")
    print(f"{'과제':16s}{'문항':>6s}{'FAMA':>9s}{'MPA':>9s}{'페널티':>9s}")
    print("-" * 50)
    for t, v in aggregate(recs).items():
        g = lambda x: f"{x:9.2f}" if x is not None else f"{'–':>9s}"
        print(f"{t:16s}{v['n']:6d}{g(v['fama'])}{g(v['mpa'])}{g(v['penalty'])}")

    # ---------- 4. 페르소나별 편차 ----------
    byp = defaultdict(list)
    for r in recs:
        byp[r["persona"]].append(r)
    rows = []
    for p, rs in byp.items():
        a = aggregate(rs)["(전체)"]
        faa = statistics.mean(r["faa"] for r in rs)
        rows.append((a["fama"], p, a["mpa"], a["penalty"], faa, len(rs)))
    rows.sort()
    print()
    print("=== 페르소나별 (전체 과제) ===")
    print(f"{'페르소나':26s}{'문항':>5s}{'FAMA':>9s}{'MPA':>9s}{'페널티':>9s}{'FAA':>8s}{'삭제수행':>9s}")
    print("-" * 76)
    for fama, p, mpa, pen, faa, n in rows:
        pp = per_persona.get(p, {})
        d_i = (pp.get("intent") or {}).get("delete", 0)
        d_a = (pp.get("actual") or {}).get("DELETE", 0)
        dr = f"{100 * d_a / d_i:8.0f}%" if d_i else f"{'–':>9s}"
        print(f"{p:26s}{n:5d}{fama:9.2f}{mpa:9.2f}{pen:9.2f}{faa * 100:8.1f}{dr}")
    fs = [r[0] for r in rows]
    if len(fs) > 1:
        sd = statistics.stdev(fs)
        print(f"\n  FAMA 페르소나 간 SD {sd:.2f} · 범위 {min(fs):.1f}~{max(fs):.1f}")
        print(f"  ⚠ 이 폭보다 작은 차이는 순위로 말하지 않음 (BEAM에서 데인 것)")

    # ---------- 5. 삭제 수행률 대 forgetting 정확도 ----------
    xs, ys = [], []
    for fama, p, mpa, pen, faa, n in rows:
        pp = per_persona.get(p, {})
        d_i = (pp.get("intent") or {}).get("delete", 0)
        if not d_i:
            continue
        xs.append(100 * (pp.get("actual") or {}).get("DELETE", 0) / d_i)
        ys.append(faa * 100)
    rho, nn = spearman(xs, ys)
    print()
    print("=== 삭제 수행률 대 forgetting 정확도 (페르소나 단위) ===")
    if rho is None:
        print("  페르소나가 적어 상관을 내지 않음")
    else:
        print(f"  Spearman rho = {rho:+.3f} (n={nn})")
        print("  양수이고 크면 '삭제를 잘한 페르소나가 무효 언급도 적다'는 뜻임.")
        print("  즉 FAMA 페널티의 원인이 저장소 쪽이라는 직접 증거가 됨.")
        print("  0 근처면 페널티의 원인이 다른 데 있음 (검색 또는 답변 규약).")

    # ---------- 6. 답변 길이 ----------
    if os.path.exists(answers_path):
        alist = load_ingest(answers_path)
        L = [len((q.get("answer") or {}).get("system_response") or "")
             for c in alist for q in c["questions"]]
        fr = Counter((q.get("answer") or {}).get("finish_reason")
                     for c in alist for q in c["questions"])
        L = [x for x in L if x]
        if L:
            L.sort()
            print()
            print(f"=== 답변 길이 ===")
            print(f"  {len(L)}건 · 중앙 {L[len(L)//2]:,}자 · 최대 {L[-1]:,}자 · finish {dict(fr)}")
            print("  ⚠ FAMA는 과다 포함을 벌줌. 길수록 무효 항목을 언급할 확률이 오름.")

    bad = [c for r in recs for c in r["criteria"] if c["got"] is None]
    ncrit = sum(len(r["criteria"]) for r in recs)
    print(f"\n판정 실패 {len(bad)}/{ncrit}건")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--period", required=True, choices=("weekly", "monthly", "quarterly"))
    p.add_argument("--version", default=None, help="기본 {period}-oss120b")
    p.add_argument("--ingest", default=None)
    p.add_argument("--answers", default=None)
    p.add_argument("--judge", default=None)
    a = p.parse_args()
    v = a.version or f"{a.period}-oss120b"
    main(a.period,
         a.ingest or f"{R}/memora-{v}/memora_eval_results.jsonl",
         a.answers or f"{R}/memora-gen-{a.period}/answers.jsonl",
         a.judge or f"{R}/memora-judge-{a.period}")
