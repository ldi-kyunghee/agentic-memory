"""원본 충실 플래그.

원본(light.py 커밋 3e12035)에는 결과에 영향을 주는 특성/버그가 여럿 있고, 논문 수치는
그것들이 있는 채로 나온 것임. 사용자 결정(2026-08-27): 기본은 **논문 그대로** 재현하고,
크래시 버그(fold 의 replace(int) TypeError)와 백본 비호환(== "yes" 완전일치)만 고침.

LIGHT_FAITHFUL=1 (기본) 이 마스터이고 개별 env 가 우선함.
플래그 상태는 ingest 시작 시 한 줄로 echo 하고 run.json(manifest)에도 기록됨.
"""
import os
from dataclasses import dataclass, asdict, fields


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v not in ("0", "false", "False", "")


def _i(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v else default


@dataclass(frozen=True)
class Flags:
    # ---- 원본 충실 (기본 = 원본 동작) ----
    # working memory 에 같은 pair 가 turn 의 메시지 수만큼 중복 삽입됨 (light.py:382-385).
    # BEAM 은 원본 루프 그대로. HaluMem/Memora 는 세션을 turn 으로 보면 ×수십이 되어
    # 원본이 정의한 적 없는 동작이므로 균일 ×2 로 맞춤 (실효 창 ≈50 유니크 pair 동일).
    wm_dup: bool = True
    # 조립 예산이 모자라면 working memory 를 오래된 것부터 채워 최신이 잘림 (light.py:535-537)
    wm_recent_first: bool = False
    # scratchpad 는 14K 예산 검사 없이 통째로 붙음 (light.py:540)
    scratchpad_budget: bool = False
    # 마지막 잔여분(<28K)은 fold 안 됨 → scratchpad 가 14K~28K 사이 (light.py:135-143)
    final_fold: bool = False
    # ---- 백본 비호환 수정 (기본 = 수정본) ----
    # 원본 == "yes" 완전일치는 reasoning 백본에서 전 조각 탈락 위험 (light.py:516)
    filter_lenient_yes: bool = True
    # ---- 수치 손잡이 (원본 값이 기본) ----
    fold_limit: int = 14000          # fold 목표 토큰. 임계는 항상 ×2 (light.py:116,135)
    reader_max_tokens: int = 14000   # 조립 예산 (light.py:473)
    wm_size: int = 100               # working memory 창 (light.py:395)
    halumem_cutoff: int = 20         # HaluMem 조립에 넣는 episodic 수 (mem0 레인 top-20 과 정렬)
    # ---- 병렬 (원본은 추출 25 스레드 / 필터 순차) ----
    # 프로세스 병렬(W_ING)과 곱해 서버 동시성 상한(단독 20)을 넘지 않게 기본을 낮게 잡음
    extract_workers: int = 2
    filter_workers: int = 8


def load_flags() -> Flags:
    faithful = _b("LIGHT_FAITHFUL", True)
    return Flags(
        wm_dup=_b("LIGHT_WM_DUP", faithful),
        wm_recent_first=_b("LIGHT_WM_RECENT_FIRST", False),
        scratchpad_budget=_b("LIGHT_SCRATCHPAD_BUDGET", False),
        final_fold=_b("LIGHT_FINAL_FOLD", False),
        filter_lenient_yes=_b("LIGHT_FILTER_LENIENT_YES", True),
        fold_limit=_i("LIGHT_FOLD_LIMIT", 14000),
        reader_max_tokens=_i("LIGHT_READER_MAX_TOKENS", 14000),
        wm_size=_i("LIGHT_WM_SIZE", 100),
        halumem_cutoff=_i("LIGHT_HALUMEM_CUTOFF", 20),
        extract_workers=_i("LIGHT_EXTRACT_WORKERS", 2),
        filter_workers=_i("LIGHT_FILTER_WORKERS", 8),
    )


def echo_flags(f: Flags) -> str:
    parts = [f"{fl.name}={getattr(f, fl.name)}" for fl in fields(f)]
    return "flags: " + " ".join(parts)


def flags_dict(f: Flags) -> dict:
    return asdict(f)
