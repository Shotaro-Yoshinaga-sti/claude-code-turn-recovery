#!/usr/bin/env bash
# SubagentStop hook: サブエージェントの *完了* と、その最終報告テキストを記録する。
# 「未完了」判定の唯一の否定材料であり、これが書かれないエージェントは中断時に
# 走っていたものとみなされる。
#
# なぜ PostToolUse ではなく SubagentStop なのか:
# サブエージェントは既定でバックグラウンド実行されるため、PostToolUse は起動が
# 返った時点で発火してしまい、1メッセージで同時発行した並列呼び出しの2本目より
# 前に来る(移植元での実測: PostToolUse は起動の0.4秒後、並列呼び出しの間隔は
# 0.8秒、SubagentStop は約6秒後)。「完了 = SubagentStop」にして初めて、
# 並列で走っている兄弟が互いを完了済みとみなさないことが保証される。
#
# SubagentStop は tool_use_id を持たないため agent_id で keying する
# (agent-call-record.sh が PostToolUse で tool_use_id ↔ agent_id を紐付ける)。
#
# ノンブロッキング: 何が起きても出力なしで常に exit 0(stopをブロックしない)。
set -u

if [ "${AGENT_CALLS_DISABLE:-}" = "1" ]; then
  exit 0
fi

# 既定の状態ディレクトリは hook 自身の位置(<root>/.claude/hooks/)から導出し、
# 書く側 (agent-call-record.sh) と一致させる契約。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export AGENT_CALLS_STATE_DIR="${AGENT_CALLS_STATE_DIR:-$REPO_ROOT/.claude/agent-calls}"

PYSCRIPT=$(
  cat <<'PYEOF'
import json
import os
import re
import sys
import time

# 出力の記録量の上限(状態ファイルの肥大と読み込みコストを抑える)。
MAX_OUTPUT_LINES = 400
MAX_OUTPUT_CHARS = 40000


def normalize_lines(text: str) -> list[str]:
    return [s for s in (line.strip() for line in text.splitlines()) if s]


def safe_name(value: str, fallback: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value) or fallback


def as_str(value: object) -> str:
    return value if isinstance(value, str) else str(value or "")


try:
    raw = sys.stdin.read()
    if not raw.strip():
        sys.exit(0)
    data = json.loads(raw)
    if not isinstance(data, dict):
        sys.exit(0)
except Exception:
    sys.exit(0)

session_id = as_str(data.get("session_id"))
prompt_id = as_str(data.get("prompt_id"))
agent_id = as_str(data.get("agent_id"))
agent_type = as_str(data.get("agent_type"))
output = as_str(data.get("last_assistant_message"))

if not session_id or not agent_id:
    sys.exit(0)

state_root = os.environ.get("AGENT_CALLS_STATE_DIR", "")
if not state_root:
    sys.exit(0)

state_dir = os.path.join(
    state_root,
    safe_name(session_id, "unknown-session"),
    safe_name(prompt_id, "no-prompt-id"),
)
try:
    os.makedirs(state_dir, exist_ok=True)
except Exception:
    sys.exit(0)

lines = normalize_lines(output[:MAX_OUTPUT_CHARS])
record = {
    "agent_id": agent_id,
    "agent_type": agent_type,
    "output_lines": lines[:MAX_OUTPUT_LINES],
    "output_truncated": len(output) > MAX_OUTPUT_CHARS or len(lines) > MAX_OUTPUT_LINES,
    "time": time.time(),
}

try:
    path = os.path.join(state_dir, f"{safe_name(agent_id, 'unknown-agent')}.done.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
except Exception:
    pass

sys.exit(0)
PYEOF
)

exec python3 -c "$PYSCRIPT"
