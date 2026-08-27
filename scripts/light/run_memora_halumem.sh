#!/usr/bin/env bash
# LIGHT · Memora(weekly→monthly) + HaluMem(20유저) 본실험.
#
#   tmux new-session -d -s lightmh "bash -lc 'cd ~/projects/agentic-memory && bash scripts/light/run_memora_halumem.sh 2>&1 | tee -a /tmp/lightmh.log'"
#
# quarterly 는 여기 안 넣음 — 투입만 31만 콜이라 C-probe 실측·비용 승인 후 별도 결정
# (LIGHT_MEMORA_PERIODS="weekly monthly quarterly" 로 확장 가능).
# HaluMem 은 질의 비용이 최대(3,467문항 × C)라 마지막에 둠.
set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"
source "$ROOT/scripts/lib/manifest.sh"
E=eval/mem0-classic-oss
STAGE_FILE=/tmp/lightmh.stage
START=$(date +%s)

export PYTHONUNBUFFERED=1
export MEM0_IMPL=light
export OPENAI_BASE_URL=${LIGHT_BASE_URL:-http://localhost:8002/v1}
export MEM0_EMBED_BASE_URL=${LIGHT_EMBED_URL:-http://localhost:8001/v1}
export MEM0_LLM_MODEL=${MEM0_LLM_MODEL:-openai/gpt-oss-120b}
export MEM0_EMBED_MODEL=${MEM0_EMBED_MODEL:-Qwen/Qwen3-Embedding-4B}
export MEM0_EMBED_DIMS=${MEM0_EMBED_DIMS:-2560}
export OPENAI_API_KEY=${OPENAI_API_KEY:-dummy}
export ANSWER_MODEL=${ANSWER_MODEL:-openai/gpt-oss-120b}
export JUDGE_MODEL=${JUDGE_MODEL:-openai/gpt-oss-120b}
export ANSWER_REASONING_EFFORT=${ANSWER_REASONING_EFFORT:-high}
export JUDGE_REASONING_EFFORT=${JUDGE_REASONING_EFFORT:-high}

W_ING=${W_ING:-10}
W_ARM=${W_ARM:-24}
export LIGHT_EXTRACT_WORKERS=${LIGHT_EXTRACT_WORKERS:-2}
export LIGHT_FILTER_WORKERS=${LIGHT_FILTER_WORKERS:-2}
EXPECT_LLM_LEN=${EXPECT_LLM_LEN:-65536}
EXPECT_EMB_LEN=${EXPECT_EMB_LEN:-32768}
PERIODS=${LIGHT_MEMORA_PERIODS:-"weekly monthly"}

if [ -z "${COST_OFF:-}" ]; then
  export PYTHONPATH="$ROOT/src/cost${PYTHONPATH:+:$PYTHONPATH}"
  export COST_SYSTEM="${COST_SYSTEM:-light}"
fi
cost_dir() { [ -n "${COST_OFF:-}" ] && { echo ""; return; }; echo "$ROOT/cost/$1"; }

LIGHT="uv run --project eval/light python"
MAIN="uv run python"

stage() { echo "$1" > "$STAGE_FILE"; echo; echo "▶ $1"; }
hms() { local s=$1; printf "%dh%02dm" $((s/3600)) $((s%3600/60)); }
n_lines() { [ -s "$1" ] && grep -c . "$1" || echo 0; }

TICK=${TICK:-600}
LOG=${LOG:-/tmp/lightmh.log}
ticker() {
  local parent=$1
  while kill -0 "$parent" 2>/dev/null; do
    sleep "$TICK"
    kill -0 "$parent" 2>/dev/null || break
    local st el p
    st=$(cat "$STAGE_FILE" 2>/dev/null || echo "?")
    el=$(( $(date +%s) - START ))
    p=$(tail -c 200000 "$LOG" 2>/dev/null | grep -oE "[0-9]+/[0-9]+ (sessions|chunks)" | tail -1)
    echo "  [진행] $(hms $el) 경과 · ${st}${p:+ · 최근 $p}"
  done
}
ticker $$ & TICKER=$!
cleanup() { kill "$TICKER" 2>/dev/null; wait "$TICKER" 2>/dev/null; }
trap cleanup EXIT INT TERM

echo "━━━ LIGHT · Memora(${PERIODS}) + HaluMem 20유저 ━━━"
echo "  LLM=${OPENAI_BASE_URL} · 임베더=${MEM0_EMBED_BASE_URL}"
echo "  워커 투입 ${W_ING}×${LIGHT_EXTRACT_WORKERS} / 팔 ${W_ARM}"

len=$(curl -sf --max-time 10 "${OPENAI_BASE_URL}/models" | python3 -c "
import json,sys
print({x['id']: x.get('max_model_len') for x in json.load(sys.stdin)['data']}.get('${MEM0_LLM_MODEL}','NONE'))") || { echo "✗ LLM 응답 없음"; exit 1; }
[ "$len" = "$EXPECT_LLM_LEN" ] || { echo "✗ LLM max_model_len=${len} (기대 ${EXPECT_LLM_LEN})"; exit 1; }
elen=$(curl -sf --max-time 10 "${MEM0_EMBED_BASE_URL}/models" | python3 -c "
import json,sys
print({x['id']: x.get('max_model_len') for x in json.load(sys.stdin)['data']}.get('${MEM0_EMBED_MODEL}','NONE'))")
[ "$elen" = "$EXPECT_EMB_LEN" ] || { echo "✗ 임베더 max_model_len=${elen} (기대 ${EXPECT_EMB_LEN})"; exit 1; }
echo "✓ LLM ${len} · 임베더 ${elen}"

stage "0 사전 점검 (verify_light)"
$LIGHT eval/light/verify_light.py || { echo "✗ verify_light 실패"; exit 1; }

# ============ Memora ============
for PER in $PERIODS; do
  N_P=$(find "Memora/data/$PER" -maxdepth 1 -mindepth 1 -type d | wc -l)
  ING="results/light/memora-${PER}-light/memora_eval_results.jsonl"
  if [ "$(n_lines "$ING")" -ge "$N_P" ]; then
    echo "▶ [Memora ${PER}] 투입 건너뜀"
  else
    stage "[Memora ${PER}] 투입 (페르소나 ${N_P})"
    COST_DIR=$(cost_dir memora-${PER}-light) COST_STAGE=ingest COST_BENCH=memora COST_SETTING=${PER} \
    $LIGHT eval/light/ingest_memora_light.py --data "Memora/data/$PER" \
        --version "${PER}-light" --top-k 200 --max-workers "$W_ING" --trace \
        || { echo "✗ [Memora ${PER}] 투입 실패"; exit 1; }
  fi
  [ "$(n_lines "$ING")" -ge "$N_P" ] || { echo "✗ [Memora ${PER}] 투입이 $(n_lines "$ING")/${N_P}"; exit 1; }
  write_manifest "$(dirname "$ING")" light memora "$PER" ingest

  GEN="results/light/memora-gen-${PER}-light/answers.jsonl"
  if [ "$(n_lines "$GEN")" -ge "$N_P" ]; then
    echo "▶ [Memora ${PER}] 답변 건너뜀"
  else
    stage "[Memora ${PER}] 답변 (cutoff 50 = 공식)"
    mkdir -p "$(dirname "$GEN")"
    COST_DIR=$(cost_dir memora-${PER}-light) COST_STAGE=answer COST_BENCH=memora COST_SETTING=${PER} \
    $MAIN $E/memora/answer_memora.py --results "$ING" --out "$GEN" \
        --cutoff 50 --max-workers "$W_ARM" || { echo "✗ [Memora ${PER}] 답변 실패"; exit 1; }
  fi
  [ "$(n_lines "$GEN")" -ge "$N_P" ] || { echo "✗ [Memora ${PER}] 답변 미완주"; exit 1; }
  write_manifest "$(dirname "$GEN")" light memora "$PER" answer

  JUD="results/light/memora-judge-${PER}-light"
  if [ -d "$JUD" ] && [ "$(find "$JUD" -name '*.json' | wc -l)" -ge "$N_P" ]; then
    echo "▶ [Memora ${PER}] 채점 건너뜀"
  else
    stage "[Memora ${PER}] 채점"
    COST_DIR=$(cost_dir memora-${PER}-light) COST_STAGE=judge COST_BENCH=memora COST_SETTING=${PER} \
    $MAIN $E/memora/judge_memora.py --results "$GEN" --out-dir "$JUD" \
        --max-workers "$W_ARM" || { echo "✗ [Memora ${PER}] 채점 실패"; exit 1; }
  fi
  [ "$(find "$JUD" -name '*.json' 2>/dev/null | wc -l)" -ge "$N_P" ] || { echo "✗ [Memora ${PER}] 채점 미완주"; exit 1; }
  write_manifest "$JUD" light memora "$PER" judge
done

# ============ HaluMem (질의 비용 최대 — 마지막) ============
HM_ING="results/light/memzero-20u-light/memzero-oss_eval_results.jsonl"
if [ "$(n_lines "$HM_ING")" -ge 20 ]; then
  echo "▶ [HaluMem] 투입 건너뜀"
else
  stage "[HaluMem] 투입 (20유저 · 중간 시점 질의 포함)"
  COST_DIR=$(cost_dir halumem-20u-light) COST_STAGE=ingest COST_BENCH=halumem COST_SETTING=20u \
  $LIGHT eval/light/ingest_halumem_light.py --data dataset/HaluMem-Medium.jsonl \
      --version 20u-light --max-workers "$W_ING" --trace \
      || { echo "✗ [HaluMem] 투입 실패"; exit 1; }
fi
[ "$(n_lines "$HM_ING")" -ge 20 ] || { echo "✗ [HaluMem] 투입이 $(n_lines "$HM_ING")/20 유저"; exit 1; }
write_manifest "$(dirname "$HM_ING")" light halumem 20u ingest

HM_GEN="results/light/memzero-20u-light/gen/memzero-oss_eval_results.jsonl"
if [ "$(n_lines "$HM_GEN")" -ge 20 ]; then
  echo "▶ [HaluMem] 답변 건너뜀"
else
  stage "[HaluMem] 답변 (QA 3,467)"
  mkdir -p "$(dirname "$HM_GEN")"
  COST_DIR=$(cost_dir halumem-20u-light) COST_STAGE=answer COST_BENCH=halumem COST_SETTING=20u \
  $MAIN $E/gen_answers.py --results "$HM_ING" --out "$HM_GEN" \
      --max-workers "$W_ARM" || { echo "✗ [HaluMem] 답변 실패"; exit 1; }
fi
[ "$(n_lines "$HM_GEN")" -ge 20 ] || { echo "✗ [HaluMem] 답변 미완주"; exit 1; }
write_manifest "$(dirname "$HM_GEN")" light halumem 20u answer

HM_JUD="results/light/memzero-20u-light/judge-light"
if [ -d "$HM_JUD/judge" ] && [ "$(find "$HM_JUD/judge" -name '*.json' | wc -l)" -ge 20 ]; then
  echo "▶ [HaluMem] 채점 건너뜀"
else
  stage "[HaluMem] 채점 (기준 18,415)"
  COST_DIR=$(cost_dir halumem-20u-light) COST_STAGE=judge COST_BENCH=halumem COST_SETTING=20u \
  $MAIN $E/judge.py --results "$HM_GEN" --out-dir "$HM_JUD" \
      --max-workers "$W_ARM" || { echo "✗ [HaluMem] 채점 실패"; exit 1; }
fi
n_jud=$(find "$HM_JUD/judge" -name '*.json' 2>/dev/null | wc -l)
[ "$n_jud" -ge 20 ] || { echo "✗ [HaluMem] 채점이 ${n_jud}/20 유저"; exit 1; }
write_manifest "$HM_JUD" light halumem 20u judge

stage "완료"
echo "━━━ 전부 완료 ($(hms $(( $(date +%s) - START )))) ━━━"
