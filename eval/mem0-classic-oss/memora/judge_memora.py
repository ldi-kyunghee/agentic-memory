"""Memora Stage B: 기준(criterion) 하나씩 yes/no 판정하고 FAMA로 집계함.

공식 하네스(`evals/agent_eval/memory_to_answer.py`)의 채점 프롬프트를 원문 그대로 씀.
기준 하나당 LLM 한 번이고, 응답은 `{answer, confidence, explanation}` JSON임. BEAM의 nugget
채점과 구조가 같음.

⚠ 공식은 **세 모델(GPT-4.1 / Claude Haiku 4.5 / Gemini 2.5 Flash) 다수결**임. 우리는 로컬
   모델 하나뿐이라 기본은 1회 판정이고, `--passes 3` 을 주면 같은 모델로 3회 돌려 다수결을 냄
   (모델 다양성이 아니라 자기 일관성만 잡는 것이라 성격이 다름. 인용할 때 밝힐 것).

⚠ 페르소나 하나가 끝날 때마다 저장함. BEAM에서 5,500콜 1.5시간치를 통째로 날린 적이 있음.
"""
import os
import re
import sys
import json
import time
import argparse
import statistics
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

sys.path.insert(0, "src/memora")
from fama import question_fama, aggregate   # noqa: E402

MODEL = os.getenv("JUDGE_MODEL", os.getenv("OPENAI_MODEL"))
REASONING_EFFORT = os.getenv("JUDGE_REASONING_EFFORT")
MAX_COMPLETION_TOKENS = int(os.getenv("JUDGE_MAX_COMPLETION_TOKENS", "32768"))

client = OpenAI()

# 공식 채점 프롬프트. 원문 그대로 옮김.
SYSTEM_PROMPT = """You are an expert evaluator assessing AI assistant responses. Your task is to answer a YES/NO evaluation question about a given response.

You must provide your answer in the following JSON format:
{
    "answer": "yes" or "no",
    "confidence": 0.0 to 1.0,
    "explanation": "Brief explanation of your reasoning"
}

Be objective and thorough in your evaluation."""

USER_PROMPT = """Please evaluate the following AI response against the evaluation question.

AI RESPONSE TO EVALUATE:
{response}

EVALUATION QUESTION:
{criterion}

Provide your evaluation in JSON format with answer (yes/no), confidence (0.0-1.0), and explanation."""


def call_llm(system: str, user: str) -> tuple[str, str]:
    kwargs = dict(model=MODEL, messages=[{"role": "system", "content": system},
                                         {"role": "user", "content": user}])
    if REASONING_EFFORT:
        kwargs["reasoning_effort"] = REASONING_EFFORT
        kwargs["max_completion_tokens"] = MAX_COMPLETION_TOKENS
    else:
        kwargs["temperature"] = 0.0
        kwargs["max_tokens"] = 1024
    for attempt in range(3):
        try:
            ch = client.chat.completions.create(**kwargs).choices[0]
            return (ch.message.content or ""), ch.finish_reason
        except Exception as e:
            if attempt == 2:
                return f"__CALL_FAIL__ {e}", "error"
            time.sleep(5 * (attempt + 1))
    return "", "error"


def parse_answer(raw: str) -> dict:
    """{"answer": "yes"|"no", ...} 를 꺼냄. 모델이 코드펜스를 붙이는 경우가 있어 벗겨냄."""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"answer": None, "confidence": None, "reason": "PARSE_FAIL", "raw": raw[:4000]}
    try:
        d = json.loads(m.group(0))
    except Exception:
        return {"answer": None, "confidence": None, "reason": "PARSE_FAIL", "raw": raw[:4000]}
    a = str(d.get("answer", "")).strip().lower()
    return {"answer": a if a in ("yes", "no") else None,
            "confidence": d.get("confidence"),
            "reason": str(d.get("explanation", ""))[:600],
            "raw": None if a in ("yes", "no") else raw[:4000]}


def judge_criterion(response: str, criterion: str, passes: int) -> dict:
    """기준 하나. passes>1 이면 같은 모델로 여러 번 돌려 다수결."""
    votes, details = [], []
    for _ in range(max(1, passes)):
        raw, finish = call_llm(SYSTEM_PROMPT,
                               USER_PROMPT.format(response=response, criterion=criterion))
        d = parse_answer(raw)
        d["finish_reason"] = finish
        details.append(d)
        if d["answer"]:
            votes.append(d["answer"])
    if not votes:
        return {"answer": None, "votes": [], "passes": details}
    win = Counter(votes).most_common(1)[0][0]
    return {"answer": win, "votes": votes,
            "passes": details if passes > 1 else details[:1]}


def judge_question(job: dict) -> dict:
    """문항 하나. 기준을 순차로 판정하고 FAMA를 계산함."""
    resp = job["response"]
    crits = []
    p_ok = p_n = f_ok = f_n = 0
    for c in job["criteria"]:
        # 답변이 비어 있으면 호출을 아끼고 자동 오답 처리함. presence 는 못 맞히고,
        # absence 는 "언급 안 함"이 되어 맞는 것으로 처리하는 것이 정의상 옳음
        if not resp.strip():
            got = "no"
            r = {"answer": got, "votes": [], "passes": [{"reason": "EMPTY_RESPONSE"}]}
        else:
            r = judge_criterion(resp, c["text"], job["passes"])
            got = r["answer"]
        exp = (c["expected"] or "").strip().lower()
        ok = (got == exp) if got else False
        crits.append({**c, "got": got, "ok": ok,
                      "confidence": (r["passes"][0] or {}).get("confidence"),
                      "reason": (r["passes"][0] or {}).get("reason"),
                      "votes": r["votes"]})
        if c["type"] == "memory_presence":
            p_n += 1; p_ok += ok
        else:
            f_n += 1; f_ok += ok
    fa = question_fama(p_ok, p_n, f_ok, f_n)
    return {"persona": job["persona"], "task": job["task"], "question_id": job["question_id"],
            "question": job["question"], "system_response": resp,
            "criteria": crits, **fa}


def main(results_path: str, out_dir: str, max_workers: int, passes: int):
    convs = [json.loads(l) for l in open(results_path, encoding="utf-8") if l.strip()]
    os.makedirs(out_dir, exist_ok=True)
    print(f"페르소나 {len(convs)}개 · judge {MODEL} · effort {REASONING_EFFORT or '기본값'} · passes {passes}")

    all_records = []
    for c in convs:
        outf = os.path.join(out_dir, f"{c['period']}_{c['persona']}.json")
        if os.path.exists(outf):
            print(f"skip {c['persona']} (채점본 있음)")
            with open(outf, encoding="utf-8") as f:
                all_records += json.load(f).get("records", [])
            continue

        jobs = [{"persona": c["persona"], "task": q["task"], "question_id": q["question_id"],
                 "question": q["question"], "criteria": q["criteria"], "passes": passes,
                 "response": (q.get("answer") or {}).get("system_response", "")}
                for q in c["questions"]]
        n_crit = sum(len(j["criteria"]) for j in jobs)
        print(f"채점 {c['persona']}: 문항 {len(jobs)} · 기준 {n_crit} · LLM 호출 {n_crit * passes}")

        recs = []
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = [ex.submit(judge_question, j) for j in jobs]
            for f in tqdm(as_completed(futs), total=len(futs), desc=f"judge {c['persona']}"):
                try:
                    recs.append(f.result())
                except Exception as e:
                    print(f"⚠ 문항 1건 유실: {e}", flush=True)

        os.makedirs(out_dir, exist_ok=True)   # 중간에 지워졌을 경우 대비
        with open(outf, "w", encoding="utf-8") as f:
            json.dump({"persona": c["persona"], "period": c["period"],
                       "judge_model": MODEL, "reasoning_effort": REASONING_EFFORT,
                       "passes": passes, "records": recs}, f, ensure_ascii=False)
        all_records += recs

    fails = [c for r in all_records for c in r["criteria"] if c["got"] is None]
    if fails:
        print(f"\n⚠ 판정 실패 {len(fails)}건 (파싱 실패 또는 호출 실패). 오답으로 처리됨")

    print("\n" + "=" * 62)
    agg = aggregate(all_records)
    print(f"{'과제':16s}{'문항':>6s}{'FAMA':>9s}{'MPA':>9s}{'페널티':>9s}")
    print("-" * 62)
    for t, v in agg.items():
        f = f"{v['fama']:9.2f}" if v["fama"] is not None else f"{'–':>9s}"
        m = f"{v['mpa']:9.2f}" if v["mpa"] is not None else f"{'–':>9s}"
        p = f"{v['penalty']:9.2f}" if v["penalty"] is not None else f"{'–':>9s}"
        print(f"{t:16s}{v['n']:6d}{f}{m}{p}")
    lam = [r["lambda"] for r in all_records]
    if lam:
        print(f"\nlambda 중앙 {statistics.median(lam):.3f} (forgetting 기준의 무게)")
    print(f"done -> {out_dir}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--results", required=True, help="Stage A' 산출물 (answers.jsonl)")
    p.add_argument("--out-dir", required=True, help="채점본 디렉토리. 다른 설정은 반드시 분리")
    p.add_argument("--max-workers", type=int, default=4)
    p.add_argument("--passes", type=int, default=1,
                   help="기준당 판정 횟수. 1이 기본이고 3을 주면 같은 모델 다수결")
    a = p.parse_args()
    main(a.results, a.out_dir, a.max_workers, a.passes)
