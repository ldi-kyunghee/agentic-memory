#!/usr/bin/env bash
# 개요 탭 빈 칸 채우기 (값싼 것만 — 투입 재실행 없음).
#
#   1) classic 20u 비용 backfill (trace + 저장된 답변에서 복원. GPU 불필요, tokenize 만)
#   2) v3   BEAM 100K · mem0 하네스 프롬프트 답변+채점   (투입 재사용)
#   3) LIGHT BEAM 100K · mem0 하네스 프롬프트 답변+채점  (투입 재사용)
#   4) LIGHT BEAM 500K · mem0 하네스 프롬프트 답변+채점  (투입 재사용)
#
# 검색 결과는 프롬프트와 무관하게 동일하므로 답변 생성부터만 다시 돎.
# 완주하면 100K 는 두 프롬프트 자리 모두 3시스템이 됨.
#
#   tmux new-session -d -s cheapfill "bash -lc 'cd ~/projects/agentic-memory && bash scripts/fill/run_cheap_fill.sh 2>&1 | tee -a /tmp/cheapfill.log'"
set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"
source "$ROOT/scripts/lib/manifest.sh"
E=eval/mem0-classic-oss
STAGE_FILE=/tmp/cheapfill.stage
START=$(date +%s)

export PYTHONUNBUFFERED=1
export OPENAI_BASE_URL=http://localhost:8002/v1
export OPENAI_API_KEY=${OPENAI_API_KEY:-dummy}
export ANSWER_MODEL=${ANSWER_MODEL:-openai/gpt-oss-120b}
export JUDGE_MODEL=${JUDGE_MODEL:-openai/gpt-oss-120b}
export ANSWER_REASONING_EFFORT=${ANSWER_REASONING_EFFORT:-high}
export JUDGE_REASONING_EFFORT=${JUDGE_REASONING_EFFORT:-high}
# 이 스크립트의 존재 이유가 mem0 하네스 프롬프트 자리를 채우는 것임. 명시로 박음.
export BEAM_ANSWER_PROMPT=mem0

W_ARM=${W_ARM:-16}
EXPECT_LLM_LEN=${EXPECT_LLM_LEN:-65536}
EXPECT_EMB_LEN=${EXPECT_EMB_LEN:-32768}

if [ -z "${COST_OFF:-}" ]; then
  export PYTHONPATH="$ROOT/src/cost${PYTHONPATH:+:$PYTHONPATH}"
fi
cost_dir() { [ -n "${COST_OFF:-}" ] && { echo ""; return; }; echo "$ROOT/cost/$1"; }

MAIN="uv run python"
stage() { echo "$1" > "$STAGE_FILE"; echo; echo "▶ $1"; }
hms() { local s=$1; printf "%dh%02dm" $((s/3600)) $((s%3600/60)); }
n_lines() { [ -s "$1" ] && grep -c . "$1" || echo 0; }

# ── 0. 사전 확인: 우리 인스턴스인가 (8000 은 남의 것, max_model_len 으로 가름) ──
stage "0 사전 확인"
for spec in "8002:$EXPECT_LLM_LEN" "8001:$EXPECT_EMB_LEN"; do
  port=${spec%%:*}; want=${spec##*:}
  got=$(curl -s "http://localhost:${port}/v1/models" | $MAIN -c \
    "import json,sys; print(json.load(sys.stdin)['data'][0].get('max_model_len'))" 2>/dev/null)
  [ "$got" = "$want" ] || { echo "✗ ${port} max_model_len=${got} (기대 ${want})"; exit 1; }
  echo "  ✓ ${port} max_model_len=${got}"
done

# ── 1. classic 20u 비용 backfill ──────────────────────────────────────────
CD_HM=$(cost_dir halumem-20u-mem0-classic)
if [ -n "$CD_HM" ]; then
  if ls "$CD_HM"/ingest__*.json >/dev/null 2>&1; then
    echo "▶ 1a 건너뜀 (backfill 산출물 있음)"
  else
    stage "1a classic 20u 투입 비용 backfill (trace 20유저)"
    $MAIN src/cost/backfill_trace.py \
        --trace-dir traces/mem0-classic-oss/oss120b20 --out "$CD_HM" \
        --system mem0-classic --benchmark halumem --setting 20u \
        || { echo "✗ 투입 backfill 실패"; exit 1; }
  fi
  if ls "$CD_HM"/answer__*.json >/dev/null 2>&1; then
    echo "▶ 1b 건너뜀"
  else
    stage "1b classic 20u 답변 비용 backfill (저장된 context+답변 재조립)"
    $MAIN src/cost/backfill_answer.py --benchmark halumem \
        --ingest results/mem0-classic-oss/genoss120/oss120b20.jsonl \
        --out "$CD_HM" --system mem0-classic --setting 20u \
        || { echo "✗ 답변 backfill 실패"; exit 1; }
  fi
fi

# ── 2~4. mem0 하네스 프롬프트 답변+채점 (투입 재사용) ─────────────────────
# fill <표시명> <투입jsonl> <대화수> <GEN> <JUD> <cost디렉토리> <cost시스템>
fill() {
  local name=$1 ing=$2 nconv=$3 gen=$4 jud=$5 cdir=$6 csys=$7
  [ "$(n_lines "$ing")" -ge "$nconv" ] || { echo "✗ [$name] 투입이 $(n_lines "$ing")/$nconv"; exit 1; }

  if [ "$(n_lines "$gen")" -ge "$nconv" ]; then
    echo "▶ [$name] 답변 건너뜀"
  else
    stage "[$name] 답변 (mem0 하네스 프롬프트)"
    mkdir -p "$(dirname "$gen")"
    COST_DIR=$(cost_dir "$cdir") COST_STAGE=answer COST_BENCH=beam \
    COST_SETTING=${name##* } COST_SYSTEM=$csys \
    $MAIN $E/beam/answer_beam.py --results "$ing" --out "$gen" --max-workers "$W_ARM" \
        || { echo "✗ [$name] 답변 실패"; exit 1; }
  fi
  [ "$(n_lines "$gen")" -ge "$nconv" ] || { echo "✗ [$name] 답변이 $(n_lines "$gen")/$nconv"; exit 1; }
  write_manifest "$(dirname "$gen")" "$csys" beam "${name##* }-mem0prompt" answer

  if [ -d "$jud" ] && [ "$(find "$jud" -name '*.json' ! -name run.json | wc -l)" -ge "$nconv" ]; then
    echo "▶ [$name] 채점 건너뜀"
  else
    stage "[$name] 채점"
    COST_DIR=$(cost_dir "$cdir") COST_STAGE=judge COST_BENCH=beam \
    COST_SETTING=${name##* } COST_SYSTEM=$csys \
    $MAIN $E/beam/judge_beam.py --results "$gen" --out-dir "$jud" --max-workers "$W_ARM" \
        || { echo "✗ [$name] 채점 실패"; exit 1; }
  fi
  local nj; nj=$(find "$jud" -name '*.json' ! -name run.json 2>/dev/null | wc -l)
  [ "$nj" -ge "$nconv" ] || { echo "✗ [$name] 채점이 ${nj}/${nconv}"; exit 1; }
  write_manifest "$jud" "$csys" beam "${name##* }-mem0prompt" judge
}

fill "v3 100k" \
  results/mem0-classic-oss/beam-100k-v3/beam_eval_results.jsonl 20 \
  results/mem0-classic-oss/beam-gen-100k-v3-mem0prompt/answers.jsonl \
  results/mem0-classic-oss/beam-judge-oss120-100k-v3-mem0prompt \
  beam-100k-mem0prompt-mem0-v3 mem0-v3

fill "light 100k" \
  results/light/beam-100k-light/beam_eval_results.jsonl 20 \
  results/light/beam-gen-100k-light-mem0prompt/answers.jsonl \
  results/light/beam-judge-100k-light-mem0prompt \
  beam-100k-mem0prompt-light light

fill "light 500k" \
  results/light/beam-500k-light/beam_eval_results.jsonl 35 \
  results/light/beam-gen-500k-light-mem0prompt/answers.jsonl \
  results/light/beam-judge-500k-light-mem0prompt \
  beam-500k-mem0prompt-light light

stage "완료"
echo "━━━ 전부 완료 ($(hms $(( $(date +%s) - START )))) ━━━"
