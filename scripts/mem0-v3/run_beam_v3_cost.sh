#!/usr/bin/env bash
# mem0 v3 · BEAM 100K 투입을 **비용 계측과 trace 를 켜고** 다시 돌린다.
#
#   tmux new-session -d -s beamcost "bash -lc 'cd ~/projects/agentic-memory && bash scripts/mem0-v3/run_beam_v3_cost.sh 2>&1 | tee -a /tmp/beamcost.log'"
#
# 왜: 2026-08-26 v3 본실험을 --trace 없이 돌려서 v3 쪽 비용(호출 수·토큰·컨텍스트)을
# 되살릴 방법이 없음. classic 은 trace 에서 전부 복원됐는데 비교 상대가 비어 있음.
# BEAM 100K 하나만 다시 돌려 최소 한 세팅에서 정면 비교를 만든다.
#
# ⚠ 기존 산출물을 덮지 않는다. --version 을 100k-v3-cost 로 따로 쓴다.
#    (같은 버전으로 돌리면 tmp 캐시 때문에 20대화를 전부 건너뛰어 아무것도 안 재고 끝난다.
#     캐시를 지우면 이번엔 기존 결과가 날아간다. 그래서 새 버전이 유일하게 안전한 길이다.)
set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"
E=eval/mem0-classic-oss
R=results/mem0-classic-oss
V=100k-v3-cost
START=$(date +%s)

export PYTHONUNBUFFERED=1
export MEM0_IMPL=v3
export FASTEMBED_CACHE_PATH=${FASTEMBED_CACHE_PATH:-$ROOT/.cache/fastembed}
export OPENAI_BASE_URL=${BEAMCOST_BASE_URL:-http://localhost:8002/v1}
export MEM0_EMBED_BASE_URL=${BEAMCOST_EMBED_URL:-http://localhost:8001/v1}
export MEM0_LLM_MODEL=${MEM0_LLM_MODEL:-openai/gpt-oss-120b}
export MEM0_EMBED_MODEL=${MEM0_EMBED_MODEL:-Qwen/Qwen3-Embedding-4B}
export MEM0_EMBED_DIMS=${MEM0_EMBED_DIMS:-2560}
export QDRANT_HOST=${QDRANT_HOST:-localhost}
export QDRANT_PORT=${QDRANT_PORT:-6333}
export OPENAI_API_KEY=${OPENAI_API_KEY:-dummy}

# hm20 이 같은 서버를 쓰는 중이라 낮게 잡는다. 20대화라 8이면 3라운드.
W_ING=${W_ING:-8}
EXPECT_LLM_LEN=${EXPECT_LLM_LEN:-65536}
EXPECT_EMB_LEN=${EXPECT_EMB_LEN:-32768}

# 비용 계측
export PYTHONPATH="$ROOT/src/cost${PYTHONPATH:+:$PYTHONPATH}"
export COST_DIR="$ROOT/cost/beam-100k-mem0-v3"
export COST_STAGE=ingest
export COST_SYSTEM=mem0-v3
export COST_BENCH=beam
export COST_SETTING=100k

hms() { local s=$1; printf "%dh%02dm" $((s/3600)) $((s%3600/60)); }
n_lines() { [ -s "$1" ] && grep -c . "$1" || echo 0; }

echo "━━━ mem0 v3 · BEAM 100K 투입 (비용 계측 + trace) ━━━"
echo "  버전 ${V} · 워커 ${W_ING} · 계측 ${COST_DIR}"

# ---- 사전 확인 ----
len=$(curl -sf --max-time 10 "${OPENAI_BASE_URL}/models" | python3 -c "
import json,sys
print({x['id']: x.get('max_model_len') for x in json.load(sys.stdin)['data']}.get('${MEM0_LLM_MODEL}','NONE'))") || { echo "✗ LLM 응답 없음"; exit 1; }
[ "$len" = "$EXPECT_LLM_LEN" ] || { echo "✗ LLM max_model_len=${len} (기대 ${EXPECT_LLM_LEN})"; exit 1; }
elen=$(curl -sf --max-time 10 "${MEM0_EMBED_BASE_URL}/models" | python3 -c "
import json,sys
print({x['id']: x.get('max_model_len') for x in json.load(sys.stdin)['data']}.get('${MEM0_EMBED_MODEL}','NONE'))")
[ "$elen" = "$EXPECT_EMB_LEN" ] || { echo "✗ 임베더 max_model_len=${elen} (기대 ${EXPECT_EMB_LEN})"; exit 1; }
echo "✓ LLM ${len} · 임베더 ${elen}"

# ⚠ 기존 v3 산출물을 건드리지 않는지 확인. 같은 경로면 즉시 멈춘다.
OLD="$R/beam-100k-v3/beam_eval_results.jsonl"
NEW="$R/beam-$V/beam_eval_results.jsonl"
[ "$OLD" != "$NEW" ] || { echo "✗ 새 버전이 기존 경로와 같음"; exit 1; }
echo "✓ 기존 산출물 보존 ($(n_lines "$OLD"))대화 · 새 경로 ${NEW}"

mkdir -p "$FASTEMBED_CACHE_PATH" && touch "$FASTEMBED_CACHE_PATH/.p" && rm -f "$FASTEMBED_CACHE_PATH/.p" \
  || { echo "✗ fastembed 캐시에 못 씀"; exit 1; }

uv run --project eval/mem0-v3 python $E/beam/ingest_beam.py \
    --chats BEAM/chats/100K --version "$V" \
    --top-k 200 --max-workers "$W_ING" --trace || { echo "✗ 투입 실패"; exit 1; }

n=$(n_lines "$NEW")
[ "$n" -ge 20 ] || { echo "✗ 투입이 ${n}/20 대화"; exit 1; }

echo
echo "━━━ 완료 ($(hms $(( $(date +%s) - START )))) · ${n}/20 대화 ━━━"
echo "  산출물 $NEW"
echo "  계측   $COST_DIR"
echo
uv run python src/cost/report.py --dir "$COST_DIR" || true
