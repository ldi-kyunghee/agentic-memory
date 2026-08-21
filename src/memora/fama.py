"""FAMA (Forgetting-Aware Memory Accuracy) 집계.

논문 §4.2 정의를 그대로 옮김.

    FAMA = max(0, MPA - lambda * (1 - FAA))

    MPA    = memory_presence 기준 중 충족한 비율
    FAA    = forgetting_absence 기준 중 충족한 비율
    lambda = N_forget / (N_presence + N_forget)     <- 문항마다 다름

`max`가 있어 문항 FAMA는 [0, 1]에 갇힘. 과제 점수는 그 과제의 문항 FAMA 평균에 100을 곱한 값임
(논문은 "sum ... and normalize to [0,100]"이라 표현했는데 문항 수로 나누는 것과 같음).

⚠ 기준이 한쪽만 있는 문항이 있음. forgetting 기준이 없으면 lambda=0 이라 FAMA=MPA 이고,
   presence 기준이 없으면 MPA 를 정의할 수 없으므로 1.0으로 두고 페널티만 받게 함
   (넣을 것이 애초에 없으니 "다 넣었다"로 보는 것이 맞음).
"""
from __future__ import annotations


def question_fama(n_presence_ok: int, n_presence: int,
                  n_forget_ok: int, n_forget: int) -> dict:
    """문항 하나의 FAMA. ok 는 '기대 답과 일치한 기준 수'임."""
    mpa = (n_presence_ok / n_presence) if n_presence else 1.0
    faa = (n_forget_ok / n_forget) if n_forget else 1.0
    total = n_presence + n_forget
    lam = (n_forget / total) if total else 0.0
    fama = max(0.0, mpa - lam * (1.0 - faa))
    return {"fama": round(fama, 6), "mpa": round(mpa, 6), "faa": round(faa, 6),
            "lambda": round(lam, 6),
            "n_presence": n_presence, "n_presence_ok": n_presence_ok,
            "n_forget": n_forget, "n_forget_ok": n_forget_ok}


def task_score(question_famas: list[float]) -> float | None:
    """과제 점수 = 문항 FAMA 평균 x 100. 문항이 없으면 None."""
    if not question_famas:
        return None
    return round(sum(question_famas) / len(question_famas) * 100, 4)


def aggregate(records: list[dict]) -> dict:
    """채점 레코드 목록 -> 과제별·전체 점수.

    레코드는 최소한 `task` 와 `fama` 를 가져야 함. `mpa` 가 있으면 함께 집계해
    페널티가 몇 점을 깎았는지 보여줌 (논문 Table 5 형식).
    """
    tasks = {}
    for r in records:
        tasks.setdefault(r["task"], []).append(r)
    out = {}
    for t, rows in sorted(tasks.items()):
        out[t] = {
            "n": len(rows),
            "fama": task_score([r["fama"] for r in rows]),
            "mpa": task_score([r["mpa"] for r in rows if r.get("mpa") is not None]),
        }
        f, m = out[t]["fama"], out[t]["mpa"]
        out[t]["penalty"] = round(m - f, 4) if (f is not None and m is not None) else None
    allrows = [r for rows in tasks.values() for r in rows]
    out["(전체)"] = {
        "n": len(allrows),
        "fama": task_score([r["fama"] for r in allrows]),
        "mpa": task_score([r["mpa"] for r in allrows if r.get("mpa") is not None]),
    }
    f, m = out["(전체)"]["fama"], out["(전체)"]["mpa"]
    out["(전체)"]["penalty"] = round(m - f, 4) if (f is not None and m is not None) else None
    return out
