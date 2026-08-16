"""Memory Update 판정만 다른 judge 모델로 재채점한다.

왜 update만인가
---------------
판정 검토 큐(분석가 3명 완료) 결과, 사람과 gpt-oss-120b(high)의 판단이 가장 크게 벌어지는
항목이 Memory Update다. judge 자기 일관성도 update가 최악이고(Fleiss κ 0.384, §17-2),
judge 간 순위 상관도 update만 ρ 0.47로 유일하게 낮다(§10). 그래서 "더 나은 judge를 쓰면
사람과의 일치가 올라가는가"를 update 축에서만 검증한다.

두 단계
-------
  --scope queue : 큐에 든 update 항목만 재채점 (기본). 사람 라벨이 있는 항목이라
                  '사람 대조'가 가능한 유일한 집합. 40건 · 입력 ~76K 토큰으로 매우 싸다.
  --scope run   : 지정 런의 update 레코드 전체 재채점. 사람 라벨은 없지만 judge 간 일치도와
                  Upd C 지표가 얼마나 달라지는지를 본다. oss120b4 기준 595건 · 입력 ~459K 토큰.

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
from eval_tools import EVALUATION_PROMPT_FOR_UPDATE_MEMORY  # noqa: E402

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


def user_record(reg: dict, run: str, uuid: str):
    if run not in _users_cache:
        path = reg[run]["results"]
        _users_cache[run] = {u["uuid"]: u for u in
                             (json.loads(l) for l in open(path, encoding="utf-8") if l.strip())}
    return _users_cache[run].get(uuid)


def build_prompt(mp: dict) -> str:
    """judge.py:137과 완전히 동일한 조립 — 입력이 다르면 대조가 성립하지 않는다."""
    return EVALUATION_PROMPT_FOR_UPDATE_MEMORY.format(
        memories="\n".join(mp.get("memories_from_system") or []),
        updated_memory=mp["memory_content"],
        original_memory="\n".join(mp.get("original_memories") or []),
    )


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
        label = json.loads(raw).get("evaluation_result")
    except json.JSONDecodeError:
        label = None
    # ⚠ 비용은 completion_tokens로 과금되며 reasoning 토큰이 여기 포함되는지 반드시 확인해야 한다.
    #    포함 여부를 가정하지 말고 details를 그대로 기록해 사후 검증이 가능하게 둔다.
    det = getattr(r.usage, "completion_tokens_details", None)
    reasoning = getattr(det, "reasoning_tokens", None) if det else None
    return {**{k: v for k, v in item.items() if k != "prompt"},
            "new_label": label, "raw": raw[:400],
            "usage": {"in": r.usage.prompt_tokens, "out": r.usage.completion_tokens,
                      "reasoning": reasoning, "total": r.usage.total_tokens},
            "duration_ms": round((time.time() - start) * 1000)}


def collect_queue(reg: dict) -> list:
    with open(QUEUE, encoding="utf-8") as f:
        q = json.load(f)["items"]
    out = []
    for x in q:
        if x["rec_type"] != "update":
            continue
        u = user_record(reg, x["run"], x["uuid"])
        if u is None:
            continue
        mp = u["sessions"][x["session_id"]]["memory_points"][x["idx"]]
        out.append({"run": x["run"], "uuid": x["uuid"], "session_id": x["session_id"],
                    "idx": x["idx"], "memory_content": mp["memory_content"],
                    "base_label": x.get("judge_label"), "prompt": build_prompt(mp)})
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
                            "memory_content": mp["memory_content"],
                            "base_label": None, "prompt": build_prompt(mp)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["queue", "run"], default="queue")
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
    items = collect_queue(reg) if args.scope == "queue" else collect_run(reg, args.run, args.user_num)
    print(f"대상 {len(items)}건")

    if args.dry_run:
        import tiktoken
        enc = tiktoken.get_encoding("o200k_base")
        tot = sum(len(enc.encode(i["prompt"])) for i in items)
        print(f"입력 토큰 합계 {tot:,} (평균 {tot // max(len(items),1):,})")
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
