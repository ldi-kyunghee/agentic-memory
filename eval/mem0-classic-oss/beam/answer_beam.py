"""
BEAM Stage A': 검색 결과를 cutoff별로 잘라 답변을 생성함.

Stage A가 문항마다 top-200을 저장해뒀으므로 여기서는 검색을 다시 하지 않음.
자르기만 달리해서 cutoff 20/50/100/200 네 벌의 답변을 만듦.

⚠ cutoff가 그 대화의 stored_memories보다 크면 그 설정은 저장소를 전부 준 것과
   같음. 100K 버킷에서는 top-200이 항상 그러함 (저장 99~186개 실측).
   판별에 쓰라고 used/stored를 답변마다 함께 기록함.

컨텍스트 조립은 mem0 팀의 BEAM 하네스를 따름: 오래된 것부터 시간순 정렬,
각 줄 앞에 [날짜]를 붙임. HaluMem 레인의 '시각: 원문'과 형식이 다른데,
BEAM은 mem0 하네스 기준을 따르기로 했으므로 그쪽에 맞춤.
"""
import os
import json
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

MODEL = os.getenv("ANSWER_MODEL", os.getenv("OPENAI_MODEL"))
REASONING_EFFORT = os.getenv("ANSWER_REASONING_EFFORT")
CUTOFFS = [int(c) for c in os.getenv("BEAM_CUTOFFS", "20,50,100,200").split(",")]

client = OpenAI()

# mem0 memory-benchmarks의 get_beam_answer_generation_prompt 이식본.
# 규칙 9개를 원문 그대로 옮김. 번역하면 판정이 달라질 수 있어 영어를 유지함.
PROMPT_BEAM = """You are an AI assistant with access to stored memories from prior conversations with a user.
Use these memories to answer the following question as accurately and completely as possible.

IMPORTANT RULES:
1. Scan ALL provided memories before answering — do not stop after the first relevant one.
2. If multiple memories contain relevant information, combine and cross-reference them.
3. If the memories contain contradictory information, prefer the more recent one.
4. If the memories don't contain enough information to answer, say exactly: "I don't have enough information to answer this question."
5. For temporal questions: pay attention to dates and relative time references.
6. For ordering questions: present events in chronological order.
7. For preference questions: use the most recently stated preference.
8. Be specific and direct — include exact names, dates, numbers, and details from the memories.
9. Do NOT invent or assume information that isn't in the memories.

QUESTION: {question}

RETRIEVED MEMORIES:
{memories}

ANSWER:"""


def build_context(retrieved: list, cutoff: int) -> tuple[str, int]:
    """검색 결과를 cutoff개로 자르고 시간순 정렬해 프롬프트용 문자열로 만듦.

    ⚠ 자르기가 먼저고 정렬이 나중임. 순서를 바꾸면 상위 N개가 아니라
       '오래된 것 N개'를 주게 되어 검색 랭킹이 무의미해짐.
    """
    sliced = retrieved[:cutoff]
    sliced = sorted(sliced, key=lambda m: m.get("session_time") or "")
    if not sliced:
        return "(No memories available)", 0
    lines = []
    for i, m in enumerate(sliced, 1):
        ts = m.get("session_time") or ""
        lines.append(f"{i}. [{ts}] {m['memory']}" if ts else f"{i}. {m['memory']}")
    return "\n".join(lines), len(sliced)


def answer_one(job: dict) -> dict:
    """(대화, 문항, cutoff) 하나에 대한 답변 생성."""
    prompt = PROMPT_BEAM.format(question=job["question"], memories=job["_ctx"])
    kwargs = dict(model=MODEL, messages=[{"role": "user", "content": prompt}])
    if REASONING_EFFORT:
        kwargs["reasoning_effort"] = REASONING_EFFORT
        MAX_COMPLETION_TOKENS = int(os.getenv("ANSWER_MAX_COMPLETION_TOKENS", "32768"))
        kwargs["max_completion_tokens"] = MAX_COMPLETION_TOKENS
    else:
        kwargs["temperature"] = 0.0
        kwargs["max_tokens"] = 1024
    start = time.time()
    resp = client.chat.completions.create(**kwargs)
    choice = resp.choices[0]
    text = choice.message.content or ""
    if "ANSWER:" in text:   # 모델이 프롬프트 꼬리를 따라 쓰는 경우 잘라냄
        text = text.rsplit("ANSWER:", 1)[-1].strip()
    return {
        "conv": job["conv"], "ability": job["ability"], "idx": job["idx"],
        "cutoff": job["cutoff"], "used": job["used"], "stored": job["stored"],
        "system_response": text,
        "finish_reason": choice.finish_reason,   # length 면 예산 부족, stop 이면 정상 종료
        "response_duration_ms": (time.time() - start) * 1000,
    }


def main(results_path: str, out_path: str, max_workers: int, regen: bool):
    convs = [json.loads(l) for l in open(results_path, encoding="utf-8") if l.strip()]
    print(f"대화 {len(convs)}개 · cutoff {CUTOFFS} · 모델 {MODEL}")
    print(f"reasoning effort: {REASONING_EFFORT or '없음 (기본값)'}")

    jobs = []
    for c in convs:
        stored = c.get("stored_memories")
        for q in c["questions"]:
            have = q.get("answers") or {}
            for k in CUTOFFS:
                # 빈 답변은 있으나 마나이므로 재생성 대상으로 둠
                if not regen and (have.get(str(k)) or {}).get("system_response", "").strip():
                    continue
                ctx, used = build_context(q["retrieved"], k)
                jobs.append({
                    "conv": c["conv_id"], "ability": q["ability"], "idx": q["idx"],
                    "question": q["question"], "cutoff": k,
                    "used": used, "stored": stored, "_ctx": ctx,
                })
    print(f"생성 대상 {len(jobs)}개 (문항 {sum(len(c['questions']) for c in convs)} × cutoff {len(CUTOFFS)})")
    if not jobs:
        print("생성할 것이 없음. --regen 을 붙이면 전부 다시 만듦")
        return

    done = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(answer_one, j) for j in jobs]
        for f in tqdm(as_completed(futs), total=len(futs), desc="answer"):
            done.append(f.result())

    # 생성 결과를 원본 레코드에 되붙임
    by_key = {(d["conv"], d["ability"], d["idx"]): {} for d in done}
    for d in done:
        by_key[(d["conv"], d["ability"], d["idx"])][str(d["cutoff"])] = {
            k: d[k] for k in ("system_response", "used", "stored", "finish_reason", "response_duration_ms")
        }
    for c in convs:
        for q in c["questions"]:
            add = by_key.get((c["conv_id"], q["ability"], q["idx"]))
            if add:
                q.setdefault("answers", {}).update(add)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for c in convs:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    # cutoff가 저장소를 넘어선 비율. 판독할 때 이 값을 먼저 봐야 함
    print("\ncutoff별 실제 투입 개수:")
    for k in CUTOFFS:
        used = [q["answers"][str(k)]["used"] for c in convs for q in c["questions"] if str(k) in (q.get("answers") or {})]
        full = sum(1 for c in convs for q in c["questions"]
                   if str(k) in (q.get("answers") or {}) and q["answers"][str(k)]["used"] < k)
        if used:
            print(f"  top-{k:<3d} 평균 {sum(used)/len(used):6.1f}개 · 저장소가 모자라 다 못 채운 문항 {full}/{len(used)}")
    empty = [(c["conv_id"], q["ability"], k) for c in convs for q in c["questions"]
             for k, a in (q.get("answers") or {}).items() if not (a.get("system_response") or "").strip()]
    if empty:
        print(f"\n⚠ 빈 답변 {len(empty)}건. 이대로 채점하면 전부 0점이 됨. 예산을 올리고 다시 돌릴 것")
        print(f"   예: {empty[:3]}")
    print(f"done -> {out_path}")



if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--results", required=True, help="Stage A 산출물 (beam_eval_results.jsonl)")
    p.add_argument("--out", required=True, help="저장 경로. in-place 덮어쓰기 금지")
    p.add_argument("--max-workers", type=int, default=20)
    p.add_argument("--regen", action="store_true", help="기존 answers 무시하고 전부 재생성")
    a = p.parse_args()
    main(a.results, a.out, a.max_workers, a.regen)