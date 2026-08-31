"""질문 유형 타당성 분석 (docs/synthesis/ability-validity-plan.md 의 구현).

  B1 요구 태깅 (Q1): HaluMem evidence 골든을 갱신·미끼 사건에 조인해 유형별 연루율
  B2 프로파일 상관·차원 (Q3): BEAM (시스템×스케일×프롬프트×cutoff) 조건 × 능력 10종
     행렬의 상관·PCA
  B3 문항 행동 응집 (Q2): BEAM 100K 문항 벡터(시스템×프롬프트×cutoff)의
     유형 내 vs 유형 간 유사도 + 순열검정
  B4 시스템×유형 상호작용 (Q4a): HaluMem 유형별 대응 McNemar + 오즈비 이질성(Woolf)
  B5 벤치마크 간 이식성 (Q4b): 같은 이름 능력의 시스템 순위 일치

의존성 문제로 yaml 을 안 씀 (경로를 명시함). numpy 필요 → eval/light venv 로 돎.

실행 (서버):
  uv run --project eval/light python src/analysis/ability_validity.py \
      --out results/exports/ability-validity.json
"""
import argparse
import glob
import json
import math
import os
from collections import defaultdict

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
R = lambda *p: os.path.join(_ROOT, *p)

# BEAM 채점 디렉토리 (runs.yaml 과 일치해야 함 — 경로가 늘면 여기도 늘림)
BEAM_DIRS = [
    # (시스템, 스케일, 프롬프트, 경로)
    ("mem0-classic", "100k", "mem0", "results/mem0-classic-oss/beam-judge-oss120-100k"),
    ("mem0-classic", "500k", "mem0", "results/mem0-classic-oss/beam-judge-oss120-500k-mem0prompt"),
    ("mem0-classic", "1m",   "mem0", "results/mem0-classic-oss/beam-judge-oss120-1m-mem0prompt"),
    ("mem0-classic", "100k", "beam", "results/mem0-classic-oss/beam-judge-oss120-100k-beamprompt"),
    ("mem0-classic", "500k", "beam", "results/mem0-classic-oss/beam-judge-oss120-500k"),
    ("mem0-classic", "1m",   "beam", "results/mem0-classic-oss/beam-judge-oss120-1m"),
    ("mem0-v3",      "100k", "beam", "results/mem0-classic-oss/beam-judge-oss120-100k-v3"),
    ("mem0-v3",      "100k", "mem0", "results/mem0-classic-oss/beam-judge-oss120-100k-v3-mem0prompt"),
    ("mem0-v3",      "500k", "beam", "results/mem0-classic-oss/beam-judge-oss120-500k-v3"),
    ("mem0-v3",      "500k", "mem0", "results/mem0-classic-oss/beam-judge-oss120-500k-v3-mem0prompt"),
    ("light",        "100k", "beam", "results/light/beam-judge-100k-light"),
    ("light",        "500k", "beam", "results/light/beam-judge-500k-light"),
    ("light",        "100k", "mem0", "results/light/beam-judge-100k-light-mem0prompt"),
    ("light",        "500k", "mem0", "results/light/beam-judge-500k-light-mem0prompt"),
]
HM_JUDGE = {
    "mem0-classic": "results/mem0-classic-oss/judge-oss120-genoss120-oss120b20/judge",
    "mem0-v3": "results/mem0-classic-oss/memzero-oss-v3/judge-v3/judge",
    "light": "results/light/memzero-20u-light/judge-light/judge",
}
MEMORA_JUDGE = {
    ("weekly", "mem0-classic"): "results/mem0-classic-oss/memora-judge-weekly",
    ("weekly", "mem0-v3"): "results/mem0-classic-oss/memora-judge-weekly-v3",
    ("weekly", "light"): "results/light/memora-judge-weekly-light",
    ("monthly", "mem0-classic"): "results/mem0-classic-oss/memora-judge-monthly",
    ("monthly", "mem0-v3"): "results/mem0-classic-oss/memora-judge-monthly-v3",
    ("monthly", "light"): "results/light/memora-judge-monthly-light",
}
SYS = ["mem0-classic", "mem0-v3", "light"]

# 벤치마크 간 대응표 (이름·정의 기준, ability-validity-plan §6)
TRANSFER = [
    ("갱신 반영", ("hm", "Dynamic Update"), ("beam", "knowledge_update"), None),
    ("다중 결합", ("hm", "Multi-hop Inference"), ("beam", "multi_session_reasoning"), ("memora", "reasoning")),
    ("사실 회수", ("hm", "Basic Fact Recall"), ("beam", "information_extraction"), ("memora", "remembering")),
    ("모른다고 하기", ("hm", "Memory Boundary"), ("beam", "abstention"), None),
    ("상충 처리", ("hm", "Memory Conflict"), ("beam", "contradiction_resolution"), None),
]


def jload(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def judge_files(d):
    return [f for f in sorted(glob.glob(R(d, "*.json")))
            if not f.endswith(("run.json", "eval_stat_result.json"))]


# ── B1 요구 태깅 ─────────────────────────────────────────────────────────
def b1_tagging():
    out = defaultdict(lambda: {"n": 0, "multi": 0, "upd": 0, "decoy_sess": 0})
    for f in judge_files(HM_JUDGE["mem0-classic"]):
        d = jload(f)
        upd_texts = set()
        for r in d.get("memory_update_records") or []:
            upd_texts.add((r.get("memory_content") or "").strip())
            try:
                for o in json.loads(r.get("original_memories") or "[]"):
                    upd_texts.add(o.strip())
            except Exception:
                pass
        decoy_sess = {r.get("session_id") for r in d.get("memory_integrity_records") or []
                      if r.get("memory_source") == "interference"}
        for q in d.get("question_answering_records") or []:
            ev = q.get("evidence") or []
            t = q.get("question_type")
            s = out[t]
            s["n"] += 1
            if len(ev) >= 2:
                s["multi"] += 1
            if any((e.get("memory_content") or "").strip() in upd_texts for e in ev):
                s["upd"] += 1
            if q.get("session_id") in decoy_sess:
                s["decoy_sess"] += 1
    print("\nB1 요구 태깅 (유형 × 다중근거율/갱신연루율/미끼세션율):")
    for t, s in sorted(out.items()):
        print(f"  {t:30s} 다중 {s['multi']/s['n']*100:5.1f}% · 갱신 {s['upd']/s['n']*100:5.1f}%"
              f" · 미끼세션 {s['decoy_sess']/s['n']*100:5.1f}% (n={s['n']})")
    return {t: {k: v for k, v in s.items()} for t, s in out.items()}


# ── B2·B3 BEAM 행렬 ──────────────────────────────────────────────────────
def load_beam():
    """cond -> ability -> [scores] · 그리고 100K 문항 벡터."""
    cond_ab = {}
    item_vecs = defaultdict(dict)   # (conv,ability,idx) -> cond키 -> score
    for sysk, scale, prompt, d in BEAM_DIRS:
        files = judge_files(d)
        if not files:
            print(f"  (건너뜀: {d} 없음)")
            continue
        for f in files:
            for r in jload(f).get("records") or []:
                cond = (sysk, scale, prompt, r["cutoff"])
                cond_ab.setdefault(cond, defaultdict(list))[r["ability"]].append(r["score"])
                if scale == "100k":
                    item_vecs[(r["conv"], r["ability"], r["idx"])][
                        (sysk, prompt, r["cutoff"])] = r["score"]
    return cond_ab, item_vecs


def b2_profile(cond_ab):
    abilities = sorted({a for v in cond_ab.values() for a in v})
    conds = sorted(cond_ab)
    M = np.array([[np.mean(cond_ab[c][a]) if a in cond_ab[c] else np.nan
                   for a in abilities] for c in conds])
    ok = ~np.isnan(M).any(axis=1)
    M2, conds2 = M[ok], [c for c, k in zip(conds, ok) if k]
    print(f"\nB2 조건 {len(conds2)}벌 × 능력 {len(abilities)}종")
    # 상관 (Spearman)
    def rank(x):
        return np.argsort(np.argsort(x))
    Rk = np.apply_along_axis(rank, 0, M2).astype(float)
    C = np.corrcoef(Rk.T)
    # PCA (열 중심화)
    X = M2 - M2.mean(axis=0)
    sv = np.linalg.svd(X, compute_uv=False)
    var = sv ** 2 / (sv ** 2).sum()
    print(f"  PCA 설명분산: PC1 {var[0]*100:.1f}% · PC2 {var[1]*100:.1f}%"
          f" · PC3 {var[2]*100:.1f}%")
    offdiag = C[np.triu_indices_from(C, 1)]
    print(f"  능력 간 Spearman 상관: 중앙 {np.median(offdiag):.3f}"
          f" · 최소 {offdiag.min():.3f} · 최대 {offdiag.max():.3f}")
    # cutoff 접은 판 (조건 = 시스템×스케일×프롬프트)
    fold = defaultdict(lambda: defaultdict(list))
    for (sysk, scale, prompt, cut), ab in cond_ab.items():
        for a, v in ab.items():
            fold[(sysk, scale, prompt)][a].extend(v)
    Mf = np.array([[np.mean(fold[c][a]) if a in fold[c] else np.nan for a in abilities]
                   for c in sorted(fold)])
    Mf = Mf[~np.isnan(Mf).any(axis=1)]
    Xf = Mf - Mf.mean(axis=0)
    svf = np.linalg.svd(Xf, compute_uv=False)
    varf = svf ** 2 / (svf ** 2).sum()
    print(f"  cutoff 접은 판 ({Mf.shape[0]}조건): PC1 {varf[0]*100:.1f}% · PC2 {varf[1]*100:.1f}%")
    return {"abilities": abilities,
            "conds": ["|".join(map(str, c)) for c in conds2],
            "corr": np.round(C, 4).tolist(),
            "pca_var": np.round(var[:5], 4).tolist(),
            "pca_var_folded": np.round(varf[:5], 4).tolist()}


def b3_coherence(item_vecs, n_perm=2000, seed=7):
    keys = [k for k, v in item_vecs.items() if len(v) >= 20]
    conds = sorted(set.intersection(*(set(item_vecs[k]) for k in keys)))
    V = np.array([[item_vecs[k][c] for c in conds] for k in keys])
    labels = np.array([k[1] for k in keys])
    V = V - V.mean(axis=1, keepdims=True)
    nrm = np.linalg.norm(V, axis=1, keepdims=True)
    nrm[nrm == 0] = 1
    Vn = V / nrm
    S = Vn @ Vn.T
    iu = np.triu_indices_from(S, 1)
    same = labels[iu[0]] == labels[iu[1]]
    gap = S[iu][same].mean() - S[iu][~same].mean()
    rng = np.random.default_rng(seed)
    cnt = 0
    for _ in range(n_perm):
        lp = rng.permutation(labels)
        sp = lp[iu[0]] == lp[iu[1]]
        if S[iu][sp].mean() - S[iu][~sp].mean() >= gap:
            cnt += 1
    p = (cnt + 1) / (n_perm + 1)
    print(f"\nB3 문항 응집 (100K {len(keys)}문항 × 조건 {len(conds)}차원):")
    print(f"  유형 내 유사도 − 유형 간 유사도 = {gap:.4f} · 순열 p = {p:.4f}")
    # 유형별 응집
    per = {}
    for t in sorted(set(labels)):
        m = labels == t
        idx = np.where(m)[0]
        if len(idx) < 2:
            continue
        sub = S[np.ix_(idx, idx)]
        within = sub[np.triu_indices_from(sub, 1)].mean()
        cross = S[np.ix_(idx, np.where(~m)[0])].mean()
        per[t] = {"within": round(float(within), 4), "cross": round(float(cross), 4)}
        print(f"    {t:28s} 내 {within:.3f} vs 간 {cross:.3f}"
              f"  ({'+' if within > cross else '−'})")
    return {"gap": round(float(gap), 4), "p": p, "per_type": per,
            "n_items": len(keys), "n_dims": len(conds)}


# ── B4 시스템×유형 상호작용 (HaluMem) ────────────────────────────────────
def b4_interaction():
    rec = defaultdict(dict)   # key -> sys -> correct
    qtype = {}
    for sysk, d in HM_JUDGE.items():
        for f in judge_files(d):
            u = jload(f)
            for r in u.get("question_answering_records") or []:
                k = (f, r.get("session_id"), r["question"])  # f 에 uuid 포함
                k = (os.path.basename(f)[:8], r.get("session_id"), r["question"])
                rec[k][sysk] = 1 if r.get("result_type") == "Correct" else 0
                qtype[k] = r.get("question_type")
    keys = [k for k in rec if len(rec[k]) == len(HM_JUDGE)]
    print(f"\nB4 시스템×유형 상호작용 (문항 {len(keys)})")
    out = {}
    for s1, s2 in (("mem0-classic", "mem0-v3"), ("mem0-classic", "light"), ("mem0-v3", "light")):
        rows, logors, ws = {}, [], []
        for t in sorted(set(qtype.values())):
            b = sum(1 for k in keys if qtype[k] == t and rec[k][s1] == 1 and rec[k][s2] == 0)
            c = sum(1 for k in keys if qtype[k] == t and rec[k][s1] == 0 and rec[k][s2] == 1)
            chi = (b - c) ** 2 / (b + c) if b + c else 0.0
            lor = math.log((c + 0.5) / (b + 0.5))
            w = 1 / (1 / (b + 0.5) + 1 / (c + 0.5))
            logors.append(lor)
            ws.append(w)
            rows[t] = {"b": b, "c": c, "mcnemar_chi2": round(chi, 2), "log_or": round(lor, 3)}
        lbar = sum(l * w for l, w in zip(logors, ws)) / sum(ws)
        Q = sum(w * (l - lbar) ** 2 for l, w in zip(logors, ws))
        out[f"{s1} vs {s2}"] = {"types": rows, "woolf_Q": round(Q, 2), "df": len(rows) - 1}
        print(f"  {s1} vs {s2}: Woolf 이질성 Q={Q:.2f} (df={len(rows)-1}, "
              f"χ² 임계 5%≈{11.07 if len(rows)-1==5 else '표 참조'})")
        for t, r in rows.items():
            print(f"    {t:28s} b={r['b']:3d} c={r['c']:3d} McNemar χ²={r['mcnemar_chi2']:6.2f}")
    return out


# ── B5 벤치마크 간 이식성 ────────────────────────────────────────────────
def b5_transfer(cond_ab):
    hm = defaultdict(dict)
    for sysk, d in HM_JUDGE.items():
        agg = defaultdict(lambda: [0, 0])
        for f in judge_files(d):
            for r in jload(f).get("question_answering_records") or []:
                a = agg[r.get("question_type")]
                a[1] += 1
                a[0] += r.get("result_type") == "Correct"
        for t, (c, n) in agg.items():
            hm[t][sysk] = c / n * 100
    beam = defaultdict(dict)
    for (sysk, scale, prompt, cut), ab in cond_ab.items():
        if scale == "100k" and prompt == "beam" and cut == 50:
            for a, v in ab.items():
                beam[a][sysk] = float(np.mean(v)) * 100
    memora = defaultdict(dict)
    for (period, sysk), d in MEMORA_JUDGE.items():
        for f in judge_files(d):
            for r in jload(f).get("records") or []:
                memora[r["task"]].setdefault(sysk, []).append(r["fama"])
    memora = {t: {s: float(np.mean(v)) * 100 for s, v in d.items()} for t, d in memora.items()}

    print("\nB5 벤치마크 간 이식성 (시스템별 점수, classic/v3/light):")
    rows = []
    for name, hm_k, beam_k, mem_k in TRANSFER:
        cols = {}
        cols["HaluMem"] = [hm.get(hm_k[1], {}).get(s) for s in SYS]
        cols["BEAM"] = [beam.get(beam_k[1], {}).get(s) for s in SYS]
        if mem_k:
            cols["Memora"] = [memora.get(mem_k[1], {}).get(s) for s in SYS]
        rows.append({"name": name, "cols": cols})
        f3 = lambda v: " / ".join("-" if x is None else f"{x:.1f}" for x in v)
        line = " · ".join(f"{b} {f3(v)}" for b, v in cols.items())
        print(f"  {name:10s} {line}")
    return {"rows": rows, "hm": dict(hm), "beam": {k: dict(v) for k, v in beam.items()},
            "memora": memora}


# ── B6 답변 프롬프트 × 시스템 상호작용 (100K, 같은 투입·같은 검색) ────────
def b6_prompt(cond_ab):
    acc = {}
    for (sysk, scale, prompt, cut), ab in cond_ab.items():
        if scale != "100k":
            continue
        slot = acc.setdefault(prompt, {}).setdefault(sysk, {"all": [], "abst": []})
        for a, v in ab.items():
            slot["all"].extend(v)
            if a == "abstention":
                slot["abst"].extend(v)
    out = {}
    for p, bysys in acc.items():
        out[p] = {s: {"overall": round(float(np.mean(d["all"])) * 100, 2),
                      "abstention": round(float(np.mean(d["abst"])) * 100, 2)}
                  for s, d in bysys.items()}
    print("\nB6 프롬프트 × 시스템 (100K 전체 평균):")
    for p, bysys in out.items():
        line = " · ".join(f"{s} {v['overall']:.2f}" for s, v in bysys.items())
        print(f"  {p:5s} {line}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results/exports/ability-validity.json")
    a = ap.parse_args()
    res = {}
    res["b1"] = b1_tagging()
    cond_ab, item_vecs = load_beam()
    res["b2"] = b2_profile(cond_ab)
    res["b3"] = b3_coherence(item_vecs)
    res["b4"] = b4_interaction()
    res["b5"] = b5_transfer(cond_ab)
    res["b6"] = b6_prompt(cond_ab)
    out = R(a.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(f"\n저장 -> {a.out}")


if __name__ == "__main__":
    main()
