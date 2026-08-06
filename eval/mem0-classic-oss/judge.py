import os
import re
import sys
import json
import copy
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from tqdm import tqdm
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_random_exponential

load_dotenv()

# 원본 HaluMem submodule의 judge prompt 4종 + 집계 함수는 그대로 재사용 -> 채점 프로토콜은 무수정 원칙
sys.path.insert(0, "HaluMem/eval")
from eval_tools import (
    EVALUATION_PROMPT_FOR_MEMORY_INTEGRITY,
    EVALUATION_PROMPT_FOR_MEMORY_ACCURACY,
    EVALUATION_PROMPT_FOR_UPDATE_MEMORY,
    EVALUATION_PROMPT_FOR_QUESTION,
)
from evaluation import aggregate_eval_results

client = OpenAI()  # OPENAI_API_KEY / OPENAI_BASE_URL env에서 자동으로 물어옴
MODEL = os.getenv("JUDGE_MODEL", os.getenv("OPENAI_MODEL"))
REASONING_EFFORT = os.getenv("JUDGE_REASONING_EFFORT")  # gpt-5 계열일 때만 설정 (예: minimal)

@retry(wait=wait_random_exponential(min=5, max=30), stop=stop_after_attempt(3), reraise=True)
def llm_json(prompt: str) -> dict:
    kwargs = dict(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    if REASONING_EFFORT:
        # reasoning 모델: temperature 미지원, max_tokens 대신 max_completion_tokens (추론 토큰 포함 예산)
        kwargs["reasoning_effort"] = REASONING_EFFORT
        kwargs["max_completion_tokens"] = 4096
    else:
        kwargs["temperature"] = 0.0
        kwargs["max_tokens"] = 2048
    resp = client.chat.completions.create(**kwargs)
    content = resp.choices[0].message.content or ""
    m = re.search(r"```json\s*(\{.*?\})\s*```", content, re.DOTALL)
    if m:
        content = m.group(1)  # 모델이 그래도 fence 쳤으면 벗겨냄 (파싱 관대하게)
    return json.loads(content)


def build_inputs(user_data: dict):
    """
    User 1명의 산출물에서 4종 judge 입력 조합함. evaluation.py의 process_user와 동일 로직
    """
    integrity, accuracy, update, qa_list = [], [], [], []

    for sid, session in enumerate(user_data["sessions"]):
        if session.get("is_generated_qa_session", False):
            continue

        golden = session["memory_points"]
        extracted = session["extracted_memories"]
        extracted_str = "\n".join(extracted)

        for mp in golden:
            m = copy.deepcopy(mp)
            m["uuid"] = user_data["uuid"]
            m["session_id"] = sid
            # Update 판정 조건 : is_update = True + 검색 스냅샷 존재하는 경우에만. 만약 아니라면 integrity로 넘어감
            if mp["is_update"] == "True" and mp.get("memories_from_system", []):
                update.append(m)
            else:
                integrity.append((m, extracted_str))

        dialogue_str = []
        for turn in session["dialogue"]:
            dialogue_str.append(f'[{turn["timestamp"]}]{turn["role"]}: {turn["content"]}')
            if turn["role"] == "assistant":
                dialogue_str.append("")
        dialogue_str = "\n".join(dialogue_str)

        golden_str = "\n".join(
            m["memory_content"] for m in golden if m["memory_source"] != "interference"
        )

        for mem_text in extracted:
            accuracy.append((dialogue_str, golden_str, {
                "uuid": user_data["uuid"], "session_id": sid, "memory_content": mem_text
            }))
        
        for q in session.get("questions", []):
            new_q = copy.deepcopy(q)
            new_q["uuid"] = user_data["uuid"]
            new_q["session_id"] = sid
            qa_list.append(new_q)

    return integrity, accuracy, update, qa_list


# 채점할 레코드 종류 제한 (반복 실험용). 메모리 판정 3종은 Stage A 산출물이 같으면 불변이므로,
# generator/judge 변동성만 반복 측정할 때는 QA만 돌리면 된다 -> 호출량이 1/10 수준으로 줄어든다.
ONLY = [k.strip() for k in (os.getenv("JUDGE_ONLY") or "").split(",") if k.strip()]


def _enabled(kind: str) -> bool:
    return not ONLY or kind in ONLY


def judge_user(user_data: dict, max_workers: int) -> dict:
    integrity, accuracy, update, qa_list = build_inputs(user_data)
    if ONLY:
        if not _enabled("integrity"): integrity = []
        if not _enabled("accuracy"): accuracy = []
        if not _enabled("update"): update = []
        if not _enabled("qa"): qa_list = []
    results = {
        "memory_integrity_records": [],
        "memory_accuracy_records": [],
        "memory_update_records": [],
        "question_answering_records": []
    }

    tasks = []  # (kind, record, prompt) 형태, 4종 채점을 단일 스레드풀로 병합해서 진행 (개별 평가이므로 섞어도 무관)

    for m, extracted_str in integrity:
        if not extracted_str.strip():
            m["memory_integrity_score"] = 0  # 추출된 메모리가 없으므로 LLM 호출해 평가할 필요 없이 0점
            results["memory_integrity_records"].append(m)
            continue
        tasks.append(("integrity", m, EVALUATION_PROMPT_FOR_MEMORY_INTEGRITY.format(memories=extracted_str, expected_memory_point=m["memory_content"])))

    for dialogue_str, golden_str, m in accuracy:
        tasks.append(("accuracy", m, EVALUATION_PROMPT_FOR_MEMORY_ACCURACY.format(dialogue=dialogue_str, golden_memories=golden_str, candidate_memory=m["memory_content"])))

    for m in update:
        tasks.append(("update", m, EVALUATION_PROMPT_FOR_UPDATE_MEMORY.format(memories="\n".join(m["memories_from_system"]), updated_memory=m["memory_content"], original_memory="\n".join(m["original_memories"]))))

    for q in qa_list:
        tasks.append(("qa", q, EVALUATION_PROMPT_FOR_QUESTION.format(question=q["question"], reference_answer=q["answer"], key_memory_points="\n".join(e["memory_content"] for e in q["evidence"]), response=q["system_response"])))

    def run_one(kind, record, prompt):
        try:
            r = llm_json(prompt)
        except Exception:
            r = {}
        
        if kind == "integrity":
            try:
                record["memory_integrity_score"] = int(r.get("score"))
            except (TypeError, ValueError):
                record["memory_integrity_score"] = None
        elif kind == "accuracy":
            try:
                record["memory_accuracy_score"] = int(r.get("accuracy_score"))
            except (TypeError, ValueError):
                record["memory_accuracy_score"] = None
            record["is_included_in_golden_memories"] = r.get("is_included_in_golden_memories", "false")
        elif kind == "update":
            record["memory_update_type"] = r.get("evaluation_result")
        elif kind == "qa":
            record["result_type"] = r.get("evaluation_result")
        return kind, record
    
    key_map = {
        "integrity": "memory_integrity_records",
        "accuracy": "memory_accuracy_records",
        "update": "memory_update_records",
        "qa": "question_answering_records",
    }
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(run_one, *t) for t in tasks]
        for f in tqdm(as_completed(futures), total=len(futures), desc=f"judge {user_data['user_name']}"):
            kind, record = f.result()
            results[key_map[kind]].append(record)

    return results


def main(results_path: str, max_workers: int, user_num: int | None = None, data_path: str = "dataset/HaluMem-Medium.jsonl", out_root: str | None = None):
    out_root = out_root or os.path.dirname(results_path)
    out_dir = os.path.join(out_root, "judge")
    os.makedirs(out_dir, exist_ok=True)

    users = [json.loads(l) for l in open(results_path, encoding="utf-8") if l.strip()]

    if user_num:
        # 병합된 jsonl은 uuid 기준 알파벳 정렬이므로, HaluMem 데이터셋 원본의 유저 순서에 맞게 첫 N명을 선별함
        order = []
        for i, l in enumerate(open(data_path, encoding="utf-8")):
            if i >= user_num:
                break
            order.append(json.loads(l)["uuid"])
        by_uuid = {u["uuid"]: u for u in users}
        users = [by_uuid[uid] for uid in order if uid in by_uuid]
        print(f"user-num={user_num}: 데이터셋 순서 기준 {len(users)}명 선별")

    for user in users:
        cache = os.path.join(out_dir, f"{user['uuid']}.json")
        if os.path.exists(cache):
            print(f"skip {user['uuid']} (cached)")
            continue
        user_result = judge_user(user, max_workers)
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(user_result, f, ensure_ascii=False)

    # 시간 통계 (원본 HaluMem Submodule의 evaluation.py에서 :430-447 이식)
    add_min, search_min = 0.0, 0.0
    for user in users:
        for s in user["sessions"]:
            add_min += s.get("add_dialogue_duration_ms", 0)
            for q in s.get("questions", []):
                search_min += q.get("search_duration_ms", 0)
    add_min, search_min = add_min / 1000 / 60, search_min / 1000 / 60  # minutes

    # overall_score 뼈대 (원본 HaluMem Submodule의 evaluation.py에서 :451-485와 동일 구조여야 aggregate_eval_results가 채울 수 있음)
    eval_results = {
        "overall_score": {
            "memory_integrity": {},
            "memory_accuracy": {},
            "memory_extraction_f1": 0,
            "memory_update": {},
            "question_answering": {},
            "memory_type_accuracy": {
                k: {"memory_integrity_acc": 0, "memory_update_acc": 0, "total_num": 0}
                for k in ["Event Memory", "Persona Memory", "Relationship Memory"]
            },
            "time_consuming": {
                "add_dialogue_duration_time": add_min,
                "search_memory_duration_time": search_min,
                "total_duration_time": add_min + search_min,
            },
        },
        "memory_integrity_records": [],
        "memory_accuracy_records": [],
        "memory_update_records": [],
        "question_answering_records": [],
    }

    for fn in os.listdir(out_dir):
        if fn.endswith(".json"):
            u = json.load(open(os.path.join(out_dir, fn), encoding="utf-8"))
            for k in ["memory_integrity_records", "memory_accuracy_records", "memory_update_records", "question_answering_records"]:
                eval_results[k].extend(u.get(k, []))

    try:
        eval_results = aggregate_eval_results(eval_results)  # 집계는 공식 함수 그대로 사용
    except ZeroDivisionError:
        # --only로 일부 레코드 종류만 채점하면 공식 집계 함수가 0으로 나눈다.
        # 반복 실험(QA만)에서는 집계 파일이 필요 없으므로 레코드만 남기고 건너뛴다.
        print("⚠ 일부 레코드 종류만 채점돼 공식 집계를 건너뜁니다 (판정 레코드는 정상 저장됨)")

    out_file = os.path.join(out_root, "eval_stat_result.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(eval_results, f, ensure_ascii=False, indent=4)
    print(f"done -> {out_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True, help="Stage A' 완료된 *_eval_results.jsonl 경로 (평가 대상)")
    parser.add_argument("--max-workers", type=int, default=8, help="Maximum number of worker threads")
    parser.add_argument("--user-num", type=int, default=None, help="데이터셋 순서 기준 첫 N명만 채점 (러너와 동일 의미론)")
    parser.add_argument("--data", default="dataset/HaluMem-Medium.jsonl", help="유저 순서의 기준이 되는 데이터셋 경로")
    parser.add_argument("--out-dir", default=None, help="판정/집계 출력 루트 (기본: --results 옆 — 재채점 시 반드시 별도 지정)")
    parser.add_argument("--only", default=None, help="채점할 레코드 종류 (쉼표 구분: integrity,accuracy,update,qa). 반복 실험은 qa만 쓰면 호출량이 1/10")
    args = parser.parse_args()
    if args.only:
        os.environ["JUDGE_ONLY"] = args.only
        ONLY = [k.strip() for k in args.only.split(",") if k.strip()]
        print(f"채점 대상 제한: {ONLY}")
    main(args.results, args.max_workers, args.user_num, args.data, args.out_dir)