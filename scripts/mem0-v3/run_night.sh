#!/usr/bin/env bash
# mem0 v3 야간 배치. 자는 동안 이어서 돌리는 용도임.
#
#   tmux new-session -d -s v3night "bash -lc 'cd ~/projects/agentic-memory && bash scripts/mem0-v3/run_night.sh 2>&1 | tee /tmp/v3night.log'"
#
# 순서
#   1. BEAM 100K 대화 1개 투입      -> v3/classic 청크 비율 측정 (BEAM 전량 여부를 가르는 근거)
#   2. Memora weekly 전량 투입       -> 첫 진짜 A 대 B 비교
#   3. Memora weekly 답변
#   4. Memora weekly 채점
#
# 단계마다 이미 끝난 것은 건너뜀. 실패하면 거기서 멈추고 뒤를 안 태움.
#
# ⚠ 투입은 v3 venv(eval/mem0-v3), 답변·채점은 본 venv 에서 돎.
#    답변·채점 스크립트는 mem0 를 안 부르므로 classic 과 같은 것을 그대로 씀 (레인 통제).
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"

# ---- 공통 env. 하나라도 빠지면 404 나 20개 검색으로 조용히 망가짐 ----
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
export JUDGE_REASONING_EFFORT=${JUDGE_REASONING_EFFORT:-high}

W_ING=${W_ING:-8}      # TP=1 한 장 기준. 벤치 무릎이 8~12 였음
W_ARM=${W_ARM:-6}      # 답변은 high effort 라 KV 를 많이 먹음. 보수적으로
EXPECT_MAX_LEN=${EXPECT_MAX_LEN:-32768}

V3="uv run --project eval/mem0-v3 python"
MAIN="uv run python"
E=eval/mem0-classic-oss

echo "━━━ mem0 v3 야간 배치 ━━━"
echo "  LLM=${OPENAI_BASE_URL} · 임베더=${MEM0_EMBED_BASE_URL}"
echo "  워커 투입 ${W_ING} / 팔 ${W_ARM} · fastembed 캐시 ${FASTEMBED_CACHE_PATH}"

# ---- 사전 확인 ----
body=$(curl -sf --max-time 10 "${OPENAI_BASE_URL}/models") || { echo "✗ ${OPENAI_BASE_URL} 응답 없음"; exit 1; }
len=$(printf '%s' "$body" | python3 -c "
import json,sys
d={x['id']: x.get('max_model_len') for x in json.load(sys.stdin)['data']}
print(d.get('${MEM0_LLM_MODEL}', 'NONE'))")
if [ "$len" != "$EXPECT_MAX_LEN" ]; then
  echo "✗ ${MEM0_LLM_MODEL} 의 max_model_len=${len} (기대 ${EXPECT_MAX_LEN}). 남의 인스턴스일 수 있음"
  echo "  ss -ltnp | grep -E ':8000|:8002'"
  exit 1
fi
echo "✓ LLM ${MEM0_LLM_MODEL} max_model_len=${len}"
curl -sf --max-time 10 "${MEM0_EMBED_BASE_URL}/models" >/dev/null || { echo "✗ 임베더 응답 없음"; exit 1; }
echo "✓ 임베더 확인"
mkdir -p "$FASTEMBED_CACHE_PATH"
touch "$FASTEMBED_CACHE_PATH/.probe" && rm -f "$FASTEMBED_CACHE_PATH/.probe" || { echo "✗ fastembed 캐시에 못 씀"; exit 1; }
echo "✓ fastembed 캐시 쓰기 가능"

n_lines() { [ -s "$1" ] && grep -c . "$1" || echo 0; }

# ---- 1. BEAM 비율 측정 (100K 대화 1개) ----
BEAM_OUT="results/mem0-classic-oss/beam-100k-v3-smoke/beam_eval_results.jsonl"
if [ "$(n_lines "$BEAM_OUT")" -ge 1 ]; then
  echo "▶ 1/4 BEAM 비율 측정 건너뜀 (이미 있음)"
else
  echo "▶ 1/4 BEAM 100K 대화 1개 투입 (v3/classic 청크 비율용)"
  $V3 $E/beam/ingest_beam.py --chats BEAM/chats/100K --version 100k-v3-smoke \
      --top-k 200 --conversations 0-0 --max-workers 1
fi
# ⚠ 완주 검사. 처음 판에는 이게 없어서 빈 산출물이 나왔는데도 '완료' 가 떴음.
#   (임베더 4096 초과로 대화가 통째로 실패했는데 그냥 2단계로 넘어감)
if [ "$(n_lines "$BEAM_OUT")" -lt 1 ]; then
  echo "✗ BEAM 산출물이 비어 있음. 실패 로그:"
  ls results/mem0-classic-oss/beam-100k-v3-smoke/tmp/*error* 2>/dev/null | head -3
  exit 1
fi
echo "✓ BEAM 비율 측정 완주"

# ---- 2. Memora weekly 투입 ----
MEM_ING="results/mem0-classic-oss/memora-weekly-v3-oss120b/memora_eval_results.jsonl"
N_PERSONA=$(find Memora/data/weekly -maxdepth 1 -mindepth 1 -type d | wc -l)
if [ "$(n_lines "$MEM_ING")" -ge "$N_PERSONA" ]; then
  echo "▶ 2/4 Memora weekly 투입 건너뜀 (완주본 있음)"
else
  echo "▶ 2/4 Memora weekly 전량 투입 (페르소나 ${N_PERSONA})"
  $V3 $E/memora/ingest_memora.py --data Memora/data/weekly \
      --version weekly-v3-oss120b --top-k 50 --max-workers "$W_ING"
fi
HAVE=$(n_lines "$MEM_ING")
if [ "$HAVE" -lt "$N_PERSONA" ]; then
  echo "✗ Memora 투입이 ${HAVE}/${N_PERSONA} 페르소나뿐임. 답변으로 안 넘어감"
  exit 1
fi
echo "✓ Memora weekly 투입 완주 (${HAVE}/${N_PERSONA})"

# ---- 3. 답변 (본 venv. mem0 를 안 부름) ----
GEN="results/mem0-classic-oss/memora-gen-weekly-v3/answers.jsonl"
n_answered() {
  [ -s "$1" ] || { echo 0; return; }
  python3 -c "
import json
n=0
for l in open('$1',encoding='utf-8'):
    if not l.strip(): continue
    for q in json.loads(l)['questions']:
        if ((q.get('answer') or {}).get('system_response') or '').strip(): n+=1
print(n)" 2>/dev/null || echo 0
}
N_Q=$(python3 -c "
import json
print(sum(len(json.loads(l)['questions']) for l in open('$MEM_ING',encoding='utf-8') if l.strip()))")
if [ "$(n_answered "$GEN")" -ge "$N_Q" ]; then
  echo "▶ 3/4 답변 건너뜀 ($(n_answered "$GEN")/${N_Q})"
else
  echo "▶ 3/4 답변 생성 (${N_Q}문항)"
  MEM0_IMPL=classic $MAIN $E/memora/answer_memora.py --results "$MEM_ING" --out "$GEN" --max-workers "$W_ARM"
fi
A=$(n_answered "$GEN")
MIN_A=$(( N_Q * 97 / 100 ))
[ "$A" -ge "$MIN_A" ] || { echo "✗ 답변 ${A}/${N_Q} (하한 ${MIN_A})"; exit 1; }
if [ "$A" -lt "$N_Q" ]; then echo "⚠ 빈 답변 $(( N_Q - A ))건 (0점 처리됨)"; fi

# ---- 4. 채점 ----
JUD="results/mem0-classic-oss/memora-judge-weekly-v3"
if [ -d "$JUD" ] && [ "$(ls "$JUD"/*.json 2>/dev/null | wc -l)" -ge "$N_PERSONA" ]; then
  echo "▶ 4/4 채점 건너뜀"
else
  echo "▶ 4/4 채점"
  MEM0_IMPL=classic $MAIN $E/memora/judge_memora.py --results "$GEN" --out-dir "$JUD" --max-workers "$W_ARM"
fi

echo
echo "━━━ 완료 ━━━"
echo "  BEAM 비율용 : results/mem0-classic-oss/beam-100k-v3-smoke/"
echo "  Memora 투입 : $MEM_ING"
echo "  Memora 답변 : $GEN"
echo "  Memora 채점 : $JUD"
