"""단계별 오라클 주입 — mem0 파이프라인의 특정 단계를 '완벽한 정답'으로 대체한다.

목적: Agentic Memory의 단계별 성능 상한을 재기 위함.
  실측 → 추출 오라클 → 추출+갱신 오라클 → 검색 오라클(gen_answers) 순으로 상한이 올라가며,
  구간 차이가 곧 그 단계의 기여분이 된다.

mem0 0.1.118 코드는 건드리지 않는다. LLM 래퍼로 두 호출만 가로챈다:
  - fact extraction : messages에 system 역할이 있음  -> 세션 골든을 facts로 반환
  - update decision : messages가 user 1개            -> 정답 ADD/UPDATE 결정을 반환
"""

import ast
import json
import re


def _toks(t: str) -> set:
    return set(re.findall(r"[a-z0-9]{3,}", (t or "").lower()))


def _sim(a: str, b: str) -> float:
    """토큰 자카드 유사도 — 원본 메모리와 저장된 메모리를 잇는 데 사용."""
    ta, tb = _toks(a), _toks(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def parse_old_memories(prompt: str) -> list:
    """update 결정 프롬프트에 박혀 있는 기존 메모리 목록을 복원.

    mem0의 get_update_memory_messages는 파이썬 repr을 백틱 블록에 그대로 넣는다:
        ```
        [{'id': '0', 'text': '...'}, ...]
        ```
    기존 메모리가 없으면 해당 블록 자체가 없다 (그때는 빈 목록).
    """
    for block in re.findall(r"```(.*?)```", prompt, re.DOTALL):
        block = block.strip()
        if not block.startswith("["):
            continue
        try:
            val = ast.literal_eval(block)
        except (ValueError, SyntaxError):
            continue
        if isinstance(val, list) and all(isinstance(x, dict) and "id" in x for x in val):
            return val
    return []


class OracleLLM:
    """mem0의 LLM 인스턴스를 감싸 특정 단계의 응답을 정답으로 대체한다.

    래핑 순서는 `TracingLLM(OracleLLM(llm))` — trace에 주입된 응답이 그대로 기록돼
    나중에 "무엇을 정답으로 넣었는지" 검증할 수 있다.
    """

    #: 갱신 오라클에서 원본 메모리를 저장소와 매칭할 때의 최소 유사도.
    #: 데이터셋의 original_memories는 표현이 조금씩 달라 완전 일치로는 못 찾는다.
    MATCH_THRESHOLD = 0.45

    def __init__(self, inner, extraction: bool = False, update: bool = False):
        # ⚠ 갱신 오라클은 '주입된 골든이 곧 추출 결과'라는 전제로 정답 ops를 만든다.
        #    추출 오라클 없이 켜면 mem0가 실제로 추출한 fact와 무관한 ops를 내보내
        #    사실상 추출까지 오라클이 된 결과를 '갱신만 오라클'로 착각하게 된다.
        if update and not extraction:
            raise ValueError(
                "MEM0_ORACLE_UPDATE는 MEM0_ORACLE_EXTRACTION 없이 쓸 수 없습니다 "
                "(갱신 오라클은 골든=추출결과 전제로 동작). 갱신 단독 오라클이 필요하면 "
                "실제 추출 fact를 프롬프트에서 파싱해 골든과 매칭하는 로직이 따로 필요합니다."
            )
        self._inner = inner
        self.extraction = extraction
        self.update = update
        self._facts: list[str] = []
        self._update_origins: dict[str, list[str]] = {}
        self.stats = {"sessions": 0, "facts": 0, "add": 0, "update": 0, "none": 0, "update_miss": 0}

    def set_session(self, memory_points: list):
        """이번 세션의 정답 골든을 설정한다 (러너가 memory.add 직전에 호출).

        미끼(interference)는 제외한다 — 완벽한 추출기라면 담지 않아야 하는 항목이므로.
        """
        self._facts = [mp["memory_content"] for mp in memory_points
                       if mp.get("memory_source") != "interference"]
        self._update_origins = {mp["memory_content"]: (mp.get("original_memories") or [])
                                for mp in memory_points if mp.get("is_update") == "True"}
        self.stats["sessions"] += 1
        self.stats["facts"] += len(self._facts)

    def _oracle_update_response(self, prompt: str) -> str:
        old = parse_old_memories(prompt)
        used: set[str] = set()
        ops = []
        for fact in self._facts:
            origins = self._update_origins.get(fact) or []
            target = None
            if origins:
                # 원본 메모리와 가장 비슷한 저장 메모리를 갱신 대상으로 지목
                best, best_score = None, 0.0
                for cand in old:
                    if cand["id"] in used:
                        continue
                    score = max((_sim(cand.get("text", ""), o) for o in origins), default=0.0)
                    if score > best_score:
                        best, best_score = cand, score
                if best is not None and best_score >= self.MATCH_THRESHOLD:
                    target = best
                else:
                    # 원본이 저장소에 없음 (데이터셋의 original_memories 다수가
                    # 대화가 아닌 프로필 필드 유래라 애초에 저장된 적이 없다) -> ADD로 처리
                    self.stats["update_miss"] += 1
            if target is not None:
                used.add(target["id"])
                ops.append({"id": target["id"], "text": fact, "event": "UPDATE",
                            "old_memory": target.get("text", "")})
                self.stats["update"] += 1
                continue
            same = next((c for c in old if _sim(c.get("text", ""), fact) >= 0.95), None)
            if same is not None:
                ops.append({"id": same["id"], "text": fact, "event": "NONE"})
                self.stats["none"] += 1
            else:
                ops.append({"id": str(len(old) + len(ops)), "text": fact, "event": "ADD"})
                self.stats["add"] += 1
        return json.dumps({"memory": ops}, ensure_ascii=False)

    def generate_response(self, messages, **kwargs):
        has_system = any(m.get("role") == "system" for m in messages)
        if self.extraction and has_system:
            return json.dumps({"facts": self._facts}, ensure_ascii=False)
        if self.update and not has_system:
            return self._oracle_update_response(messages[0].get("content", ""))
        return self._inner.generate_response(messages, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)
