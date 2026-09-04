#!/usr/bin/env bash
# PostToolUse hook (matcher: Agent): Agent呼び出しの「中身」と agent_id への
# 紐付けを台帳に記録する。turn-failure-record.sh / session-start.sh が読む。
#
# 重要: このイベントは *完了* ではない。サブエージェントは既定でバックグラウンド
# 実行されるため、PostToolUse は起動が返った時点
# (tool_response.status == "async_launched") で発火する。完了の記録は
# SubagentStop の agent-call-complete.sh が担当する。
#
# ここで記録するのは prompt 側の情報。PostToolUse は tool_input.prompt と
# tool_response.agentId の両方を持つ唯一のイベントなので、
# 「どの agent_id がどんな prompt で起動されたか」をここでしか確定できない。
#
# ノンブロッキング: 何が起きても出力なしで常に exit 0。
set -u

if [ "${AGENT_CALLS_DISABLE:-}" = "1" ]; then
  exit 0
fi

# 既定の状態ディレクトリは hook 自身の位置(<root>/.claude/hooks/)から導出し、
# 読む側と一致させる契約。特定のパスを決め打ちにしない
# (チェックアウト先が異なる環境ですれ違う)。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export AGENT_CALLS_STATE_DIR="${AGENT_CALLS_STATE_DIR:-$REPO_ROOT/.claude/agent-calls}"

PYSCRIPT=$(
  cat <<'PYEOF'
import json
import os
import re
import sys
import time

# prompt の記録量の上限(状態ファイルの肥大を防ぐ)。
MAX_PROMPT_LINES = 400


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

tool_input = data.get("tool_input")
if not isinstance(tool_input, dict):
    tool_input = {}
tool_response = data.get("tool_response")
if not isinstance(tool_response, dict):
    tool_response = {}

session_id = as_str(data.get("session_id"))
prompt_id = as_str(data.get("prompt_id"))
tool_use_id = as_str(data.get("tool_use_id"))
subagent_type = as_str(tool_input.get("subagent_type"))
prompt = as_str(tool_input.get("prompt"))
agent_id = as_str(tool_response.get("agentId") or data.get("agent_id"))

# agent_id が取れないと完了記録 (.done.json) と紐付けられない。黙って諦める。
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

norm_lines = normalize_lines(prompt)
record = {
    "agent_id": agent_id,
    "tool_use_id": tool_use_id,
    "subagent_type": subagent_type,
    "norm_lines": norm_lines[:MAX_PROMPT_LINES],
    "prompt_truncated": len(norm_lines) > MAX_PROMPT_LINES,
    "time": time.time(),
}

try:
    path = os.path.join(state_dir, f"{safe_name(agent_id, 'unknown-agent')}.call.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
except Exception:
    pass

sys.exit(0)
PYEOF
)

exec python3 -c "$PYSCRIPT"
