#!/usr/bin/env bash
# 대시보드 재시작. tmux 창에 들어갈 필요 없이 이것만 돌리면 됨.
#
#   bash src/web-dashboard/restart.sh
#
# 세션이 있으면 그 창의 프로세스만 갈아끼우고(붙어 있어도 안 끊김), 없으면 새로 만듦.
# 기동까지 기다렸다가 응답을 확인하고 끝남. 실패하면 로그 마지막 줄을 보여줌.
#
# 환경변수로 바꿀 수 있는 것: DASH_SESSION(기본 dashboard) DASH_PORT(기본 8501)
set -uo pipefail

SESSION=${DASH_SESSION:-dashboard}
PORT=${DASH_PORT:-8501}
HOST=${DASH_HOST:-0.0.0.0}
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

CMD="cd '$ROOT' && ulimit -n 8192 && OPENAI_API_KEY=dummy exec uv run --project src/web-dashboard \
uvicorn app:app --app-dir src/web-dashboard --host $HOST --port $PORT"

if tmux has-session -t "=$SESSION" 2>/dev/null; then
  tmux respawn-window -k -t "=$SESSION" "bash -lc \"$CMD\"" \
    && echo "재시작: tmux 세션 $SESSION" \
    || { tmux kill-session -t "=$SESSION"
         tmux new-session -d -s "$SESSION" "bash -lc \"$CMD\""
         echo "세션을 새로 만듦: $SESSION"; }
else
  tmux new-session -d -s "$SESSION" "bash -lc \"$CMD\""
  echo "새로 띄움: tmux 세션 $SESSION"
fi

# uv 가 의존성을 받는 첫 실행은 느릴 수 있어 넉넉히 기다림
for _ in $(seq 1 60); do
  if curl -sf "http://localhost:$PORT/api/runs" -o /dev/null; then
    echo "OK  http://localhost:$PORT"
    exit 0
  fi
  sleep 1
done

echo "⚠ ${PORT}번 포트가 60초 안에 응답하지 않음. 로그:"
tmux capture-pane -pt "=$SESSION" | grep -v '^$' | tail -20
exit 1
