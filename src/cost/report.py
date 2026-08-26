"""비용 계측 산출물을 합쳐 읽는다. `sitecustomize.py` 가 떨군 프로세스별 json 을 모은다.

  uv run python src/cost/report.py --dir cost/beam-100k-mem0-classic
  uv run python src/cost/report.py --dir cost --glob "beam-100k-*"   # 여러 세팅 비교
"""
import argparse
import glob as globmod
import json
import os

BUCKET = 250  # sitecustomize._BUCKET 과 반드시 같아야 함


def load(d: str) -> dict:
    """디렉토리 하나(=런 하나)의 프로세스별 json 을 합침."""
    agg, meta = {}, {}
    for p in sorted(globmod.glob(os.path.join(d, "*.json"))):
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
        for k in ("system", "benchmark", "setting"):
            if doc.get(k):
                meta[k] = doc[k]
        stage = doc.get("stage", "unknown")
        for r in doc.get("rows", []):
            key = (stage, r["kind"], r["model"])
            s = agg.setdefault(key, {
                "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                "reasoning_tokens": 0, "wall_ms": 0.0, "errors": 0,
                "prompt_max": 0, "hist": [0] * len(r.get("hist") or [0]),
            })
            for f_ in ("calls", "prompt_tokens", "completion_tokens", "reasoning_tokens", "wall_ms", "errors"):
                s[f_] += r.get(f_, 0)
            s["prompt_max"] = max(s["prompt_max"], r.get("prompt_max", 0))
            h = r.get("hist") or []
            if len(h) > len(s["hist"]):
                s["hist"] += [0] * (len(h) - len(s["hist"]))
            for i, v in enumerate(h):
                s["hist"][i] += v
    return {"meta": meta, "agg": agg}


def pct(hist: list, q: float) -> int | None:
    """히스토그램에서 분위수(칸 중앙값으로 근사). 프롬프트 길이 분포를 보는 용도."""
    n = sum(hist)
    if not n:
        return None
    target, run = n * q, 0
    for i, v in enumerate(hist):
        run += v
        if run >= target:
            return i * BUCKET + BUCKET // 2
    return len(hist) * BUCKET


def summarize(d: str) -> dict:
    r = load(d)
    agg = r["agg"]
    out = {"meta": r["meta"], "stages": {}, "total": {
        "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "wall_ms": 0.0}}
    for (stage, kind, model), s in agg.items():
        st = out["stages"].setdefault(stage, {})
        st[f"{kind}:{model}"] = {
            "calls": s["calls"], "prompt_tokens": s["prompt_tokens"],
            "completion_tokens": s["completion_tokens"], "reasoning_tokens": s["reasoning_tokens"],
            "wall_ms": round(s["wall_ms"], 1), "errors": s["errors"],
            "prompt_p50": pct(s["hist"], 0.5), "prompt_p95": pct(s["hist"], 0.95),
            "prompt_max": s["prompt_max"],
        }
        for f_ in ("calls", "prompt_tokens", "completion_tokens", "reasoning_tokens", "wall_ms"):
            out["total"][f_] += s[f_]
    out["total"]["wall_ms"] = round(out["total"]["wall_ms"], 1)
    out["total"]["tokens"] = out["total"]["prompt_tokens"] + out["total"]["completion_tokens"]
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dir", required=True, help="계측 디렉토리 (또는 --glob 의 부모)")
    p.add_argument("--glob", default=None, help="부모 밑에서 고를 패턴. 주면 여러 런을 나란히 봄")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    dirs = sorted(globmod.glob(os.path.join(a.dir, a.glob))) if a.glob else [a.dir]
    dirs = [d for d in dirs if os.path.isdir(d)]
    if not dirs:
        raise SystemExit(f"✗ 계측 디렉토리 없음: {a.dir}" + (f"/{a.glob}" if a.glob else ""))

    res = {os.path.basename(d): summarize(d) for d in dirs}
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    for name, r in res.items():
        m = r["meta"]
        tag = " · ".join(v for v in (m.get("system"), m.get("benchmark"), m.get("setting")) if v)
        print(f"\n━━━ {name}{'  (' + tag + ')' if tag else ''} ━━━")
        print("  {:22s}{:>9s}{:>13s}{:>13s}{:>10s}{:>9s}{:>9s}".format(
            "단계 · 모델", "호출", "입력토큰", "출력토큰", "p50", "p95", "최대"))
        for stage, models in r["stages"].items():
            for key, s in models.items():
                print("  {:22s}{:>9,}{:>13,}{:>13,}{:>10}{:>9}{:>9,}".format(
                    f"{stage} · {key.split(':')[0]}", s["calls"], s["prompt_tokens"],
                    s["completion_tokens"],
                    f'{s["prompt_p50"]:,}' if s["prompt_p50"] else "–",
                    f'{s["prompt_p95"]:,}' if s["prompt_p95"] else "–",
                    s["prompt_max"]))
        t = r["total"]
        print("  " + "-" * 84)
        print("  {:22s}{:>9,}{:>13,}{:>13,}   합계 토큰 {:,}".format(
            "합계", t["calls"], t["prompt_tokens"], t["completion_tokens"], t["tokens"]))


if __name__ == "__main__":
    main()
