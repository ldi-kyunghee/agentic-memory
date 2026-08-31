#!/usr/bin/env bash
# mem0 v3 · BEAM 500K 전체: 투입 1번 → 답변·채점 2벌 (BEAM 공식 / mem0 하네스 프롬프트).
# 끝나면 BEAM 이 100K·500K × 두 프롬프트 × 3시스템 완전 격자가 됨.
#
#   tmux new-session -d -s v3beam500k "bash -lc 'cd ~/projects/agentic-memory && bash scripts/fill/run_v3_beam500k.sh 2>&1 | tee -a /tmp/v3beam500k.log'"
#
# 예상 (서버 단독, 투입 20 / 팔 24): 투입 ~15h + 답변·채점 2벌 ~12h ≈ 27~30h.
# 실측 근거: v3 100K 투입 4.0h(2,866청크·워커10) × 청크 6.6배 ÷ 워커 2배,
#            LIGHT 500K 답변+채점 1벌 6.8h(워커16).
set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"
source "$ROOT/scripts/lib/manifest.sh"
E=eval/mem0-classic-oss
R=results/mem0-classic-oss
STAGE_FILE=/tmp/v3beam500k.stage
START=$(date +%s)

export PYTHONUNBUFFERED=1
export MEM0_IMPL=v3
export FASTEMBED_CACHE_PATH=${FASTEMBED_CACHE_PATH:-$ROOT/.cache/fastembed}
export OPENAI_BASE_URL=http://localhost:8002/v1
export MEM0_EMBED_BASE_URL=http://localhost:8001/v1
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
# BEAM_ANSWER_PROMPT 는 기본값에 절대 안 기댐 — 답변 단계마다 명시함 (2026-08-26 부호 뒤집힘 사고)

# 서버 단독 사용 전제 (2026-08-31 사용자 확인). 공유로 돌아가면 W_ING=10 W_ARM=12 로 내림
W_ING=${W_ING:-20}
W_ARM=${W_ARM:-24}
EXPECT_LLM_LEN=${EXPECT_LLM_LEN:-65536}
EXPECT_EMB_LEN=${EXPECT_EMB_LEN:-32768}

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

TICK=${TICK:-600}
LOG=${LOG:-/tmp/v3beam500k.log}
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

echo "━━━ mem0 v3 · BEAM 500K (투입 1 + 프롬프트 2벌) ━━━"
echo "  LLM=${OPENAI_BASE_URL} · 임베더=${MEM0_EMBED_BASE_URL} · 워커 투입 ${W_ING} / 팔 ${W_ARM}"

# ---- 0. 사전 확인 ----
stage "0 사전 확인"
body=$(curl -sf --max-time 10 "${OPENAI_BASE_URL}/models") || { echo "✗ LLM 응답 없음"; exit 1; }
len=$(printf '%s' "$body" | python3 -c "
import json,sys
print({x['id']: x.get('max_model_len') for x in json.load(sys.stdin)['data']}.get('${MEM0_LLM_MODEL}','NONE'))")
[ "$len" = "$EXPECT_LLM_LEN" ] || { echo "✗ LLM max_model_len=${len} (기대 ${EXPECT_LLM_LEN})"; exit 1; }
elen=$(curl -sf --max-time 10 "${MEM0_EMBED_BASE_URL}/models" | python3 -c "
import json,sys
print({x['id']: x.get('max_model_len') for x in json.load(sys.stdin)['data']}.get('${MEM0_EMBED_MODEL}','NONE'))")
[ "$elen" = "$EXPECT_EMB_LEN" ] || { echo "✗ 임베더 max_model_len=${elen} (기대 ${EXPECT_EMB_LEN})"; exit 1; }
echo "✓ LLM ${len} · 임베더 ${elen}"
mkdir -p "$FASTEMBED_CACHE_PATH"
# v3 3신호(임베딩+BM25+엔티티) 생존 검사. 조용히 퇴화하면 v3 가 아니라 다른 것을 돌리게 됨
$V3 eval/mem0-v3/verify_v3.py || { echo "✗ verify_v3 실패"; exit 1; }

# ---- 1. 투입 (프롬프트 무관, 1번만) ----
ING="$R/beam-500k-v3/beam_eval_results.jsonl"
TMPD="$R/beam-500k-v3/tmp"
# ⚠ 부분 실패 후 재시도 시 잔재 history db 가 get_last_messages 를 오염시킴 (2026-08-25 사고).
#   완주 json 이 없는 대화의 history db 는 지우고 시작함
if [ -d "$TMPD" ]; then
  for h in "$TMPD"/history_*.db; do
    [ -e "$h" ] || continue
    key=$(basename "$h" .db); key=${key#history_}
    [ -s "$TMPD/${key}.json" ] || { echo "  잔재 정리: $(basename "$h")"; rm -f "$h"; }
  done
fi
if [ "$(n_lines "$ING")" -ge 35 ]; then
  echo "▶ 1/5 투입 건너뜀 (35대화 완주본 있음)"
else
  stage "1/5 투입 (대화 35 · 청크 19,029 · ~15h)"
  COST_DIR=$(cost_dir beam-500k-mem0-v3) COST_STAGE=ingest COST_BENCH=beam COST_SETTING=500k \
  $V3 $E/beam/ingest_beam.py --chats BEAM/chats/500K --version 500k-v3 \
      --top-k 200 --max-workers "$W_ING" --trace || { echo "✗ 투입 실패"; exit 1; }
fi
[ "$(n_lines "$ING")" -ge 35 ] || { echo "✗ 투입이 $(n_lines "$ING")/35 대화"; exit 1; }
write_manifest "$(dirname "$ING")" mem0-v3 beam 500k-beamprompt ingest

# ---- 2~5. 답변·채점 2벌 ----
# lane <이름> <프롬프트> <GEN> <JUD> <cost디렉토리> <cost세팅>
lane() {
  local name=$1 prompt=$2 gen=$3 jud=$4 cdir=$5 cset=$6 n_stage=$7
  if [ "$(n_lines "$gen")" -ge 35 ]; then
    echo "▶ ${n_stage} [${name}] 답변 건너뜀"
  else
    stage "${n_stage} [${name}] 답변 (문항 700 × cutoff 4)"
    mkdir -p "$(dirname "$gen")"
    BEAM_ANSWER_PROMPT=$prompt \
    COST_DIR=$(cost_dir "$cdir") COST_STAGE=answer COST_BENCH=beam COST_SETTING=$cset \
    MEM0_IMPL=classic $MAIN $E/beam/answer_beam.py --results "$ING" --out "$gen" \
        --max-workers "$W_ARM" || { echo "✗ [${name}] 답변 실패"; exit 1; }
  fi
  [ "$(n_lines "$gen")" -ge 35 ] || { echo "✗ [${name}] 답변이 $(n_lines "$gen")/35 대화"; exit 1; }
  write_manifest "$(dirname "$gen")" mem0-v3 beam "$cset" answer

  local nj
  nj=$(find "$jud" -name '*.json' ! -name run.json 2>/dev/null | wc -l)
  if [ "$nj" -ge 35 ]; then
    echo "▶ [${name}] 채점 건너뜀"
  else
    stage "[${name}] 채점 (레코드 2,800)"
    COST_DIR=$(cost_dir "$cdir") COST_STAGE=judge COST_BENCH=beam COST_SETTING=$cset \
    MEM0_IMPL=classic $MAIN $E/beam/judge_beam.py --results "$gen" --out-dir "$jud" \
        --max-workers "$W_ARM" || { echo "✗ [${name}] 채점 실패"; exit 1; }
  fi
  nj=$(find "$jud" -name '*.json' ! -name run.json 2>/dev/null | wc -l)
  [ "$nj" -ge 35 ] || { echo "✗ [${name}] 채점이 ${nj}/35 대화"; exit 1; }
  write_manifest "$jud" mem0-v3 beam "$cset" judge
}

lane "BEAM 공식" beam \
  "$R/beam-genoss120-500k-v3/answers.jsonl" \
  "$R/beam-judge-oss120-500k-v3" \
  beam-500k-mem0-v3 500k-beamprompt "2/5"

lane "mem0 하네스" mem0 \
  "$R/beam-genoss120-500k-v3-mem0prompt/answers.jsonl" \
  "$R/beam-judge-oss120-500k-v3-mem0prompt" \
  beam-500k-mem0prompt-mem0-v3 500k "4/5"

# ---- 판독 자료 재계산 (산출물 읽기 전용) ----
stage "5/5 판독 자료 재계산"
uv run --project src/web-dashboard python src/analysis/deep_probe.py --scale 20u \
    --out results/exports/deep-probe-halumem.json || echo "  (deep_probe 실패 — 판독 탭은 이전 값 유지)"
uv run --project eval/light python src/analysis/ability_validity.py \
    --out results/exports/ability-validity.json || echo "  (ability_validity 실패 — 판독 탭은 이전 값 유지)"

stage "완료"
echo "━━━ 전부 완료 ($(hms $(( $(date +%s) - START )))) ━━━"
