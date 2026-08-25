# 긴 작업을 넘기기 전 점검표

몇 시간~하루짜리 실행을 시작하기 전에 훑는 목록임. **여기 있는 항목은 전부 실제로 사고를
낸 것들임.** 추측이 아니라 겪은 것만 적음.

---

## 0. 가장 작은 조각으로 먼저 돌린다 (제일 중요)

**아래 항목을 아무리 늘려도 안 보면 소용없음.** 실제로 2026-08-22 하루에 네 번 연달아
사고를 냈고, 그중 셋(경로·엔드포인트·인자 이름)은 **5분짜리 축소 실행 한 번이면 전부
걸렸을 것임.**

새로 만들거나 고친 러너는 **본 실행 전에 최소 조각으로 한 번 통과시킴.** 단계가
처음부터 끝까지 한 번은 이어져야 함.

```bash
# 페르소나 하나 · cutoff 하나 · 제일 작은 기간. 5분이면 끝남
uv run python eval/mem0-classic-oss/memora/ingest_memora.py   --data Memora/data/weekly --version smoke-$(date +%H%M)-oss120b   --top-k 800 --personas academic_researcher --max-workers 1
```

축소 실행이 끝까지 가야 본 실행을 시작함. 사람에게 커맨드를 넘길 때는 **"확인했다"는 말
대신 축소 실행의 실제 출력을 함께 보여줌.**

원칙 하나로 줄이면 이것임: **머릿속 파이프라인 모양으로 커맨드를 짜지 않고, 넷을 실제
값과 대조함 — 엔드포인트 · env · 인자 이름 · 경로.** 그리고 그 대조를 사람이 아니라
스크립트가 하게 만듦.

---

## 1. 엔드포인트

```bash
curl -s http://localhost:8002/v1/models | python3 -m json.tool | head -12
```

- [ ] 응답이 있는가
- [ ] 원하는 모델 id가 있는가
- [ ] **`max_model_len`이 우리 값인가** (LLM 32768 / 임베더 4096)

⚠ **모델 이름만 보면 안 됨.** 이 서버에는 다른 사용자(`/home/dania/agentic-memory`)가
**같은 `openai/gpt-oss-120b`를 포트 8000에** 띄워둠. 이름은 똑같고 `max_model_len`만 다름
(그쪽 131072, 우리 32768). 이름만 확인하는 사전 검사는 통과해버림.

| 포트 | 주인 | 모델 | max_model_len |
|---|---|---|---|
| 8000 | **dania (남의 것)** | openai/gpt-oss-120b | 131072 |
| 8001 | 우리 | Qwen/Qwen3-Embedding-4B | 4096 |
| 8002 | 우리 | openai/gpt-oss-120b | **32768** |

포트 주인 확인:

```bash
ss -ltnp | grep -E ":8000|:8001|:8002"; ps -eo pid,etime,args | grep "vllm serve" | grep -v grep | cut -c1-120
```

---

## 2. env

```bash
grep -E "OPENAI_|MEM0_" .env; echo "--- 셸에 이미 잡힌 것 ---"; env | grep -E "OPENAI_|MEM0_|ANSWER_|JUDGE_"
```

- [ ] `.env` 값이 이번 실험에 맞는가
- [ ] 셸에 이미 export된 값이 있는가

⚠ **`${VAR:-기본값}`은 이미 잡혀 있으면 안 먹음.** 반드시 우리 값이어야 하는 것은 `:-`를
쓰지 말고 **고정**하고, 오버라이드는 별도 이름으로 엶 (`MEMORA_BASE_URL` 같은 식).

⚠ python 쪽은 `load_dotenv()`가 `.env`를 읽음. 셸에서 export하지 않으면 `.env` 값이 들어감.

| env | 이 리포에서의 값 | 안 주면 |
|---|---|---|
| `OPENAI_BASE_URL` | `http://localhost:8002/v1` | `.env`의 8000 = **남의 서버** |
| `MEM0_LLM_MODEL` | `openai/gpt-oss-120b` | `.env`의 `OPENAI_MODEL`(Qwen3-4B) → 404 |
| `ANSWER_MODEL` / `JUDGE_MODEL` | `openai/gpt-oss-120b` | 위와 같음 |
| `ANSWER_REASONING_EFFORT` / `JUDGE_REASONING_EFFORT` | `high` | 모델 기본값 |
| `MEM0_REASONING_EFFORT` | **주지 않음** | (agent는 기본값 medium이 통제 조건) |

---

## 3. 인자 이름

- [ ] 쓰려는 플래그가 **실제로 있는지** `--help`로 확인했는가

⚠ 스크립트마다 이름이 다르다. 특히 **`judge_memora.py`는 답변 파일을 `--answers`가 아니라
`--results`로 받는다.** Stage A′ 산출물인데 이름이 `--results`라 헷갈림.

| 스크립트 | 입력 | 출력 |
|---|---|---|
| `ingest_memora.py` | `--data` (기간 디렉토리) | `--version` (→ `results/mem0-classic-oss/memora-{version}/`) |
| `answer_memora.py` | `--results` (ingest jsonl) | `--out` (파일) |
| `judge_memora.py` | **`--results`** (answers jsonl) | `--out-dir` (디렉토리) |

```bash
for f in ingest answer judge; do echo "== $f"; uv run python eval/mem0-classic-oss/memora/${f}_memora.py --help 2>&1 | grep -E "^ +--"; done
```

긴 실행 스크립트에는 이 확인을 **코드로 넣는다** (`run_cutoff_sweep.sh`의 `check_flags`).
`set -e`가 걸린 스크립트에서 인자 하나가 틀리면 그때까지의 단계가 다 끝난 뒤에 죽는다.

---

## 4. 경로

- [ ] 스크립트가 실제로 쓰는 경로를 **코드에서** 확인했는가

⚠ **기존 산출물 이름에서 규칙을 역추론하지 않음.** `memora-weekly-oss120b`는 스크립트가
접미사를 붙인 게 아니라 `--version weekly-oss120b`로 넘긴 것임. `ingest_memora.py`는
`results/mem0-classic-oss/memora-{version}/`을 그대로 씀.

```bash
grep -n "save_path\s*=\|out_dir\|--out" eval/mem0-classic-oss/memora/*.py
```

---

## 5. 스크립트 자체에 넣을 것

**사전 확인(preflight).** 몇 시간을 태우고 나서 틀린 걸 알면 안 됨. 시작 전에 엔드포인트·
모델·`max_model_len`을 확인하고 어긋나면 **시작하지 않음.**

**중간 완주 검사.** 단계 사이에서 산출물이 온전한지 봄. `[ -f "$FILE" ]`만 보면 안 됨 —
투입이 통째로 실패해도 **0바이트 파일이 남아** 다음 단계가 빈 입력으로 돌고 결과가 나온
것처럼 보임. 기대 개수(페르소나 수 등)와 실제 줄 수를 대조함.

**이어 돌기.** 단위 작업이 끝날 때마다 저장함. 중단 후 재시작이 처음부터 다시 돌면 안 됨
(`ingest_memora.py`의 `tmp/{key}.json` 캐시 방식). **단계마다 "이미 다 됐으면 건너뛴다"를
넣는다** — `answer_memora.py`는 입력이 ingest 파일이라 자기 출력물을 보지 않으므로,
러너 쪽에서 출력물의 완료 개수를 세서 건너뛰어야 함.

**실패를 조용히 삼키지 않기.** 빈 결과·파싱 실패 건수를 마지막에 경고로 출력함.

참고 구현: `scripts/memora/run_cutoff_sweep.sh`

---

## 6. 실행 형태

- 장시간 작업은 **tmux**. `nohup`/`&` 쓰지 않음
- 긴 작업에 `| tail`, `| head` 금지. `tmux + tee + PYTHONUNBUFFERED`
- 파괴적 명령(`rm -rf`, `kill`, `git reset --hard`)은 별도 단계로 분리하고, 지금 도는
  작업이 없는지 먼저 확인함
- 같은 이름 tmux 세션이 살아 있으면 `tmux new -d -s X`는 조용히 실패함
- ⚠ **`tmux kill-session`으로 죽이면 `finally`가 안 돈다.** Qdrant 컬렉션 정리가 건너뛰어져
  고아 컬렉션이 남음. BEAM 때 180개에서 Qdrant가 죽었으니 누적을 방치하면 안 됨.
  강제 종료한 뒤에는 **반드시 남은 컬렉션을 확인하고 지움**:

  ```bash
  curl -s http://localhost:6333/collections | python3 -c "
  import json,sys
  c=[x['name'] for x in json.load(sys.stdin)['result']['collections']]
  print(f'총 {len(c)}'); [print('  ',n) for n in sorted(c) if 'memora' in n or 'beam' in n]
  "
  ```

  지울 때는 **접두사를 정확히 잡음.** `memora_monthly-k800_`과 `memora_monthly-k800-oss120b_`는
  다른 실행임. 지금 도는 것을 지우지 않도록 삭제 전에 목록만 먼저 출력해 눈으로 확인함
- `--max-workers`는 4가 기본 ([[CLAUDE.md]] 참조). 투입은 페르소나 단위 병렬이라
  페르소나 수보다 크게 줘도 소용없음

---

- **재시도 전에 실패분의 찌꺼기를 지운다.** 컬렉션은 `finally`에서 지워지지만 `tmp/history_{key}.db`는 남는다.
  남은 채로 재시도하면 첫 청크가 이전 회차의 직전 메시지를 컨텍스트로 받아 결과가 오염된다.
- **`--max-model-len`은 가장 큰 프롬프트를 기준으로 잡는다.** 답변·채점은 `max_completion_tokens=4096`으로 코드에
  박혀 있어 무관하고, 실제로 창을 정하는 것은 투입 단계의 추출 프롬프트다.

## 겪은 사고 목록

| 언제 | 무엇 | 대가 |
|---|---|---|
| 2026-08-25 | LLM `--max-model-len 32768`이 mem0 v3 투입에 모자람(실측 34,863). BEAM 대화 1/20이 죽고 배치가 4단계에서 멈춤 | 12시간 유휴 |
| 2026-08-25 | 실패한 대화의 `tmp/history_*.db`가 안 지워짐. 그대로 재시도하면 `get_last_messages`가 이전 회차를 물어옴 | 사전 차단 |
| 2026-08-22 | `OPENAI_BASE_URL`이 셸/`.env`의 8000으로 잡혀 **남의 vLLM으로 투입이 나감** | 즉시 중단 |
| 2026-08-22 | `--version`과 결과 디렉토리 경로 불일치. 투입 완주 후 죽는 구조 | 20분 (조기 발견) |
| 2026-08-22 | `MEM0_LLM_MODEL` 누락 → `.env`의 Qwen3-4B를 불러 404 재시도만 쌓임 | 20분 |
| 2026-08-22 | 0바이트 산출물을 `-f`로 검사해 Stage A를 건너뛸 뻔함 | 사전 차단 |
| 2026-08-22 | `tmux kill-session`이 `finally`를 건너뛰어 고아 컬렉션 4개가 남음 | 수동 정리 |
| 2026-08-22 | judge에 `--answers`를 넘김(실제는 `--results`). 투입 4.1시간 + 답변 22분을 마친 뒤 죽음 | 40분 유휴 |
| 2026-08-21 | `_spearman` 이름 충돌로 Metrics 탭 전체가 죽음. 탭 전환만 보고 내용을 안 봄 | 하루 |
| 2026-08-18 | 돌고 있던 채점의 출력 디렉토리를 `rm -rf`로 지움 | 1.5시간 |
| 2026-08-18 | Qdrant 컬렉션을 안 지워 180개에서 서버가 죽음 | 500K·1M 투입 유실 |
| 2026-08-17 | 답변을 메모리에 모았다가 끝에 한 번 저장 → 예외 하나로 1,600건 유실 | 재실행 |
