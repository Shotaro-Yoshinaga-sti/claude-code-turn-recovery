#!/usr/bin/env bash
# StopFailure hook: APIエラーでターンが打ち切られたとき、その時点で失われた作業を
# ディスクに記録する。次のセッション開始時に session-start.sh がこれを読んで
# 再開ブリーフィングとしてコンテキストへ注入する。
#
# なぜ必要か:
# usage limit / rate limit に当たるとサブエージェントは道半ばで殺され、そこまでに
# 消費したトークンが成果ゼロで消える。何がどこまで終わっていたかがどこにも残らない
# ため、再開後に同じ調査を新しいサブエージェントで流し直すことになり二重に無駄が出る。
#
# 未完了の判定に使う材料は台帳側にある: agent-call-record.sh が起動時に .call.json を、
# agent-call-complete.sh が完了時に .done.json を書いている。
# 「.call.json はあるが .done.json がない」= 中断された瞬間に走っていたエージェント。
#
# matcher は付けない。rate_limit 以外 (overloaded / server_error / billing_error) でも
# 作業が飛ぶ事情は同じなので全部記録し、区別は読む側 (session-start.sh) が行う。
#
# StopFailure は stdout と終了コードが完全に無視されるイベントなので、
# 「ディスクへ書くだけ」の設計にしている。常に exit 0。
set -u

if [ "${TURN_RECOVERY_DISABLE:-}" = "1" ]; then
  exit 0
fi

# リポジトリルートは hook 自身の位置(<root>/.claude/hooks/)から導出する。
# 特定のパスを決め打ちにすると、チェックアウト先が異なる環境ですれ違う。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export TURN_RECOVERY_REPO_ROOT="$REPO_ROOT"
export TURN_RECOVERY_STATE_DIR="${TURN_RECOVERY_STATE_DIR:-$REPO_ROOT/.claude/recovery}"
# 未完了エージェントの判定元。agent-call-record.sh / agent-call-complete.sh と同じ契約。
export AGENT_CALLS_STATE_DIR="${AGENT_CALLS_STATE_DIR:-$REPO_ROOT/.claude/agent-calls}"
# .call.json の鮮度判定に使う TTL。中断は数時間放置されうるので、短い値を流用しない。
# session-start.sh の DEFAULT_TTL_SEC (86400秒 = 24h、中断記録そのものの有効期限)と
# 揃える。中断記録が生きている間は、その中断で巻き込まれたエージェントの呼び出しも
# 同じだけ「まだ話題として有効」とみなせる。
export TURN_RECOVERY_CALL_TTL_SEC="${TURN_RECOVERY_CALL_TTL_SEC:-86400}"
# 未完了エージェント判定の2パス走査(pending_agents_shared.py)を
# session-start.sh と共有する。理由はそのモジュールの冒頭コメント参照。
PENDING_AGENTS_HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PENDING_AGENTS_HOOKS_DIR

PYSCRIPT=$(
  cat <<'PYEOF'
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.environ.get("PENDING_AGENTS_HOOKS_DIR", ""))
from pending_agents_shared import (  # noqa: E402
    cast_number,
    collect_done_keys,
    collect_pending_calls,
)

# 再開ブリーフィングは「何を再開すべきか」が分かれば足りる。全文を持ち回ると
# SessionStart の注入がコンテキストを圧迫するので、ここで刈り込んでおく。
MAX_AGENTS = 12
MAX_HEAD_LINES = 6
MAX_MESSAGE_CHARS = 2000
MAX_STATUS_LINES = 10


def safe_name(value: str, fallback: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value) or fallback


def as_str(value: object) -> str:
    return value if isinstance(value, str) else str(value or "")


def str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def call_ttl_seconds() -> float:
    try:
        return float(os.environ.get("TURN_RECOVERY_CALL_TTL_SEC", "86400") or "86400")
    except ValueError:
        return 86400.0


def load_record(path: str) -> dict[str, object] | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): v for k, v in data.items()}


try:
    raw = sys.stdin.read()
    if not raw.strip():
        sys.exit(0)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        sys.exit(0)
    data = {str(k): v for k, v in parsed.items()}
except Exception:
    sys.exit(0)

session_id = as_str(data.get("session_id"))
if not session_id:
    sys.exit(0)

state_root = os.environ.get("TURN_RECOVERY_STATE_DIR", "")
if not state_root:
    sys.exit(0)

prompt_id = as_str(data.get("prompt_id"))
repo_root = os.environ.get("TURN_RECOVERY_REPO_ROOT", "")


def git_output(args: list[str]) -> str:
    if not repo_root:
        return ""
    try:
        completed = subprocess.run(
            ["git", "-C", repo_root, *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return ""
    return completed.stdout if completed.returncode == 0 else ""


def collect_agents(
    now: float,
) -> tuple[list[dict[str, object]], list[dict[str, object]], int]:
    """(未完了, このターンで完了済み, 未完了の総数) のエージェント一覧を返す。

    未完了の判定 (2パス走査: .done.json キーの集約 → .call.json の未完了抽出、
    TTL鮮度判定) は pending_agents_shared.py を session-start.sh と共有する
    (モジュール冒頭コメント参照)。ここでの関心事は:
    - 対象セッション: 現在の session_id のみ
    - 完了済み一覧 (finished): 「今のターンで既に終わったので再実行するな」を
      伝えるための情報なので、現在の prompt_id のぶんだけを見る
    - 並べ替え・件数上限(下記)

    未完了 (pending) 側の並び順:
    - prompt_id は UUID で発行順とは無関係なので、`os.listdir` のソート順
      (辞書順)のまま MAX_AGENTS で切り捨てると、たまたま辞書順で後ろに来た
      prompt_id にある「いま本当に走っているエージェント」が枠から漏れる
      (移植元での再現: 古い prompt_id に未完了 .call.json を15件、現在の
      prompt_id に本当に走っていた呼び出しを1件置いたところ、現在の prompt_id が
      辞書順で後ろに来る配置では live な呼び出しが pending から完全に脱落した)。
      呼び出し時刻の降順に並べてから切る。time が無い/不正なレコードは
      最も古い扱いにする。
    """
    calls_root = os.environ.get("AGENT_CALLS_STATE_DIR", "")
    if not calls_root:
        return [], [], 0
    session_dir = os.path.join(calls_root, safe_name(session_id, "unknown-session"))
    try:
        prompt_dirs = sorted(os.listdir(session_dir))
    except Exception:
        return [], [], 0

    current_prompt = safe_name(prompt_id, "no-prompt-id")
    call_ttl_sec = call_ttl_seconds()

    done_keys = collect_done_keys(session_dir, prompt_dirs)
    pending_calls = collect_pending_calls(session_dir, prompt_dirs, done_keys, now, call_ttl_sec)

    pending_with_time: list[tuple[float, dict[str, object]]] = []
    for pending_call in pending_calls:
        call = pending_call.call
        entry: dict[str, object] = {
            "agent_id": as_str(call.get("agent_id")) or pending_call.key,
            "subagent_type": as_str(call.get("subagent_type")),
            "prompt_id": pending_call.prompt_dir_name,
            "prompt_head": str_list(call.get("norm_lines"))[:MAX_HEAD_LINES],
        }
        try:
            sort_time = float(cast_number(call.get("time")))
        except TypeError:
            sort_time = 0.0
        entry["age_sec"] = now - sort_time
        pending_with_time.append((sort_time, entry))

    # 完了済み一覧は現在の prompt_id の .done.json のみを見る(上記docstring参照)。
    finished: list[dict[str, object]] = []
    current_prompt_dir = os.path.join(session_dir, current_prompt)
    try:
        current_prompt_names = sorted(os.listdir(current_prompt_dir))
    except Exception:
        current_prompt_names = []
    for name in current_prompt_names:
        if not name.endswith(".done.json"):
            continue
        done = load_record(os.path.join(current_prompt_dir, name))
        if done is None:
            continue
        finished.append(
            {
                "agent_id": as_str(done.get("agent_id")),
                "agent_type": as_str(done.get("agent_type")),
                "output_head": str_list(done.get("output_lines"))[:MAX_HEAD_LINES],
            }
        )

    # 新しい (= now に近い) 呼び出しほど前に来るよう降順で並べる。
    pending_with_time.sort(key=lambda pair: pair[0], reverse=True)
    pending_total = len(pending_with_time)
    pending = [entry for _, entry in pending_with_time[:MAX_AGENTS]]

    return pending, finished[:MAX_AGENTS], pending_total


record_time = time.time()
pending_agents, finished_agents, pending_total = collect_agents(record_time)


def first_nonempty(*candidates: str) -> str:
    for candidate in candidates:
        if candidate:
            return candidate
    return ""


# StopFailure の入力ペイロードのキー名は、公式ドキュメント
# (https://code.claude.com/docs/ja/hooks §StopFailure input) によれば
# common input fields に加えて次の3つだけを持つ:
#   - error (必須): エラー種別そのもの (例: "rate_limit"。他に overloaded /
#     authentication_failed / oauth_org_not_allowed / account_on_hold /
#     billing_error / invalid_request / model_not_found / server_error /
#     max_output_tokens / unknown)
#   - error_details (optional): エラーの追加詳細
#   - last_assistant_message (optional): 会話に表示されたエラー文字列そのもの
#     (Stop/SubagentStop と異なり、StopFailure では Claude の発言ではなく
#     APIエラー文字列が入ると明記されている)
# ここでの record 側フィールド名 error_type / error_message は、このリポジトリの
# 中断記録のスキーマとして選んだ名前であり、StopFailure の入力フィールド名では
# ない(紛らわしいが別物)。ドキュメント記載の正式名を優先しつつ、将来ペイロードの
# 形が変わったときに備えて他の想定しうる別名も候補として残す。
# 生ペイロードのキー一覧 (payload_keys) と先頭値のプレビューは、次に形が変わった
# ときも同じやり方で気付けるよう、実測が安定した後も記録を続ける。
error_obj = data.get("error")
error_obj_map = error_obj if isinstance(error_obj, dict) else {}
error_obj_str = error_obj if isinstance(error_obj, str) else ""

error_type = first_nonempty(
    error_obj_str,
    as_str(data.get("error_type")),
    as_str(data.get("errorType")),
    as_str(error_obj_map.get("type")),
    as_str(data.get("stop_failure_reason")),
    as_str(data.get("reason")),
    as_str(data.get("type")),
)

error_message = first_nonempty(
    as_str(data.get("last_assistant_message")),
    # error_details はドキュメント記載の optional フィールド。
    # last_assistant_message も optional なので、それが無く error_details だけが
    # 来る中断ではこれが無いとメッセージ欄が空のままになる。
    as_str(data.get("error_details")),
    as_str(data.get("error_message")),
    as_str(data.get("errorMessage")),
    as_str(error_obj_map.get("message")),
    as_str(data.get("message")),
)[:MAX_MESSAGE_CHARS]

# 候補キーがどれも埋まらなくても、メッセージ本文に制限系の語が現れていれば
# 種別だけは推定する(rate_limit / usage limit / 429 / 529 / overloaded は
# いずれも「作業が飛ぶ」という点で扱いが同じなので、大まかな推定で十分)。
RATE_LIMIT_HINT = re.compile(r"rate limit|usage limit|429|529|overloaded", re.IGNORECASE)
if not error_type and RATE_LIMIT_HINT.search(error_message):
    error_type = "rate_limit"

payload_keys = sorted(str(k) for k in data.keys())

# 既に他のフィールドで保持しているキーはプレビューから除く(二重に持つだけ無駄)。
PREVIEW_EXCLUDED_KEYS = {
    "session_id",
    "prompt_id",
    "transcript_path",
    "cwd",
    "error_type",
    "error_message",
    "error",
    "error_details",
    "last_assistant_message",
}
MAX_PREVIEW_KEYS = 20
MAX_PREVIEW_VALUE_CHARS = 120


def preview_text(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:MAX_PREVIEW_VALUE_CHARS]


payload_preview = {
    key: preview_text(data[key])
    for key in list(data.keys())[:MAX_PREVIEW_KEYS]
    if key not in PREVIEW_EXCLUDED_KEYS
}

record = {
    "session_id": session_id,
    "prompt_id": prompt_id,
    "error_type": error_type,
    "error_message": error_message,
    "time": record_time,
    "branch": git_output(["branch", "--show-current"]).strip(),
    "git_status_lines": git_output(["status", "--porcelain"]).splitlines()[:MAX_STATUS_LINES],
    "pending_agents": pending_agents,
    # MAX_AGENTS で切り捨てる前の未完了総数。session-start.sh が「他 N 件」を
    # 出せるようにするための情報(切り捨てが起きたこと自体を読む側が観測できる)。
    "pending_total": pending_total,
    "finished_agents": finished_agents,
    # 診断専用。session-start.sh のブリーフィングには出さない(雑音になる)。
    "payload_keys": payload_keys,
    "payload_preview": payload_preview,
}

# 原子的に置換する。中断はターンの途中で起きるので、読む側が壊れた JSON を
# 掴まないよう「完全な内容が見えるか、古い内容が見えるか」の二択にする。
try:
    os.makedirs(state_root, exist_ok=True)
    path = os.path.join(state_root, f"{safe_name(session_id, 'unknown-session')}.json")
    fd, tmp_path = tempfile.mkstemp(dir=state_root, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
except Exception:
    pass

sys.exit(0)
PYEOF
)

exec python3 -c "$PYSCRIPT"
