"""trace 에서 비용을 되살린다. 계측기를 붙이기 전에 끝난 런용.

trace 는 LLM 호출마다 **프롬프트 원문(messages)과 응답**을 통째로 남긴다.
입력 토큰은 안 남기지만 vLLM 의 `/tokenize` 로 정확히 셀 수 있다 (근사가 아니라 실제 토크나이저).

  uv run python src/cost/backfill_trace.py \
      --trace-dir traces/mem0-classic-oss/beam-100k-oss120b \
      --out cost/beam-100k-mem0-classic \
      --system mem0-classic --benchmark beam --setting 100k

⚠ 되살릴 수 있는 것은 **투입 단계뿐이다.** tracer 가 mem0 의 LLM 만 감싸므로 답변·채점은
  애초에 trace 에 없다. 그래서 산출물에 stage=ingest 로만 적고 source 를 표시해,
  화면에서 계측본과 구분되게 한다. 섞어서 "전 단계 비용" 으로 읽으면 안 된다.
"""
import argparse
import glob
import json
import os
from concurrent.futures import ThreadPoolExecutor

import requests

BUCKET = 250      # sitecustomize._BUCKET 과 반드시 같아야 함
NBUCKET = 513


def count_tokens(base_url: str, model: str, messages: list, sess: requests.Session) -> int | None:
    try:
        r = sess.post(f"{base_url}/tokenize", json={"model": model, "messages": messages}, timeout=60)
        r.raise_for_status()
        return r.json().get("count")
    except Exception:
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--trace-dir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--system", required=True)
    p.add_argument("--benchmark", required=True)
    p.add_argument("--setting", required=True)
    p.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", "http://localhost:8002/v1"))
    p.add_argument("--model", default=os.getenv("MEM0_LLM_MODEL", "openai/gpt-oss-120b"))
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--limit", type=int, default=0, help="검증용: 앞 N건만")
    a = p.parse_args()

    base = a.base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]   # /tokenize 는 /v1 밖에 있다

    files = sorted(glob.glob(os.path.join(a.trace_dir, "*.jsonl")))
    if not files:
        raise SystemExit(f"✗ trace 파일 없음: {a.trace_dir}")

    recs = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                d = json.loads(line)
                llm = d.get("llm") or {}
                if llm.get("messages"):
                    recs.append((llm["messages"], llm.get("completion_tokens"),
                                 llm.get("reasoning_tokens"), d.get("duration_ms") or 0.0))
        if a.limit and len(recs) >= a.limit:
            recs = recs[:a.limit]
            break
    print(f"  LLM 레코드 {len(recs):,}건 · 파일 {len(files)}개")

    sess = requests.Session()
    sess.mount("http://", requests.adapters.HTTPAdapter(pool_maxsize=a.workers * 2))

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        counts = list(ex.map(lambda r: count_tokens(base, a.model, r[0], sess), recs))

    miss = sum(1 for c in counts if c is None)
    if miss:
        print(f"  ⚠ 토크나이즈 실패 {miss:,}건 (입력 토큰에서 빠짐)")

    hist = [0] * NBUCKET
    row = {"kind": "chat", "model": a.model, "calls": 0,
           "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
           "wall_ms": 0.0, "errors": miss, "prompt_max": 0, "hist": hist}
    no_ct = 0
    for (msgs, ct, rt, dur), pt in zip(recs, counts):
        row["calls"] += 1
        row["wall_ms"] += dur or 0.0
        if ct is None:
            no_ct += 1
        row["completion_tokens"] += ct or 0
        row["reasoning_tokens"] += rt or 0
        if pt:
            row["prompt_tokens"] += pt
            row["prompt_max"] = max(row["prompt_max"], pt)
            hist[min(pt // BUCKET, NBUCKET - 1)] += 1
    if no_ct:
        print(f"  ⚠ 출력 토큰이 없는 레코드 {no_ct:,}건 (그 런이 토큰 캡처 전이면 정상)")

    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "ingest__backfill.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"stage": "ingest", "system": a.system, "benchmark": a.benchmark,
                   "setting": a.setting, "pid": 0, "source": "trace-backfill",
                   "rows": [row]}, f, ensure_ascii=False)
    print(f"  저장 -> {path}")
    print(f"  호출 {row['calls']:,} · 입력 {row['prompt_tokens']:,} · 출력 {row['completion_tokens']:,}"
          f" · 최대 컨텍스트 {row['prompt_max']:,}")


if __name__ == "__main__":
    main()
