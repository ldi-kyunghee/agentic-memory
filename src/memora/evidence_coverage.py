"""검색 예산(cutoff)이 근거 도달률과 점수에 어떻게 연결되는지 계산함.

Memora 문항에는 `memory_evidence`(답에 들어가야 할 사실 조각)가 붙어 있음. 그것이
  ① mem0 가 한 번이라도 뽑았는가      -> 추출 상한
  ② cutoff 안의 검색 결과에 들어왔는가 -> 답변 모델이 실제로 본 재료
를 갈라 보면, remembering 붕괴가 추출 실패인지 검색 실패인지 가려짐.

저장소 원본은 산출물에 없지만 ingest 이벤트 로그에 ADD/UPDATE 텍스트가 전부 남아 있음.
그 합집합이 '한 번이라도 알고 있던 것'의 상한임.

⚠ 매칭은 휴리스틱임 (부분문자열 또는 불용어 제거 후 토큰 겹침 60%). 음성 대조로 바닥을
   함께 냄: 같은 근거를 **다른 페르소나**의 추출물에 맞춰본 비율. 이 값이 높으면 아래
   수치를 믿으면 안 됨. monthly 실측 기준 자기 95.9% 대 남 14% 수준임.

사용:
    uv run python src/memora/evidence_coverage.py --period monthly \
        --ingest results/mem0-classic-oss/memora-monthly-k800-oss120b/memora_eval_results.jsonl \
        --cutoffs 50,100,200,400,800 --judge-prefix memora-judge-monthly-k
"""
import os
import re
import json
import glob
import math
import argparse
import statistics

R = "results/mem0-classic-oss"
STOP = set("the a an of to in for and or my me i is are was were on at with about this that "
           "have has had do does did will would can could your his her their it its".split())


def toks(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP and len(w) > 2}


def leaves(ev) -> list[str]:
    """memory_evidence 안의 문자열 값을 전부 끌어냄. 구조가 문항마다 달라 재귀로 훑음."""
    out = []

    def walk(x):
        if isinstance(x, dict):
            if "value" in x and isinstance(x["value"], (str, int, float)):
                out.append(str(x["value"]))
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, str):
            out.append(x)

    walk(ev)
    return [s for s in out if len(s) > 3]


def covered(needle: str, pool_lower: list[str], pool_tokens: list[set], thr=0.6) -> bool:
    n = needle.lower().strip()
    for t in pool_lower:
        if n in t:
            return True
    nt = toks(needle)
    if not nt:
        return False
    return any(len(nt & tt) / len(nt) >= thr for tt in pool_tokens)


def pool(texts: list[str]):
    return [t.lower() for t in texts], [toks(t) for t in texts]


def sign_test(diffs: list[float]) -> tuple[int, int, float]:
    d = [x for x in diffs if x != 0]
    pos = sum(1 for x in d if x > 0)
    neg = len(d) - pos
    if not d:
        return 0, 0, 1.0
    p = sum(math.comb(len(d), i) for i in range(0, min(pos, neg) + 1)) / 2 ** len(d) * 2
    return pos, neg, min(p, 1.0)


def spearman(xs, ys):
    n = len(xs)
    if n < 3:
        return None

    def rank(v):
        o = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[o[j + 1]] == v[o[i]]:
                j += 1
            for k in range(i, j + 1):
                r[o[k]] = (i + j) / 2 + 1
            i = j + 1
        return r

    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return round(sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / den, 4) if den else None


def load_judge(d: str) -> dict:
    out = {}
    for f in sorted(glob.glob(os.path.join(d, "*.json"))):
        if f.endswith("_error.json"):
            continue
        for x in json.load(open(f, encoding="utf-8"))["records"]:
            out[(x["persona"], x["question_id"])] = x
    return out


def main(period, ingest, cutoffs, judge_prefix, task, out):
    convs = [json.loads(l) for l in open(ingest, encoding="utf-8") if l.strip()]
    ever = {c["persona"]: pool([e["text"] for s in c["ingest"]
                                for e in (s.get("events") or []) if e]) for c in convs}
    qs = [(c["persona"], q) for c in convs for q in c["questions"] if q["task"] == task]

    n_ev = sum(len(leaves(q.get("memory_evidence") or {})) for _, q in qs)
    n_ext = sum(sum(1 for e in leaves(q.get("memory_evidence") or {}) if covered(e, *ever[p]))
                for p, q in qs)

    # 음성 대조: 남의 페르소나 추출물에 맞춰본 오탐 바닥
    names = sorted(ever)
    n_other = sum(sum(1 for e in leaves(q.get("memory_evidence") or {})
                      if covered(e, *ever[names[(names.index(p) + 1) % len(names)]]))
                  for p, q in qs)

    per_cut = {}
    for K in cutoffs:
        hit = 0
        per_q = {}
        for p, q in qs:
            top = [m["memory"] for m in (q.get("retrieved") or [])[:K]]
            pl = pool(top)
            ev = leaves(q.get("memory_evidence") or {})
            c = sum(1 for e in ev if covered(e, *pl))
            hit += c
            if ev:
                per_q[f"{p}|{q['question_id']}"] = round(c / len(ev), 4)
        per_cut[K] = {"coverage": round(100 * hit / n_ev, 2), "per_q": per_q}

    res = {"period": period, "task": task, "ingest": ingest,
           "n_questions": len(qs), "n_evidence": n_ev,
           "extracted": round(100 * n_ext / n_ev, 2),
           "false_positive_floor": round(100 * n_other / n_ev, 2),
           "store_median": sorted(c["stored_memories"] for c in convs)[len(convs) // 2],
           "retrieved_median": sorted(len(q.get("retrieved") or []) for _, q in qs)[len(qs) // 2],
           "cutoffs": {str(K): per_cut[K]["coverage"] for K in cutoffs}}

    # 자연 대조: k 를 올려 근거가 새로 들어온 문항 대 주변 메모리만 늘어난 문항
    lo, hi = cutoffs[0], [K for K in cutoffs if os.path.isdir(f"{R}/{judge_prefix}{K}")][-1]
    jl, jh = load_judge(f"{R}/{judge_prefix}{lo}"), load_judge(f"{R}/{judge_prefix}{hi}")
    gain, ctrl, dc, dm = [], [], [], []
    for key, c_lo in per_cut[lo]["per_q"].items():
        p, qid = key.split("|", 1)
        c_hi = per_cut[hi]["per_q"].get(key)
        if c_hi is None or (p, qid) not in jl or (p, qid) not in jh:
            continue
        d_mpa = 100 * (jh[(p, qid)]["mpa"] - jl[(p, qid)]["mpa"])
        dc.append(100 * (c_hi - c_lo))
        dm.append(d_mpa)
        (gain if c_hi > c_lo else ctrl).append(d_mpa)
    gp, gn, gpv = sign_test(gain)
    cp, cn, cpv = sign_test(ctrl)
    res["natural_experiment"] = {
        "lo": lo, "hi": hi,
        "mean_d_coverage": round(statistics.mean(dc), 2) if dc else None,
        "mean_d_mpa": round(statistics.mean(dm), 2) if dm else None,
        "rho": spearman(dc, dm),
        "gain": {"n": len(gain), "mean_d_mpa": round(statistics.mean(gain), 2) if gain else None,
                 "up": gp, "down": gn, "p": round(gpv, 4)},
        "control": {"n": len(ctrl), "mean_d_mpa": round(statistics.mean(ctrl), 2) if ctrl else None,
                    "up": cp, "down": cn, "p": round(cpv, 4)},
    }

    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)

    print(f"━━━ 근거 도달 ({period} / {task}) ━━━")
    print(f"  문항 {res['n_questions']} · 근거 조각 {n_ev:,}")
    print(f"  저장 메모리 중앙 {res['store_median']:,} · 검색 결과 중앙 {res['retrieved_median']:,}")
    print(f"  mem0가 한 번이라도 뽑은 것 {res['extracted']:5.1f}%"
          f"   (오탐 바닥 {res['false_positive_floor']:.1f}%)")
    for K in cutoffs:
        print(f"    cutoff {K:4d} 도달 {per_cut[K]['coverage']:5.1f}%")
    ne = res["natural_experiment"]
    print(f"\n  자연 대조 (k{ne['lo']} -> k{ne['hi']}): 근거 {ne['mean_d_coverage']:+.1f}pp · "
          f"MPA {ne['mean_d_mpa']:+.1f}pp · rho {ne['rho']:+.3f}")
    print(f"    근거가 새로 들어온 문항 {ne['gain']['n']:2d}개  MPA {ne['gain']['mean_d_mpa']:+6.2f}"
          f"  개선 {ne['gain']['up']} 악화 {ne['gain']['down']} p={ne['gain']['p']}")
    print(f"    주변 메모리만 늘어난 문항 {ne['control']['n']:2d}개  MPA {ne['control']['mean_d_mpa']:+6.2f}"
          f"  개선 {ne['control']['up']} 악화 {ne['control']['down']} p={ne['control']['p']}")
    print(f"\n저장 -> {out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--period", required=True)
    p.add_argument("--ingest", required=True, help="큰 k 로 검색한 Stage A 산출물")
    p.add_argument("--cutoffs", default="50,100,200,400,800")
    p.add_argument("--judge-prefix", required=True, help="예: memora-judge-monthly-k")
    p.add_argument("--task", default="remembering")
    p.add_argument("--out", default=None)
    a = p.parse_args()
    cuts = [int(x) for x in a.cutoffs.split(",") if x.strip()]
    main(a.period, a.ingest, cuts, a.judge_prefix, a.task,
         a.out or f"{R}/memora-coverage-{a.period}-{a.task}.json")
