"""세 ingest 가 공유하는 실행 뼈대: 추출 병렬 + 재개 캐시 + fold 재생 + 진행 출력.

⚠ 진행 출력은 `[key] N/M chunks` 형태를 지킴 — run 스크립트 ticker 의 grep 정규식이
  `[0-9]+/[0-9]+ (sessions|chunks)` 라 "pairs" 로 찍으면 조용히 안 보임.

재개: pair 당 2콜(episodic + note)이 HaluMem 20유저에서 6만 콜이라, 완료분을
{tmp_prefix}_episodic.jsonl / {tmp_prefix}_notes.jsonl 에 append-flush 로 남기고
재실행 시 이미 있는 gid 는 건너뜀 (원본의 scrach_pad_new.txt/long_term_chunks.pkl
캐시의 확장판. 원본과 달리 부분 재개가 됨).
"""
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import ScratchpadFolder, extract_episodic, extract_scratch_note


def _load_done(path: str) -> dict:
    out = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    d = json.loads(line)
                    out[int(d["gid"])] = d
    return out


def ingest_pairs(memory, pairs: list[dict], flags, tmp_prefix: str,
                 progress_key: str, tracer=None) -> tuple[list[dict], list[dict]]:
    """전 pair 의 episodic + scratch note 추출. 반환 (episodic_chunks, notes) — gid 순 정렬.

    fold 는 여기서 하지 않음 (note 순서에만 의존하므로 추출 병렬화가 fold 를 깨지 않음).
    """
    ep_path = tmp_prefix + "_episodic.jsonl"
    nt_path = tmp_prefix + "_notes.jsonl"
    os.makedirs(os.path.dirname(tmp_prefix), exist_ok=True)
    done_ep = _load_done(ep_path)
    done_nt = _load_done(nt_path)

    todo = [p for p in pairs if p["gid"] not in done_ep or p["gid"] not in done_nt]
    total = len(pairs)
    done_n = total - len(todo)
    if done_n:
        print(f"[{progress_key}] 재개: {done_n}/{total} chunks 캐시", flush=True)

    ep_f = open(ep_path, "a", encoding="utf-8")
    nt_f = open(nt_path, "a", encoding="utf-8")

    def work(p):
        gid = p["gid"]
        if tracer:
            tracer.set_context(stage="ingest", ref={"gid": gid})
        ep = done_ep.get(gid)
        if ep is None:
            ep = extract_episodic(memory, p["pair"], p["history"], gid, p["meta"],
                                  time_prefix=p.get("time_prefix"))
            ep["gid"] = gid
        nt = done_nt.get(gid)
        if nt is None:
            nt = extract_scratch_note(memory, p["pair"], p["history"], gid)
            nt["gid"] = gid
        return gid, ep, nt

    try:
        with ThreadPoolExecutor(max_workers=flags.extract_workers) as ex:
            futures = [ex.submit(work, p) for p in todo]
            for fut in as_completed(futures):
                gid, ep, nt = fut.result()
                if gid not in done_ep:
                    ep_f.write(json.dumps(ep, ensure_ascii=False) + "\n")
                    ep_f.flush()
                    done_ep[gid] = ep
                if gid not in done_nt:
                    nt_f.write(json.dumps(nt, ensure_ascii=False) + "\n")
                    nt_f.flush()
                    done_nt[gid] = nt
                done_n += 1
                if done_n % 10 == 0 or done_n == total:
                    print(f"[{progress_key}] {done_n}/{total} chunks", flush=True)
    finally:
        ep_f.close()
        nt_f.close()

    # ⚠ fold 재생은 전역 int gid 정렬을 전제함 (문자열이면 "10"<"2" 사전순 버그)
    episodic = [done_ep[g] for g in sorted(done_ep)]
    notes = [done_nt[g] for g in sorted(done_nt)]
    return episodic, notes


def replay_fold(memory, notes: list[dict], flags, tracer=None,
                upto_gid: int | None = None,
                folder: ScratchpadFolder | None = None,
                fed_upto: int = -1) -> tuple[ScratchpadFolder, int]:
    """note 를 gid 순으로 feed. HaluMem 스냅샷은 (folder, fed_upto) 를 이어받아
    세션 경계까지만 feed 하는 식으로 점진 재생함.

    반환 (folder, 마지막으로 feed 한 gid).
    """
    if folder is None:
        folder = ScratchpadFolder(memory, flags)
    if tracer:
        tracer.set_context(stage="fold", ref=None)
    for n in notes:
        gid = int(n["gid"])
        if gid <= fed_upto:
            continue
        if upto_gid is not None and gid > upto_gid:
            break
        folder.feed(n["response"])
        fed_upto = gid
    return folder, fed_upto
