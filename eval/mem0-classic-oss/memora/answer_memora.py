"""Memora Stage A': 검색 결과로 답변을 생성함.

Stage A가 문항마다 검색 결과를 저장해뒀으므로 여기서 검색을 다시 하지 않음.

기본 레인은 공식 하네스의 단일 limit(50)을 따름. cutoff 스윕은 Stage A 를 큰 k 로 한 번
돌려두고 `--cutoff` 로 잘라 만듦 (BEAM 하네스와 동일한 방식). Memora 공식에는 cutoff 개념이
없으므로 이것은 우리 진단용 팔이고, 공식 수치는 언제나 cutoff 50 임.

프롬프트와 컨텍스트 포맷은 공식 하네스(`evals/agent_eval/memory_to_answer.py`)를 그대로 옮김.
BEAM에서 배운 것이 이유임: 저장·검색을 고정해도 **답변 규약만으로 점수가 25% 움직임**
(beam-experiment.md §7). 그래서 원본이 있으면 원본을 쓰고, 우리 변형은 대조군으로만 둠.

⚠ 공식 컨텍스트에는 **날짜가 안 들어감.** `N. {memory} (relevance: 0.87)` 형식임.
   BEAM에서는 우리가 `[날짜]` 접두사를 붙였는데 여기서는 원본을 따름. 시간 문항에 불리하지만
   그것이 원본의 조건임. 날짜를 붙인 대조군은 MEMORA_CTX_DATE=1 로 켤 수 있음.

⚠ 공식은 gpt-4o-mini + max_tokens=500 + temperature=0.7 임. 우리는 레인을 맞추려고
   gpt-oss-120b 를 씀. reasoning 모델은 temperature 를 안 받고 사고 토큰이 예산을 함께 먹으므로
   ANSWER_REASONING_EFFORT 가 설정되면 max_completion_tokens 로 분기함.
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
CTX_DATE = os.getenv("MEMORA_CTX_DATE") == "1"      # 대조군: 컨텍스트에 세션 날짜를 붙임
MAX_TOKENS = int(os.getenv("ANSWER_MAX_TOKENS", "500"))            # 공식값
MAX_COMPLETION_TOKENS = int(os.getenv("ANSWER_MAX_COMPLETION_TOKENS", "32768"))

client = OpenAI()

# 공식 memory_to_answer.py 의 프롬프트 두 개. 원문 그대로 옮김.
SYSTEM_PROMPT = """You are a helpful AI assistant with access to a user's personal memory system.

Your task is to answer questions based ONLY on the user's stored memories and preferences.

Guidelines:
1. Use ONLY the information from the provided memories
2. Be specific and reference actual items/preferences from memory
3. If the question is about recommendations, suggest based on similar items in memory
4. Be conversational and helpful
5. Don't make up information not in the memories"""

USER_PROMPT = """User's Question: {question}

User's Relevant Memories:
{memories}

Please provide a helpful answer based on these memories."""


def build_context(retrieved: list, cutoff: int | None = None) -> tuple[str, int]:
    """공식 포맷: `N. {memory} (relevance: 0.87)`.

    ⚠ 정렬하지 않음. 공식은 검색 점수 순 그대로 씀.

    cutoff 를 주면 앞에서 그만큼만 씀. Stage A 를 큰 k 로 한 번만 돌리고 답변 단계에서
    잘라 쓰는 방식임 (BEAM 하네스와 동일). Stage A 재실행은 quarterly 기준 13.6시간이라
    cutoff 마다 다시 투입할 수 없음. 미지정이면 저장된 전량을 씀.
    """
    if not retrieved:
        return "(No memories available)", 0
    if cutoff:
        retrieved = retrieved[:cutoff]
    lines = []
    for i, m in enumerate(retrieved, 1):
        score = m.get("score")
        tail = f" (relevance: {score:.2f})" if isinstance(score, (int, float)) else ""
        head = f"[{m.get('session_date')}] " if (CTX_DATE and m.get("session_date")) else ""
        lines.append(f"{i}. {head}{m['memory']}{tail}")
    return "\n".join(lines), len(retrieved)


def answer_one(job: dict) -> dict:
    kwargs = dict(model=MODEL, messages=[
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_PROMPT.format(question=job["question"], memories=job["_ctx"])},
    ])
    if REASONING_EFFORT:
        kwargs["reasoning_effort"] = REASONING_EFFORT
        kwargs["max_completion_tokens"] = MAX_COMPLETION_TOKENS
    else:
        kwargs["temperature"] = 0.7          # 공식값
        kwargs["max_tokens"] = MAX_TOKENS

    start = time.time()
    # ⚠ 예외를 밖으로 내보내면 안 됨. 저장이 전체 완료 후 한 번뿐이라 1건 실패가 나머지를 날림.
    #    재시도하고, 그래도 안 되면 빈 답변으로 남김. 빈 답변은 다음 실행에서 자동 재생성됨.
    text, finish = "", "error"
    for attempt in range(3):
        try:
            choice = client.chat.completions.create(**kwargs).choices[0]
            text = (choice.message.content or "").strip()
            finish = choice.finish_reason
            break
        except Exception as e:
            if attempt == 2:
                print(f"⚠ 3회 실패 {job['persona']}/{job['question_id']}: {e}", flush=True)
            else:
                time.sleep(5 * (attempt + 1))

    return {"persona": job["persona"], "task": job["task"], "question_id": job["question_id"],
            "system_response": text, "used": job["used"], "stored": job["stored"],
            "ctx_date": CTX_DATE, "finish_reason": finish,
            "response_duration_ms": (time.time() - start) * 1000}


def main(results_path: str, out_path: str, max_workers: int, regen: bool,
         cutoff: int | None = None):
    convs = [json.loads(l) for l in open(results_path, encoding="utf-8") if l.strip()]
    n_q = sum(len(c["questions"]) for c in convs)
    print(f"페르소나 {len(convs)}개 · 문항 {n_q}개 · 모델 {MODEL}")
    print(f"reasoning effort: {REASONING_EFFORT or '없음 (기본값)'}")
    print(f"컨텍스트 날짜 접두사: {'켬 (대조군)' if CTX_DATE else '끔 (공식 포맷)'}")
    print(f"검색 cutoff: {cutoff if cutoff else '없음 (저장된 전량)'}")

    jobs = []
    for c in convs:
        for q in c["questions"]:
            if not regen and (q.get("answer") or {}).get("system_response", "").strip():
                continue
            ctx, used = build_context(q.get("retrieved") or [], cutoff)
            jobs.append({"persona": c["persona"], "task": q["task"],
                         "question_id": q["question_id"], "question": q["question"],
                         "used": used, "stored": c.get("stored_memories"), "_ctx": ctx})
    print(f"생성 대상 {len(jobs)}개")
    if not jobs:
        print("생성할 것이 없음. --regen 을 붙이면 전부 다시 만듦")
        return

    done = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(answer_one, j) for j in jobs]
        for f in tqdm(as_completed(futs), total=len(futs), desc="answer"):
            try:
                done.append(f.result())
            except Exception as e:   # answer_one이 이미 잡지만 이중으로 막아둠
                print(f"⚠ 워커 예외로 1건 유실: {e}", flush=True)

    by_key = {(d["persona"], d["question_id"]): d for d in done}
    for c in convs:
        for q in c["questions"]:
            add = by_key.get((c["persona"], q["question_id"]))
            if add:
                q["answer"] = {k: add[k] for k in
                               ("system_response", "used", "stored", "ctx_date",
                                "finish_reason", "response_duration_ms")}

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for c in convs:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    lens = [len(q["answer"]["system_response"]) for c in convs for q in c["questions"]
            if q.get("answer")]
    empty = [(c["persona"], q["question_id"]) for c in convs for q in c["questions"]
             if q.get("answer") and not q["answer"]["system_response"].strip()]
    if lens:
        lens.sort()
        print(f"\n답변 {len(lens)}건 · 중앙 {lens[len(lens)//2]}자 · 최대 {lens[-1]}자")
    if empty:
        print(f"⚠ 빈 답변 {len(empty)}건. 이대로 채점하면 전부 0점이 됨. 예산을 올리고 다시 돌릴 것")
        print(f"   예: {empty[:3]}")
    print(f"done -> {out_path}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--results", required=True, help="Stage A 산출물 (memora_eval_results.jsonl)")
    p.add_argument("--out", required=True, help="저장 경로. in-place 덮어쓰기 금지")
    p.add_argument("--max-workers", type=int, default=4)
    p.add_argument("--regen", action="store_true", help="기존 answer 무시하고 전부 재생성")
    p.add_argument("--cutoff", type=int, default=None,
                   help="검색 결과를 앞에서 N개만 씀. 미지정이면 저장된 전량. "
                        "Stage A 를 큰 k 로 한 번 돌리고 여기서 잘라 여러 팔을 만듦")
    a = p.parse_args()
    main(a.results, a.out, a.max_workers, a.regen, a.cutoff)
