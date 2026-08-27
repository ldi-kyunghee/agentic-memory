#!/usr/bin/env bash
# LIGHT · BEAM 본실험 (100K → 500K). 각 버킷: 투입 → 답변 → 채점.
#
#   tmux new-session -d -s lightbeam "bash -lc 'cd ~/projects/agentic-memory && bash scripts/light/run_beam.sh 2>&1 | tee -a /tmp/lightbeam.log'"
#
# ⚠ 본실행 전에 C-probe 를 먼저 돎 (eval/light/measure_c.py). noise filter 콜 수가
#   조각 수(C)에 비례해 비용이 자릿수로 변할 수 있음. 이 스크립트는 그 게이트를 지났다는
#   전제임.
set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"
source "$ROOT/scripts/lib/manifest.sh"
E=eval/mem0-classic-oss
STAGE_FILE=/tmp/lightbeam.stage
START=$(date +%s)

export PYTHONUNBUFFERED=1
export MEM0_IMPL=light                       # manifest 기록용 (LIGHT 는 이 스위치를 안 탐)
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
# ⚠ LIGHT 원본의 답변 프롬프트가 BEAM 공식(answer_generation_for_rag)이고, 등록 레인도
#   {100k,500k}-beamprompt 임 (mem0 v3 와 3자 비교가 성립하는 자리). 기본값에 기대지 않고
#   명시함 — 2026-08-26 프롬프트 불일치로 결론의 부호가 뒤집힌 사고의 재발 지점.
export BEAM_ANSWER_PROMPT=${BEAM_ANSWER_PROMPT:-beam}

# 워커 곱(W_ING × LIGHT_EXTRACT_WORKERS)이 서버 상한(단독 20)을 넘지 않게 짝지음
W_ING=${W_ING:-10}
W_ARM=${W_ARM:-24}
export LIGHT_EXTRACT_WORKERS=${LIGHT_EXTRACT_WORKERS:-2}
export LIGHT_FILTER_WORKERS=${LIGHT_FILTER_WORKERS:-2}
EXPECT_LLM_LEN=${EXPECT_LLM_LEN:-65536}
EXPECT_EMB_LEN=${EXPECT_EMB_LEN:-32768}
BUCKETS=${LIGHT_BEAM_BUCKETS:-"100K 500K"}

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
LOG=${LOG:-/tmp/lightbeam.log}
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

echo "━━━ LIGHT · BEAM 본실험 (${BUCKETS}) ━━━"
echo "  LLM=${OPENAI_BASE_URL} · 임베더=${MEM0_EMBED_BASE_URL}"
echo "  워커 투입 ${W_ING}×${LIGHT_EXTRACT_WORKERS} / 팔 ${W_ARM} · 답변 프롬프트 ${BEAM_ANSWER_PROMPT}"

# ---- 사전 확인 ----
len=$(curl -sf --max-time 10 "${OPENAI_BASE_URL}/models" | python3 -c "
import json,sys
print({x['id']: x.get('max_model_len') for x in json.load(sys.stdin)['data']}.get('${MEM0_LLM_MODEL}','NONE'))") || { echo "✗ LLM 응답 없음"; exit 1; }
[ "$len" = "$EXPECT_LLM_LEN" ] || { echo "✗ LLM max_model_len=${len} (기대 ${EXPECT_LLM_LEN}). 남의 인스턴스일 수 있음"; exit 1; }
elen=$(curl -sf --max-time 10 "${MEM0_EMBED_BASE_URL}/models" | python3 -c "
import json,sys
print({x['id']: x.get('max_model_len') for x in json.load(sys.stdin)['data']}.get('${MEM0_EMBED_MODEL}','NONE'))")
[ "$elen" = "$EXPECT_EMB_LEN" ] || { echo "✗ 임베더 max_model_len=${elen} (기대 ${EXPECT_EMB_LEN})"; exit 1; }
echo "✓ LLM ${len} · 임베더 ${elen}"

stage "0/3 사전 점검 (verify_light)"
$LIGHT eval/light/verify_light.py || { echo "✗ verify_light 실패"; exit 1; }

for B in $BUCKETS; do
  b=$(echo "$B" | tr "A-Z" "a-z")
  case "$B" in
    100K) N_CONV=20 ;;
    500K) N_CONV=35 ;;
    *) echo "✗ 모르는 버킷 $B"; exit 1 ;;
  esac
  [ -d "BEAM/chats/$B" ] || { echo "✗ BEAM/chats/$B 없음"; exit 1; }

  ING="results/light/beam-${b}-light/beam_eval_results.jsonl"
  if [ "$(n_lines "$ING")" -ge "$N_CONV" ]; then
    echo "▶ [${B}] 투입 건너뜀 (${N_CONV}대화 완주본 있음)"
  else
    stage "[${B}] 1/3 투입 (대화 ${N_CONV})"
    COST_DIR=$(cost_dir beam-${b}-light) COST_STAGE=ingest COST_BENCH=beam COST_SETTING=${b}-beamprompt \
    $LIGHT eval/light/ingest_beam_light.py --chats "BEAM/chats/$B" \
        --version "${b}-light" --top-k 200 --max-workers "$W_ING" --trace \
        || { echo "✗ [${B}] 투입 실패"; exit 1; }
  fi
  [ "$(n_lines "$ING")" -ge "$N_CONV" ] || { echo "✗ [${B}] 투입이 $(n_lines "$ING")/${N_CONV} 대화"; exit 1; }
  write_manifest "$(dirname "$ING")" light beam "${b}-beamprompt" ingest

  GEN="results/light/beam-gen-${b}-light/answers.jsonl"
  if [ "$(n_lines "$GEN")" -ge "$N_CONV" ]; then
    echo "▶ [${B}] 답변 건너뜀"
  else
    stage "[${B}] 2/3 답변"
    mkdir -p "$(dirname "$GEN")"
    COST_DIR=$(cost_dir beam-${b}-light) COST_STAGE=answer COST_BENCH=beam COST_SETTING=${b}-beamprompt \
    $MAIN $E/beam/answer_beam.py --results "$ING" --out "$GEN" \
        --max-workers "$W_ARM" || { echo "✗ [${B}] 답변 실패"; exit 1; }
  fi
  [ "$(n_lines "$GEN")" -ge "$N_CONV" ] || { echo "✗ [${B}] 답변이 $(n_lines "$GEN")/${N_CONV} 대화"; exit 1; }
  write_manifest "$(dirname "$GEN")" light beam "${b}-beamprompt" answer

  JUD="results/light/beam-judge-${b}-light"
  if [ -d "$JUD" ] && [ "$(find "$JUD" -name '*.json' | wc -l)" -ge "$N_CONV" ]; then
    echo "▶ [${B}] 채점 건너뜀"
  else
    stage "[${B}] 3/3 채점"
    COST_DIR=$(cost_dir beam-${b}-light) COST_STAGE=judge COST_BENCH=beam COST_SETTING=${b}-beamprompt \
    $MAIN $E/beam/judge_beam.py --results "$GEN" --out-dir "$JUD" \
        --max-workers "$W_ARM" || { echo "✗ [${B}] 채점 실패"; exit 1; }
  fi
  n_jud=$(find "$JUD" -name '*.json' 2>/dev/null | wc -l)
  [ "$n_jud" -ge "$N_CONV" ] || { echo "✗ [${B}] 채점이 ${n_jud}/${N_CONV} 대화"; exit 1; }
  write_manifest "$JUD" light beam "${b}-beamprompt" judge
done

stage "완료"
echo "━━━ 전부 완료 ($(hms $(( $(date +%s) - START )))) ━━━"
for B in $BUCKETS; do
  b=$(echo "$B" | tr "A-Z" "a-z")
  echo "  [${B}] results/light/beam-judge-${b}-light"
done
