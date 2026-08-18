"""재채점 산출물의 new_label을 저장된 원문(raw)에서 올바른 키로 다시 뽑는다.

⚠ 초기 실행에서 LABEL_KEY를 레코드 필드명(memory_*_score)으로 잘못 잡아 integrity/accuracy가
   전부 파싱 실패했다. 실제 키는 judge.py:150~162 기준 score / accuracy_score / evaluation_result다.
   원문을 저장해뒀으므로 재호출 없이 복구한다 (accuracy는 키가 앞에 와 100% 복구, integrity는
   reasoning이 길어 400자 절단에 걸린 일부가 복구 불가 — 그 항목만 재실행하면 된다).
"""
import os
import re
import json

D = "results/mem0-classic-oss/rejudge-update"
KEY = {"integrity": "score", "accuracy": "accuracy_score",
       "update": "evaluation_result", "qa": "evaluation_result"}

for fn in sorted(f for f in os.listdir(D) if f.endswith(".json")):
    path = os.path.join(D, fn)
    d = json.load(open(path, encoding="utf-8"))
    fixed = still = 0
    for it in d["items"]:
        if it.get("new_label") is not None:
            continue
        k = KEY.get(it.get("rec_type", "update"))
        raw = it.get("raw") or ""
        val = None
        try:                                   # 온전한 JSON이면 그대로
            val = json.loads(raw).get(k)
        except json.JSONDecodeError:           # 잘렸으면 키만 정규식으로
            m = re.search(rf'"{k}"\s*:\s*"?([^",}}\n]+)"?', raw)
            val = m.group(1).strip() if m else None
        if val is None:
            still += 1
        else:
            it["new_label"] = str(val)
            fixed += 1
    json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{fn:44s} 복구 {fixed:>3d}건 · 여전히 실패 {still:>3d}건")
