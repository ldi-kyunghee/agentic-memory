#!/usr/bin/env bash
# HaluMem classic 20유저 (백본 gpt-oss-120b). 팔 A 를 v3 와 같은 규모로 맞추기 위한 런임.
#
#   tmux new-session -d -s hm20 "bash -lc 'cd ~/projects/agentic-memory && W_ING=20 W_ARM=24 bash scripts/mem0-v3/run_halumem_classic20.sh 2>&1 | tee -a /tmp/hm20.log'"
#
# 왜 필요한가: 백본이 gpt-oss-120b 인 classic HaluMem 런은 4유저(oss120b4)뿐이라
# v3(20유저)와 공통 4유저로만 비교할 수 있었음. 20유저로 맞추면 전량 비교가 됨.
#
# 산출물 경로는 기존 oss120 레인 규약을 따름 (runs.yaml 의 {run} 템플릿에 그대로 꽂힘):
#   투입   results/mem0-classic-oss/memzero-oss-oss120b20/memzero-oss_eval_results.jsonl
#   답변   results/mem0-classic-oss/genoss120/oss120b20.jsonl
#   채점   results/mem0-classic-oss/judge-oss120-genoss120-oss120b20/judge/
set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"
E=eval/mem0-classic-oss
R=results/mem0-classic-oss
V=oss120b20
STAGE_FILE=/tmp/hm20.stage
START=$(date +%s)

export PYTHONUNBUFFERED=1
export MEM0_IMPL=classic
export OPENAI_BASE_URL=${HM20_BASE_URL:-http://localhost:8002/v1}
export MEM0_EMBED_BASE_URL=${HM20_EMBED_URL:-http://localhost:8001/v1}
export MEM0_LLM_MODEL=${MEM0_LLM_MODEL:-openai/gpt-oss-120b}
export MEM0_EMBED_MODEL=${MEM0_EMBED_MODEL:-Qwen/Qwen3-Embedding-4B}
export MEM0_EMBED_DIMS=${MEM0_EMBED_DIMS:-2560}
export QDRANT_HOST=${QDRANT_HOST:-localhost}
export QDRANT_PORT=${QDRANT_PORT:-6333}
export OPENAI_API_KEY=${OPENAI_API_KEY:-dummy}
export ANSWER_MODEL=${ANSWER_MODEL:-openai/gpt-oss-120b}
export JUDGE_MODEL=${JUDGE_MODEL:-openai/gpt-oss-120b}
export ANSWER_REASONING_EFFORT=${ANSWER_REASONING_EFFORT:-high}
export JUDGE_REASONING_EFFORT=${JUDGE_REASONING_EFFORT:-high}
# ⚠ agent LLM 의 effort 는 기존 그리드와 통제를 맞춰 **설정하지 않음**(모델 기본값 medium).
#   v3 런도 "reasoning effort override: 없음" 으로 돌았음.

W_ING=${W_ING:-20}
W_ARM=${W_ARM:-24}
EXPECT_LLM_LEN=${EXPECT_LLM_LEN:-65536}
EXPECT_EMB_LEN=${EXPECT_EMB_LEN:-32768}

# ---- 비용 계측 (평가 코드 무수정, sitecustomize 방식) ----
# 단계마다 COST_STAGE 를 바꿔 끼운다. 산출물은 cost/{설정}/{stage}__{pid}.json 이고
# src/cost/report.py 가 합친다. 끄려면 COST_OFF=1 을 준다.
if [ -z "${COST_OFF:-}" ]; then
  export PYTHONPATH="$ROOT/src/cost${PYTHONPATH:+:$PYTHONPATH}"
  export COST_SYSTEM="${COST_SYSTEM:-mem0-classic}"
fi
cost_dir() { [ -n "${COST_OFF:-}" ] && { echo ""; return; }; echo "$ROOT/cost/$1"; }

MAIN="uv run python"

stage() { echo "$1" > "$STAGE_FILE"; echo; echo "▶ $1"; }
hms() { local s=$1; printf "%dh%02dm" $((s/3600)) $((s%3600/60)); }
n_lines() { [ -s "$1" ] && grep -c . "$1" || echo 0; }

TICK=${TICK:-600}
LOG=${LOG:-/tmp/hm20.log}
ticker() {
  local parent=$1
  while kill -0 "$parent" 2>/dev/null; do
    sleep "$TICK"
    kill -0 "$parent" 2>/dev/null || break
    local st el p
    st=$(cat "$STAGE_FILE" 2>/dev/null || echo "?")
    el=$(( $(date +%s) - START ))
    p=$(tail -c 200000 "$LOG" 2>/dev/null | grep -oE "[0-9]+/[0-9]+ sessions" | tail -1)
    echo "  [진행] $(hms $el) 경과 · ${st}${p:+ · 최근 $p}"
  done
}
ticker $$ & TICKER=$!
cleanup() { kill "$TICKER" 2>/dev/null; wait "$TICKER" 2>/dev/null; }
trap cleanup EXIT INT TERM

echo "━━━ HaluMem classic 20유저 (${V}) ━━━"
echo "  LLM=${OPENAI_BASE_URL} · 임베더=${MEM0_EMBED_BASE_URL}"
echo "  워커 투입 ${W_ING} / 팔 ${W_ARM}"

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

# ============ 1. 투입 ============
ING="$R/memzero-oss-${V}/memzero-oss_eval_results.jsonl"
if [ "$(n_lines "$ING")" -ge 20 ]; then
  echo "▶ 1/3 투입 건너뜀 (20유저 완주본 있음)"
else
  stage "1/3 투입 (20유저)"
  COST_DIR=$(cost_dir halumem-20u-mem0-classic) COST_STAGE=ingest COST_BENCH=halumem COST_SETTING=20u \
  $MAIN $E/eval_memzero_oss.py --data dataset/HaluMem-Medium.jsonl --version "$V" \
      --top-k 20 --max-workers "$W_ING" --trace || { echo "✗ 투입 실패"; exit 1; }
fi
[ "$(n_lines "$ING")" -ge 20 ] || { echo "✗ 투입이 $(n_lines "$ING")/20 유저"; exit 1; }

# ============ 2. 답변 ============
GEN="$R/genoss120/${V}.jsonl"
if [ "$(n_lines "$GEN")" -ge 20 ]; then
  echo "▶ 2/3 답변 건너뜀"
else
  stage "2/3 답변 (QA 3,467)"
  mkdir -p "$(dirname "$GEN")"
  COST_DIR=$(cost_dir halumem-20u-mem0-classic) COST_STAGE=answer COST_BENCH=halumem COST_SETTING=20u \
  $MAIN $E/gen_answers.py --results "$ING" --out "$GEN" \
      --max-workers "$W_ARM" || { echo "✗ 답변 실패"; exit 1; }
fi
[ "$(n_lines "$GEN")" -ge 20 ] || { echo "✗ 답변이 $(n_lines "$GEN")/20 유저"; exit 1; }

# ============ 3. 채점 ============
JUD="$R/judge-oss120-genoss120-${V}"
if [ -d "$JUD/judge" ] && [ "$(find "$JUD/judge" -name '*.json' | wc -l)" -ge 20 ]; then
  echo "▶ 3/3 채점 건너뜀"
else
  stage "3/3 채점 (기준 18,415)"
  COST_DIR=$(cost_dir halumem-20u-mem0-classic) COST_STAGE=judge COST_BENCH=halumem COST_SETTING=20u \
  $MAIN $E/judge.py --results "$GEN" --out-dir "$JUD" \
      --max-workers "$W_ARM" || { echo "✗ 채점 실패"; exit 1; }
fi
n_jud=$(find "$JUD/judge" -name '*.json' 2>/dev/null | wc -l)
[ "$n_jud" -ge 20 ] || { echo "✗ 채점이 ${n_jud}/20 유저"; exit 1; }

stage "완료"
echo "━━━ 전부 완료 ($(hms $(( $(date +%s) - START )))) ━━━"
echo "  투입 $ING"
echo "  답변 $GEN"
echo "  채점 $JUD/judge"
echo
echo "  팔 비교:"
echo "    uv run python src/mem0-v3/compare_halumem.py \\"
echo "        --arm classic=$JUD/judge \\"
echo "        --arm v3=$R/memzero-oss-v3/judge-v3/judge"
