"""Memora Stage A: 페르소나별 세션을 mem0에 투입하고 문항별 검색 결과를 저장함.

BEAM용 ingest_beam.py와 뼈대가 같고 네 가지가 다름.

  1. 투입 단위가 **세션 통째**임. 공식 하네스(evals/agent_eval/mem_0/mem0_integration.py)의
     `extract_conversation_messages` -> `add(messages, ...)` 가 세션의 모든 턴을 한 번에 넣음.
     BEAM의 2메시지 청크와 다르고 HaluMem과 같음.
  2. speaker 이름이 다름: `user_agent` -> user, `ai_agent` -> assistant (공식 매핑 그대로).
  3. 검색 top-k 기본이 **50**임 (공식 `search_memories(limit=50)`).
  4. 문항이 대화가 아니라 `evaluation_questions_<persona>.json` 한 파일에 세 과제로 묶여 있음.

⚠ 공식은 `add(..., timestamp=<unix>)` 로 세션 시각을 넘기는데 그것은 mem0 Cloud API 인자임.
   우리가 쓰는 OSS 0.1.118 `Memory.add()` 에는 그 인자가 없어 BEAM과 같이 metadata 로 넣음.
   추출 LLM은 metadata 를 못 보므로, 날짜가 메모리에 남으려면 대화 본문에 적혀 있어야 함.

⚠ Memora 문항의 `question_date` 는 전부 그 기간의 마지막 날임(실측 확인). 그래서 세션을
   전부 투입한 뒤 질문해도 미래 정보가 새지 않음. 체크포인트가 필요 없는 이유임.
"""
import os
import re
import sys
import json
import argparse
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

from tqdm import tqdm
from dotenv import load_dotenv

load_dotenv()

import logging
logging.getLogger("posthog").setLevel(logging.CRITICAL)  # mem0가 만드는 잡음. 진행 줄을 가림

# 기존 Stage A의 mem0 설정/재시도 래퍼 그대로 재사용
sys.path.insert(0, "eval/mem0-classic-oss")
sys.path.insert(0, "src/mem0-classic-oss")

# MEM0_IMPL=v3 이면 mem0 최신판(2.0.18) 어댑터를 씀. 하네스 로직은 이 파일 그대로 공유해서
# A(classic)와 B(v3)가 갈라지지 않게 함. 계획은 docs/mem0-v3/implementation-plan.md.
#
# ⚠ env 로 가르는 이유: 이 스크립트는 ProcessPoolExecutor(forkserver)를 씀. 자식은 모듈을
#    다시 import 하므로 sys.modules 를 갈아끼우는 식의 우회는 자식에서 조용히 classic 으로
#    되돌아감. env 는 자식이 물려받으므로 안전함.
# ⚠ v3 는 별도 venv 임: uv run --project eval/mem0-v3 ...
_IMPL = os.getenv("MEM0_IMPL", "classic")
if _IMPL == "v3":
    sys.path.insert(0, "eval/mem0-v3")
    from compat import build_memory, add_with_retry, search_with_retry
elif _IMPL == "classic":
    from eval_memzero_oss import build_memory, add_with_retry, search_with_retry
else:
    raise SystemExit(f"MEM0_IMPL 은 classic 또는 v3 임 (받은 값: {_IMPL!r})")

from tracing import TraceLogger, TracingLLM, TracingVectorStore
from bm25_store import build_bm25_store

# 공식 하네스의 role 매핑 그대로
def _mem0_version() -> str:
    try:
        import mem0
        return getattr(mem0, "__version__", "?")
    except Exception:
        return "?"


ROLE_MAP = {"user_agent": "user", "ai_agent": "assistant",
            "user": "user", "assistant": "assistant"}

TASKS = ("remembering", "reasoning", "recommending")


def load_sessions(persona_dir: str) -> list[dict]:
    """conversations/session_NNNN.json 을 session_id 순으로 읽음."""
    cdir = os.path.join(persona_dir, "conversations")
    files = sorted(
        (f for f in os.listdir(cdir) if f.endswith(".json")),
        key=lambda f: int(re.sub(r"\D", "", f) or 0),
    )
    out = []
    for f in files:
        with open(os.path.join(cdir, f), encoding="utf-8") as fh:
            out.append(json.load(fh))
    out.sort(key=lambda d: (d.get("date") or "", d.get("session_id") or 0))
    return out


def to_messages(session: dict) -> list[dict]:
    """세션의 모든 턴을 mem0가 받는 형태로. 공식 extract_conversation_messages 이식본."""
    msgs = []
    for t in session.get("conversation") or []:
        role = ROLE_MAP.get(t.get("speaker", ""), t.get("speaker", ""))
        msg = t.get("message", "")
        if msg and role in ("user", "assistant"):
            msgs.append({"role": role, "content": msg})
    return msgs


def load_questions(persona_dir: str, persona: str) -> tuple[list[dict], dict]:
    """평가 파일 -> flat list. 과제 라벨을 문항에 실어 둠."""
    path = os.path.join(persona_dir, f"evaluation_questions_{persona}.json")
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    out = []
    for task in TASKS:
        for q in (d.get("questions") or {}).get(task) or []:
            evs = (q.get("evaluation") or {}).get("evaluation_questions") or []
            out.append({
                "task": task,
                "question_id": q.get("question_id"),
                "question": q.get("question"),
                "question_date": q.get("question_date"),
                "memory_evidence": q.get("memory_evidence"),
                "forgetting_evidence": q.get("forgetting_evidence"),
                # 채점에 쓰는 것은 이 목록뿐임. 나머지는 판독용
                "criteria": [{"id": e.get("evaluation_question_id"),
                              "text": e.get("evaluation_question"),
                              "expected": e.get("expected_answer"),
                              "type": e.get("evaluation_type")} for e in evs],
            })
    return out, (d.get("date_range") or {})


def process_persona(persona_dir: str, period: str, top_k: int, save_path: str,
                    collection_name: str, use_tqdm: bool = True,
                    trace_dir: str | None = None) -> str:
    persona = os.path.basename(persona_dir.rstrip("/"))
    key = f"{period}_{persona}"
    user_id = f"memora_{key}"

    tmp_file = os.path.join(save_path, "tmp", f"{key}.json")
    if os.path.exists(tmp_file):
        return f"skip {key} (cached)"

    tracer = None
    memory = None
    try:
        memory = build_memory(
            collection_name=f"{collection_name}_{persona}",
            history_db_path=os.path.join(save_path, "tmp", f"history_{key}.db"),
        )
        # retriever 교체는 delete_all보다 선행해야 새 store가 초기화됨
        if os.getenv("MEM0_RETRIEVER") == "bm25":
            memory.vector_store = build_bm25_store(f"{collection_name}_{persona}")
        memory.delete_all(user_id=user_id)

        if trace_dir:
            tracer = TraceLogger(os.path.join(trace_dir, f"{key}.jsonl"),
                                 system="mem0-classic-oss", run=collection_name, user=key)
            memory.llm = TracingLLM(memory.llm, tracer)
            memory.vector_store = TracingVectorStore(memory.vector_store, tracer)

        sessions = load_sessions(persona_dir)
        questions, date_range = load_questions(persona_dir, persona)

        out = {"persona": persona, "period": period, "user_id": user_id,
               "date_range": date_range, "n_sessions": len(sessions), "ingest": []}

        # ------ 투입: 세션 하나 = add() 한 번 ------
        bar = tqdm(total=len(sessions), desc=f"ingest {key}") if use_tqdm else None
        for i, s in enumerate(sessions):
            msgs = to_messages(s)
            if not msgs:
                if bar:
                    bar.update(1)
                continue
            if tracer:
                tracer.set_context(session=i, stage="ingest",
                                   ref={"session_id": s.get("session_id")})
            result, dur = add_with_retry(
                memory, msgs, user_id=user_id,
                metadata={"session_date": s.get("date") or "unknown_time",
                          "session_id": s.get("session_id")},
            )
            out["ingest"].append({
                "idx": i,
                "session_id": s.get("session_id"),
                "date": s.get("date"),
                # 데이터셋이 그 세션에서 의도한 연산. 나중에 mem0의 실제 연산과 대조함
                "session_type": s.get("session_type"),
                "operation": s.get("operation"),
                "operation_details": s.get("operation_details"),
                "n_turns": len(msgs),
                "duration_ms": dur,
                "events": [{"op": r["event"], "text": r["memory"]} for r in result["results"]],
            })
            if bar:
                bar.update(1)
            elif (i + 1) % 25 == 0:
                print(f"[{key}] {i + 1}/{len(sessions)} sessions", flush=True)
        if bar:
            bar.close()

        # ------ 저장소에 남은 최종 메모리 개수 ------
        stored = memory.get_all(user_id=user_id, limit=100000)
        out["stored_memories"] = len(stored.get("results", stored) if isinstance(stored, dict) else stored)
        if out["stored_memories"] >= 100000:   # limit에 딱 걸렸다면 잘렸을 가능성이 있음
            print(f"⚠ [{key}] get_all이 limit(100000)에 도달했음. limit을 올릴 것", flush=True)

        # ------ 문항별 검색 ------
        out["questions"] = []
        for q in questions:
            if tracer:
                tracer.set_context(stage="qa_retrieval", ref={"question": q["question"]})
            found, dur = search_with_retry(memory, q["question"], user_id, top_k)
            q["retrieved"] = [
                {"memory": r["memory"], "score": r.get("score"),
                 "created_at": r.get("created_at"),
                 "session_date": (r.get("metadata") or {}).get("session_date"),
                 "session_id": (r.get("metadata") or {}).get("session_id")}
                for r in found["results"]
            ]
            q["search_duration_ms"] = dur
            out["questions"].append(q)

        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        return f"saved {key} ({len(sessions)} sessions, {len(out['questions'])} questions)"
    except Exception:
        err = os.path.join(save_path, "tmp", f"{key}_error.log")
        with open(err, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        return f"FAILED {key} -> {err}"
    finally:
        # 검색 결과까지 tmp에 저장했으므로 이 페르소나의 컬렉션은 더 필요 없음.
        # ⚠ 안 지우면 컬렉션이 쌓여 Qdrant 가 스레드 생성에 실패함 (BEAM 실측: 2026-08-18).
        if memory is not None and os.getenv("MEMORA_KEEP_COLLECTIONS") != "1":
            try:
                memory.vector_store.delete_col()
            except Exception as e:
                print(f"⚠ [{key}] 컬렉션 정리 실패, 무시하고 진행: {e}", flush=True)
        if tracer:
            tracer.close()


def main(data_dir: str, version: str, top_k: int, personas: str | None,
         max_workers: int, trace: bool):
    period = os.path.basename(data_dir.rstrip("/"))
    save_path = f"results/mem0-classic-oss/memora-{version}/"
    os.makedirs(os.path.join(save_path, "tmp"), exist_ok=True)
    collection_name = f"memora_{version}"

    print(f"구현: mem0 {_IMPL}" + (f" ({_mem0_version()})" if True else ""))
    print(f"기간: {period}, top_k: {top_k}")
    print(f"retriever: {'BM25 (Qdrant sparse + IDF)' if os.getenv('MEM0_RETRIEVER') == 'bm25' else '임베딩 (Qdrant dense)'}")
    print(f"reasoning effort override: {os.getenv('MEM0_REASONING_EFFORT') or '없음 (모델 기본값)'}")

    trace_dir = None
    if trace:
        trace_dir = f"traces/mem0-classic-oss/memora-{version}/"
        os.makedirs(trace_dir, exist_ok=True)

    dirs = sorted(d.path for d in os.scandir(data_dir) if d.is_dir())
    if personas:
        want = {p.strip() for p in personas.split(",") if p.strip()}
        dirs = [d for d in dirs if os.path.basename(d) in want]
    print(f"페르소나 {len(dirs)}개: {[os.path.basename(d) for d in dirs]}")

    if max_workers <= 1:
        for d in dirs:
            print(process_persona(d, period, top_k, save_path, collection_name, True, trace_dir))
    else:
        with ProcessPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(process_persona, d, period, top_k, save_path,
                                 collection_name, False, trace_dir): d for d in dirs}
            for i, future in enumerate(as_completed(futures), 1):
                print(f"[{i}/{len(futures)}] {future.result()}", flush=True)

    output = os.path.join(save_path, "memora_eval_results.jsonl")
    with open(output, "w", encoding="utf-8") as f_out:
        for file in sorted(os.listdir(os.path.join(save_path, "tmp"))):
            if file.endswith(".json"):
                with open(os.path.join(save_path, "tmp", file), encoding="utf-8") as f_in:
                    f_out.write(json.dumps(json.load(f_in), ensure_ascii=False) + "\n")
    print(f"done -> {output}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--data", default="Memora/data/weekly", help="기간 디렉토리 (weekly/monthly/quarterly)")
    p.add_argument("--version", default="dev")
    p.add_argument("--top-k", type=int, default=50, help="공식 하네스 기본값이 50임")
    p.add_argument("--personas", default=None, help="쉼표 구분 (예: academic_researcher). 미지정이면 전체")
    p.add_argument("--max-workers", type=int, default=4)
    p.add_argument("--trace", action="store_true")
    a = p.parse_args()
    main(a.data, a.version, a.top_k, a.personas, a.max_workers, a.trace)
