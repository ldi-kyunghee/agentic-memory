import os
import re
import json
import argparse
from collections import Counter


def load_traces(trace_path: str) -> list[dict]:
    return [json.loads(l) for l in open(trace_path, encoding="utf-8")]


def norm_tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def jaccard(a: str, b: str) -> float:
    ta, tb = norm_tokens(a), norm_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def best_match(target: str, candidates: list[str], threshold: float) -> tuple[int, float]:
    """골든 텍스트 <-> 시스템 저장 텍스트의 근사 정렬. 임계 미달이면 (-1, best_score)
    주의: 1차 구현은 토큰 Jaccard임. 패러프레이즈에 약함 -> 추후 임베딩 매처로 교체 지점"""
    best_i, best_s = -1, 0.0
    for i, c in enumerate(candidates):
        s = jaccard(target, c)
        if s > best_s:
            best_i, best_s = i, s
    return (best_i if best_s >= threshold else -1), best_s


# ========================== for: mem0-classic-oss ==========================
def parse_decision_actions(response: str) -> list[dict]:
    # update_decision LLM 응답에서 액션 목록을 관대하게 파싱함 (코드펜스 유무 불문)
    m = re.search(r"\{.*\}", response, re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group(0)).get("memory", [])
    except json.JSONDecodeError:
        return []
# ========================== for: mem0-classic-oss ==========================


# ========================== for: generic, applies to all memory systems ==========================
def analyze_lost_updates(traces: list[dict]) -> dict:
    """
    LLM이 의도한 액션(purpose=update_decision 응답) vs 실제 적용(memory_write) 대조.
    mem0의 UUID 매핑 범위 밖 id 참조(환각)로 조용히 버려진 액션을 정량화함
    """
    
    by_session_writes = {}
    for r in traces:
        if r["event"] == "memory_write":
            by_session_writes[r["session"]] = {(w["op"], w["text"]) for w in r["writes"]}

    intended_total, lost = 0, []
    for r in traces:
        if r["event"] == "llm_call" and r.get("purpose") == "update_decision":
            applied = by_session_writes.get(r["session"], set())
            for a in parse_decision_actions(r["llm"]["response"]):
                op, text = a.get("event"), a.get("text")
                if op not in ("ADD", "UPDATE", "DELETE") or not text:
                    continue  # NONE은 적용대상이 아니므로 제외
                intended_total += 1
                if (op, text) not in applied:
                    lost.append({"session": r["session"], "op": op, "text": text[:80]})

    return {"intended": intended_total, "lost": len(lost),
            "lost_rate": len(lost) / intended_total if intended_total else 0.0,
            "lost_by_op": dict(Counter(l["op"] for l in lost)), "samples": lost[:5]}


def classify_omissions(traces: list[dict], judge_records: dict, threshold: float) -> dict:
    """
    judge가 Omission 판정한 update MP의 원인을 trace로 소급해 4분류함.
    분류 트리: probe hits내에 old가 있음 -> decision_miss
                                없음 -> 전 세션 write에도 없음 -> extraction_miss (논문 §6.2.1 가설)
                                    -> write됐지만 이후 UPDATE/DELETE로 소실 -> overwritten
                                    -> 저장돼 있는데 검색 미달 -> retrieval_miss
    """
    probes = {(r["session"], r["ref"]["mp_index"]): r["retrieval"]["hits"]
              for r in traces if r["stage"] == "update_probe" and r.get("ref")}
    writes_all = [(r["session"], w) for r in traces if r["event"] == "memory_write"
                  for w in r["writes"]]

    causes, cases = Counter(), []
    omissions = [m for m in judge_records["memory_update_records"]
                 if m.get("memory_update_type") == "Omission"]

    for mp in omissions:
        old = mp["original_memories"][0] if mp["original_memories"] else ""
        hits = probes.get((mp["session_id"], mp["index"]), [])

        hit_i, hit_s = best_match(old, [h["text"] for h in hits], threshold)
        if hit_i >= 0:
            cause = "decision_miss"
        else:
            w_i, w_s = best_match(old, [w["text"] for _, w in writes_all], threshold)
            if w_i < 0:
                cause = "extraction_miss"
            else:
                later = [w for s, w in writes_all[w_i + 1:]
                         if w["op"] in ("UPDATE", "DELETE")
                         and w.get("prev_text") and jaccard(old, w["prev_text"]) >= threshold]
                cause = "overwritten" if later else "retrieval_miss"

        causes[cause] += 1
        cases.append({"session": mp["session_id"], "mp_index": mp["index"], "cause": cause,
                      "old": old[:70], "best_probe_score": round(hit_s, 3)})

    return {"omission_total": len(omissions), "causes": dict(causes), "cases": cases}


def classify_qa_failures(traces: list[dict], judge_records: dict, threshold: float) -> dict:
    """
    틀린 QA(Hallucination/Omission)의 실패 지점을 상류로 분해함.
    evidence가 top-20 context에 있었나 -> 있었다면 생성 단계 잘못 (generation_fault)
    저장소엔 있었나 -> retrieval_fault / 추출된 적 없나 -> extraction_fault
    """
    qa_hits = {r["ref"]["question"]: r["retrieval"]["hits"]
               for r in traces if r["stage"] == "qa_retrieval" and r.get("ref")}
    write_texts = [w["text"] for r in traces if r["event"] == "memory_write" for w in r["writes"]]

    causes, cases = Counter(), []
    wrong = [q for q in judge_records["question_answering_records"]
             if q.get("result_type") in ("Hallucination", "Omission")]

    for qa in wrong:
        hits = [h["text"] for h in qa_hits.get(qa["question"], [])]
        evid = [e["memory_content"] for e in qa["evidence"]]
        # evidence 여러 개면 전부 context에 있어야 답 가능 -> 하나라도 없는 지점이 실패 원인
        missing = [e for e in evid if best_match(e, hits, threshold)[0] < 0]
        if not missing:
            cause = "generation_fault"       # 재료는 다 있었음 -> 답변 생성이 잘못
        elif all(best_match(e, write_texts, threshold)[0] >= 0 for e in missing):
            cause = "retrieval_fault"        # 저장은 됐는데 top-20에 못 듦
        else:
            cause = "extraction_fault"       # 애초에 저장된 적 없음 (상류 전파)
        causes[cause] += 1
        cases.append({"question": qa["question"][:60], "type": qa["result_type"],
                      "cause": cause, "missing": len(missing), "evidence": len(evid)})

    return {"wrong_total": len(wrong), "causes": dict(causes),
            "by_type": {t: dict(Counter(c["cause"] for c in cases if c["type"] == t))
                        for t in ("Hallucination", "Omission")}, "cases": cases[:10]}
# ========================== for: generic, applies to all memory systems ==========================



def main(trace_path: str, judge_path: str | None, threshold: float, out_path: str):
    traces = load_traces(trace_path)
    report = {"trace_file": trace_path, "threshold": threshold,
              "lost_updates": analyze_lost_updates(traces)}

    if judge_path and os.path.exists(judge_path):
        judge = json.load(open(judge_path, encoding="utf-8"))
        report["omission_linkage"] = classify_omissions(traces, judge, threshold)
        report["qa_failure_linkage"] = classify_qa_failures(traces, judge, threshold)

    summary = {k: {kk: vv for kk, vv in v.items() if kk not in ("cases", "samples")}
               if isinstance(v, dict) else v for k, v in report.items()}
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"saved -> {out_path}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--trace", required=True, help="traces/<run>/<uuid>.jsonl")
    p.add_argument("--judge", default=None, help="results/.../judge/<uuid>.json (없으면 ⓐ만)")
    p.add_argument("--threshold", type=float, default=0.4, help="근사 매칭 Jaccard 임계값")
    p.add_argument("--out", default="reports/trace_analysis.json")
    args = p.parse_args()
    main(args.trace, args.judge, args.threshold, args.out)