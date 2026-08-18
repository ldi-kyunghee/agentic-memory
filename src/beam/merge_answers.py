"""이미 만든 답변을 새 투입 산출물에 옮겨 붙임.

투입이 부분 실패해서 나중에 재투입하면 `beam_eval_results.jsonl`이 통째로 다시 쓰임.
그 파일에는 `answers` 필드가 없으므로 그대로 답변 단계에 넣으면 이미 만든 것까지
다시 만듦. 성공했던 대화의 검색 결과는 tmp/*.json 그대로라 답변을 재사용해도 됨.

`answer_beam.py`는 문항에 비어 있지 않은 답변이 있으면 그 cutoff를 건너뛰므로,
여기서 붙여두면 새로 들어온 대화만 생성함.

⚠ 검색 결과가 바뀌었다면 붙이면 안 됨. 재투입으로 같은 대화를 다시 돌린 경우가 그럼.
   그래서 붙이기 전에 검색 결과가 같은지 대조하고, 다르면 그 문항은 건너뜀.

사용:
  uv run python src/beam/merge_answers.py \
    --ingest results/mem0-classic-oss/beam-500k-oss120b/beam_eval_results.jsonl \
    --answers results/mem0-classic-oss/beam-genoss120-500k-mem0prompt/answers.jsonl \
    --out results/mem0-classic-oss/beam-genoss120-500k-mem0prompt/merged.jsonl
"""
import json
import argparse


def sig(q: dict) -> tuple:
    """검색 결과가 같은지 판별할 지문. 개수와 앞뒤 항목 텍스트로 봄"""
    ret = q.get("retrieved") or []
    head = (ret[0] or {}).get("memory") if ret else None
    tail = (ret[-1] or {}).get("memory") if ret else None
    return (len(ret), head, tail)


def main(ingest_path: str, answers_path: str, out_path: str):
    convs = [json.loads(l) for l in open(ingest_path, encoding="utf-8") if l.strip()]
    olds = [json.loads(l) for l in open(answers_path, encoding="utf-8") if l.strip()]

    have = {}
    for c in olds:
        for q in c["questions"]:
            if q.get("answers"):
                have[(c["conv_id"], q["ability"], q["idx"])] = (sig(q), q["answers"])

    moved = skipped = 0
    convs_touched = set()
    for c in convs:
        for q in c["questions"]:
            got = have.get((c["conv_id"], q["ability"], q["idx"]))
            if not got:
                continue
            old_sig, answers = got
            if old_sig != sig(q):   # 재투입으로 검색 결과가 달라진 문항
                skipped += 1
                continue
            q.setdefault("answers", {}).update(answers)
            moved += 1
            convs_touched.add(c["conv_id"])

    with open(out_path, "w", encoding="utf-8") as f:
        for c in convs:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    total_q = sum(len(c["questions"]) for c in convs)
    print(f"투입 {len(convs)}대화 {total_q}문항 · 기존 답변 파일 {len(olds)}대화")
    print(f"답변 이식 {moved}문항 ({len(convs_touched)}대화)")
    if skipped:
        print(f"⚠ 검색 결과가 달라져 건너뛴 문항 {skipped}개. 이 문항은 답변을 새로 만듦")
    print(f"남은 생성 대상 {total_q - moved}문항 × cutoff")
    print(f"done -> {out_path}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--ingest", required=True, help="재투입 후의 beam_eval_results.jsonl")
    p.add_argument("--answers", required=True, help="이미 만들어둔 answers.jsonl")
    p.add_argument("--out", required=True, help="병합 결과. answer_beam의 --results 로 넣음")
    a = p.parse_args()
    main(a.ingest, a.answers, a.out)
