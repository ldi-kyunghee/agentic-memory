"""
BEAM 공식 채점 코드에서 그대로 옮겨온 것들. 여기 있는 함수는 수정하지 않음.

출처: github.com/mohammadtavakoli78/BEAM 커밋 3e12035
      src/evaluation/compute_metrics.py 의 llm_equivalence / align_with_llm /
      event_ordering_score (원본 줄번호 195~305)

⚠ 왜 import하지 않고 복사했는가:
   compute_metrics.py 는 맨 위에서 `from src.llm import *` 를 하는데,
   src/llm.py 가 import 시점에 OpenAI 클라이언트 세 개를 생성함. 키가 없으면
   그 자리에서 죽고, sentence_transformers/rouge_score/nltk 도 함께 끌려옴.
   프롬프트(prompts.py)는 상수뿐이라 그대로 import 가능하므로 그쪽은 복사하지 않음.

⚠ 원본에 있는 알려진 문제 두 가지. 고치지 않고 그대로 두되 판독 시 감안할 것:
   1. evaluate_event_ordering 이 extract_facts 결과를 만들어놓고 바로
      llm_response.split("\\n") 으로 덮어씀. 사실 분할이 죽어 있고 개행 분리로 동작함.
   2. 공식 report_results.py 는 tau_norm 만 최종 점수로 쓰고 f1 과 final_score 를 버림.
      그 결과 답변을 짧게 쓸수록 유리해짐 (실측: 사건 1개만 언급해도 tau_norm 0.908).
   backbone-experiment.md 의 BEAM 절에 근거와 재현 수치를 적어둘 것.
"""
from scipy.stats import kendalltau


def llm_equivalence(first_paragraph: str, second_paragraph: str, llm) -> bool:
    system_msg = {
        "role": "system",
        "content": """
            You are a binary classifier.
            If the TWO snippets describe the SAME event/fact, reply **YES**
            Otherwise reply **NO**. No extra words.
            DO NOT provide any exaplanation.
        """,
    }
    user_msg = {
        "role": "user",
        "content": f"""First snippet: {first_paragraph} \n
                       Second snippet: {second_paragraph}
                    """,
    }
    response = llm(messages=[system_msg, user_msg]).lower()
    return "yes" in response


def align_with_llm(reference: list, system: list, llm) -> tuple[list, list]:
    used = set()
    system_out = []
    for s in system:
        matched_index = None
        for index, r in enumerate(reference):
            if index in used:
                continue
            if llm_equivalence(first_paragraph=r, second_paragraph=s, llm=llm):
                matched_index = index
                break
        if matched_index is not None:
            system_out.append(reference[matched_index])
            used.add(matched_index)
        else:
            system_out.append(s)
    return reference, system_out


def event_ordering_score(reference_list: list, system_list: list, llm) -> dict:
    reference_canon, system_canon = align_with_llm(reference_list, system_list, llm)

    tp = len(set(reference_canon) & set(system_canon))
    fp = len([x for x in system_canon if x not in reference_canon])
    fn = len([x for x in reference_canon if x not in system_canon])

    precision = tp / (tp + fp) if tp + fp else 0
    recall = tp / (tp + fn) if tp + fn else 0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0

    union = list(dict.fromkeys(reference_canon + system_canon))
    tie_rank = len(union) + 1

    def to_rank(seq):
        r = {item: i + 1 for i, item in enumerate(seq)}
        return [r.get(u, tie_rank) for u in union]

    tau_b, _ = kendalltau(to_rank(reference_canon), to_rank(system_canon),
                          variant="b", method="auto")
    tau_b_norm = (tau_b + 1) / 2 if tau_b == tau_b else 0   # NaN 방어

    return dict(precision=precision, recall=recall, f1=f1,
                tau_norm=tau_b_norm, final_score=tau_b_norm * f1)