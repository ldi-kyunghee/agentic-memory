"""Update 판정을 사람 합의와 대조한다 — judge 모델별 일치율·Cohen κ.

목적: "더 나은 judge를 쓰면 사람과의 일치가 올라가는가"에 답한다.
대조 가능한 집합은 **판정 검토 큐의 update 항목뿐**이다 (사람 라벨이 거기에만 있다).

⚠ 사람 합의를 '정답'으로 놓는 것 자체에 한계가 있다 — 3인이 항상 합의하지는 않는다.
   합의가 안 된 항목(동률)은 대조에서 빼고, 그 수를 함께 보고한다.
⚠ n이 40 미만이라 κ 신뢰구간이 ±0.2 수준이다. 등급(견고/보통/약함) 구분까지가 한계이고
   소수점 순위는 읽지 않는다. 부트스트랩 CI를 함께 출력한다.

실행:
  uv run python eval/mem0-classic-oss/compare_update_judges.py \
      --db src/web-dashboard/data/comments.sqlite3
"""

import os
import json
import random
import sqlite3
import argparse
from collections import Counter, defaultdict

REJUDGE_DIR = "results/mem0-classic-oss/rejudge-update"


def kappa(pairs, ci=True):
    n = len(pairs)
    if not n:
        return {"n": 0}

    def k_of(ps):
        m = len(ps)
        if not m:
            return None
        po = sum(1 for a, b in ps if a == b) / m
        L = {x for p in ps for x in p}
        ca = {l: sum(1 for a, _ in ps if a == l) / m for l in L}
        cb = {l: sum(1 for _, b in ps if b == l) / m for l in L}
        pe = sum(ca[l] * cb[l] for l in L)
        return None if pe >= 1 else (po - pe) / (1 - pe)

    po = sum(1 for a, b in pairs if a == b) / n
    k = k_of(pairs)
    out = {"n": n, "agree": round(po * 100, 1), "kappa": None if k is None else round(k, 3)}
    if ci and k is not None and n >= 10:
        rnd = random.Random(20260812)   # 고정 시드 — 재실행마다 값이 흔들리지 않게
        ks = sorted(x for _ in range(2000)
                    if (x := k_of([pairs[rnd.randrange(n)] for _ in range(n)])) is not None)
        if len(ks) >= 200:
            out["ci"] = (round(ks[int(len(ks) * .025)], 3), round(ks[int(len(ks) * .975)], 3))
    return out


def grade(k):
    if k is None:
        return ""
    return ("거의 완전" if k >= .8 else "견고" if k >= .6 else
            "보통" if k >= .4 else "약함" if k >= .2 else "거의 없음")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="src/web-dashboard/data/comments.sqlite3")
    args = ap.parse_args()

    # 사람 라벨 -> 항목별 합의
    con = sqlite3.connect(args.db)
    rows = con.execute("SELECT annotator, run, uuid, session_id, idx, label FROM annotations "
                       "WHERE rec_type='update' AND label != ''").fetchall()
    con.close()
    by_item = defaultdict(dict)
    for ann, run, uid, sid, idx, lab in rows:
        by_item[(run, uid, sid, idx)][ann] = lab

    def consensus(labs):
        t = Counter(labs.values())
        top = max(t.values())
        win = [k for k, v in t.items() if v == top]
        return win[0] if len(win) == 1 else None

    # judge 재채점 결과들
    files = sorted(f for f in os.listdir(REJUDGE_DIR) if f.endswith(".json")) if os.path.isdir(REJUDGE_DIR) else []
    judges = {}
    base_pairs, base_seen = [], {}
    for fn in files:
        d = json.load(open(os.path.join(REJUDGE_DIR, fn), encoding="utf-8"))
        if d.get("scope") != "queue":
            continue
        name = f"{d['model']} ({d.get('effort') or 'none'})"
        prs, cost = [], {"in": 0, "out": 0, "reasoning": 0}
        for it in d["items"]:
            key = (it["run"], it["uuid"], it["session_id"], it["idx"])
            for k in cost:
                cost[k] += (it.get("usage") or {}).get(k) or 0
            h = consensus(by_item.get(key, {}))
            if h and it.get("new_label"):
                prs.append((h, it["new_label"]))
            # 기준 judge(gpt-oss-120b) 라벨은 어느 파일에나 같은 값이 들어 있다 — 한 번만 모은다
            if h and it.get("base_label") and key not in base_seen:
                base_seen[key] = True
                base_pairs.append((h, it["base_label"]))
        judges[name] = {"pairs": prs, "cost": cost, "n_items": d["n"]}

    n_total = len({k for k in by_item})
    n_tie = sum(1 for k, v in by_item.items() if consensus(v) is None)
    n_by = Counter(len(v) for v in by_item.values())
    print(f"사람 라벨 항목 {n_total}개 · 합의 불성립(동률) {n_tie}개 · 라벨한 사람 수 분포 {dict(n_by)}")
    print()
    print(f"{'judge':34s} {'n':>4s} {'일치율':>7s} {'κ':>7s} {'95% CI':>16s}  등급")
    res = [("gpt-oss-120b (high) — 기존", kappa(base_pairs))]
    res += [(name, kappa(v["pairs"])) for name, v in sorted(judges.items())]
    for name, k in res:
        if not k.get("n"):
            continue
        ci = f"[{k['ci'][0]:+.2f}, {k['ci'][1]:+.2f}]" if k.get("ci") else "-"
        print(f"{name:34s} {k['n']:>4d} {k['agree']:>6.1f}% {str(k['kappa']):>7s} {ci:>16s}  {grade(k['kappa'])}")

    print("\n불일치 방향 (사람 합의 → judge)")
    def diffs(prs):
        return dict(Counter(f"{h}→{m}" for h, m in prs if h != m).most_common(4))
    print(f"  {'gpt-oss-120b':28s} {diffs(base_pairs)}")
    for name, v in sorted(judges.items()):
        print(f"  {name:28s} {diffs(v['pairs'])}")

    print("\n토큰 사용 (재채점분)")
    for name, v in sorted(judges.items()):
        c = v["cost"]
        print(f"  {name:28s} 입력 {c['in']:>8,} · 출력 {c['out']:>7,} (reasoning {c['reasoning']:>7,})")

    print("\n⚠ n이 작아 κ 신뢰구간이 넓다 — 등급 구분까지가 한계이고 소수점 순위는 읽지 않는다.")
    print("⚠ '사람 합의'도 정답이 아니다: 3인이 항상 일치하지 않으며 동률 항목은 제외됐다.")


if __name__ == "__main__":
    main()
