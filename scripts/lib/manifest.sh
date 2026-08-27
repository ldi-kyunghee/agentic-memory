# 실행이 스스로 이력을 남긴다. 산출물 디렉토리 옆에 run.json 을 떨군다.
#
#   source scripts/lib/manifest.sh
#   write_manifest <산출물디렉토리> <system> <benchmark> <setting> <stage>
#
# 왜 필요한가: 지금까지는 사람이 runs.yaml 에 손으로 등록해야 화면에 떴다. 두 가지가 문제였다.
#   1. 등록을 잊으면 산출물이 있어도 화면에 없다
#   2. **디렉토리 이름에 안 담기는 변인이 있다.** 2026-08-26에 BEAM 답변 프롬프트가 두 팔에서
#      달랐는데 경로만 봐서는 알 수 없었고, 그대로 비교해 결론의 부호가 뒤집혔다.
#      이름으로 유추하는 자동화는 같은 실수를 더 빨리 저지를 뿐이다.
# 그래서 **실행 시점의 실제 env 를 그대로 적는다.**
write_manifest() {
  local dir="$1" system="$2" bench="$3" setting="$4" stage="$5"
  [ -n "$dir" ] || return 0
  mkdir -p "$dir" 2>/dev/null || return 0
  python3 - "$dir" "$system" "$bench" "$setting" "$stage" <<'PY'
import json, os, sys, datetime
d, system, bench, setting, stage = sys.argv[1:6]
path = os.path.join(d, "run.json")
doc = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        doc = {}
doc.update({"system": system, "benchmark": bench, "setting": setting})
env = doc.setdefault("stages", {}).setdefault(stage, {})
env.update({
    "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    # 비교 가능성을 좌우하는 변인만 고른다. 전부 적으면 무엇이 중요한지 안 보인다.
    "mem0_impl": os.getenv("MEM0_IMPL"),
    "agent_model": os.getenv("MEM0_LLM_MODEL"),
    "agent_effort": os.getenv("MEM0_REASONING_EFFORT") or "(모델 기본값)",
    "embed_model": os.getenv("MEM0_EMBED_MODEL"),
    "embed_dims": os.getenv("MEM0_EMBED_DIMS"),
    "answer_model": os.getenv("ANSWER_MODEL"),
    "answer_effort": os.getenv("ANSWER_REASONING_EFFORT"),
    "judge_model": os.getenv("JUDGE_MODEL"),
    "judge_effort": os.getenv("JUDGE_REASONING_EFFORT"),
    "beam_answer_prompt": os.getenv("BEAM_ANSWER_PROMPT"),
    "retriever": os.getenv("MEM0_RETRIEVER") or "dense",
    "base_url": os.getenv("OPENAI_BASE_URL"),
})
# LIGHT 전용 플래그. 다른 시스템 실행에서는 값이 없어 빈 dict 로 남음 (additive).
light = {k: v for k, v in os.environ.items() if k.startswith("LIGHT_")}
if light:
    env["light_flags"] = light
with open(path, "w", encoding="utf-8") as f:
    json.dump(doc, f, ensure_ascii=False, indent=2)
print("  이력 기록 -> " + path)
PY
}
