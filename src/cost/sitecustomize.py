"""LLM·임베딩 호출 비용 계측기. **평가 코드를 한 줄도 안 고치고** 붙는다.

파이썬은 시작할 때 sys.path 에서 `sitecustomize` 를 찾아 자동으로 import 한다.
그래서 실행 커맨드 앞에 `PYTHONPATH=src/cost` 만 붙이면 계측이 걸린다.
ProcessPoolExecutor 가 spawn 으로 워커를 띄워도 env 는 상속되므로 워커에도 그대로 걸린다
(sys.modules 패치는 spawn 을 못 넘지만 env 는 넘는다).

켜는 법: `COST_DIR` 을 주면 켜지고, 없으면 아무 일도 안 한다.
  COST_DIR=cost/beam-100k-light COST_STAGE=ingest PYTHONPATH=src/cost uv run python ...

산출물: `$COST_DIR/{stage}__{pid}.json` 을 프로세스 종료 시 하나씩 떨어뜨린다.
합치는 것은 `src/cost/report.py`.

⚠ 왜 mem0 의 trace 를 안 쓰고 이걸 따로 두는가:
  1. trace 는 `--trace` 를 켠 투입 단계에서만 돌고 답변·채점은 아예 안 센다
  2. trace 는 completion_tokens 만 남기고 **prompt_tokens 를 안 남긴다.** 이 파이프라인들은
     입력 토큰이 비용의 대부분이라 그게 빠지면 비용 비교가 성립하지 않는다
  3. 임베딩 호출은 세지 않는다
  여기서는 실제로 선을 타고 나간 것을 센다. 그래서 mem0 버전이든 LIGHT 든 구현과 무관하게 같은 잣대다.
"""
import atexit
import json
import os
import threading
import time

_DIR = os.getenv("COST_DIR")

if _DIR:
    # 프롬프트 길이 분포용 히스토그램: 250 토큰 폭, 0~128k. 마지막 칸은 넘침.
    # ⚠ 폭을 1k 로 두면 짧은 프롬프트가 전부 0번 칸에 몰려 p50 이 500 으로 뭉갠다.
    _BUCKET = 250
    _NBUCKET = 513

    _lock = threading.Lock()
    _acc: dict = {}

    def _slot(kind: str, model: str) -> dict:
        # ⚠ stage 는 기록 시점의 env 를 읽는다. LIGHT 처럼 한 프로세스가 투입과 질의를
        #   연달아 하는 시스템은 실행 중에 COST_STAGE 를 바꿔 단계를 가른다
        #   (mem0 는 프로세스=단계라 정적이어도 맞았지만 LIGHT 에서 필터 비용이
        #   전부 투입으로 찍혔음 — 2026-08-28).
        stage = os.getenv("COST_STAGE", "unknown")
        key = f"{stage}:{kind}:{model}"
        s = _acc.get(key)
        if s is None:
            s = {
                "stage": stage, "kind": kind, "model": model, "calls": 0,
                "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
                "wall_ms": 0.0, "errors": 0,
                "prompt_max": 0, "hist": [0] * _NBUCKET,
            }
            _acc[key] = s
        return s

    def _record(kind, model, usage, wall_ms, err=False):
        pt = ct = rt = 0
        if usage is not None:
            pt = getattr(usage, "prompt_tokens", None) or 0
            ct = getattr(usage, "completion_tokens", None) or 0
            det = getattr(usage, "completion_tokens_details", None)
            rt = (getattr(det, "reasoning_tokens", None) or 0) if det else 0
        with _lock:
            s = _slot(kind, model or "?")
            s["calls"] += 1
            s["prompt_tokens"] += pt
            s["completion_tokens"] += ct
            s["reasoning_tokens"] += rt
            s["wall_ms"] += wall_ms
            if err:
                s["errors"] += 1
            if pt:
                s["prompt_max"] = max(s["prompt_max"], pt)
                s["hist"][min(pt // _BUCKET, _NBUCKET - 1)] += 1
        _maybe_flush()

    _last_flush = [0.0]
    _FLUSH_SEC = float(os.getenv("COST_FLUSH_SEC", "5"))

    def _maybe_flush():
        """주기적으로 흘려쓴다.

        ⚠ 종료 훅에 기대면 안 된다. ProcessPoolExecutor 워커는 os._exit() 로 끝나서
          atexit 도 multiprocessing.util.Finalize 도 안 돈다 (2026-08-26 실측: 워커가
          호출을 다 기록해놓고도 파일을 한 개도 안 남겼음). 그래서 기록 중에 흘려쓴다.
        """
        now = time.time()
        if now - _last_flush[0] < _FLUSH_SEC:
            return
        _last_flush[0] = now
        _dump()

    def _wrap(cls, kind):
        orig = cls.create

        def create(self, *a, **kw):
            t0 = time.time()
            try:
                resp = orig(self, *a, **kw)
            except Exception:
                _record(kind, kw.get("model"), None, (time.time() - t0) * 1000, err=True)
                raise
            _record(kind, kw.get("model"), getattr(resp, "usage", None), (time.time() - t0) * 1000)
            return resp

        # ⚠ 표식을 __wrapped__ 로 두면 안 된다. openai 의 원본 create 는 @required_args 가
        #   functools.wraps 를 쓰기 때문에 **이미 __wrapped__ 를 갖고 있다.** 그걸 보고
        #   "이미 감쌌다" 로 오판하면 설치를 건너뛰고 조용히 0 을 기록한다 (2026-08-26에 겪음).
        create.__cost_meter__ = orig
        cls.create = create

    def _install():
        try:
            from openai.resources.chat.completions import Completions
            from openai.resources.embeddings import Embeddings
        except Exception:
            return  # openai 를 안 쓰는 프로세스(대시보드 등)에서는 조용히 넘어간다
        # 중복 설치 방지: 같은 인터프리터에서 두 번 감싸면 호출이 두 번 세진다
        if not getattr(Completions.create, "__cost_meter__", None):
            _wrap(Completions, "chat")
        if not getattr(Embeddings.create, "__cost_meter__", None):
            _wrap(Embeddings, "embed")

    def _dump():
        with _lock:
            if not _acc:
                return
            rows = list(_acc.values())
        try:
            os.makedirs(_DIR, exist_ok=True)
            # stage 별로 파일을 나눠 쓴다 (로더가 파일 단위로 stage 를 읽음)
            by_stage: dict = {}
            for r in rows:
                by_stage.setdefault(r.get("stage", "unknown"), []).append(r)
            for stage, srows in by_stage.items():
                path = os.path.join(_DIR, f"{stage}__{os.getpid()}.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({
                        "stage": stage,
                        "system": os.getenv("COST_SYSTEM", ""),
                        "benchmark": os.getenv("COST_BENCH", ""),
                        "setting": os.getenv("COST_SETTING", ""),
                        "pid": os.getpid(),
                        "rows": srows,
                    }, f, ensure_ascii=False)
        except Exception:
            pass  # 계측 실패가 본 파이프라인을 막지 않는다

    _install()
    # ⚠ atexit 만으로는 워커를 못 잡는다. multiprocessing 자식은 os._exit() 로 끝나서
    #   atexit 핸들러를 돌지 않는다 (2026-08-26 실측: 워커 2개 호출이 통째로 유실됨).
    #   multiprocessing 자체의 종료 훅에도 같이 건다. 부모에서는 둘 다 돌지만 같은 파일을
    #   같은 내용으로 덮으므로 무해하다.
    atexit.register(_dump)
    try:
        from multiprocessing.util import Finalize
        Finalize(None, _dump, exitpriority=16)
    except Exception:
        pass

    def installed() -> dict:
        """계측이 실제로 걸렸는지 확인용. 긴 실행 전에 이걸로 사전 점검한다."""
        try:
            from openai.resources.chat.completions import Completions
            from openai.resources.embeddings import Embeddings
            return {"chat": bool(getattr(Completions.create, "__cost_meter__", None)),
                    "embed": bool(getattr(Embeddings.create, "__cost_meter__", None))}
        except Exception:
            return {"chat": False, "embed": False}
