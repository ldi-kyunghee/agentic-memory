"""판정 검토 큐의 항목을 다른 judge 모델로 재채점한다 (4종 전부 지원).

왜 update에서 출발했나
----------------------
판정 검토 큐(분석가 3명 완료) 결과, 사람과 gpt-oss-120b(high)의 판단이 가장 크게 벌어지는
항목이 Memory Update다. judge 자기 일관성도 update가 최악이고(Fleiss κ 0.384, §17-2),
judge 간 순위 상관도 update만 ρ 0.47로 유일하게 낮다(§10). 그래서 "더 나은 judge를 쓰면
사람과의 일치가 올라가는가"를 update 축에서만 검증한다.

두 단계
-------
  --scope queue --types ... : 큐 항목만 재채점. 사람 라벨이 있는 항목이라 '사람 대조'가
                  가능한 유일한 집합이다. 실측: integrity 35건/35K · accuracy 39건/166K ·
                  update 40건/76K · qa 39건/35K 토큰. accuracy가 대화 전문을 담아 가장 비싸다.
  --scope run   : 지정 런의 update 레코드 전체 재채점 (update 전용). 사람 라벨은 없지만
                  judge 간 일치도와 Upd C 지표 변화를 본다. oss120b4 기준 595건 · ~459K 토큰.

⚠ 추출 메모리가 빈 integrity 항목은 judge.py가 LLM 호출 없이 0점 처리하므로(judge.py:127)
  재채점 대상에서 제외한다 — 큐에서 4건.
⚠ QA만 답변 생성 레인에 종속된다. 큐 항목의 generator 레인에서 system_response를 읽는다.

채점 프로토콜은 HaluMem 원본 프롬프트를 그대로 import 한다 (무수정 원칙).
모델만 바뀌고 입력은 기존 judge와 비트 단위로 같아야 대조가 성립한다.

산출물: results/mem0-classic-oss/rejudge-update/{tag}.json
  [{run, uuid, session_id, idx, memory_content, base_label, new_label, raw, usage}, ...]
"""

import os
import sys
import json
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml
from dotenv import load_dotenv
from tqdm import tqdm
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_random_exponential

load_dotenv()

# 채점 프롬프트는 서브모듈 원본 그대로 (프로토콜 무수정)
sys.path.insert(0, "HaluMem/eval")
from eval_tools import (  # noqa: E402
    EVALUATION_PROMPT_FOR_MEMORY_INTEGRITY,
    EVALUATION_PROMPT_FOR_MEMORY_ACCURACY,
    EVALUATION_PROMPT_FOR_UPDATE_MEMORY,
    EVALUATION_PROMPT_FOR_QUESTION,
)

HERE = os.path.dirname(os.path.abspath(__file__))
REG = os.path.join(HERE, "..", "..", "src", "web-dashboard", "runs.yaml")
QUEUE = os.path.join(HERE, "..", "..", "src", "web-dashboard", "data", "annotation_queue.json")
OUT_DIR = "results/mem0-classic-oss/rejudge-update"

client = OpenAI()
MODEL = os.getenv("REJUDGE_MODEL")
EFFORT = os.getenv("REJUDGE_REASONING_EFFORT")   # 설정 시 reasoning 모델로 취급

_users_cache: dict = {}


def load_registry() -> dict:
    with open(REG, encoding="utf-8") as f:
        return yaml.safe_load(f)["runs"]


def user_record(reg: dict, run: str, uuid: str, lane: str | None = None):
    """lane이 주어지면 그 generator 레인의 jsonl에서 읽는다 (QA는 답변이 레인 종속)."""
    doc = yaml.safe_load(open(REG, encoding="utf-8"))
    if lane and lane != "qwen4b":
        g = (doc.get("generators") or {}).get(lane) or {}
        path = g["results"].format(run=run) if g.get("results") else reg[run]["results"]
    else:
        path = reg[run]["results"]
    if path not in _users_cache:
        if not os.path.exists(path):
            _users_cache[path] = {}
        else:
            _users_cache[path] = {u["uuid"]: u for u in
                                  (json.loads(l) for l in open(path, encoding="utf-8") if l.strip())}
    return _users_cache[path].get(uuid)


# ⚠ LLM이 반환하는 JSON 키 — 레코드 필드명(memory_*_score)과 다르다. judge.py:150~162 기준:
#     integrity -> r.get("score")            (record: memory_integrity_score)
#     accuracy  -> r.get("accuracy_score")   (record: memory_accuracy_score)
#     update/qa -> r.get("evaluation_result")
#   레코드 필드명으로 착각하면 integrity/accuracy가 전부 파싱 실패한다 (실제로 겪음).
LABEL_KEY = {"integrity": "score", "accuracy": "accuracy_score",
             "update": "evaluation_result", "qa": "evaluation_result"}


def _dialogue_str(session: dict) -> str:
    """judge.py build_inputs와 동일한 대화 직렬화 — 한 글자라도 다르면 대조가 깨진다."""
    out = []
    for t in session["dialogue"]:
        out.append(f'[{t["timestamp"]}]{t["role"]}: {t["content"]}')
        if t["role"] == "assistant":
            out.append("")
    return "\n".join(out)


def build_prompt(rec_type: str, session: dict, idx: int) -> str | None:
    """judge.py:126~139와 완전히 동일한 조립 — 입력이 다르면 대조가 성립하지 않는다."""
    golden = session.get("memory_points") or []
    extracted = session.get("extracted_memories") or []
    if rec_type == "integrity":
        mp = golden[idx]
        if not "\n".join(extracted).strip():
            return None          # judge.py는 LLM 호출 없이 0점 처리 — 재채점 대상이 아니다
        return EVALUATION_PROMPT_FOR_MEMORY_INTEGRITY.format(
            memories="\n".join(extracted), expected_memory_point=mp["memory_content"])
    if rec_type == "accuracy":
        golden_str = "\n".join(m["memory_content"] for m in golden
                               if m.get("memory_source") != "interference")
        return EVALUATION_PROMPT_FOR_MEMORY_ACCURACY.format(
            dialogue=_dialogue_str(session), golden_memories=golden_str,
            candidate_memory=extracted[idx])
    if rec_type == "update":
        mp = golden[idx]
        return EVALUATION_PROMPT_FOR_UPDATE_MEMORY.format(
            memories="\n".join(mp.get("memories_from_system") or []),
            updated_memory=mp["memory_content"],
            original_memory="\n".join(mp.get("original_memories") or []))
    if rec_type == "qa":
        q = (session.get("questions") or [])[idx]
        # ⚠ QA만 답변 생성 레인에 종속된다 — system_response가 없으면 그 레인 A′가 없다는 뜻
        if not q.get("system_response"):
            return None
        return EVALUATION_PROMPT_FOR_QUESTION.format(
            question=q["question"], reference_answer=q["answer"],
            key_memory_points="\n".join(e["memory_content"] for e in q.get("evidence") or []),
            response=q["system_response"])
    return None


@retry(wait=wait_random_exponential(min=5, max=60), stop=stop_after_attempt(4), reraise=True)
def judge_one(item: dict) -> dict:
    kwargs = dict(model=MODEL, messages=[{"role": "user", "content": item["prompt"]}],
                  response_format={"type": "json_object"})
    if EFFORT:
        kwargs["reasoning_effort"] = EFFORT
        kwargs["max_completion_tokens"] = 8192
    else:
        kwargs["temperature"] = 0.0
        kwargs["max_tokens"] = 512
    start = time.time()
    r = client.chat.completions.create(**kwargs)
    raw = r.choices[0].message.content or ""
    try:
        label = json.loads(raw).get(LABEL_KEY[item["rec_type"]])
    except json.JSONDecodeError:
        label = None
    # ⚠ 비용은 completion_tokens로 과금되며 reasoning 토큰이 여기 포함되는지 반드시 확인해야 한다.
    #    포함 여부를 가정하지 말고 details를 그대로 기록해 사후 검증이 가능하게 둔다.
    det = getattr(r.usage, "completion_tokens_details", None)
    reasoning = getattr(det, "reasoning_tokens", None) if det else None
    return {**{k: v for k, v in item.items() if k != "prompt"},
            "new_label": label, "raw": raw[:4000],
            "usage": {"in": r.usage.prompt_tokens, "out": r.usage.completion_tokens,
                      "reasoning": reasoning, "total": r.usage.total_tokens},
            "duration_ms": round((time.time() - start) * 1000)}


def collect_queue(reg: dict, types: list) -> list:
    with open(QUEUE, encoding="utf-8") as f:
        q = json.load(f)["items"]
    out, skipped = [], 0
    for x in q:
        if x["rec_type"] not in types:
            continue
        # QA는 답변 생성 레인에 종속 — 큐 항목에 기록된 generator 레인으로 읽어야 한다
        lane = x.get("generator") or "qwen4b"
        u = user_record(reg, x["run"], x["uuid"], lane if x["rec_type"] == "qa" else None)
        if u is None:
            skipped += 1
            continue
        p = build_prompt(x["rec_type"], u["sessions"][x["session_id"]], x["idx"])
        if p is None:
            skipped += 1
            continue
        out.append({"run": x["run"], "uuid": x["uuid"], "session_id": x["session_id"],
                    "idx": x["idx"], "rec_type": x["rec_type"], "generator": x.get("generator", ""),
                    "base_label": x.get("judge_label"), "prompt": p})
    if skipped:
        print(f"⚠ 프롬프트 복원 불가로 제외 {skipped}건")
    return out


def collect_run(reg: dict, run: str, user_num: int) -> list:
    order = []
    for i, l in enumerate(open("dataset/HaluMem-Medium.jsonl", encoding="utf-8")):
        if i >= user_num:
            break
        order.append(json.loads(l)["uuid"])
    out = []
    for uid in order:
        u = user_record(reg, run, uid)
        if u is None:
            continue
        for si, s in enumerate(u["sessions"]):
            for idx, mp in enumerate(s.get("memory_points") or []):
                if str(mp.get("is_update", "")).lower() != "true" or not mp.get("memories_from_system"):
                    continue
                out.append({"run": run, "uuid": uid, "session_id": si, "idx": idx,
                            "rec_type": "update", "generator": "",
                            "base_label": None, "prompt": build_prompt("update", s, idx)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["queue", "run"], default="queue")
    ap.add_argument("--types", default="update",
                    help="재채점할 레코드 종류 (쉼표: integrity,accuracy,update,qa). --scope run은 update만")
    ap.add_argument("--run", default="oss120b4", help="--scope run 일 때 대상 런")
    ap.add_argument("--user-num", type=int, default=4)
    ap.add_argument("--tag", required=True, help="산출물 파일명 (모델·effort를 알아볼 수 있게)")
    ap.add_argument("--max-workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true", help="호출 없이 대상 수와 토큰만 집계")
    args = ap.parse_args()

    if not MODEL and not args.dry_run:
        sys.exit("REJUDGE_MODEL env가 필요합니다")

    base = os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1 (기본)"
    print(f"모델: {MODEL} · effort: {EFFORT or '없음'} · scope: {args.scope}")
    print(f"base_url: {base}")
    # ⚠ .env에 OPENAI_BASE_URL=http://localhost:8000/v1 이 들어 있다. load_dotenv는 override=False라
    #    셸에서 명시하지 않으면 그 값이 그대로 쓰인다. OpenAI 모델명으로 로컬 엔드포인트에 쏘면
    #    조용히 **다른 모델의 판정**이 그 태그로 저장돼 실험이 오염된다 — 아예 막는다.
    if not args.dry_run and MODEL.startswith("gpt-") and ("localhost" in base or "127.0.0.1" in base):
        sys.exit(f"❌ OpenAI 모델({MODEL})인데 base_url이 로컬({base})입니다.\n"
                 f"   OPENAI_BASE_URL=https://api.openai.com/v1 을 커맨드 앞에 명시하세요.")

    reg = load_registry()
    types = [t.strip() for t in args.types.split(",") if t.strip()]
    items = (collect_queue(reg, types) if args.scope == "queue"
             else collect_run(reg, args.run, args.user_num))
    print(f"대상 {len(items)}건")

    if args.dry_run:
        import tiktoken
        enc = tiktoken.get_encoding("o200k_base")
        from collections import Counter
        per = Counter()
        cnt = Counter()
        for i in items:
            per[i["rec_type"]] += len(enc.encode(i["prompt"]))
            cnt[i["rec_type"]] += 1
        for t in ["integrity", "accuracy", "update", "qa"]:
            if cnt[t]:
                print(f"  {t:10s} {cnt[t]:>4d}건 · 입력 {per[t]:>9,} tok (평균 {per[t]//cnt[t]:>6,})")
        print(f"  {'합계':10s} {sum(cnt.values()):>4d}건 · 입력 {sum(per.values()):>9,} tok")
        return

    results = []
    with ThreadPoolExecutor(max_workers=args.max_workers) as ex:
        futs = [ex.submit(judge_one, i) for i in items]
        for f in tqdm(as_completed(futs), total=len(futs), desc="재채점"):
            results.append(f.result())

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{args.tag}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"model": MODEL, "effort": EFFORT, "scope": args.scope,
                   "run": args.run if args.scope == "run" else None,
                   "n": len(results), "items": results}, f, ensure_ascii=False, indent=1)

    bad = sum(1 for r in results if r["new_label"] is None)
    ti = sum(r["usage"]["in"] for r in results)
    to = sum(r["usage"]["out"] for r in results)
    tr = sum(r["usage"]["reasoning"] or 0 for r in results)
    tt = sum(r["usage"]["total"] or 0 for r in results)
    print(f"done -> {path}")
    print(f"  파싱 실패 {bad}건")
    print(f"  토큰 입력 {ti:,} / 출력 {to:,} (그중 reasoning {tr:,}) / total {tt:,}")
    if tt and tt != ti + to:
        print(f"  ⚠ total != 입력+출력 — 과금 토큰이 따로 잡힙니다. 차 {tt - ti - to:,}")
    if args.scope == "queue":
        same = sum(1 for r in results if r["base_label"] and r["new_label"] == r["base_label"])
        cmp_n = sum(1 for r in results if r["base_label"] and r["new_label"])
        print(f"  기존 judge(gpt-oss-120b)와 일치: {same}/{cmp_n}")


if __name__ == "__main__":
    main()
