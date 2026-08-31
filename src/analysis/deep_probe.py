"""HaluMem 기제 분석 3종 (심층 분석용, 추가 인퍼런스 없음).

  A1 근거 도달률: 골든 답변이 각 시스템의 답변 컨텍스트 안에 실제로 있었는가.
     정오를 '검색이 못 올린 것'과 '올렸는데 못 읽은 것'으로 분해함.
  A2 미끼 영향: interference 골든이 있는 세션의 질문에서 환각률이 오르는가.
  A3 갱신 문항 불일치 목록: Dynamic Update 문항 중 시스템 간 정오가 갈린 것을
     골든·시스템별 답변과 함께 파일로 떨굼 (사람이 낡은 값인지 분류하는 입력).

실행 (서버):
  uv run --project src/web-dashboard python src/analysis/deep_probe.py --scale 20u \
      --out results/exports/deep-probe-halumem.json
"""
import argparse
import json
import os
import re

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RES = ("Correct", "Hallucination", "Omission")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").casefold()).strip()


def toks(s: str) -> set:
    return {t for t in re.findall(r"[0-9a-z가-힣]+", (s or "").casefold()) if len(t) > 2}


def load_users(judge_dir):
    out = {}
    for fn in sorted(os.listdir(judge_dir)):
        if fn.endswith(".json") and fn != "eval_stat_result.json" and fn != "run.json":
            with open(os.path.join(judge_dir, fn), encoding="utf-8") as f:
                out[fn[:-5]] = json.load(f)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", default="20u")
    ap.add_argument("--out", default="results/exports/deep-probe-halumem.json")
    ap.add_argument("--du-dump", default="/tmp/du_disagreements.txt")
    a = ap.parse_args()

    with open(os.path.join(_ROOT, "src", "web-dashboard", "runs.yaml"), encoding="utf-8") as f:
        y = yaml.safe_load(f)
    cfg = y["halumem"]["scales"][a.scale]["by_system"]
    sysd = list(y["systems"])

    data = {}
    for sk in sysd:
        jd = os.path.join(_ROOT, (cfg.get(sk) or {}).get("judge", ""))
        if os.path.isdir(jd):
            data[sk] = load_users(jd)
    common = sorted(set.intersection(*(set(v) for v in data.values())))
    print(f"시스템 {list(data)} · 공통 유저 {len(common)}")

    # ── A1 근거 도달률 ────────────────────────────────────────────────────
    # strict: 정규화한 골든 답변 문자열이 컨텍스트에 통째로 있음
    # loose:  답변 토큰(3자 이상)의 70% 이상이 컨텍스트에 있음
    a1 = {sk: {"n": 0, "in_strict": 0, "in_loose": 0,
               "c_in": 0, "n_in": 0, "c_out": 0, "n_out": 0,
               "by_type": {}} for sk in data}
    for sk, users in data.items():
        for u in common:
            for r in users[u]["question_answering_records"]:
                qt = r.get("question_type")
                ans = r.get("answer") or ""
                if qt == "Memory Boundary" or len(ans) > 120 or not ans.strip():
                    continue
                ctx_n = norm(r.get("context") or "")
                ctx_t = toks(r.get("context") or "")
                strict = norm(ans) in ctx_n
                at = toks(ans)
                loose = bool(at) and len(at & ctx_t) / len(at) >= 0.7
                hit = strict or loose
                ok = r.get("result_type") == "Correct"
                s = a1[sk]
                s["n"] += 1
                s["in_strict"] += strict
                s["in_loose"] += loose
                bt = s["by_type"].setdefault(qt, {"n": 0, "hit": 0, "c_in": 0, "n_in": 0,
                                                  "c_out": 0, "n_out": 0})
                bt["n"] += 1
                bt["hit"] += hit
                key = ("in" if hit else "out")
                s[f"n_{key}"] += 1
                bt[f"n_{key}"] += 1
                if ok:
                    s[f"c_{key}"] += 1
                    bt[f"c_{key}"] += 1
    print("\nA1 근거 도달률 (Memory Boundary 제외):")
    for sk, s in a1.items():
        cov = (s["in_strict"] + 0) / s["n"]
        covl = s["n_in"] / s["n"]
        pin = s["c_in"] / s["n_in"] if s["n_in"] else None
        pout = s["c_out"] / s["n_out"] if s["n_out"] else None
        print(f"  {sk:13s} 도달(strict {cov*100:.1f}% / 관대 {covl*100:.1f}%)"
              f" · 도달 시 정답 {pin*100:.1f}% · 미도달 시 정답 {pout*100:.1f}% (n={s['n']})")

    # ── A2 미끼 영향 ─────────────────────────────────────────────────────
    decoy_sessions = set()
    decoy_texts = {}
    any_sys = next(iter(data))
    for u in common:
        for r in data[any_sys][u]["memory_integrity_records"]:
            if r.get("memory_source") == "interference":
                decoy_sessions.add((u, r.get("session_id")))
                decoy_texts.setdefault(u, []).append(r.get("memory_content") or "")
    a2 = {sk: {"decoy": {t: 0 for t in RES} | {"n": 0},
               "clean": {t: 0 for t in RES} | {"n": 0},
               "decoy_in_ctx": 0, "decoy_ctx_n": 0} for sk in data}
    for sk, users in data.items():
        for u in common:
            for r in users[u]["question_answering_records"]:
                slot = a2[sk]["decoy" if (u, r.get("session_id")) in decoy_sessions else "clean"]
                slot["n"] += 1
                if r.get("result_type") in RES:
                    slot[r["result_type"]] += 1
                if (u, r.get("session_id")) in decoy_sessions:
                    a2[sk]["decoy_ctx_n"] += 1
                    ctx = norm(r.get("context") or "")
                    if any(norm(t) and norm(t) in ctx for t in decoy_texts.get(u, [])):
                        a2[sk]["decoy_in_ctx"] += 1
    print(f"\nA2 미끼 영향 (미끼 세션 {len(decoy_sessions)}개):")
    for sk, s in a2.items():
        d, c = s["decoy"], s["clean"]
        hd = d["Hallucination"] / d["n"] * 100 if d["n"] else 0
        hc = c["Hallucination"] / c["n"] * 100 if c["n"] else 0
        cd = d["Correct"] / d["n"] * 100 if d["n"] else 0
        cc = c["Correct"] / c["n"] * 100 if c["n"] else 0
        ic = s["decoy_in_ctx"] / s["decoy_ctx_n"] * 100 if s["decoy_ctx_n"] else 0
        print(f"  {sk:13s} 미끼 세션: C {cd:.1f} H {hd:.1f} (n={d['n']})"
              f" · 그 외: C {cc:.1f} H {hc:.1f} (n={c['n']})"
              f" · 미끼 원문이 컨텍스트에 들어간 비율 {ic:.1f}%")

    # ── A3 갱신 문항 불일치 덤프 ─────────────────────────────────────────
    rows = {}
    for sk, users in data.items():
        for u in common:
            for r in users[u]["question_answering_records"]:
                if r.get("question_type") != "Dynamic Update":
                    continue
                k = (u, r.get("session_id"), r["question"])
                rows.setdefault(k, {"golden": r.get("answer"), "sys": {}})
                rows[k]["sys"][sk] = (r.get("result_type"), (r.get("system_response") or "")[:90])
    dis = {k: v for k, v in rows.items()
           if len({t for t, _ in v["sys"].values()}) > 1}
    with open(a.du_dump, "w", encoding="utf-8") as f:
        for (u, sid, q), v in sorted(dis.items()):
            f.write(f"Q: {q[:100]}\n골든: {(v['golden'] or '')[:90]}\n")
            for sk in data:
                t, resp = v["sys"].get(sk, ("없음", ""))
                mark = {"Correct": "✓", "Hallucination": "H", "Omission": "O"}.get(t, "?")
                f.write(f"  {mark} {sk:13s} {resp}\n")
            f.write("\n")
    print(f"\nA3 Dynamic Update 불일치 {len(dis)}/{len(rows)}건 -> {a.du_dump}")

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"a1": a1, "a2": {k: {kk: vv for kk, vv in v.items()} for k, v in a2.items()},
                   "n_decoy_sessions": len(decoy_sessions)}, f, ensure_ascii=False, indent=1)
    print(f"저장 -> {a.out}")


if __name__ == "__main__":
    main()
