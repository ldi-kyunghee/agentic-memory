"""HaluMem QA 판정을 시스템 간 조인해 외부 공유용 JSON 하나로 내보냄.

용도: 협업자가 질문 유형별 성능 표를 직접 만들고 RAG 베이스라인과 비교할 수 있게,
문항 단위 원자료(질문·유형·정답·시스템별 답변·판정)와 집계를 한 파일에 담음.
runs.yaml 의 by_system 판정 경로를 그대로 읽으므로 등록된 시스템은 자동 포함됨
(LIGHT 채점이 끝난 뒤 다시 돌리면 light 가 저절로 붙음).

컨텍스트 문자열은 뺌 (파일이 수십 MB 로 커지고, 비교 표 작성에는 불필요).
필요하면 --with-context 로 포함.

실행 (서버):
  uv run --project src/web-dashboard python src/analysis/export_halumem_qa.py \
      --scale 20u --out results/exports/halumem-qa-20u.json
"""
import argparse
import json
import os
from datetime import datetime, timezone

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESULT_TYPES = ("Correct", "Hallucination", "Omission")


def load_judge_users(judge_dir: str) -> dict:
    """uuid -> QA 레코드 목록."""
    out = {}
    for fn in sorted(os.listdir(judge_dir)):
        if not fn.endswith(".json") or fn == "eval_stat_result.json":
            continue
        with open(os.path.join(judge_dir, fn), encoding="utf-8") as f:
            recs = json.load(f).get("question_answering_records") or []
        if recs:
            out[fn[:-5]] = recs
    return out


def summarize(rows: list, key) -> dict:
    """rows 를 key 로 묶어 C/H/O 비율 집계."""
    g: dict = {}
    for r in rows:
        s = g.setdefault(key(r), {"n": 0, **{t: 0 for t in RESULT_TYPES}})
        s["n"] += 1
        if r["result_type"] in RESULT_TYPES:
            s[r["result_type"]] += 1
    for s in g.values():
        valid = sum(s[t] for t in RESULT_TYPES)
        s["correct_ratio"] = round(s["Correct"] / valid, 4) if valid else None
        s["hallucination_ratio"] = round(s["Hallucination"] / valid, 4) if valid else None
        s["omission_ratio"] = round(s["Omission"] / valid, 4) if valid else None
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", default="20u")
    ap.add_argument("--out", default=None,
                    help="기본: results/exports/halumem-qa-{scale}.json")
    ap.add_argument("--with-context", action="store_true",
                    help="답변에 쓰인 검색 컨텍스트 문자열까지 포함 (파일이 매우 커짐)")
    a = ap.parse_args()

    with open(os.path.join(_ROOT, "src", "web-dashboard", "runs.yaml"), encoding="utf-8") as f:
        y = yaml.safe_load(f)
    cfg = y["halumem"]["scales"][a.scale]
    sysd = y.get("systems") or {}

    per_sys, skipped = {}, []
    for sk in sysd:
        by = (cfg.get("by_system") or {}).get(sk)
        jd = os.path.join(_ROOT, by["judge"]) if by else None
        if jd and os.path.isdir(jd):
            users = load_judge_users(jd)
            if users:
                per_sys[sk] = users
                continue
        skipped.append(sk)
    if not per_sys:
        raise SystemExit("판정 산출물이 있는 시스템이 없음")
    if skipped:
        print(f"※ 판정 미완이라 제외: {skipped} (완료 후 다시 돌리면 자동 포함됨)")

    # 시스템 간 공통 유저로 자름 (표본이 다르면 알고리즘 차이를 못 읽음)
    common = sorted(set.intersection(*(set(v) for v in per_sys.values())))

    # 조인 키 = (uuid, session_id, 질문 원문). 세션 인덱스와 질문은 데이터셋에서 오므로
    # 시스템 간 동일함.
    questions: dict = {}
    for sk, users in per_sys.items():
        for uuid in common:
            for r in users[uuid]:
                k = (uuid, r.get("session_id"), r["question"])
                q = questions.setdefault(k, {
                    "uuid": uuid, "session_id": r.get("session_id"),
                    "question": r["question"],
                    "question_type": r.get("question_type"),
                    "difficulty": r.get("difficulty"),
                    "golden_answer": r.get("answer"),
                    "evidence": r.get("evidence"),
                    "results": {}})
                entry = {"system_response": r.get("system_response"),
                         "result_type": r.get("result_type")}
                if a.with_context:
                    entry["context"] = r.get("context")
                q["results"][sk] = entry

    qlist = sorted(questions.values(),
                   key=lambda q: (q["uuid"], q["session_id"] or 0, q["question"]))
    incomplete = sum(1 for q in qlist if len(q["results"]) < len(per_sys))

    flat = {sk: [{"question_type": q["question_type"], "result_type": q["results"][sk]["result_type"]}
                 for q in qlist if sk in q["results"]]
            for sk in per_sys}
    summary = {sk: {"overall": summarize(rows, lambda r: "all")["all"],
                    "by_question_type": summarize(rows, lambda r: r["question_type"])}
               for sk, rows in flat.items()}

    doc = {
        "benchmark": "HaluMem-Medium",
        "scale": a.scale,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": {
            "note": cfg.get("note"),
            "judge_protocol": "HaluMem 공식 judge 프롬프트·집계 무수정 (result_type: Correct/Hallucination/Omission)",
            "models": {
                "agent": "openai/gpt-oss-120b (reasoning effort: medium)",
                "answer_generator": "openai/gpt-oss-120b (reasoning effort: high)",
                "judge": "openai/gpt-oss-120b (reasoning effort: high)",
                "embedder": "Qwen/Qwen3-Embedding-4B (2560d)"},
            "retrieval_top_k": 20,
            "caveat": ("절대 수치는 judge 모델 선택에 지배됨. 다른 judge 로 채점한 "
                       "베이스라인과 절대값을 직접 비교하면 안 되고, 같은 judge 로 "
                       "채점한 결과끼리만 비교해야 함. QA 재실행 편차 실측 약 ±2.2%p."),
        },
        "systems": {sk: {"label": (sysd.get(sk) or {}).get("label", sk),
                         "version": (sysd.get(sk) or {}).get("version")}
                    for sk in per_sys},
        "systems_pending": skipped,
        "common_users": len(common),
        "n_questions": len(qlist),
        "n_questions_missing_some_system": incomplete,
        "summary": summary,
        "questions": qlist,
    }

    out = a.out or os.path.join("results", "exports", f"halumem-qa-{a.scale}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(f"시스템 {list(per_sys)} · 공통 유저 {len(common)} · 문항 {len(qlist)}"
          f" (일부 시스템 누락 문항 {incomplete})")
    for sk, s in summary.items():
        o = s["overall"]
        print(f"  {sk}: C {o['correct_ratio']} · H {o['hallucination_ratio']}"
              f" · O {o['omission_ratio']} (n={o['n']})")
    print(f"-> {out} ({os.path.getsize(out) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
