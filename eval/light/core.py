"""LIGHT 알고리즘 본체. 원본 BEAM/src/answer_probing_questions/light.py (커밋 3e12035) 이식.

이식 원칙 (사용자 확정, 2026-08-27):
- 결과에 영향 주는 원본 특성/버그는 그대로 둠 (working memory 중복, 오래된 것부터 예산,
  마지막 fold 안 함, //3.5 와 //3.7 상수 혼용, `ASSISTAN` 오타 포함 프롬프트 전부)
- 고친 것 두 가지뿐:
  1. fold 의 replace("<tokens_limit>", int) → str().  원본은 fold 가 처음 걸리는 순간
     TypeError 로 죽는 코드라 "충실히 재현할 동작" 자체가 없음 (light.py:137)
  2. noise filter 판정 == "yes" → startswith("yes").  reasoning 백본이 "Yes." 를 내면
     전 조각이 탈락해 scratchpad 가 빈 문자열이 됨 (light.py:516). flags 로 원본 복원 가능
- 결과가 같은 구현 개선: 필터 병렬화, 재청킹 캐시(scratchpad 내용 해시 키), id 매칭 dict 조회

프롬프트는 원본 저장소에서 그대로 import 함 (prompts.py 는 상수뿐이라 안전).
episodic 추출 프롬프트만 light.py 함수 안에 인라인이라 여기 원문 복사함 (오타 포함).
"""
import hashlib
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager


@contextmanager
def cost_stage(name: str):
    """비용 계측의 단계 라벨을 잠시 바꿈.

    LIGHT 는 한 프로세스가 투입과 질의를 연달아 하므로 이걸로 갈라야 필터 비용이
    투입으로 안 찍힘 (sitecustomize 가 기록 시점의 COST_STAGE 를 읽음).
    단계가 순차라 스레드 충돌 없음 (추출 스레드풀과 필터 스레드풀이 시간상 안 겹침).
    """
    prev = os.environ.get("COST_STAGE")
    os.environ["COST_STAGE"] = name
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("COST_STAGE", None)
        else:
            os.environ["COST_STAGE"] = prev

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, "BEAM", "src"))
from prompts import (  # noqa: E402
    scratchpad_generation_prompt,
    scratchpad_summarizer_iterative_prompt,
    filter_chunk_context_prompt,
)


# ─────────────────────────────────────────────────────────────────────────────
# pair 텍스트 (원본 f-string 을 들여쓰기·오타까지 그대로 — 임의 통일 금지.
# episodic 은 `ASSISTAN`, working 은 `ASSISTANT` 로 원본에서 서로 다름)
# ─────────────────────────────────────────────────────────────────────────────

def _assistant_content(pair):
    """원본 절단 규칙: 24,000토큰(len//3.7) 넘으면 70,000자에서 자름 (light.py:20-26)."""
    if len(pair) == 2:
        c = pair[1]["content"]
        return c if (len(c) // 3.7) <= 24000 else c[:70000]
    return "N/A"


def pair_text_episodic(pair, time_prefix: str | None = None) -> str:
    """episodic 원문(검색이 돌려주는 것). light.py:164-180 원문 그대로.

    time_prefix: HaluMem 만 발화 시각을 넣음 (mem0 레인이 context 에 받는 것과 같은 정보
    — 사용자 결정 'mem0 자리 맞춤'). BEAM·Memora 는 None (원본·공식과 동일).
    """
    a = _assistant_content(pair)
    u = pair[0]["content"]
    if time_prefix:
        u = f"[{time_prefix}] {u}"
    if len(pair) == 2:
        return f"""
                            USER: {u} \n\n
                            ASSISTAN: {a}
                            """
    return f"""
                        USER: {u} \n\n
                        ASSISTAN: N/A
                        """


def pair_text_working(pair, time_prefix: str | None = None) -> str:
    """working memory 텍스트. light.py:386-389 원문 그대로 (여기는 ASSISTANT, 절단 없음)."""
    u = pair[0]["content"]
    if time_prefix:
        u = f"[{time_prefix}] {u}"
    a = pair[1]["content"] if len(pair) == 2 else "N/A"
    return f"""
                        USER: {u} \n\n
                        ASSISTANT: {a}
                        """


def history_text(conversation_history) -> str:
    """light.py:28-30 원문."""
    out = ""
    for message in conversation_history:
        out += f"{message['role'].upper()}: {message['content']} \n"
    return out


def _clip_history(history: str, other_len: int) -> str:
    """light.py:32-37 예산 규칙 원문. 뒤쪽만 남김."""
    remain_tokens = 27000 - (other_len // 3.7)
    if (len(history) // 3.7) > remain_tokens:
        remain_tokens -= 2000
        remain_tokens = int(remain_tokens * 3.7)
        history = history[-remain_tokens:]
    return history


# ─────────────────────────────────────────────────────────────────────────────
# 추출 (pair 당 2콜: episodic KV + scratchpad note)
# ─────────────────────────────────────────────────────────────────────────────

# light.py:189-206 인라인 프롬프트 원문 (오타 `it to`/`brieft`/`ouput` 포함)
_EPISODIC_PROMPT = """ I provide you with a text. Your task it to identify all the details stated in the text,
                        and output that in key: value format.
                        E.g.:
                        Key 1: Value 1,
                        Key 2: Value 2,
                        Key 3: Value 3,
                        ....

                        Also at the end, I want to provide a brieft summary of what this text was about in this format: Summary: 'summarized text'

                        Note: only ouput key-values and the summary. DO NOT provide any explanation before or after that.
                        Note: Do not ouput Key 1, Key 2, ...

                        **Previous Context:**
                        {history}

                        text: {text}
                        """


def extract_episodic(memory, pair, conversation_history, pair_id, meta,
                     time_prefix=None) -> dict:
    """pair 당 1콜. 반환 chunk 는 원본 구조 (light.py:210-222): KV 로 검색, 원문을 반환."""
    text = pair_text_episodic(pair, time_prefix)
    history = _clip_history(history_text(conversation_history), len(text))
    prompt = _EPISODIC_PROMPT.format(history=history, text=text)
    kv = memory.llm.generate_response([{"role": "user", "content": prompt}])
    return {
        "text": kv,
        "original_text": {"text": text, "id": str(pair_id)},
        "metadata": {"id": str(pair_id), **meta},
    }


def extract_scratch_note(memory, pair, conversation_history, pair_id) -> dict:
    """pair 당 1콜 (light.py:16-48). fold 재생을 위해 전역 int id 를 함께 반환."""
    latest_user = pair[0]["content"]
    latest_assistant = _assistant_content(pair)
    history = _clip_history(history_text(conversation_history), len(latest_assistant))
    prompt = (scratchpad_generation_prompt
              .replace("<history>", history)
              .replace("<latest_user_message>", latest_user)
              .replace("<latest_assistant_message>", latest_assistant))
    resp = memory.llm.generate_response([{"role": "user", "content": prompt}])
    return {"id": int(pair_id), "response": resp}


# ─────────────────────────────────────────────────────────────────────────────
# scratchpad fold
# ─────────────────────────────────────────────────────────────────────────────

class ScratchpadFolder:
    """note 를 순서대로 누적하다 임계(fold_limit×2 토큰, len//3.7)를 넘으면 압축.

    원본 light.py:114-143. content 프로퍼티가 HaluMem 중간 스냅샷의 읽기 지점임.
    ⚠ fold 순서는 전역 int id 정렬을 전제함 — 문자열 id 면 "10"<"2" 사전순 버그.
    """

    def __init__(self, memory, flags):
        self._m = memory
        self._f = flags
        self.content = ""
        self.n_folds = 0

    def feed(self, note_text: str):
        self.content += note_text + "\n\n"
        if (len(self.content) // 3.7) > self._f.fold_limit * 2:
            self._fold()

    def _fold(self):
        # ⚠ 원본 크래시 버그 수정 지점: str() 없이는 TypeError (light.py:137)
        prompt = (scratchpad_summarizer_iterative_prompt
                  .replace("<content>", self.content)
                  .replace("<tokens_limit>", str(self._f.fold_limit)))
        self.content = self._m.llm.generate_response(
            [{"role": "user", "content": prompt}])
        self.n_folds += 1

    def finalize(self):
        """원본은 마지막 잔여분을 fold 안 함 (14K~28K 사이로 끝남). flags.final_fold 로만 변경."""
        if self._f.final_fold and (len(self.content) // 3.7) > self._f.fold_limit:
            self._fold()
        return self.content


# ─────────────────────────────────────────────────────────────────────────────
# episodic 인덱스 · working memory
# ─────────────────────────────────────────────────────────────────────────────

class EpisodicIndex:
    """KV 텍스트로 색인하고 원문 pair 를 돌려줌 (원본의 FAISS+original_text 분리 구조)."""

    def __init__(self, memory):
        self._m = memory
        self.originals: dict[str, str] = {}   # id -> 원문 pair (원본의 strip 비교 루프를 dict 로)

    def add_pairs(self, chunks: list[dict]):
        if not chunks:
            return
        vecs = self._m.embedding_model.embed_batch([c["text"] for c in chunks], "add")
        ids, payloads = [], []
        for c in chunks:
            cid = c["metadata"]["id"]
            ids.append(cid)
            payloads.append({"data": c["text"], "pair_id": cid,
                             **{k: v for k, v in c["metadata"].items() if k != "id"}})
            self.originals[cid.strip()] = c["original_text"]["text"]
        self._m.vector_store.add(ids, vecs, payloads)

    def search(self, query: str, k: int):
        vec = self._m.embedding_model.embed(query, "search")
        # ⚠ 키워드 인자로 불러야 TracingVectorStore 가 query/limit 을 trace 에 남김
        return self._m.vector_store.search(query=query, vectors=vec, limit=k)

    def original_of(self, rec) -> str | None:
        return self.originals.get(str(rec.id).strip())

    @property
    def size(self):
        return self._m.vector_store.size


class WorkingMemory:
    """최근 wm_size 개 (light.py:395 `chunks[-100:]`).

    원본 버그(중복 삽입, light.py:382-385): 한 turn 의 pair 들이 turn 의 메시지 수만큼
    반복 append 됨. BEAM 은 turn 이 2~6 메시지라 ×2~×6. flags.wm_dup=True(기본)면
    **균일 ×2** 로 재현함 — BEAM 실측 창 내 유니크 ≈32~50개와 같은 규모이고,
    HaluMem/Memora 는 세션이 turn 대응이라 원본 규칙 그대로면 ×수십이 되어 원본이
    정의한 적 없는 동작이 되기 때문 (설계 결정, light-review §9 에 기록).
    """

    def __init__(self, flags):
        self._f = flags
        self._chunks: list[str] = []

    def append(self, text: str):
        n = 2 if self._f.wm_dup else 1
        for _ in range(n):
            self._chunks.append(text)

    def snapshot(self) -> list[str]:
        return list(self._chunks[-self._f.wm_size:])


# ─────────────────────────────────────────────────────────────────────────────
# noise filter (질의 시점, LLM 다회 — LIGHT 비용의 대부분)
# ─────────────────────────────────────────────────────────────────────────────

def _chunk_scratchpad(memory, scratchpad: str, cache: dict) -> list[str]:
    """SemanticChunker (percentile 80, buffer 1 — light.py:502-507).

    ⚠ 캐시 키는 반드시 **scratchpad 내용 해시**. "유저당 1회" 로 캐시하면 HaluMem
      중간 스냅샷에서 자란 scratchpad 에 옛 조각을 쓰는 오답이 됨. 같은 세션의 여러
      질문은 해시가 같아 자동으로 청킹 1회.
    langchain import 는 여기 지연 — mem0 venv 가 core.assemble_context 만 import 할 때
    langchain 의존이 안 걸리게 함.
    """
    key = hashlib.sha1(scratchpad.encode("utf-8")).hexdigest()
    if key in cache:
        return cache[key]
    from langchain_experimental.text_splitter import SemanticChunker
    from adapter import LCEmbeddingsAdapter
    chunker = SemanticChunker(
        embeddings=LCEmbeddingsAdapter(memory),
        buffer_size=1,
        breakpoint_threshold_type="percentile",
        breakpoint_threshold_amount=80.0,
    )
    docs = chunker.create_documents([scratchpad])
    chunks = [d.page_content for d in docs]
    cache[key] = chunks
    return chunks


def _is_yes(resp: str, lenient: bool) -> bool:
    r = (resp or "").strip().lower()
    if lenient:
        return r.startswith("yes")
    return r == "yes"   # 원본 (light.py:516)


def noise_filter(memory, scratchpad: str, query: str, chunk_cache: dict, flags) -> dict:
    """조각마다 yes/no 1콜 (light.py:510-519). 원본은 순차 — 결과 동일하므로 병렬화.

    ⚠ as_completed 는 무순서라 인덱스로 재조립해 원본의 조각 순서를 유지함.
    빈 응답/파싱 불가는 no 로 처리하고 개수를 남김 (조용한 전멸 방지).
    """
    if not scratchpad.strip():
        return {"kept": [], "n_chunks": 0, "n_kept": 0, "n_bad": 0}
    chunks = _chunk_scratchpad(memory, scratchpad, chunk_cache)

    def judge(i_text):
        i, doc_text = i_text
        prompt = (filter_chunk_context_prompt
                  .replace("<query>", query)
                  .replace("<doc_text>", doc_text))
        try:
            resp = memory.llm.generate_response([{"role": "user", "content": prompt}])
        except Exception:
            return i, None
        return i, resp

    results: dict[int, str | None] = {}
    with ThreadPoolExecutor(max_workers=flags.filter_workers) as ex:
        for i, resp in ex.map(judge, enumerate(chunks)):
            results[i] = resp

    kept, n_bad = [], 0
    for i, doc_text in enumerate(chunks):
        resp = results.get(i)
        if resp is None or not resp.strip():
            n_bad += 1
            continue
        if _is_yes(resp, flags.filter_lenient_yes):
            kept.append(doc_text)
    return {"kept": kept, "n_chunks": len(chunks), "n_kept": len(kept), "n_bad": n_bad}


# ─────────────────────────────────────────────────────────────────────────────
# 컨텍스트 조립 (질의당 1회. answer 스크립트도 이 함수를 import 함 — 순수함수 유지)
# ─────────────────────────────────────────────────────────────────────────────

def assemble_context(episodic_texts: list[str], working_texts: list[str],
                     filtered_scratchpad_chunks: list[str],
                     reader_max_tokens: int = 14000,
                     wm_recent_first: bool = False,
                     scratchpad_budget: bool = False) -> tuple[str, dict]:
    """원본 조립 규칙 (light.py:531-540):
    ① episodic (리트리버 랭크 순) → 예산(len//3.5 < reader_max_tokens) 안에서.
      넘는 항목은 break 없이 건너뛰고 계속 봄 (뒤의 짧은 항목은 들어감)
    ② working memory → 남은 예산. ⚠ 원본은 **오래된 것부터** 채워 최신이 잘림
    ③ SCRATCH PAD: 필터 통과분 통째 — ⚠ 원본은 예산 검사 없음

    반환: (context, {"in_budget_episodic", "in_budget_working"}) —
    14K 예산이 cutoff 차이를 흡수할 수 있어 판독에 실제 포함 수가 필요함.
    """
    context = ""
    n_ep = 0
    for text in episodic_texts:
        if (len(context + text)) // 3.5 < reader_max_tokens:
            context += text + "\n\n"
            n_ep += 1
    wm = list(reversed(working_texts)) if wm_recent_first else working_texts
    n_wm = 0
    for text in wm:
        if (len(context + text)) // 3.5 < reader_max_tokens:
            context += text + "\n\n"
            n_wm += 1
    scratch = "\n\n".join(filtered_scratchpad_chunks)
    if scratchpad_budget:
        while scratch and (len(context + scratch)) // 3.5 >= reader_max_tokens:
            filtered_scratchpad_chunks = filtered_scratchpad_chunks[:-1]
            scratch = "\n\n".join(filtered_scratchpad_chunks)
    context += f"\n\n\n SCRATCH PAD: {scratch}"
    return context, {"in_budget_episodic": n_ep, "in_budget_working": n_wm}
