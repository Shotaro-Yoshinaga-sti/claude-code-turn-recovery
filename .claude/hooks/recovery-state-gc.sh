#!/usr/bin/env bash
# UserPromptSubmit hook: このハーネスが残す状態ディレクトリを掃除する。
#
# 状態は <session_id>/<prompt_id>/ に分離されているので、新しい発言では自動的に
# 別ディレクトリになる。タスク境界のためのリセットは不要で、ここに残るのは
# 古い状態の GC だけ。
#
# 掃除する対象は3つ。いずれも「後から読み返す価値が無くなったら消えてよい」もの:
#   agent-calls/  … 未完了エージェント判定の台帳 (.call.json / .done.json)
#   recovery/     … turn-failure-record.sh が書く中断記録。session-start.sh が
#                   提示済みのものは .consumed に改名されて残る
#   agent-notes/  … サブエージェントが中断に備えて残す途中成果
#
# ノンブロッキング: 何が起きても常に exit 0。
set -u

# リポジトリルートは hook 自身の位置(<root>/.claude/hooks/)から導出する。
# 既定の状態ディレクトリは各hookと一致させる契約。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CALLS_DIR="${AGENT_CALLS_STATE_DIR:-$REPO_ROOT/.claude/agent-calls}"
RECOVERY_DIR="${TURN_RECOVERY_STATE_DIR:-$REPO_ROOT/.claude/recovery}"
NOTES_DIR="${AGENT_NOTES_DIR:-$REPO_ROOT/.claude/agent-notes}"

# 保持期間。中断記録もノートも、7日経てば再開の役には立たない。
GC_DAYS="${HOOK_STATE_GC_DAYS:-7}"

# stdin は読み捨てる(このhookは入力を必要としない)。
cat >/dev/null 2>&1 || true

if [ -d "$CALLS_DIR" ]; then
  # 古いセッション状態をディレクトリごと掃除する
  # (入れ子の .presented.json サイドカーも一緒に回収される)。
  find "$CALLS_DIR" -mindepth 1 -maxdepth 1 -type d -mtime "+$GC_DAYS" -exec rm -rf {} + 2>/dev/null || true
fi

if [ -d "$RECOVERY_DIR" ]; then
  # 提示前 (.json) と提示済み (.json.consumed) の両方。名前で絞るのは、
  # 利用者が同じ場所に置いた無関係なファイルを巻き込まないため。
  find "$RECOVERY_DIR" -mindepth 1 -maxdepth 1 -type f \
    \( -name '*.json' -o -name '*.json.consumed' \) -mtime "+$GC_DAYS" -delete 2>/dev/null || true
fi

if [ -d "$NOTES_DIR" ]; then
  find "$NOTES_DIR" -mindepth 1 -maxdepth 1 -type f -name '*.md' -mtime "+$GC_DAYS" -delete 2>/dev/null || true
fi

exit 0
