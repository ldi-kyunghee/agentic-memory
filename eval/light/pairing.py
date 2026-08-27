"""벤치마크별 pair 분해.

공통 반환 Pair:
  {"gid": int(전역 증가 — fold 재생 정렬의 전제, 문자열이면 "10"<"2" 사전순 버그),
   "pair": [msg, msg?],           # msg = {"role","content"} (+원본 필드)
   "history": [msg...],           # 원본의 conversation_history 규칙
   "meta": {...},                 # session_time/session_date/batch 등
   "time_prefix": str|None,       # HaluMem 만 발화 시각 (mem0 자리 맞춤)
   "session_idx": int|None}       # HaluMem 스냅샷 경계 표시

원본의 conversation_history 규칙 (light.py:80-104):
  pair 0 이고 turn>0  → 직전 turn 전체
  turn 0 이고 batch>0 → 직전 batch 의 마지막 turn
  둘 다 0            → []
  pair>0             → 같은 turn 안의 앞선 pair 들 누적
HaluMem/Memora 는 세션을 turn 자리에 대응시킴 (세션 첫 pair 의 history = 직전 세션
dialogue 전체 — 27K 절단은 core._clip_history 가 뒤에서 함).
"""


def _turn_pairs(turn: list) -> list[list]:
    return [turn[i:i + 2] for i in range(0, len(turn), 2)]


def pairs_beam(chat: list) -> list[dict]:
    """chat.json (list[{"batch_number","turns","time_anchor"}]) — 원본 규칙 그대로.

    메시지에 전역 증가 int id 가 있음 (실측: 0..N 단조). 있으면 그것을 gid 로 쓰고
    (원본 scratchpad 정렬과 동일), 단조가 깨지면 즉시 죽음 — 조용히 순서가 틀어지는
    것보다 나음.
    """
    out = []
    counter = 0
    prev_gid = -1
    for batch_number, batch in enumerate(chat):
        turns = batch["turns"]
        anchor = None
        for group in turns:
            for m in group:
                if m.get("time_anchor"):
                    anchor = m["time_anchor"]
                    break
            if anchor:
                break
        for turn_number, turn in enumerate(turns):
            pairs = _turn_pairs(turn)
            for pair_number, pair in enumerate(pairs):
                if pair_number == 0:
                    if turn_number > 0:
                        history = turns[turn_number - 1]
                    elif batch_number > 0:
                        history = chat[batch_number - 1]["turns"][-1]
                    else:
                        history = []
                else:
                    history = []
                    for p in pairs[:pair_number]:
                        history = history + list(p)
                gid = pair[0].get("id")
                if gid is None:
                    gid = counter
                gid = int(gid)
                assert gid > prev_gid, f"BEAM 메시지 id 가 단조 증가가 아님: {gid} <= {prev_gid}"
                prev_gid = gid
                counter += 1
                out.append({
                    "gid": gid, "pair": pair, "history": history,
                    "meta": {"batch_number": batch_number + 1,
                             "turn_number": turn_number + 1,
                             "pair_number": pair_number + 1,
                             "session_time": anchor},
                    "time_prefix": None,       # BEAM 은 원본과 동일하게 시각 미주입
                    "session_idx": None,
                })
    return out


def pairs_halumem(sessions: list) -> list[dict]:
    """세션 = turn 대응. dialogue 는 {"role","content","timestamp"}.

    time_prefix = 발화 timestamp — mem0 레인이 context 에 `시각: 원문` 으로 받는 것과
    같은 정보를 LIGHT 메모리 본문에 줌 (사용자 결정 'mem0 자리 맞춤').
    """
    out = []
    gid = 0
    for si, s in enumerate(sessions):
        dialogue = s.get("dialogue") or []
        pairs = _turn_pairs(dialogue)
        for pi, pair in enumerate(pairs):
            if pi == 0:
                history = list(sessions[si - 1].get("dialogue") or []) if si > 0 else []
            else:
                history = []
                for p in pairs[:pi]:
                    history = history + list(p)
            out.append({
                "gid": gid, "pair": pair, "history": history,
                "meta": {"session_idx": si + 1, "pair_number": pi + 1,
                         "session_time": s.get("start_time")},
                "time_prefix": pair[0].get("timestamp") or s.get("start_time"),
                "session_idx": si,
            })
            gid += 1
    return out


ROLE_MAP = {"user_agent": "user", "ai_agent": "assistant"}


def pairs_memora(sessions: list) -> list[dict]:
    """Memora 세션 (conversation[] 의 speaker/message). 날짜는 meta 에만 —
    본문 미주입 (Memora 공식 컨텍스트에 날짜가 없는 것과 정렬. mem0 레인과 같은 조건).
    """
    out = []
    gid = 0
    for si, s in enumerate(sessions):
        conv = [{"role": ROLE_MAP.get(t.get("speaker"), "user"),
                 "content": t.get("message") or ""}
                for t in (s.get("conversation") or [])]
        pairs = _turn_pairs(conv)
        prev_conv = None
        for pi, pair in enumerate(pairs):
            if pi == 0:
                if si > 0:
                    if prev_conv is None:
                        prev_conv = [{"role": ROLE_MAP.get(t.get("speaker"), "user"),
                                      "content": t.get("message") or ""}
                                     for t in (sessions[si - 1].get("conversation") or [])]
                    history = prev_conv
                else:
                    history = []
            else:
                history = []
                for p in pairs[:pi]:
                    history = history + list(p)
            out.append({
                "gid": gid, "pair": pair, "history": history,
                "meta": {"session_idx": si + 1, "pair_number": pi + 1,
                         "session_date": s.get("date") or "unknown_time",
                         "session_id": s.get("session_id")},
                "time_prefix": None,
                "session_idx": si,
            })
            gid += 1
    return out
