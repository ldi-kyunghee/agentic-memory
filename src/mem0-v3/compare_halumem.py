"""HaluMem 채점본 여러 개를 **같은 유저 집합으로 맞춰** 나란히 읽음.

왜 필요한가: 팔 A(classic)와 팔 B(v3)의 유저 수가 다를 수 있음. 2026-08-26 기준
백본이 gpt-oss-120b 인 classic HaluMem 런은 4유저(oss120b4)뿐인데 v3 는 20유저임.
유저 수가 다른 두 집계를 나란히 놓으면 알고리즘 차이가 아니라 표본 차이를 읽게 됨.

⚠ 이 스크립트는 레인(백본·generator·judge)이 같은지 확인해주지 않음. 부르는 쪽이
  runs.yaml 의 backbone 필드와 judge 디렉토리 이름으로 맞춰야 함.

집계는 HaluMem 서브모듈의 aggregate_eval_results 를 그대로 씀 (채점 프로토콜 무수정 원칙).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, "HaluMem/eval")
from evaluation import aggregate_eval_results  # noqa: E402

REC = [
    "memory_integrity_records",
    "memory_accuracy_records",
    "memory_update_records",
    "question_answering_records",
]

ROWS = [
    ("메모리 온전성 (weighted recall)", ("memory_integrity", "weighted_recall(valid)")),
    ("추출 F1", ("memory_extraction_f1",)),
    ("Target 정확도", ("memory_accuracy", "target_accuracy(valid)")),
    ("Interference 미포함률", ("memory_accuracy", "interference_accuracy(valid)")),
    ("가중 정확도", ("memory_accuracy", "weighted_accuracy(valid)")),
    ("update Correct", ("memory_update", "correct_update_memory_ratio(valid)")),
    ("update Hallucination", ("memory_update", "hallucination_update_memory_ratio(valid)")),
    ("update Omission", ("memory_update", "omission_update_memory_ratio(valid)")),
    ("QA Correct", ("question_answering", "correct_qa_ratio(valid)")),
    ("QA Hallucination", ("question_answering", "hallucination_qa_ratio(valid)")),
    ("QA Omission", ("question_answering", "omission_qa_ratio(valid)")),
]


def user_files(d):
    """{uuid: 경로}. eval_stat_result.json 은 집계본이라 제외함."""
    return {
        f[:-5]: os.path.join(d, f)
        for f in os.listdir(d)
        if f.endswith(".json") and f != "eval_stat_result.json"
    }


def aggregate(paths):
    ev = {
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
            # 시간 지표는 투입 산출물에서 오는 값이라 여기서는 0. 점수 계산에는 안 쓰임.
            "time_consuming": {
                "add_dialogue_duration_time": 0,
                "search_memory_duration_time": 0,
                "total_duration_time": 0,
            },
        },
        **{k: [] for k in REC},
    }
    for p in paths:
        u = json.load(open(p, encoding="utf-8"))
        for k in REC:
            ev[k].extend(u.get(k, []))
    return aggregate_eval_results(ev)


def dig(d, path):
    for k in path:
        d = d[k]
    return d


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--arm", action="append", required=True,
                   help="이름=채점디렉토리. 여러 번 줄 수 있음")
    p.add_argument("--users", default=None, help="쉼표로 구분한 uuid. 없으면 공통 유저 전체")
    a = p.parse_args()

    arms = []
    for spec in a.arm:
        name, _, d = spec.partition("=")
        if not d:
            raise SystemExit(f"✗ --arm 은 이름=디렉토리 형식임: {spec}")
        if not os.path.isdir(d):
            raise SystemExit(f"✗ 디렉토리 없음: {d}")
        arms.append((name, d, user_files(d)))

    common = set.intersection(*[set(f) for _, _, f in arms])
    if a.users:
        want = {u.strip() for u in a.users.split(",") if u.strip()}
        missing = want - common
        if missing:
            raise SystemExit(f"✗ 모든 팔에 없는 uuid: {sorted(missing)}")
        common = want
    if not common:
        raise SystemExit("✗ 모든 팔에 공통으로 있는 유저가 없음")

    print(f"  공통 유저 {len(common)}명")
    for name, d, fs in arms:
        print(f"    {name:10s} 전체 {len(fs):2d}명 · {d}")
    print()

    stats = {name: aggregate([fs[u] for u in sorted(common)]) for name, d, fs in arms}

    names = [n for n, _, _ in arms]
    head = "  {:32s}".format("지표") + "".join(f"{n:>12s}" for n in names)
    if len(names) == 2:
        head += f"{'차':>10s}"
    print(head)
    print("  " + "-" * (len(head) - 2))
    for label, path in ROWS:
        vals = [dig(stats[n]["overall_score"], path) * 100 for n in names]
        line = "  {:32s}".format(label) + "".join(f"{v:12.2f}" for v in vals)
        if len(vals) == 2:
            line += f"{vals[1] - vals[0]:+10.2f}"
        print(line)

    print()
    print("  판정 건수")
    for n in names:
        s = stats[n]["overall_score"]
        print(f"    {n:10s} 온전성 {s['memory_integrity']['memory_num']:>6,}"
              f" · 정확도 {s['memory_accuracy']['memory_num']:>6,}"
              f" · update {s['memory_update']['update_memory_num']:>6,}"
              f" · QA {s['question_answering']['qa_num']:>6,}")


if __name__ == "__main__":
    main()
