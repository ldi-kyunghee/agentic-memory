#!/usr/bin/env bash
# Memora 검색 예산(cutoff) 스윕.
#
#   bash scripts/memora/run_cutoff_sweep.sh monthly
#   bash scripts/memora/run_cutoff_sweep.sh quarterly 50,100,200,400,800
#
# Stage A 를 큰 k(SEARCH_K, 기본 800)로 한 번만 돌리고, 답변 단계에서 cutoff 마다 잘라
# 팔을 만듦. Stage A 재실행이 quarterly 기준 13.6시간이라 cutoff 마다 재투입할 수 없음.
#
# ⚠ 모델 env 를 여기서 한곳에 박아둠. .env 의 OPENAI_MODEL 은 HaluMem 백본(Qwen3-4B)이라
#   그대로 두면 8002(gpt-oss-120b)에 없는 모델을 불러 404 가 남. 실제로 한 번 겪음.
# ⚠ agent LLM 의 effort 는 기존 그리드와 통제를 맞춰 기본값(medium)으로 둠. answer/judge 만 high.
set -euo pipefail

PERIOD=${1:?"기간을 주세요: weekly | monthly | quarterly"}
CUTOFFS=${2:-50,100,200,400}
SEARCH_K=${SEARCH_K:-800}
W=${W:-4}
VER="${PERIOD}-k${SEARCH_K}"
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"

export PYTHONUNBUFFERED=1
export OPENAI_BASE_URL=${OPENAI_BASE_URL:-http://localhost:8002/v1}
export MEM0_LLM_MODEL=${MEM0_LLM_MODEL:-openai/gpt-oss-120b}
export ANSWER_MODEL=${ANSWER_MODEL:-openai/gpt-oss-120b}
export JUDGE_MODEL=${JUDGE_MODEL:-openai/gpt-oss-120b}
export ANSWER_REASONING_EFFORT=${ANSWER_REASONING_EFFORT:-high}
export JUDGE_REASONING_EFFORT=${JUDGE_REASONING_EFFORT:-high}

ING="results/mem0-classic-oss/memora-${VER}-oss120b/memora_eval_results.jsonl"

echo "━━━ Memora cutoff 스윕: ${PERIOD} ━━━"
echo "  검색 k=${SEARCH_K} · cutoff=${CUTOFFS} · 워커 ${W}"
echo "  agent=${MEM0_LLM_MODEL}(effort 기본) answer/judge=${ANSWER_MODEL}(high)"
echo "  base_url=${OPENAI_BASE_URL}"

# 모델이 실제로 떠 있는지 먼저 확인함. 몇 시간짜리 작업을 404 로 태우지 않기 위함
if ! curl -sf "${OPENAI_BASE_URL}/models" | grep -q "${MEM0_LLM_MODEL}"; then
  echo "✗ ${OPENAI_BASE_URL} 에 ${MEM0_LLM_MODEL} 이 없습니다. 서빙부터 확인하세요:"
  curl -sf "${OPENAI_BASE_URL}/models" || echo "  (응답 없음)"
  exit 1
fi
echo "✓ 모델 확인됨"

if [ -f "$ING" ]; then
  echo "▶ Stage A 건너뜀 (이미 있음): $ING"
else
  echo "▶ Stage A 투입 (k=${SEARCH_K})"
  uv run python eval/mem0-classic-oss/memora/ingest_memora.py \
    --data "Memora/data/${PERIOD}" --version "$VER" --top-k "$SEARCH_K" --max-workers "$W"
fi

for K in ${CUTOFFS//,/ }; do
  GEN="results/mem0-classic-oss/memora-gen-${PERIOD}-k${K}/answers.jsonl"
  JUD="results/mem0-classic-oss/memora-judge-${PERIOD}-k${K}"
  echo "▶ cutoff ${K}: 답변"
  uv run python eval/mem0-classic-oss/memora/answer_memora.py \
    --results "$ING" --out "$GEN" --cutoff "$K" --max-workers "$W"
  echo "▶ cutoff ${K}: 채점"
  uv run python eval/mem0-classic-oss/memora/judge_memora.py \
    --answers "$GEN" --out-dir "$JUD" --max-workers "$W"
done

echo "━━━ 완료 ━━━"
for K in ${CUTOFFS//,/ }; do
  echo "  cutoff ${K} -> results/mem0-classic-oss/memora-judge-${PERIOD}-k${K}"
done
