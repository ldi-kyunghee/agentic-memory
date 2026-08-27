"""C-probe 집계: 축소 실행 산출물에서 C(스크래치패드 조각 수)·kept 비율·콜당 지연을 재고
본실행 비용을 외삽함. 본실행의 게이트 — 이 보고를 보고 나서 run 스크립트를 돎.

  uv run --project eval/light python eval/light/measure_c.py \
      --results results/light/beam-c-probe/tmp/100K_1.json \
      --trace traces/light/beam-c-probe/100K_1.jsonl \
      --full-questions 400 --full-pairs 2866
"""
import argparse
import json
import statistics


def collect_questions(doc: dict) -> list[dict]:
    """BEAM/Memora 는 최상위 questions, HaluMem 은 sessions[].questions."""
    if "questions" in doc:
        return [q for q in doc["questions"] if isinstance(q.get("light"), dict)]
    out = []
    for s in doc.get("sessions") or []:
        for q in (s.get("questions") or []):
            if isinstance(q.get("light"), dict):
                out.append(q)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, help="tmp 의 대화/유저/페르소나 json 하나")
    ap.add_argument("--trace", default=None, help="같은 조각의 trace jsonl")
    ap.add_argument("--full-questions", type=int, default=None)
    ap.add_argument("--full-pairs", type=int, default=None)
    a = ap.parse_args()

    with open(a.results, encoding="utf-8") as f:
        doc = json.load(f)
    qs = collect_questions(doc)
    if not qs:
        raise SystemExit("✗ light 블록이 있는 질문이 없음 (LIGHT 산출물이 맞는지 확인)")

    C = [q["light"]["n_chunks"] for q in qs]
    kept = [q["light"]["n_kept"] for q in qs]
    bad = sum(q["light"].get("n_bad", 0) for q in qs)
    print(f"━━━ C-probe: {a.results} ━━━")
    print(f"  질문 {len(qs)}개")
    print(f"  C (조각 수)   평균 {statistics.mean(C):.1f} · 중앙 {statistics.median(C):.0f}"
          f" · 최소 {min(C)} · 최대 {max(C)}")
    print(f"  kept          평균 {statistics.mean(kept):.1f}"
          f" ({100 * sum(kept) / max(sum(C), 1):.0f}%) · 판정 불가 {bad}건")

    # trace 에서 noise_filter 콜당 지연·토큰 실측
    if a.trace:
        lat, ptok = [], []
        stage = None
        with open(a.trace, encoding="utf-8") as f:
            for line in f:
                d = json.loads(line)
                if d.get("stage"):
                    stage = d["stage"]
                if d.get("event") == "llm_call" and stage == "noise_filter":
                    if d.get("duration_ms"):
                        lat.append(d["duration_ms"])
                    pt = (d.get("llm") or {}).get("prompt_tokens")
                    if pt:
                        ptok.append(pt)
        if lat:
            print(f"  필터 1콜      지연 중앙 {statistics.median(lat) / 1000:.2f}s"
                  f" · 입력 중앙 {statistics.median(ptok) if ptok else 0:,}토큰")

    # 외삽
    if a.full_questions:
        c_mean = statistics.mean(C)
        total_filter = a.full_questions * c_mean
        print()
        print(f"━━━ 본실행 외삽 ━━━")
        print(f"  필터 총콜     {a.full_questions:,}문항 × C̄{c_mean:.1f} ≈ {total_filter:,.0f}")
        if a.full_pairs:
            print(f"  투입 총콜     2 × {a.full_pairs:,} = {2 * a.full_pairs:,} (+fold)")
        if a.trace and lat:
            med_s = statistics.median(lat) / 1000
            for workers in (8, 16):
                hrs = total_filter * med_s / workers / 3600
                print(f"  필터 벽시계   {workers}동시 기준 ≈ {hrs:.1f}h")
            if ptok:
                print(f"  필터 입력토큰 ≈ {total_filter * statistics.median(ptok) / 1e6:.1f}M")
        print()
        print("  ⚠ 리뷰의 가정(C≈30)과 크게 다르면 워커 수·기간 축소를 먼저 정하고 본실행")


if __name__ == "__main__":
    main()
