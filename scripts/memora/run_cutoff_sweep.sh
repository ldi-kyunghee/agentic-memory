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

# PERSONAS 를 주면 축소 실행임. 본 실행 산출물을 덮지 않도록 이름에 -smoke 를 붙이고,
# 완주 검사 기준도 지정한 페르소나 수로 바꿈. 러너를 고친 뒤에는 이걸로 먼저 통과시킴.
#   PERSONAS=academic_researcher bash scripts/memora/run_cutoff_sweep.sh weekly 50,100
PERSONAS=${PERSONAS:-}
SUFFIX=""
[ -n "$PERSONAS" ] && SUFFIX="-smoke"
# ⚠ ingest_memora.py 는 결과를 `results/mem0-classic-oss/memora-{version}/` 에 그대로 씀.
#   접미사를 붙여주지 않으므로 version 문자열에 -oss120b 까지 넣어야 기존 레인
#   (memora-weekly-oss120b …) 과 이름이 맞음. 여기서 어긋나면 투입이 끝난 뒤에야
#   경로를 못 찾고 죽음. 실제로 한 번 그럴 뻔했음.
VER="${PERIOD}-k${SEARCH_K}${SUFFIX}-oss120b"
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"

# ⚠ OPENAI_BASE_URL 은 `:-` 로 두면 안 됨. 셸이나 .env 에 8000 이 이미 잡혀 있으면
#   그 값이 이겨서 **다른 사용자(dania)의 vLLM 으로 나감.** 실제로 한 번 그랬음.
#   우리 서버는 8002 이고, 바꾸려면 MEMORA_BASE_URL 로 명시해야 함.
export PYTHONUNBUFFERED=1
export OPENAI_BASE_URL=${MEMORA_BASE_URL:-http://localhost:8002/v1}
export MEM0_EMBED_BASE_URL=${MEMORA_EMBED_URL:-http://localhost:8001/v1}
export MEM0_LLM_MODEL=${MEM0_LLM_MODEL:-openai/gpt-oss-120b}
export ANSWER_MODEL=${ANSWER_MODEL:-openai/gpt-oss-120b}
export JUDGE_MODEL=${JUDGE_MODEL:-openai/gpt-oss-120b}
export ANSWER_REASONING_EFFORT=${ANSWER_REASONING_EFFORT:-high}
export JUDGE_REASONING_EFFORT=${JUDGE_REASONING_EFFORT:-high}
# 우리 8002 는 --max-model-len 32768 로 띄움. 8000 (남의 것) 은 131072 라 이걸로 가름
EXPECT_MAX_LEN=${EXPECT_MAX_LEN:-32768}

ING="results/mem0-classic-oss/memora-${VER}/memora_eval_results.jsonl"

echo "━━━ Memora cutoff 스윕: ${PERIOD}${SUFFIX} ━━━"
echo "  검색 k=${SEARCH_K} · cutoff=${CUTOFFS} · 워커 ${W}${PERSONAS:+ · 페르소나 ${PERSONAS}}"
echo "  agent=${MEM0_LLM_MODEL}(effort 기본) answer/judge=${ANSWER_MODEL}(high)"
echo "  LLM=${OPENAI_BASE_URL} · 임베더=${MEM0_EMBED_BASE_URL}"

# 사전 확인. 몇 시간짜리 작업을 404 로 태우거나 남의 서버로 보내지 않기 위함.
# ⚠ 모델 이름만 보면 안 됨. 8000 의 남의 인스턴스도 같은 openai/gpt-oss-120b 를 서빙함.
#   max_model_len 까지 대조해야 우리 것인지 가려짐.
preflight() {
  local url="$1" want_model="$2" want_len="$3" what="$4"
  local body
  if ! body=$(curl -sf --max-time 10 "${url}/models"); then
    echo "✗ ${what}: ${url} 이 응답하지 않습니다. 서빙부터 띄우세요."
    return 1
  fi
  local got
  got=$(printf '%s' "$body" | python3 -c '
import json, sys
d = json.load(sys.stdin)["data"]
m = {x["id"]: x.get("max_model_len") for x in d}
print(json.dumps(m))
') || { echo "✗ ${what}: ${url} 응답을 해석하지 못했습니다."; return 1; }

  if ! printf '%s' "$got" | grep -q "\"${want_model}\""; then
    echo "✗ ${what}: ${url} 에 ${want_model} 이 없습니다. 떠 있는 것: ${got}"
    return 1
  fi
  # 길이는 비교값이 없어도 항상 읽어서 찍음. 임베더가 기본값(40960)으로 떠 있는 것을
  # 눈으로 잡을 수 있어야 함
  local len
  len=$(printf '%s' "$got" | python3 -c "import json,sys;print(json.load(sys.stdin).get('${want_model}'))")
  if [ -n "$want_len" ]; then
    if [ "$len" != "$want_len" ]; then
      echo "✗ ${what}: ${url} 의 ${want_model} 이 max_model_len=${len} 입니다 (우리 것은 ${want_len})."
      echo "  다른 사람의 인스턴스일 수 있습니다. 포트를 확인하세요:"
      echo "    ss -ltnp | grep -E ':8000|:8002'"
      return 1
    fi
  fi
  echo "✓ ${what}: ${url} · ${want_model} · max_model_len=${len}"
}

preflight "$OPENAI_BASE_URL" "$MEM0_LLM_MODEL" "$EXPECT_MAX_LEN" "LLM" || exit 1
preflight "$MEM0_EMBED_BASE_URL" "${MEM0_EMBED_MODEL:-Qwen/Qwen3-Embedding-4B}" "" "임베더" || exit 1

# 쓰려는 플래그가 실제로 있는지 --help 로 대조함. 인자 이름을 틀려서 네 번 데었음
# (judge 는 답변 파일을 --answers 가 아니라 --results 로 받음. 이름이 헷갈리게 돼 있음)
check_flags() {
  local script="$1"; shift
  local help
  help=$(uv run python "$script" --help 2>&1) || { echo "✗ ${script} --help 실패"; return 1; }
  local bad=0
  for f in "$@"; do
    printf '%s' "$help" | grep -q -- "$f" || { echo "✗ ${script##*/} 에 ${f} 없음"; bad=1; }
  done
  [ "$bad" = 0 ] && echo "✓ 인자 확인: ${script##*/} ($*)"
  return $bad
}
E=eval/mem0-classic-oss/memora
check_flags "$E/ingest_memora.py" --data --version --top-k --max-workers || exit 1
check_flags "$E/answer_memora.py" --results --out --cutoff --max-workers || exit 1
check_flags "$E/judge_memora.py"  --results --out-dir --max-workers || exit 1

# 산출물이 이만큼 있어야 완주한 것임. 축소 실행이면 지정한 페르소나 수만 기대함
if [ -n "$PERSONAS" ]; then
  # grep -c 는 매치 0 이면 exit 1 이라 set -e 에 걸림. awk 로 셈
  N_EXPECT=$(printf '%s' "$PERSONAS" | awk -F, '{n=0; for(i=1;i<=NF;i++) if($i!="") n++; print n}')
else
  N_EXPECT=$(find "Memora/data/${PERIOD}" -maxdepth 1 -mindepth 1 -type d | wc -l)
fi
n_lines() { [ -s "$1" ] && grep -c . "$1" || echo 0; }

# ⚠ `-f` 로만 보면 안 됨. 투입이 통째로 실패해도 0바이트 파일이 남아서 Stage A 를
#   건너뛰고 빈 입력으로 답변 팔을 돌게 됨. 실제로 한 번 그럴 뻔했음.
HAVE=$(n_lines "$ING")
if [ "$HAVE" -ge "$N_EXPECT" ] && [ "$N_EXPECT" -gt 0 ]; then
  echo "▶ Stage A 건너뜀 (완주본 있음, ${HAVE}/${N_EXPECT} 페르소나): $ING"
else
  if [ -e "$ING" ]; then
    echo "▶ 이전 산출물이 불완전함 (${HAVE}/${N_EXPECT} 페르소나). 지우고 다시 투입함"
    rm -rf "$(dirname "$ING")"
  fi
  echo "▶ Stage A 투입 (k=${SEARCH_K}, 페르소나 ${N_EXPECT})"
  uv run python eval/mem0-classic-oss/memora/ingest_memora.py \
    --data "Memora/data/${PERIOD}" --version "$VER" --top-k "$SEARCH_K" --max-workers "$W" \
    ${PERSONAS:+--personas "$PERSONAS"}
fi

# 투입이 끝났어도 페르소나가 모자라면 여기서 멈춤. 몇 시간짜리 답변·채점을
# 반쪽 입력으로 태우지 않기 위함임
HAVE=$(n_lines "$ING")
if [ "$HAVE" -lt "$N_EXPECT" ]; then
  echo "✗ Stage A 산출물이 ${HAVE}/${N_EXPECT} 페르소나뿐입니다. 답변 단계로 넘어가지 않습니다."
  echo "  실패 로그: $(dirname "$ING")/tmp/*_error.log"
  exit 1
fi
echo "✓ Stage A 완주 (${HAVE}/${N_EXPECT} 페르소나)"

# 문항 총수. 답변이 이만큼 채워져 있으면 그 팔은 다시 안 돌림
N_Q=$(python3 -c "
import json,sys
print(sum(len(json.loads(l)['questions']) for l in open('$ING',encoding='utf-8') if l.strip()))")
n_answered() {
  [ -s "$1" ] || { echo 0; return; }
  python3 -c "
import json,sys
n=0
for l in open('$1',encoding='utf-8'):
    if not l.strip(): continue
    for q in json.loads(l)['questions']:
        if ((q.get('answer') or {}).get('system_response') or '').strip(): n+=1
print(n)" 2>/dev/null || echo 0
}

for K in ${CUTOFFS//,/ }; do
  GEN="results/mem0-classic-oss/memora-gen-${PERIOD}${SUFFIX}-k${K}/answers.jsonl"
  JUD="results/mem0-classic-oss/memora-judge-${PERIOD}${SUFFIX}-k${K}"

  HAVE_A=$(n_answered "$GEN")
  if [ "$HAVE_A" -ge "$N_Q" ]; then
    echo "▶ cutoff ${K}: 답변 건너뜀 (${HAVE_A}/${N_Q} 완료)"
  else
    echo "▶ cutoff ${K}: 답변 (${HAVE_A}/${N_Q} 있음)"
    uv run python eval/mem0-classic-oss/memora/answer_memora.py \
      --results "$ING" --out "$GEN" --cutoff "$K" --max-workers "$W"
    HAVE_A=$(n_answered "$GEN")
    [ "$HAVE_A" -ge "$N_Q" ] || { echo "✗ cutoff ${K}: 답변이 ${HAVE_A}/${N_Q} 뿐입니다"; exit 1; }
  fi

  # ⚠ judge 는 답변 파일을 --results 로 받음 (--answers 아님). 이름이 헷갈리게 돼 있음
  echo "▶ cutoff ${K}: 채점"
  uv run python eval/mem0-classic-oss/memora/judge_memora.py \
    --results "$GEN" --out-dir "$JUD" --max-workers "$W"
done

echo "━━━ 완료 ━━━"
for K in ${CUTOFFS//,/ }; do
  echo "  cutoff ${K} -> results/mem0-classic-oss/memora-judge-${PERIOD}${SUFFIX}-k${K}"
done
