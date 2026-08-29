"""시스템 간 문항 단위 대조 도구. 종합 분석(docs/synthesis/analysis-plan.md)의
1·2단계용임. runs.yaml 의 by_system 경로를 그대로 읽으므로 등록된 산출물만 보임.

  matrix: 문항 단위 시스템 점수표 + 격차 상위 목록 (+ --dump 로 전량 jsonl)
  show:   문항 하나를 시스템별로 나란히 펼침 (질문·기준·답변·판정)

실행 (서버, 읽기 전용):
  uv run --project src/web-dashboard python src/analysis/question_diff.py \
      matrix --bench beam --setting 100k-beamprompt --cutoff 50 --top 20
  uv run --project src/web-dashboard python src/analysis/question_diff.py \
      show --bench memora --setting weekly --key "weekly_content_writer|q12"
"""
import argparse
import glob
import json
import os
import sys

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RUNS = os.path.join(_ROOT, "src", "web-dashboard", "runs.yaml")


def _load_runs():
    with open(_RUNS, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _lanes(y, bench, setting):
    """시스템 -> judge 경로. 기본 시스템(mem0-classic)은 레인의 최상위 경로임."""
    node = {"halumem": lambda: y["halumem"]["scales"][setting],
            "beam": lambda: y["beam"]["buckets"][setting],
            "memora": lambda: y["memora"]["periods"][setting]}[bench]()
    out = {}
    if node.get("judge"):
        out["mem0-classic"] = node["judge"]
    for sysname, paths in (node.get("by_system") or {}).items():
        if paths.get("judge"):
            out[sysname] = paths["judge"]
    order = list((y.get("systems") or {}).keys()) or list(out)
    return {s: out[s] for s in order if s in out}


def _exists(path):
    p = os.path.join(_ROOT, path)
    return p if os.path.exists(p) else None


# ---- 벤치마크별 로더: {문항키: 레코드} ----------------------------------------
# 레코드 공통 필드: score(비교용 수치) · label(사람이 읽는 판정) · question · raw

def _load_beam(judge_dir, cutoff):
    rows = {}
    for f in glob.glob(os.path.join(judge_dir, "*.json")):
        d = json.load(open(f, encoding="utf-8"))
        for r in d.get("records") or []:
            if r.get("cutoff") != cutoff:
                continue
            key = f'{r["conv"]}|{r["ability"]}|{r["idx"]}'
            rows[key] = {"score": float(r["score"]), "label": f'{r["score"]:.2f}',
                         "question": r.get("question"), "raw": r}
    return rows


def _load_memora(judge_dir):
    rows = {}
    for f in glob.glob(os.path.join(judge_dir, "*.json")):
        if os.path.basename(f) == "run.json":
            continue
        d = json.load(open(f, encoding="utf-8"))
        for r in d.get("records") or []:
            key = f'{r["persona"]}|{r["question_id"]}'
            rows[key] = {"score": float(r["fama"]), "label": f'FAMA {r["fama"]:.1f}',
                         "question": r.get("question"),
                         "task": r.get("task"), "raw": r}
    return rows


def _load_halumem(judge_dir):
    """QA 레코드만 문항 단위 조인. 키는 (uuid, session_id, 질문 원문) 임.
    세션을 빼면 같은 질문 원문이 겹치는 278문항이 조용히 사라짐 (2026-08-29 실측:
    3,467 중 3,189 만 남고 평균이 63.05 에서 60.08 로 밀림)."""
    rows = {}
    for f in glob.glob(os.path.join(judge_dir, "*.json")):
        d = json.load(open(f, encoding="utf-8"))
        for r in d.get("question_answering_records") or []:
            key = f'{r["uuid"][:8]}|s{r.get("session_id")}|{r["question"]}'
            ok = 1.0 if r.get("result_type") == "Correct" else 0.0
            rows[key] = {"score": ok, "label": r.get("result_type"),
                         "question": r.get("question"),
                         "qtype": r.get("question_type"), "raw": r}
    return rows


def _load_lane(bench, judge_dir, cutoff):
    if bench == "beam":
        return _load_beam(judge_dir, cutoff)
    if bench == "memora":
        return _load_memora(judge_dir)
    return _load_halumem(judge_dir)


def _collect(args):
    y = _load_runs()
    lanes = _lanes(y, args.bench, args.setting)
    data, missing = {}, []
    for s, jdir in lanes.items():
        p = _exists(jdir)
        if not p:
            missing.append(s)
            continue
        rows = _load_lane(args.bench, p, args.cutoff)
        if rows:
            data[s] = rows
        else:
            missing.append(s)
    if missing:
        print(f"※ 산출물 없음(제외): {missing}", file=sys.stderr)
    if len(data) < 2:
        sys.exit("비교할 시스템이 2개 미만임. 경로/세팅 확인 요망")
    return data


def cmd_matrix(args):
    data = _collect(args)
    systems = list(data)
    keys = set.intersection(*(set(v) for v in data.values()))
    print(f"# {args.bench} · {args.setting}"
          + (f" · cutoff {args.cutoff}" if args.bench == "beam" else ""))
    print(f"시스템: {systems} · 교집합 문항 {len(keys)}"
          f" (시스템별 {' / '.join(str(len(data[s])) for s in systems)})\n")

    rows = []
    for k in keys:
        scores = {s: data[s][k]["score"] for s in systems}
        gap = max(scores.values()) - min(scores.values())
        rows.append((gap, k, scores))
    rows.sort(key=lambda x: -x[0])

    # 요약: 시스템별 평균과 (이진이면) 패턴 분포
    print("평균:", " · ".join(
        f"{s} {sum(data[s][k]['score'] for k in keys) / len(keys):.4f}"
        for s in systems))
    if all(v["score"] in (0.0, 1.0) for v in data[systems[0]].values()):
        from collections import Counter
        pat = Counter(
            " ".join(f"{s.split('-')[-1]}{'✓' if data[s][k]['score'] else '✗'}"
                     for s in systems) for k in keys)
        print("패턴 분포 (전 시스템 일치 포함):")
        for p, n in pat.most_common():
            print(f"  {n:5d}  {p}")

    print(f"\n격차 상위 {args.top} (gap · 문항키 · 시스템별 판정):")
    for gap, k, _ in rows[:args.top]:
        labels = " | ".join(f"{s}: {data[s][k]['label']}" for s in systems)
        q = (data[systems[0]][k].get("question") or "")[:70]
        print(f"  {gap:6.2f}  {k}\n          {labels}\n          Q: {q}")

    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as f:
            for gap, k, scores in rows:
                f.write(json.dumps(
                    {"key": k, "gap": gap, "scores": scores,
                     "question": data[systems[0]][k].get("question"),
                     "meta": {s: {kk: vv for kk, vv in data[s][k].items()
                                  if kk not in ("raw", "question")}
                              for s in systems}},
                    ensure_ascii=False) + "\n")
        print(f"\n전량 -> {args.dump}")


def cmd_show(args):
    data = _collect(args)
    hits = {s: [k for k in v if args.key in k] for s, v in data.items()}
    keys = sorted(set(sum(hits.values(), [])))
    if not keys:
        sys.exit(f"'{args.key}' 에 걸리는 문항 없음")
    if len(keys) > 1 and not args.all:
        print(f"{len(keys)}개 매칭. 첫 건만 표시 (--all 로 전부):")
        for k in keys[:10]:
            print("  ", k)
        keys = keys[:1]
    for k in keys:
        print(f"\n{'━' * 70}\n문항: {k}")
        first = next(s for s in data if k in data[s])
        r0 = data[first][k]["raw"]
        print(f"Q: {r0.get('question')}")
        ref = r0.get("rubric") or r0.get("criteria") or r0.get("answer")
        print(f"기준/정답: {json.dumps(ref, ensure_ascii=False)[:600]}")
        for s, rows in data.items():
            if k not in rows:
                print(f"\n── {s}: (해당 산출물 없음)")
                continue
            r = rows[k]["raw"]
            print(f"\n── {s}  [{rows[k]['label']}]")
            resp = r.get("system_response") or ""
            print(f"  답변: {resp[:800]}")
            if args.bench == "beam" and r.get("nugget_scores") is not None:
                print(f"  nugget: {r['nugget_scores']}")
            if args.bench == "memora" and r.get("criteria"):
                print(f"  criteria 판정: {json.dumps(r['criteria'], ensure_ascii=False)[:600]}")
            if args.bench == "halumem" and args.context:
                print(f"  컨텍스트({len(r.get('context') or '')}자):"
                      f" {(r.get('context') or '')[:1500]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("matrix", cmd_matrix), ("show", cmd_show)):
        p = sub.add_parser(name)
        p.add_argument("--bench", required=True,
                       choices=["beam", "memora", "halumem"])
        p.add_argument("--setting", required=True,
                       help="beam: 100k-beamprompt 등 / memora: weekly·monthly / halumem: 20u")
        p.add_argument("--cutoff", type=int, default=50, help="BEAM 만 씀")
        p.set_defaults(fn=fn)
        if name == "matrix":
            p.add_argument("--top", type=int, default=20)
            p.add_argument("--dump", default=None, help="전량 jsonl 출력 경로")
        else:
            p.add_argument("--key", required=True, help="문항키 부분 문자열")
            p.add_argument("--all", action="store_true")
            p.add_argument("--context", action="store_true",
                           help="halumem: 답변에 쓰인 컨텍스트도 표시")
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
