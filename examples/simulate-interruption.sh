#!/usr/bin/env bash
# 中断と再開の一連の流れを、実際にレート制限に当たらずに手元で再現する。
#
# 本物の中断を待っていると動作確認ができないため、hook に渡される JSON を
# こちらで組み立てて順に流し込む。使うのは実運用と同じ hook スクリプトで、
# 状態ディレクトリだけを一時ディレクトリへ向ける(実運用の .claude/recovery/ と
# .claude/agent-calls/ は汚さない)。
#
# 流れ:
#   1. サブエージェントを3本起動      → PostToolUse   → .call.json ×3
#   2. うち2本が完了                  → SubagentStop  → .done.json ×2
#   3. レート制限でターンが打ち切られる → StopFailure   → recovery/<session>.json
#   4. 次のセッションが開く            → SessionStart  → 再開ブリーフィングを出力
#   5. もう一度セッションを開く        → SessionStart  → 二度は出さない(沈黙)
#
# 3本の役割は、確認したい性質ごとに分けてある:
#   agent_investigator … 別の prompt_id で完了 → pending から正しく落ちる
#   agent_reviewer     … 同じ prompt_id で完了 → 「再実行しない」として併記される
#   agent_implementer  … 完了しないまま中断    → 再開すべき対象として提示される
#
# 使い方: bash examples/simulate-interruption.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOKS_DIR="$REPO_ROOT/.claude/hooks"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

export TURN_RECOVERY_STATE_DIR="$WORK_DIR/recovery"
export AGENT_CALLS_STATE_DIR="$WORK_DIR/agent-calls"

SESSION="demo-session"
PROMPT="demo-prompt"

step() {
  printf '\n\033[1m== %s\033[0m\n' "$1"
}

step "1. サブエージェントを3本起動する (PostToolUse: Agent)"
for spec in "agent_investigator:investigator:認証まわりのエラー処理を洗い出す" \
  "agent_reviewer:reviewer:変更差分のセキュリティレビュー" \
  "agent_implementer:implementer:サムネイル生成のキャッシュを追加する"; do
  agent_id="${spec%%:*}"
  rest="${spec#*:}"
  role="${rest%%:*}"
  prompt_text="${rest#*:}"
  printf '%s' "{
    \"session_id\": \"$SESSION\",
    \"prompt_id\": \"$PROMPT\",
    \"tool_use_id\": \"toolu_$agent_id\",
    \"tool_input\": {\"subagent_type\": \"$role\", \"prompt\": \"$prompt_text\"},
    \"tool_response\": {\"agentId\": \"$agent_id\", \"status\": \"async_launched\"}
  }" | bash "$HOOKS_DIR/agent-call-record.sh"
  echo "  起動を記録: $role [$agent_id]"
done

step "2. investigator と reviewer が完了する (SubagentStop)"
# investigator は起動と別の prompt_id で完了する。バックグラウンドの
# サブエージェントはユーザーの発言をまたいで生き続けるため、これが通常の姿。
# reviewer は同じ prompt_id で完了する(= このターンで完了済み)。
for spec in "another-prompt:agent_investigator:investigator:結論: 応答形式は3種類あった" \
  "$PROMPT:agent_reviewer:reviewer:重大な指摘なし"; do
  done_prompt="${spec%%:*}"
  rest="${spec#*:}"
  agent_id="${rest%%:*}"
  rest="${rest#*:}"
  role="${rest%%:*}"
  output_text="${rest#*:}"
  printf '%s' "{
    \"session_id\": \"$SESSION\",
    \"prompt_id\": \"$done_prompt\",
    \"agent_id\": \"$agent_id\",
    \"agent_type\": \"$role\",
    \"last_assistant_message\": \"$output_text\"
  }" | bash "$HOOKS_DIR/agent-call-complete.sh"
  echo "  完了を記録: $role [$agent_id] (prompt_id: $done_prompt)"
done

step "3. レート制限でターンが打ち切られる (StopFailure)"
printf '%s' "{
  \"session_id\": \"$SESSION\",
  \"prompt_id\": \"$PROMPT\",
  \"hook_event_name\": \"StopFailure\",
  \"error\": \"rate_limit\",
  \"last_assistant_message\": \"You've hit your session limit · resets 8:40pm (Asia/Tokyo)\"
}" | bash "$HOOKS_DIR/turn-failure-record.sh"
echo "  中断記録を作成: $TURN_RECOVERY_STATE_DIR/$SESSION.json"
echo
echo "  --- 記録された JSON (抜粋) ---"
python3 - "$TURN_RECOVERY_STATE_DIR/$SESSION.json" <<'PYEOF'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    record = json.load(f)

excerpt = {
    "error_type": record["error_type"],
    "error_message": record["error_message"],
    "pending_agents": [
        {"agent_id": a["agent_id"], "subagent_type": a["subagent_type"]}
        for a in record["pending_agents"]
    ],
    "finished_agents": [
        {"agent_id": a["agent_id"], "agent_type": a["agent_type"]}
        for a in record["finished_agents"]
    ],
}
print(json.dumps(excerpt, ensure_ascii=False, indent=2))
PYEOF

step "4. 制限が明けて、次のセッションを開く (SessionStart)"
echo "  --- コンテキストに注入されるブリーフィング ---"
printf '%s' "{\"session_id\": \"$SESSION\"}" | bash "$HOOKS_DIR/session-start.sh"

step "5. もう一度セッションを開く (提示済みなので沈黙する)"
output="$(printf '%s' "{\"session_id\": \"$SESSION\"}" | bash "$HOOKS_DIR/session-start.sh")"
if [ -z "$output" ]; then
  echo "  出力なし(期待どおり: 一度提示した記録は二度出さない)"
else
  echo "  想定外: 2回目にも出力がありました" >&2
  printf '%s\n' "$output" >&2
  exit 1
fi

step "完了"
cat <<'EOF'
  ここで確認できたこと:
    - investigator は pending に入らない。起動と完了で prompt_id が違っても、
      .done.json をセッション全体から集約しているので正しく突き合わせられる
    - 未完了の implementer だけが「再開すべき対象」として提示される
    - 同じ prompt_id で完了した reviewer は「再実行しない」として併記される
    - 一度提示した記録は .json.consumed に改名され、二度は出ない
EOF
