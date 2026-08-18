import os
import re
import json
import argparse
from collections import Counter

from tqdm import tqdm
from dotenv import load_dotenv
load_dotenv()


def load_traces(trace_path: str) -> list[dict]:
    return [json.loads(l) for l in open(trace_path, encoding="utf-8")]


def norm_tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def jaccard(a: str, b: str) -> float:
    ta, tb = norm_tokens(a), norm_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


_EMB = {"client": None, "model": None, "cache": {}}  # --matcher embed일 때만 초기화됨


def init_embed_matcher():
    from openai import OpenAI
    _EMB["client"] = OpenAI(
        base_url=os.getenv("MEM0_EMBED_BASE_URL") or os.getenv("OPENAI_BASE_URL"),
        api_key=os.getenv("OPENAI_API_KEY", "dummy"),
    )
    _EMB["model"] = os.getenv("MEM0_EMBED_MODEL", "text-embedding-3-small")


def _embed(texts: list[str]) -> list[list[float]]:
    # 텍스트 단위 캐시: writes 목록이 반복 사용되므로 API 호출은 신규 텍스트만 발생함
    missing = [t for t in texts if t not in _EMB["cache"]]
    for i in range(0, len(missing), 256):
        batch = missing[i:i + 256]
        resp = _EMB["client"].embeddings.create(model=_EMB["model"], input=batch)
        for t, d in zip(batch, resp.data):
            _EMB["cache"][t] = d.embedding
    return [_EMB["cache"][t] for t in texts]


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def best_match(target: str, candidates: list[str], threshold: float) -> tuple[int, float]:
    if not candidates:
        return -1, 0.0
    if _EMB["client"]:
        vecs = _embed([target] + candidates)
        scores = [_cosine(vecs[0], v) for v in vecs[1:]]
    else:
        scores = [jaccard(target, c) for c in candidates]
    best_i = max(range(len(scores)), key=lambda i: scores[i])
    best_s = scores[best_i]
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

    intended_total, lost, intended_by_op = 0, [], Counter()
    for r in traces:
        if r["event"] == "llm_call" and r.get("purpose") == "update_decision":
            applied = by_session_writes.get(r["session"], set())
            for a in parse_decision_actions(r["llm"]["response"]):
                op, text = a.get("event"), a.get("text")
                if op not in ("ADD", "UPDATE", "DELETE") or not text:
                    continue  # NONE은 적용대상이 아니므로 제외
                intended_total += 1
                intended_by_op[op] += 1
                if (op, text) not in applied:
                    lost.append({"session": r["session"], "op": op, "text": text[:80]})

    return {"intended": intended_total, "intended_by_op": dict(intended_by_op), "lost": len(lost),
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

    for mp in tqdm(omissions, desc="Classifying omissions", leave=False):
        olds = mp["original_memories"] or [""]
        hits = probes.get((mp["session_id"], mp["index"]), [])
        hit_texts = [h["text"] for h in hits]

        # 병합형 갱신 대응: 이전 버전 중 하나라도 잡히면 인정, 이후 단계는 최고 매칭 버전을 대표로 씀
        results = [best_match(o, hit_texts, threshold) for o in olds]
        best_oi = max(range(len(olds)), key=lambda i: results[i][1])
        old = olds[best_oi]
        hit_i, hit_s = results[best_oi]
        if hit_i >= 0:
            cause = "decision_miss"
        else:
            # probe 시점(mp의 세션) 이전에 쓰인 것만 "저장돼 있었다"로 인정함
            past = [(s, w) for s, w in writes_all if s <= mp["session_id"]]
            w_i, w_s = best_match(old, [w["text"] for _, w in past], threshold)

            if w_i < 0:
                cause = "extraction_miss"
            else:
                later = [w for s, w in past[w_i + 1:]
                        if w["op"] in ("UPDATE", "DELETE")
                        and w.get("prev_text")
                        and best_match(old, [w["prev_text"]], threshold)[0] >= 0]
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
    writes_all = [(r["session"], w["text"]) for r in traces if r["event"] == "memory_write"
                for w in r["writes"]]

    causes, cases = Counter(), []
    wrong = [q for q in judge_records["question_answering_records"]
             if q.get("result_type") in ("Hallucination", "Omission")]

    for qa in tqdm(wrong, desc="qa-failures", leave=False):
        hits = [h["text"] for h in qa_hits.get(qa["question"], [])]
        evid = [e["memory_content"] for e in qa["evidence"]]
        # evidence 여러 개면 전부 context에 있어야 답 가능 -> 하나라도 없는 지점이 실패 원인
        past_texts = [t for s, t in writes_all if s <= qa["session_id"]]
        missing = [e for e in evid if best_match(e, hits, threshold)[0] < 0]
        if not missing:
            cause = "generation_fault"       # 재료는 다 있었음 -> 답변 생성이 잘못
        elif all(best_match(e, past_texts, threshold)[0] >= 0 for e in missing):
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


def analyze_one(trace_path: str, judge_path: str | None, threshold: float) -> dict:
    traces = load_traces(trace_path)
    report = {"trace_file": trace_path, "lost_updates": analyze_lost_updates(traces)}
    if judge_path and os.path.exists(judge_path):
        judge = json.load(open(judge_path, encoding="utf-8"))
        report["omission_linkage"] = classify_omissions(traces, judge, threshold)
        report["qa_failure_linkage"] = classify_qa_failures(traces, judge, threshold)
    return report


def aggregate(reports: list[dict]) -> dict:
    agg = {"users": len(reports)}
    li = sum(r["lost_updates"]["intended"] for r in reports)
    ll = sum(r["lost_updates"]["lost"] for r in reports)
    agg["lost_updates"] = {"intended": li, "lost": ll, "lost_rate": ll / li if li else 0.0}
    for key, total_key in (("omission_linkage", "omission_total"),
                           ("qa_failure_linkage", "wrong_total")):
        causes, total = Counter(), 0
        for r in reports:
            if key in r:
                causes.update(r[key]["causes"])
                total += r[key][total_key]
        agg[key] = {total_key: total, "causes": dict(causes)}
    return agg


def main(trace: str, judge: str | None, threshold: float, out_path: str):
    # --trace에 디렉토리를 주면 안의 모든 *.jsonl을 처리하고 집계함 (judge도 디렉토리로 대응)
    if os.path.isdir(trace):
        pairs = []
        for f in sorted(os.listdir(trace)):
            if f.endswith(".jsonl"):
                jp = os.path.join(judge, f[:-6] + ".json") if judge else None
                pairs.append((os.path.join(trace, f), jp))
    else:
        pairs = [(trace, judge)]

    reports = []
    for tp, jp in tqdm(pairs, desc="Analyzing user traces"):
        reports.append(analyze_one(tp, jp, threshold))

    result = {"threshold": threshold, "aggregate": aggregate(reports), "per_user": reports}
    print(json.dumps({"threshold": threshold, "aggregate": result["aggregate"]},
                     ensure_ascii=False, indent=2))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"saved -> {out_path}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--trace", required=True, help="traces/<run>/<uuid>.jsonl")
    p.add_argument("--judge", default=None, help="results/.../judge/<uuid>.json (없으면 ⓐ만)")
    p.add_argument("--threshold", type=float, default=0.4, help="근사 매칭 Jaccard 임계값")
    p.add_argument("--out", default="reports/mem0-classic-oss/trace_analysis.json")
    p.add_argument("--matcher", choices=["jaccard", "embed"], default="jaccard")
    args = p.parse_args()
    if args.matcher == "embed":
        init_embed_matcher()
        if args.threshold == 0.4:  # jaccard 기본값 그대로면 코사인용 기본으로 승격
            args.threshold = 0.65
    main(args.trace, args.judge, args.threshold, args.out)