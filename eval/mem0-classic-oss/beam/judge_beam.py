"""
BEAM Stage B: rubric nugget 단위로 채점함.

채점 프로토콜은 BEAM 공식 것을 그대로 씀.
  - 프롬프트: BEAM/src/prompts.py 의 unified_llm_judge_base_prompt 를 import (무수정)
  - 능력 10종 전부 같은 프롬프트를 rubric 항목마다 한 번씩 돌림
  - 문항 점수 = nugget 점수 평균, 각 nugget 은 0 / 0.5 / 1.0

event_ordering 만 예외로 Kendall tau 를 추가 계산함 (src/beam/beam_official.py).
공식 리포트는 tau_norm 을, mem0 하네스는 nugget 평균을 최종 점수로 씀.
정의가 갈리므로 둘 다 저장하고 판독 단계에서 고름.

산출물은 cutoff 별로 분리됨. judge 캐시는 대화 단위라 기존 파일이 있으면 건너뜀.
다른 모델로 재채점할 때는 --out-dir 을 반드시 바꿀 것.
"""
import os
import re
import sys
import json
import argparse
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

sys.path.insert(0, "BEAM/src")      # 공식 프롬프트 (상수뿐이라 부작용 없음)
sys.path.insert(0, "src/beam")      # 공식 점수 함수 복사본
from prompts import unified_llm_judge_base_prompt
from beam_official import event_ordering_score

MODEL = os.getenv("JUDGE_MODEL", os.getenv("OPENAI_MODEL"))
REASONING_EFFORT = os.getenv("JUDGE_REASONING_EFFORT")
MAX_COMPLETION_TOKENS = int(os.getenv("JUDGE_MAX_COMPLETION_TOKENS", "32768"))
client = OpenAI()


def call_llm(prompt: str = None, messages: list = None) -> tuple[str, str]:
    """(응답 텍스트, finish_reason) 을 돌려줌.

    finish_reason 이 length 면 예산 부족임. 그 경우 응답이 잘리거나 비어서
    파싱에 실패하는데, 모델이 형식을 어긴 것과 구분해야 원인을 잡을 수 있음.
    """
    kwargs = dict(model=MODEL,
                  messages=messages or [{"role": "user", "content": prompt}])
    if REASONING_EFFORT:
        kwargs["reasoning_effort"] = REASONING_EFFORT
        kwargs["max_completion_tokens"] = MAX_COMPLETION_TOKENS
    else:
        kwargs["temperature"] = 0.0
        kwargs["max_tokens"] = 1024
    ch = client.chat.completions.create(**kwargs).choices[0]
    return (ch.message.content or ""), ch.finish_reason


def parse_score(raw: str) -> dict:
    """judge 응답에서 JSON 을 꺼냄. 코드펜스나 앞뒤 설명이 붙어도 견디게 함."""
    t = raw.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return {"score": 0.0, "reason": "PARSE_FAIL", "raw": raw[:4000]}
    try:
        d = json.loads(m.group(0))
        s = float(d.get("score", 0))
        return {"score": s if s in (0.0, 0.5, 1.0) else max(0.0, min(1.0, s)),
                "reason": str(d.get("reason", ""))[:600]}
    except Exception:
        return {"score": 0.0, "reason": "PARSE_FAIL", "raw": raw[:4000]}


def judge_nugget(question: str, nugget: str, response: str) -> dict:
    """공식 프롬프트의 치환자 세 개를 채워 호출함. 프롬프트 본문은 건드리지 않음."""
    prompt = (unified_llm_judge_base_prompt
              .replace("<question>", question)
              .replace("<rubric_item>", nugget)
              .replace("<llm_response>", response))
    raw, finish = call_llm(prompt=prompt)
    out = parse_score(raw)
    out["finish_reason"] = finish
    return out


def judge_one(job: dict) -> dict:
    q, resp = job["question"], job["response"]
    nuggets = [judge_nugget(q, n, resp) for n in job["rubric"]]
    score = statistics.mean(n["score"] for n in nuggets) if nuggets else 0.0
    out = {"conv": job["conv"], "ability": job["ability"], "idx": job["idx"],
           "cutoff": job["cutoff"], "used": job["used"], "stored": job["stored"],
           "question": q, "rubric": job["rubric"], "system_response": resp,
           "nugget_scores": nuggets, "score": round(score, 4)}

    if job["ability"] == "event_ordering" and job["rubric"]:
        # ⚠ 원본이 개행 분리를 쓰므로 그대로 따름 (beam_official.py 주석 참고)
        try:
            eo = event_ordering_score(job["rubric"], resp.split("\n"),
                                      llm=lambda messages: call_llm(messages=messages)[0])
            out["event_ordering"] = {k: round(v, 4) for k, v in eo.items()}
        except Exception as e:
            out["event_ordering"] = {"error": str(e)[:200]}
    return out


def main(results_path: str, out_dir: str, max_workers: int, cutoffs: list[int] | None):
    convs = [json.loads(l) for l in open(results_path, encoding="utf-8") if l.strip()]
    os.makedirs(out_dir, exist_ok=True)
    print(f"대화 {len(convs)}개 · judge {MODEL} · effort {REASONING_EFFORT or '기본값'}")

    # ⚠ 대화 하나가 끝날 때마다 즉시 저장함. 전부 모았다가 맨 끝에 한 번 쓰면
    #    그 지점에서 죽을 때 몇 시간치가 통째로 날아감 (실측: 5500콜 1.5시간 유실).
    #    파일이 이미 있으면 건너뛰므로 중단 후 재실행도 이어서 됨.
    results = []
    todo = []
    for c in convs:
        if os.path.exists(os.path.join(out_dir, f"{c['conv_id']}.json")):
            print(f"skip {c['conv_id']} (채점본 있음)")
            continue
        jobs = []
        for q in c["questions"]:
            for k, a in (q.get("answers") or {}).items():
                if cutoffs and int(k) not in cutoffs:
                    continue
                jobs.append({
                    "conv": c["conv_id"], "ability": q["ability"], "idx": q["idx"],
                    "cutoff": int(k), "used": a.get("used"), "stored": a.get("stored"),
                    "question": q["question"], "rubric": q["rubric"],
                    "response": a["system_response"],
                })
        if jobs:
            todo.append((c["conv_id"], jobs))

    total = sum(len(j) for _, j in todo)
    print(f"채점 대상 {total}건 (대화 {len(todo)}개)")
    if not total:
        print("채점할 것이 없음. out-dir 을 비우거나 다른 경로를 줄 것")
        return

    bar = tqdm(total=total, desc="judge")
    for conv, jobs in todo:
        rows = []
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = [ex.submit(judge_one, j) for j in jobs]
            for f in as_completed(futs):
                rows.append(f.result())
                bar.update(1)
        os.makedirs(out_dir, exist_ok=True)   # 실행 중 디렉토리가 사라져도 다시 만듦
        with open(os.path.join(out_dir, f"{conv}.json"), "w", encoding="utf-8") as f:
            json.dump({"conv_id": conv, "judge_model": MODEL,
                       "reasoning_effort": REASONING_EFFORT, "records": rows},
                      f, ensure_ascii=False)
        results += rows
        bar.write(f"saved {conv} ({len(rows)}건)")
    bar.close()

    fails = [n for r in results for n in r["nugget_scores"] if n["reason"] == "PARSE_FAIL"]
    budget = sum(1 for n in fails if n.get("finish_reason") == "length")
    print(f"\nJSON 파싱 실패 nugget {len(fails)}건 (예산 부족 {budget}건, 형식 위반 {len(fails)-budget}건)")
    if budget:
        print("  ⚠ 예산 부족분은 JUDGE_MAX_COMPLETION_TOKENS 를 올려 다시 채점할 것")
    for k in sorted({r["cutoff"] for r in results}):
        rows = [r for r in results if r["cutoff"] == k]
        print(f"  top-{k:<3d} 문항 {len(rows):>4d} · 평균점수 {statistics.mean(r['score'] for r in rows):.4f}")
    print(f"done -> {out_dir}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--results", required=True, help="Stage A' 산출물")
    p.add_argument("--out-dir", required=True, help="채점본 저장 경로. 모델을 바꾸면 반드시 분리할 것")
    p.add_argument("--max-workers", type=int, default=20)
    p.add_argument("--cutoffs", default=None, help="쉼표 구분. 미지정이면 전부")
    a = p.parse_args()
    main(a.results, a.out_dir, a.max_workers,
         [int(c) for c in a.cutoffs.split(",")] if a.cutoffs else None)