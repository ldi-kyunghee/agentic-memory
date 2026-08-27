"""답변(질의) 단계의 비용을 **다시 안 돌리고** 계산한다.

투입 산출물에 **검색 결과가 통째로 저장돼 있다** (BEAM/Memora 는 `questions[].retrieved`,
HaluMem 은 조립된 `context` 문자열). 답변 산출물에는 생성된 답변이 있다.
그래서 답변 프롬프트를 그대로 다시 조립해 vLLM `/tokenize` 로 세면 실제 값이 나온다.

왜 필요한가: **같은 top-k 라도 메모리 시스템마다 컨텍스트 토큰량이 다르다.**
항목 길이가 다르기 때문이다. 투입 비용만 보면 이 차이가 통째로 안 보인다.

  uv run python src/cost/backfill_answer.py --benchmark beam \
      --ingest results/mem0-classic-oss/beam-100k-oss120b/beam_eval_results.jsonl \
      --cutoff 200 --out cost/beam-100k-mem0-classic \
      --system mem0-classic --setting 100k

⚠ 포맷은 추측하지 않는다. 실제 답변 스크립트의 `build_context`/프롬프트 상수를 import 해서 쓴다.
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUCKET = 250
NBUCKET = 513


def load_builders(bench: str):
    """실제 답변 스크립트에서 컨텍스트·프롬프트 조립기를 그대로 가져온다."""
    E = os.path.join(ROOT, "eval", "mem0-classic-oss")
    if bench == "beam":
        sys.path.insert(0, os.path.join(E, "beam"))
        sys.path.insert(0, ROOT)
        import answer_beam as m
        return (lambda q, r, cut: m.build_prompt(q, m.build_context(r, cut)[0]))
    if bench == "memora":
        sys.path.insert(0, os.path.join(E, "memora"))
        import answer_memora as m
        return (lambda q, r, cut: m.SYSTEM_PROMPT + "\n\n"
                + m.USER_PROMPT.format(question=q, memories=m.build_context(r, cut)[0]))
    if bench == "halumem":
        sys.path.insert(0, E)
        from prompts import PROMPT_MEMZERO
        return (lambda q, ctx, cut: PROMPT_MEMZERO.format(context=ctx, question=q))
    raise SystemExit(f"✗ 모르는 벤치마크: {bench}")


def stored_response(q: dict, bench: str, cutoff):
    """저장된 답변을 꺼낸다. 벤치마크마다 담는 모양이 다르다.

      HaluMem  q["system_response"]                 (문자열)
      BEAM     q["answers"][str(cutoff)]["system_response"]   (cutoff 별로 네 벌)
      Memora   q["answer"]["system_response"]
    """
    if bench == "halumem":
        return q.get("system_response")
    if bench == "beam":
        a = q.get("answers")
        if isinstance(a, dict) and a:
            key = str(cutoff) if cutoff and str(cutoff) in a else sorted(a, key=lambda x: int(x))[-1]
            return (a.get(key) or {}).get("system_response")
        return q.get("system_response")
    a = q.get("answer")
    if isinstance(a, dict):
        return a.get("system_response")
    return a or q.get("system_response")


def jobs_from(bench: str, path: str, cutoff):
    """(질문, 프롬프트 재료, 저장된 답변) 목록."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            if bench == "halumem":
                for s in d.get("sessions") or []:
                    for q in (s.get("questions") or []):
                        if q.get("context") is not None:
                            out.append((q["question"], q["context"], stored_response(q, bench, cutoff)))
            else:
                for q in (d.get("questions") or []):
                    if q.get("retrieved") is not None:
                        out.append((q["question"], q["retrieved"], stored_response(q, bench, cutoff)))
    return out


def count(base, model, text, sess):
    try:
        r = sess.post(f"{base}/tokenize", json={"model": model, "prompt": text}, timeout=60)
        r.raise_for_status()
        return r.json().get("count")
    except Exception:
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--benchmark", required=True, choices=("beam", "memora", "halumem"))
    p.add_argument("--ingest", required=True)
    p.add_argument("--answers", default=None, help="답변 jsonl. 없으면 투입 산출물의 system_response 를 씀")
    p.add_argument("--cutoff", type=int, default=None)
    p.add_argument("--out", required=True)
    p.add_argument("--system", required=True)
    p.add_argument("--setting", required=True)
    p.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", "http://localhost:8002/v1"))
    p.add_argument("--model", default=os.getenv("ANSWER_MODEL", "openai/gpt-oss-120b"))
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--limit", type=int, default=0)
    a = p.parse_args()

    base = a.base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]

    build = load_builders(a.benchmark)
    jobs = jobs_from(a.benchmark, a.answers or a.ingest, a.cutoff)
    if a.limit:
        jobs = jobs[:a.limit]
    if not jobs:
        raise SystemExit("✗ 문항을 못 찾음")
    print(f"  문항 {len(jobs):,}건 · cutoff {a.cutoff or '전량'}")

    prompts = [build(q, r, a.cutoff) for q, r, _ in jobs]
    answers = [(ans or "") for _, _, ans in jobs]

    sess = requests.Session()
    sess.mount("http://", requests.adapters.HTTPAdapter(pool_maxsize=a.workers * 2))
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        pin = list(ex.map(lambda t: count(base, a.model, t, sess), prompts))
        pout = list(ex.map(lambda t: count(base, a.model, t, sess) if t else 0, answers))

    hist = [0] * NBUCKET
    row = {"kind": "chat", "model": a.model, "calls": len(jobs),
           "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
           "wall_ms": 0.0, "errors": sum(1 for v in pin if v is None),
           "prompt_max": 0, "hist": hist}
    for v in pin:
        if v:
            row["prompt_tokens"] += v
            row["prompt_max"] = max(row["prompt_max"], v)
            hist[min(v // BUCKET, NBUCKET - 1)] += 1
    row["completion_tokens"] = sum(v or 0 for v in pout)

    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "answer__backfill.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"stage": "answer", "system": a.system, "benchmark": a.benchmark,
                   "setting": a.setting, "pid": 0, "source": "trace-backfill",
                   "rows": [row]}, f, ensure_ascii=False)
    print(f"  저장 -> {path}")
    print(f"  호출 {row['calls']:,} · 입력 {row['prompt_tokens']:,} · 출력 {row['completion_tokens']:,}"
          f" · 최대 컨텍스트 {row['prompt_max']:,}")
    if row["errors"]:
        print(f"  ⚠ 토크나이즈 실패 {row['errors']:,}건")


if __name__ == "__main__":
    main()
