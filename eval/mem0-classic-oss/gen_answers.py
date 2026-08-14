import os
import re
import sys
import json
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from tqdm import tqdm
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_random_exponential

load_dotenv()

# 서브모듈의 QA 프롬프트를 그대로 import (프로토콜 충실성 — 복사본 만들지 않음)
sys.path.insert(0, "HaluMem/eval")
from prompts import PROMPT_MEMZERO

client = OpenAI()  # OPENAI_API_KEY / OPENAI_BASE_URL env 자동 사용
MODEL = os.getenv("ANSWER_MODEL", os.getenv("OPENAI_MODEL"))
REASONING_EFFORT = os.getenv("ANSWER_REASONING_EFFORT")  # gpt-5 계열일 때만 설정 (예: minimal)


# 오라클 모드: 메모리 시스템의 검색 결과 대신 '정답 근거 골든'만 context로 준다.
# 검색·저장을 완벽하게 했다고 가정했을 때의 QA 상한 -> generator 자체가 병목인지 판별용.
ORACLE = os.getenv("ANSWER_ORACLE_CONTEXT") == "1"
# 오라클 context를 실제 검색 규모(top-k)까지 채울지. 0이면 evidence만.
# evidence만 주면 '검색 정확도'와 '무관정보 제거' 효과가 섞이므로, 실제 검색 결과에서
# 무관 항목을 끌어와 같은 규모로 맞춘 대조군을 만든다 -> 두 효과를 분리 측정.
ORACLE_PAD = int(os.getenv("ANSWER_ORACLE_PAD", "0"))

TEMPLATE_MEM0 = """Memories for user {user_id}:

    {memories}
"""


def _parse_context(raw) -> list[str]:
    """저장된 검색 context 문자열에서 메모리 목록만 복원."""
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if not isinstance(raw, str):
        return []
    a, b = raw.find("["), raw.rfind("]")
    if a < 0 or b <= a:
        return []
    try:
        arr = json.loads(raw[a:b + 1])
        return [str(x) for x in arr] if isinstance(arr, list) else []
    except json.JSONDecodeError:
        return []


def _toks(t: str) -> set:
    return set(re.findall(r"[a-z0-9]{4,}", (t or "").lower()))


_TS_RE = re.compile(r"^(.{3} \d{2}, \d{4}, \d{2}:\d{2}:\d{2}): (.*)$", re.S)


def build_ts_map(user: dict) -> dict:
    """메모리 원문 -> 검색 context에 실제로 실렸던 '시각: 원문' 문자열.

    ⚠ 검색 context 항목은 `Sep 04, 2025, 18:42:18: <원문>` 처럼 **시각 접두사**를 달고 있는데
    dataset의 evidence는 접두사 없는 원문이다. 이를 그대로 끼워넣으면 오라클 컨텍스트에서
    **정답 근거만 시각 정보를 잃고 낡은 방해 항목들은 시각을 유지**하게 되어, 시점이 맞는
    버전을 고르는 능력을 정답 쪽에서만 빼앗는 꼴이 된다(§13 '검색 계층의 시간 인지'가 역으로 작동).
    그래서 evidence도 동일 포맷으로 복원한다.

    1순위: 이 유저의 검색 context 어딘가에 실린 원문 그대로 (실측 커버리지 95.3%)
    2순위: 골든 MP의 timestamp로 합성 (mem0 저장 시각과 44~150분 차이라 세션 간 순서는 보존)
    """
    out: dict = {}
    for s in user.get("sessions", []):
        for qa in s.get("questions", []) or []:
            for item in _parse_context(qa.get("context", "")):
                m = _TS_RE.match(item)
                if m:
                    out.setdefault(m.group(2), item)
    for s in user.get("sessions", []):
        for mp in s.get("memory_points", []) or []:
            txt, ts = mp.get("memory_content"), mp.get("timestamp")
            if txt and ts and txt not in out:
                out[txt] = f"{ts}: {txt}"
    return out


def oracle_context(qa: dict, user_name: str, ts_map: dict | None = None) -> str:
    """evidence(정답 근거 골든)로 검색 context와 동일한 포맷을 구성.

    ANSWER_ORACLE_PAD=N이면 실제 검색 결과 중 evidence와 겹치지 않는 항목으로 N개까지 채운다.
    -> '정답이 항상 포함되되 주변 잡음은 실제와 동일한' 조건이 된다.
    """
    ts_map = ts_map or {}
    raw = [e["memory_content"] for e in qa.get("evidence", [])]
    mems = [ts_map.get(t, t) for t in raw]          # 시각 접두사 복원 (없으면 원문 그대로)
    if ORACLE_PAD:
        # ⚠ 중복 판정은 **접두사 없는 원문** 기준으로 한다 — 접두사를 포함하면 연도 토큰이
        #    섞여 같은 메모리를 다른 것으로 보고 중복을 통과시킨다.
        ev_toks = [_toks(m) for m in raw]
        for item in _parse_context(qa.get("context", "")):
            if len(mems) >= ORACLE_PAD:
                break
            m = _TS_RE.match(item)
            it = _toks(m.group(2) if m else item)
            # 이미 넣은 evidence와 사실상 같은 내용이면 건너뜀 (중복 방지)
            if any(et and len(et & it) / len(et) >= 0.8 for et in ev_toks):
                continue
            mems.append(item)
    return TEMPLATE_MEM0.format(user_id=user_name, memories=json.dumps(mems, indent=4))


@retry(wait=wait_random_exponential(min=5, max=30), stop=stop_after_attempt(3), reraise=True)
def answer_one(qa: dict) -> dict:
    context = qa["_oracle_context"] if ORACLE else qa["context"]
    prompt = PROMPT_MEMZERO.format(context=context, question=qa["question"])
    start = time.time()
    kwargs = dict(model=MODEL, messages=[{"role": "user", "content": prompt}])
    if REASONING_EFFORT:
        # reasoning 모델: temperature 미지원, max_tokens 대신 max_completion_tokens (추론 토큰 포함 예산)
        kwargs["reasoning_effort"] = REASONING_EFFORT
        kwargs["max_completion_tokens"] = 4096
    else:
        kwargs["temperature"] = 0.0
        kwargs["max_tokens"] = 1024  # 정답은 5-6단어 지시라 넉넉함
    resp = client.chat.completions.create(**kwargs)
    qa["system_response"] = resp.choices[0].message.content
    qa["response_duration_ms"] = (time.time() - start) * 1000
    return qa


def main(results_path: str, max_workers: int, out_path: str | None = None, regen: bool = False, user_num: int | None = None):
    users = [json.loads(l) for l in open(results_path, encoding="utf-8") if l.strip()]

    if user_num:
        # 데이터셋 순서 기준 첫 N명만 (judge.py --user-num과 동일 의미론 — 병합 jsonl은 uuid 정렬이므로)
        order = []
        for i, l in enumerate(open("dataset/HaluMem-Medium.jsonl", encoding="utf-8")):
            if i >= user_num:
                break
            order.append(json.loads(l)["uuid"])
        by_uuid = {u["uuid"]: u for u in users}
        users = [by_uuid[uid] for uid in order if uid in by_uuid]
        print(f"user-num={user_num}: 데이터셋 순서 기준 {len(users)}명 선별")

    # 답변 없는 질문만 수집 (재실행 시 자동 resume). --regen이면 기존 답변 무시하고 전부 재생성
    pending = [
        qa
        for user in users
        for s in user["sessions"]
        for qa in s.get("questions", [])
        if regen or "system_response" not in qa
    ]
    if ORACLE:
        # 유저별 이름을 붙여 오라클 context를 미리 계산 (검색 context는 건드리지 않고 별도 키에 보관)
        for user in users:
            ts_map = build_ts_map(user)   # evidence에 검색 context와 같은 시각 접두사를 붙이기 위한 맵
            for s in user["sessions"]:
                for qa in s.get("questions", []):
                    qa["_oracle_context"] = oracle_context(qa, user.get("user_name", ""), ts_map)
        n_ev = sum(1 for qa in pending if qa.get("evidence"))
        print(f"⚠ 오라클 모드: 검색 context 대신 정답 근거 골든만 제공 (evidence 보유 {n_ev}/{len(pending)})")
    print(f"답변 생성 대상: {len(pending)}개 질문")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(answer_one, qa) for qa in pending]
        for f in tqdm(as_completed(futures), total=len(futures)):
            f.result()  # 예외 있으면 여기서 드러남

    # 원자적 저장. --out 지정 시 원본은 보존하고 새 경로에 저장 (generator 교체 실험용 — 원본 답변 파괴 금지)
    dest = out_path or results_path
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp_path = dest + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        for user in users:
            if ORACLE:  # 임시 키는 산출물에 남기지 않음
                for s in user["sessions"]:
                    for qa in s.get("questions", []):
                        qa.pop("_oracle_context", None)
            f.write(json.dumps(user, ensure_ascii=False) + "\n")
    os.replace(tmp_path, dest)
    print(f"done -> {dest}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default="results/mem0-classic-oss/memzero-oss-smoke/memzero-oss_eval_results.jsonl")
    parser.add_argument("--max-workers", type=int, default=20)
    parser.add_argument("--out", default=None, help="저장 경로 (기본: --results in-place. generator 실험 시 반드시 별도 지정)")
    parser.add_argument("--regen", action="store_true", help="기존 system_response 무시하고 전부 재생성")
    parser.add_argument("--user-num", type=int, default=None, help="데이터셋 순서 기준 첫 N명만")
    args = parser.parse_args()
    main(args.results, args.max_workers, args.out, args.regen, args.user_num)