"""Accuracy 점수의 유래별(ADD vs UPDATE) 분해.

"후보 메모리"에는 신규 추출(ADD)과 갱신 재작성본(UPDATE)이 섞여 있음 (러너가 DELETE만 제외).
judge accuracy 레코드를 러너가 보존한 memory_events와 (session_id, 메모리 텍스트)로 조인해,
낮은 Acc의 주범이 추출인지 재작성인지 가른다.

사용:
    uv run python src/mem0-classic-oss/analyze_acc_by_origin.py \
        --artifacts results/mem0-classic-oss/memzero-oss-full-traced/tmp \
        --judge results/mem0-classic-oss/judge-gpt5nano-4u/judge

주의: UPDATE 유래 0점에는 "진짜 drift"와 "구조적 채점 불리"(재작성본은 과거 세션 누적
내용을 담는데 accuracy 채점은 당해 세션 대화·골든과만 대조)가 섞여 있을 수 있음 — 판별은 정성분석 몫.
"""

import os
import json
import argparse
from collections import Counter


def main(artifacts_dir: str, judge_dir: str):
    by_op = {"ADD": Counter(), "UPDATE": Counter()}
    unmatched = 0

    # judge 디렉토리에 있는 유저만 대상 (재채점 범위와 자동 일치)
    uids = [f[:-5] for f in sorted(os.listdir(judge_dir)) if f.endswith(".json")]
    for u in uids:
        art = json.load(open(os.path.join(artifacts_dir, f"{u}.json"), encoding="utf-8"))
        jd = json.load(open(os.path.join(judge_dir, f"{u}.json"), encoding="utf-8"))

        # (세션 인덱스, 메모리 텍스트) -> 이벤트 op. judge의 session_id와 동일하게 전체 sessions enumerate 기준
        ev = {}
        for si, s in enumerate(art["sessions"]):
            for r in s.get("memory_events", []):
                if r["event"] in ("ADD", "UPDATE"):
                    ev[(si, r["memory"])] = r["event"]

        for rec in jd["memory_accuracy_records"]:
            op = ev.get((rec["session_id"], rec["memory_content"]))
            sc = rec["memory_accuracy_score"]
            if op is None or sc is None:
                unmatched += 1
                continue
            by_op[op][sc] += 1

    print(f"유저 {len(uids)}명 / 매칭 실패·무효: {unmatched}")
    for op, c in by_op.items():
        n = sum(c.values())
        if not n:
            continue
        acc = (c[2] + 0.5 * c[1]) / n * 100
        print(f"{op:7s} n={n:5,}  0점 {c[0]/n*100:5.1f}%  1점 {c[1]/n*100:5.1f}%  "
              f"2점 {c[2]/n*100:5.1f}%  -> Acc {acc:.1f}%")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--artifacts", default="results/mem0-classic-oss/memzero-oss-full-traced/tmp",
                   help="러너 유저별 산출물(tmp) 디렉토리 — memory_events 포함")
    p.add_argument("--judge", default="results/mem0-classic-oss/judge-gpt5nano-4u/judge",
                   help="accuracy 레코드를 읽을 judge 디렉토리")
    args = p.parse_args()
    main(args.artifacts, args.judge)
