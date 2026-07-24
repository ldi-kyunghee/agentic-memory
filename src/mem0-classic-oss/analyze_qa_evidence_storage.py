"""실패 QA의 evidence 저장 상태 분해 — judge 라벨 기반 (matcher 무관).

실패한 QA의 evidence 골든 MP들이 integrity/update 라벨상 "저장됨"으로 판정됐는지 조인해
QA 실패를 저장 문제 vs 검색·활용 문제로 가른다. 문단형 런에서 임베딩 matcher의
granularity blindspot(원자 evidence ↔ 장문 문단 cosine 저하)을 우회하는 교차검증용.

사용:
    uv run python src/mem0-classic-oss/analyze_qa_evidence_storage.py \
        --judge results/mem0-classic-oss/memzero-oss-full-custom/judge

판정: 완전 저장 = integrity 2점 또는 update Correct / 부분 = integrity 1점 / 미저장 = 그 외.
매칭불가(~30%)는 evidence가 다른 세션 유래인 경우 — 런 간 비교 시 동일 조건이라 무해.
"""

import os
import json
import argparse
from collections import Counter


def main(judge_dir: str):
    agg = Counter()
    n_fail = 0
    for fn in sorted(os.listdir(judge_dir)):
        if not fn.endswith(".json"):
            continue
        j = json.load(open(os.path.join(judge_dir, fn), encoding="utf-8"))

        # 골든 MP -> 저장 판정 점수
        stored = {}
        for r in j["memory_integrity_records"]:
            stored[(r["session_id"], r["memory_content"])] = r["memory_integrity_score"]
        for r in j["memory_update_records"]:
            t = r.get("memory_update_type")
            stored[(r["session_id"], r["memory_content"])] = 2 if t == "Correct" else (0 if t else None)

        for q in j["question_answering_records"]:
            if q.get("result_type") == "Correct" or not q.get("evidence"):
                continue
            n_fail += 1
            scores = [stored.get((e.get("session_id", q["session_id"]), e["memory_content"]))
                      for e in q["evidence"]]
            scores = [s for s in scores if s is not None]
            if not scores:
                agg["evidence 매칭불가"] += 1
            elif all(s == 2 for s in scores):
                agg["전부 완전저장(2) -> 검색·활용 실패"] += 1
            elif all(s >= 1 for s in scores):
                agg["전부 저장, 일부 부분(1)"] += 1
            else:
                agg["일부 미저장(0) -> 추출 실패"] += 1

    print(f"실패 QA {n_fail:,}건 ({judge_dir})")
    for k, v in agg.most_common():
        print(f"  {k:32s} {v:5,} ({v/n_fail*100:.1f}%)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--judge", required=True, help="judge 라벨 디렉토리 (유저별 {uuid}.json)")
    args = p.parse_args()
    main(args.judge)
