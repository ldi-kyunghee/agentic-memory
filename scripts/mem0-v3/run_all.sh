#!/usr/bin/env bash
# mem0 v3(2.0.18) 본실험 배치. HaluMem -> BEAM 100K -> Memora monthly/quarterly.
#
#   tmux new-session -d -s v3all "bash -lc 'cd ~/projects/agentic-memory && bash scripts/mem0-v3/run_all.sh 2>&1 | tee -a /tmp/v3all.log'"
#
# 약 41시간. 단계마다 이미 끝난 것은 건너뛰고, 실패하면 거기서 멈춰 뒤를 안 태움.
# **10분마다 진행 요약 한 줄**을 찍어서 tmux 창에 들어오면 어디쯤인지 바로 보이게 함.
#
# mem0 classic 산출물은 이미 있음. 여기서 만드는 것은 전부 mem0 v3 것임.
#
# ⚠ 투입 단계는 **항상 --trace 를 켠다.** 2026-08-26에 실행 시간을 줄이려고 껐다가,
#   나중에 비용(호출 수·토큰·컨텍스트 크기)을 되살릴 방법이 통째로 사라졌음.
#   trace 는 프롬프트 원문과 응답을 남기므로 사후에 무엇이든 다시 셀 수 있음.
#   용량은 런당 수십~수백 MB 수준이라(전체 2.7G) 끌 이유가 못 됨.
set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"
source "$ROOT/scripts/lib/manifest.sh"
E=eval/mem0-classic-oss
R=results/mem0-classic-oss
STAGE_FILE=/tmp/v3all.stage
START=$(date +%s)

export PYTHONUNBUFFERED=1
export MEM0_IMPL=v3
export FASTEMBED_CACHE_PATH=${FASTEMBED_CACHE_PATH:-$ROOT/.cache/fastembed}
export OPENAI_BASE_URL=${MEM0V3_BASE_URL:-http://localhost:8002/v1}
export MEM0_EMBED_BASE_URL=${MEM0V3_EMBED_URL:-http://localhost:8001/v1}
export MEM0_LLM_MODEL=${MEM0_LLM_MODEL:-openai/gpt-oss-120b}
export MEM0_EMBED_MODEL=${MEM0_EMBED_MODEL:-Qwen/Qwen3-Embedding-4B}
export MEM0_EMBED_DIMS=${MEM0_EMBED_DIMS:-2560}
export QDRANT_HOST=${QDRANT_HOST:-localhost}
export QDRANT_PORT=${QDRANT_PORT:-6333}
export OPENAI_API_KEY=${OPENAI_API_KEY:-dummy}
export ANSWER_MODEL=${ANSWER_MODEL:-openai/gpt-oss-120b}
export JUDGE_MODEL=${JUDGE_MODEL:-openai/gpt-oss-120b}
export ANSWER_REASONING_EFFORT=${ANSWER_REASONING_EFFORT:-high}
# ⚠ answer_beam.py 의 BEAM_ANSWER_PROMPT 기본값은 `beam`(BEAM 공식)인데, classic 대표 레인은
#   `mem0` 하네스 프롬프트로 돌렸다. 안 맞추면 메모리 시스템 차이가 아니라 프롬프트 차이를 잰다
#   (2026-08-26 실제로 그렇게 나갔고 부호가 뒤집혔음). **기본값에 기대지 말고 항상 명시한다.**
export BEAM_ANSWER_PROMPT=${BEAM_ANSWER_PROMPT:-mem0}
export JUDGE_REASONING_EFFORT=${JUDGE_REASONING_EFFORT:-high}

W_ING=${W_ING:-10}     # 투입. 구조적 상한: HaluMem 20유저 / BEAM 20대화 / Memora 10페르소나
W_ARM=${W_ARM:-12}     # 답변·채점. 서버 무릎이 c=12 (실측)
# v3 추출 프롬프트는 고정 시스템(~8.4k) + 기존메모리 top10 + 직전 10메시지 + 현재 청크로 조립됨.
# BEAM 100K 에서 34,863 토큰까지 커져 32768 에서 대화 하나가 죽었음 (2026-08-25). 상한은
# 대화 길이가 아니라 메시지 크기에 달려 있어 누적으로 늘지는 않음. 임베더는 32768 로 충분함.
EXPECT_LLM_LEN=${EXPECT_LLM_LEN:-65536}
EXPECT_EMB_LEN=${EXPECT_EMB_LEN:-32768}

# ---- 비용 계측 (평가 코드 무수정, sitecustomize 방식) ----
# 단계마다 COST_STAGE 를 바꿔 끼운다. 산출물은 cost/{설정}/{stage}__{pid}.json 이고
# src/cost/report.py 가 합친다. 끄려면 COST_OFF=1 을 준다.
if [ -z "${COST_OFF:-}" ]; then
  export PYTHONPATH="$ROOT/src/cost${PYTHONPATH:+:$PYTHONPATH}"
  export COST_SYSTEM="${COST_SYSTEM:-mem0-v3}"
fi
cost_dir() { [ -n "${COST_OFF:-}" ] && { echo ""; return; }; echo "$ROOT/cost/$1"; }

V3="uv run --project eval/mem0-v3 python"
MAIN="uv run python"

stage() { echo "$1" > "$STAGE_FILE"; echo; echo "▶ $1"; }
hms() { local s=$1; printf "%dh%02dm" $((s/3600)) $((s%3600/60)); }
n_lines() { [ -s "$1" ] && grep -c . "$1" || echo 0; }

# ---- TICK 초마다 진행 한 줄 (tmux 창에 들어왔을 때 어디쯤인지 바로 보이게) ----
# ⚠ 백그라운드 프로세스가 stdout 을 붙잡으면 파이프 소비자가 안 끝남. 부모가 죽으면
#   스스로 빠져나오게 하고, trap 에서 kill 한 뒤 wait 로 거둠.
TICK=${TICK:-600}
ticker() {
  local parent=$1
  while kill -0 "$parent" 2>/dev/null; do
    sleep "$TICK"
    kill -0 "$parent" 2>/dev/null || break
    local st el p
    st=$(cat "$STAGE_FILE" 2>/dev/null || echo "?")
    el=$(( $(date +%s) - START ))
    # 하네스가 찍는 "N/M sessions|chunks" 마지막 줄에서 진행 추출
    p=$(tail -c 200000 "$LOG" 2>/dev/null | grep -oE "[0-9]+/[0-9]+ (sessions|chunks)" | tail -1)
    echo "  [진행] $(hms $el) 경과 · ${st}${p:+ · 최근 $p}"
  done
}
LOG=${LOG:-/tmp/v3all.log}
ticker $$ & TICKER=$!
cleanup() { kill "$TICKER" 2>/dev/null; wait "$TICKER" 2>/dev/null; }
trap cleanup EXIT INT TERM

echo "━━━ mem0 v3 본실험 배치 ━━━"
echo "  LLM=${OPENAI_BASE_URL} · 임베더=${MEM0_EMBED_BASE_URL}"
echo "  워커 투입 ${W_ING} / 팔 ${W_ARM}"

# ---- 사전 확인 ----
body=$(curl -sf --max-time 10 "${OPENAI_BASE_URL}/models") || { echo "✗ LLM 응답 없음"; exit 1; }
len=$(printf '%s' "$body" | python3 -c "
import json,sys
print({x['id']: x.get('max_model_len') for x in json.load(sys.stdin)['data']}.get('${MEM0_LLM_MODEL}','NONE'))")
[ "$len" = "$EXPECT_LLM_LEN" ] || { echo "✗ LLM max_model_len=${len} (기대 ${EXPECT_LLM_LEN}). 남의 인스턴스이거나 32768 로 떠 있음"; exit 1; }
elen=$(curl -sf --max-time 10 "${MEM0_EMBED_BASE_URL}/models" | python3 -c "
import json,sys
print({x['id']: x.get('max_model_len') for x in json.load(sys.stdin)['data']}.get('${MEM0_EMBED_MODEL}','NONE'))")
[ "$elen" = "$EXPECT_EMB_LEN" ] || { echo "✗ 임베더 max_model_len=${elen} (기대 ${EXPECT_EMB_LEN}). v3 는 입력 블록 전체를 임베딩하므로 4096 이면 BEAM 이 실패함"; exit 1; }
echo "✓ LLM ${len} · 임베더 ${elen}"
mkdir -p "$FASTEMBED_CACHE_PATH" && touch "$FASTEMBED_CACHE_PATH/.p" && rm -f "$FASTEMBED_CACHE_PATH/.p" \
  || { echo "✗ fastembed 캐시에 못 씀"; exit 1; }
echo "✓ fastembed 캐시"

# ============ 1. HaluMem ============
HM_ING="$R/memzero-oss-v3/memzero-oss_eval_results.jsonl"
if [ "$(n_lines "$HM_ING")" -ge 20 ]; then
  echo "▶ 1/8 HaluMem 투입 건너뜀 (20유저 완주본 있음)"
else
  stage "1/8 HaluMem 투입 (20유저)"
  COST_DIR=$(cost_dir halumem-20u-mem0-v3) COST_STAGE=ingest COST_BENCH=halumem COST_SETTING=20u \
  $V3 $E/eval_memzero_oss.py --data dataset/HaluMem-Medium.jsonl --version v3 \
      --top-k 20 --max-workers "$W_ING" --trace || { echo "✗ HaluMem 투입 실패"; exit 1; }
fi
[ "$(n_lines "$HM_ING")" -ge 20 ] || { echo "✗ HaluMem 투입이 $(n_lines "$HM_ING")/20 유저"; exit 1; }
write_manifest "$(dirname "$HM_ING")" mem0-v3 halumem 20u ingest

HM_GEN="$R/memzero-oss-v3/gen-v3/memzero-oss_eval_results.jsonl"
if [ "$(n_lines "$HM_GEN")" -ge 20 ]; then
  echo "▶ 2/8 HaluMem 답변 건너뜀"
else
  stage "2/8 HaluMem 답변 (QA 3,467)"
  mkdir -p "$(dirname "$HM_GEN")"
  COST_DIR=$(cost_dir halumem-20u-mem0-v3) COST_STAGE=answer COST_BENCH=halumem COST_SETTING=20u \
  MEM0_IMPL=classic $MAIN $E/gen_answers.py --results "$HM_ING" --out "$HM_GEN" \
      --max-workers "$W_ARM" || { echo "✗ HaluMem 답변 실패"; exit 1; }
fi

HM_JUD="$R/memzero-oss-v3/judge-v3"
if [ -d "$HM_JUD" ] && [ "$(find "$HM_JUD" -name '*.json' | wc -l)" -ge 20 ]; then
  echo "▶ 3/8 HaluMem 채점 건너뜀"
else
  stage "3/8 HaluMem 채점 (기준 18,415)"
  COST_DIR=$(cost_dir halumem-20u-mem0-v3) COST_STAGE=judge COST_BENCH=halumem COST_SETTING=20u \
  MEM0_IMPL=classic $MAIN $E/judge.py --results "$HM_GEN" --out-dir "$HM_JUD" \
      --max-workers "$W_ARM" || { echo "✗ HaluMem 채점 실패"; exit 1; }
fi

# ============ 2. BEAM 100K ============
BM_ING="$R/beam-100k-v3/beam_eval_results.jsonl"
if [ "$(n_lines "$BM_ING")" -ge 20 ]; then
  echo "▶ 4/8 BEAM 투입 건너뜀"
else
  stage "4/8 BEAM 100K 투입 (대화 20 · 청크 2,866)"
  COST_DIR=$(cost_dir beam-100k-mem0-v3) COST_STAGE=ingest COST_BENCH=beam COST_SETTING=100k \
  $V3 $E/beam/ingest_beam.py --chats BEAM/chats/100K --version 100k-v3 \
      --top-k 200 --max-workers "$W_ING" --trace || { echo "✗ BEAM 투입 실패"; exit 1; }
fi
[ "$(n_lines "$BM_ING")" -ge 20 ] || { echo "✗ BEAM 투입이 $(n_lines "$BM_ING")/20 대화"; exit 1; }
write_manifest "$(dirname "$BM_ING")" mem0-v3 beam 100k ingest

BM_GEN="$R/beam-genoss120-100k-v3/answers.jsonl"
if [ "$(n_lines "$BM_GEN")" -ge 20 ]; then
  echo "▶ 5/8 BEAM 답변 건너뜀"
else
  stage "5/8 BEAM 답변 (문항 400)"
  COST_DIR=$(cost_dir beam-100k-mem0-v3) COST_STAGE=answer COST_BENCH=beam COST_SETTING=100k \
  MEM0_IMPL=classic $MAIN $E/beam/answer_beam.py --results "$BM_ING" --out "$BM_GEN" \
      --max-workers "$W_ARM" || { echo "✗ BEAM 답변 실패"; exit 1; }
fi

BM_JUD="$R/beam-judge-oss120-100k-v3"
if [ -d "$BM_JUD" ] && [ "$(find "$BM_JUD" -name '*.json' | wc -l)" -ge 1 ]; then
  echo "▶ 6/8 BEAM 채점 건너뜀"
else
  stage "6/8 BEAM 채점"
  COST_DIR=$(cost_dir beam-100k-mem0-v3) COST_STAGE=judge COST_BENCH=beam COST_SETTING=100k \
  MEM0_IMPL=classic $MAIN $E/beam/judge_beam.py --results "$BM_GEN" --out-dir "$BM_JUD" \
      --max-workers "$W_ARM" || { echo "✗ BEAM 채점 실패"; exit 1; }
fi

# ============ 3. Memora monthly / quarterly ============
for PER in monthly quarterly; do
  N_P=$(find "Memora/data/$PER" -maxdepth 1 -mindepth 1 -type d | wc -l)
  MO_ING="$R/memora-${PER}-v3-oss120b/memora_eval_results.jsonl"
  if [ "$(n_lines "$MO_ING")" -ge "$N_P" ]; then
    echo "▶ 7/8 Memora ${PER} 투입 건너뜀"
  else
    stage "7/8 Memora ${PER} 투입 (페르소나 ${N_P})"
    COST_DIR=$(cost_dir memora-${PER}-mem0-v3) COST_STAGE=ingest COST_BENCH=memora COST_SETTING=${PER} \
    $V3 $E/memora/ingest_memora.py --data "Memora/data/$PER" \
        --version "${PER}-v3-oss120b" --top-k 50 --max-workers "$W_ING" --trace \
        || { echo "✗ Memora ${PER} 투입 실패"; exit 1; }
  fi
  [ "$(n_lines "$MO_ING")" -ge "$N_P" ] || { echo "✗ Memora ${PER} 투입 미완주"; exit 1; }
write_manifest "$(dirname "$MO_ING")" mem0-v3 memora "$PER" ingest

  MO_GEN="$R/memora-gen-${PER}-v3/answers.jsonl"
  if [ "$(n_lines "$MO_GEN")" -ge "$N_P" ]; then
    echo "▶ 8/8 Memora ${PER} 답변 건너뜀"
  else
    stage "8/8 Memora ${PER} 답변"
    COST_DIR=$(cost_dir memora-${PER}-mem0-v3) COST_STAGE=answer COST_BENCH=memora COST_SETTING=${PER} \
    MEM0_IMPL=classic $MAIN $E/memora/answer_memora.py --results "$MO_ING" --out "$MO_GEN" \
        --max-workers "$W_ARM" || { echo "✗ Memora ${PER} 답변 실패"; exit 1; }
  fi

  MO_JUD="$R/memora-judge-${PER}-v3"
  if [ -d "$MO_JUD" ] && [ "$(ls "$MO_JUD"/*.json 2>/dev/null | wc -l)" -ge "$N_P" ]; then
    echo "▶ 8/8 Memora ${PER} 채점 건너뜀"
  else
    stage "8/8 Memora ${PER} 채점"
    COST_DIR=$(cost_dir memora-${PER}-mem0-v3) COST_STAGE=judge COST_BENCH=memora COST_SETTING=${PER} \
    MEM0_IMPL=classic $MAIN $E/memora/judge_memora.py --results "$MO_GEN" --out-dir "$MO_JUD" \
        --max-workers "$W_ARM" || { echo "✗ Memora ${PER} 채점 실패"; exit 1; }
  fi
done

# 채점까지 끝난 뒤 이력을 한 번에 남긴다. 단계마다 env 가 같으므로 시점 차이는 없다.
write_manifest "$HM_JUD" mem0-v3 halumem 20u judge
write_manifest "$BM_JUD" mem0-v3 beam 100k judge
for PER in monthly quarterly; do
  write_manifest "$R/memora-judge-${PER}-v3" mem0-v3 memora "$PER" judge
done

stage "완료"
echo
echo "━━━ 전부 완료 ($(hms $(( $(date +%s) - START )))) ━━━"
echo "  HaluMem : $HM_JUD"
echo "  BEAM    : $BM_JUD"
echo "  Memora  : $R/memora-judge-monthly-v3 · $R/memora-judge-quarterly-v3"
