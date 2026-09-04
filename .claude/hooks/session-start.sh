#!/usr/bin/env bash
# SessionStart hook: 前回ターンがAPIエラーで打ち切られていた場合、その中断記録を
# 再開ブリーフィングとしてコンテキストへ注入する。常に exit 0。
#
# SessionStart は stdout がそのまま Claude のコンテキストに追加される数少ない
# イベントのひとつ (他は UserPromptSubmit / UserPromptExpansion)。
# 逆に StopFailure は stdout も終了コードも無視されるため、
# 「書く側 = StopFailure」「読ませる側 = SessionStart」に分けている。
set -u

# リポジトリルートは hook 自身の位置(<root>/.claude/hooks/)から導出する。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# stdin の JSON から session_id を取り出して中断記録を引き当てる。
# 端末から直接叩かれたとき(パイプ無し)に固まらないよう TTY では読まない。
raw_input=""
if [ ! -t 0 ]; then
  raw_input="$(cat 2>/dev/null || true)"
fi

branch=$(git -C "$REPO_ROOT" branch --show-current 2>/dev/null || echo "")

export TURN_RECOVERY_STATE_DIR="${TURN_RECOVERY_STATE_DIR:-$REPO_ROOT/.claude/recovery}"
export SESSION_START_CURRENT_BRANCH="$branch"
# 中断記録が無かったときのフォールバック(orphaned agent 走査)が使う台帳。
# turn-failure-record.sh と同じ環境変数・同じ既定値を使う(鮮度判定の基準を揃える)。
export AGENT_CALLS_STATE_DIR="${AGENT_CALLS_STATE_DIR:-$REPO_ROOT/.claude/agent-calls}"
export TURN_RECOVERY_CALL_TTL_SEC="${TURN_RECOVERY_CALL_TTL_SEC:-86400}"
# 未完了エージェント判定の2パス走査(pending_agents_shared.py)を
# turn-failure-record.sh と共有する。理由はそのモジュールの冒頭コメント参照。
PENDING_AGENTS_HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PENDING_AGENTS_HOOKS_DIR

PYSCRIPT=$(
  cat <<'PYEOF'
"""前回ターンの中断記録があれば再開ブリーフィングとして出力する。

turn-failure-record.sh (StopFailure hook) が書いた記録を読む。目的は
「制限に当たらないようにする」ことではなく、当たった後の最初のターンで
何が失われ何が残っているかを分かるようにして、再実行を避けること。
"""

import json
import os
import re
import sys
import time

sys.path.insert(0, os.environ.get("PENDING_AGENTS_HOOKS_DIR", ""))
from pending_agents_shared import collect_done_keys, collect_pending_calls  # noqa: E402

# 既定 24 時間。これを過ぎた中断は別の作業の話とみなす。自動継続で同じセッションが
# そのまま走り続けた場合の書き置きも、これで自然に落ちる。
DEFAULT_TTL_SEC = 86400.0
MAX_AGENTS_SHOWN = 8
MAX_HEAD_CHARS = 90
MAX_MESSAGE_CHARS = 200
# この秒数を超える pending エージェントには "(古い)" を付ける。
STALE_AGENT_AGE_SEC = 3600.0

# 中断記録が無いときのフォールバック (orphaned agent 走査) の表示上限。
ORPHAN_MAX_SHOWN = 8
ORPHAN_HEADING = "## 前セッションで走ったまま終わったサブエージェント"


def as_str(value: object) -> str:
    return value if isinstance(value, str) else str(value or "")


def safe_name(value: str, fallback: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value) or fallback


def object_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [{str(k): v for k, v in item.items()} for item in value if isinstance(item, dict)]


def head_text(value: object) -> str:
    if not isinstance(value, list):
        return ""
    parts = [item for item in value if isinstance(item, str)]
    joined = " / ".join(parts)
    return joined[:MAX_HEAD_CHARS] + ("…" if len(joined) > MAX_HEAD_CHARS else "")


def load(path: str) -> dict[str, object] | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): v for k, v in data.items()}


def ttl_seconds() -> float:
    try:
        return float(os.environ.get("TURN_RECOVERY_TTL_SEC", "") or DEFAULT_TTL_SEC)
    except ValueError:
        return DEFAULT_TTL_SEC


def read_session_id() -> str:
    try:
        raw = sys.stdin.read()
    except Exception:
        return ""
    if not raw.strip():
        return ""
    try:
        data = json.loads(raw)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    return as_str(data.get("session_id"))


def float_or_zero(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def pick_record(state_root: str, session_id: str) -> tuple[str, dict[str, object], bool] | None:
    """(パス, 記録, 別セッション由来か) を返す。

    --continue / --resume は同じ session_id でセッションを開き直すので完全一致が
    直撃する。一方、制限に当たった後は新しいセッションを立てる運用も多いため、
    一致しなければ TTL 内で最新の記録を拾って「別セッションの記録」として出す。
    """
    cutoff = time.time() - ttl_seconds()

    if session_id:
        exact = os.path.join(state_root, f"{safe_name(session_id, 'unknown-session')}.json")
        record = load(exact)
        if record is not None and float_or_zero(record.get("time")) >= cutoff:
            return exact, record, False

    try:
        names = [n for n in os.listdir(state_root) if n.endswith(".json")]
    except Exception:
        return None

    best: tuple[str, dict[str, object], bool] | None = None
    best_time = cutoff
    for name in names:
        path = os.path.join(state_root, name)
        record = load(path)
        if record is None:
            continue
        stamp = float_or_zero(record.get("time"))
        if stamp >= best_time:
            best_time = stamp
            best = (path, record, True)
    return best


def consume(path: str) -> None:
    """一度出したら二度出さない。GC が拾えるよう消さずに印を付けて残す。"""
    try:
        os.replace(path, f"{path}.consumed")
    except Exception:
        pass


def call_ttl_seconds() -> float:
    """turn-failure-record.sh の TURN_RECOVERY_CALL_TTL_SEC と同じ基準・同じ環境変数。

    中断記録そのものの生存期間 (TTL) の間は、その中断で巻き込まれた
    エージェントの呼び出しも「まだ話題として有効」とみなす。
    """
    try:
        return float(os.environ.get("TURN_RECOVERY_CALL_TTL_SEC", "86400") or "86400")
    except ValueError:
        return 86400.0


class OrphanAgent:
    def __init__(self, role: str, agent_id: str, head: str, call_path: str) -> None:
        self.role = role
        self.agent_id = agent_id
        self.head = head
        self.call_path = call_path


def presented_marker_path(call_path: str) -> str:
    """`call_path` (.call.json) に対応する提示済みマーカー(サイドカー)のパス。

    `.call.json` そのものはリネームしない(mark_orphan_presented の docstring 参照)。
    """
    if call_path.endswith(".call.json"):
        return call_path[: -len(".call.json")] + ".presented.json"
    return call_path + ".presented.json"


def is_orphan_presented(call_path: str) -> bool:
    return os.path.exists(presented_marker_path(call_path))


def find_orphan_agents(
    calls_root: str, current_session_id: str, now: float
) -> tuple[list[OrphanAgent], int]:
    """中断記録が無いとき、他セッションに走ったまま終わったサブエージェントを探す。

    レート制限はサブエージェントだけを殺してメインのターンを生かすことがある。
    この場合 StopFailure は発火せず turn-failure-record.sh は何も書かないため、
    その中断はどこにも記録されない。ここでは同じ台帳 (.call.json / .done.json) を、
    pending_agents_shared.py の2パス判定(turn-failure-record.sh と共有)で
    セッションをまたいで走査する: ある prompt_id の .call.json に対応する
    .done.json が、そのセッション内のどの prompt_id にも無ければ
    「走ったまま終わった」とみなす。

    現在の session_id は除外する(まだ生きている可能性があるため。
    turn-failure-record.sh 側は逆に現在の session_id だけを見る。
    どちらも呼び出し側の関心事なので pending_agents_shared.py には持たせていない)。

    一度提示済み(mark_orphan_presented 済み)のものは除外する。
    """
    ttl_sec = call_ttl_seconds()

    try:
        session_dirs = sorted(os.listdir(calls_root))
    except Exception:
        return [], 0

    candidates: list[tuple[float, OrphanAgent]] = []
    for session_dir_name in session_dirs:
        if session_dir_name == safe_name(current_session_id, "unknown-session"):
            continue
        session_dir = os.path.join(calls_root, session_dir_name)
        try:
            prompt_dirs = sorted(os.listdir(session_dir))
        except Exception:
            continue

        done_keys = collect_done_keys(session_dir, prompt_dirs)
        pending_calls = collect_pending_calls(session_dir, prompt_dirs, done_keys, now, ttl_sec)

        for pending_call in pending_calls:
            if is_orphan_presented(pending_call.call_path):
                continue
            call = pending_call.call
            role = as_str(call.get("subagent_type")) or "(役割不明)"
            head = head_text(call.get("norm_lines"))
            agent_id = as_str(call.get("agent_id")) or pending_call.key
            candidates.append(
                (
                    float_or_zero(call.get("time")),
                    OrphanAgent(role, agent_id, head, pending_call.call_path),
                )
            )

    candidates.sort(key=lambda pair: pair[0], reverse=True)
    total = len(candidates)
    shown = [entry for _, entry in candidates[:ORPHAN_MAX_SHOWN]]
    return shown, total


def mark_orphan_presented(call_path: str) -> None:
    """一度提示したら二度出さない。

    `.call.json` は複数の hook が共有で読む台帳の主データであり、
    `.claude/recovery/` の `.json` → `.json.consumed` (中断記録そのもの、
    消費したら役目が終わる通知) とは性質が違う。移植元では当初ここも同じ考え方で
    `.call.json` 自体を `.call.json.presented` にリネームしていたが、孤児スキャンが
    現在のセッションを除外していて他の hook が現在のセッションしか見ないために
    実害が出ていなかっただけで、将来どれかの hook がセッションをまたいで
    `.call.json` を読むようにした瞬間、提示済みのものだけが黙って見えなくなる
    (`--resume` で古いセッションを開き直した場合、そのセッションの `.call.json` は
    既にリネームされていて turn-failure-record.sh から見えなくなる、という形でも
    顕在化する)。主データは変更せず、サイドカーの `.presented.json` を別に置く。
    recovery-state-gc.sh はセッションディレクトリごと `rm -rf` するので、
    入れ子のサイドカーも一緒に回収される。
    """
    marker_path = presented_marker_path(call_path)
    try:
        with open(marker_path, "w", encoding="utf-8") as f:
            json.dump({"time": time.time()}, f, ensure_ascii=False)
    except Exception:
        pass


state_root = os.environ.get("TURN_RECOVERY_STATE_DIR", "")
if not state_root:
    sys.exit(0)

session_id = read_session_id()
picked = pick_record(state_root, session_id)
if picked is None:
    # 中断記録は無い。ただしサブエージェントだけが殺されてメインのターンが
    # 生き残るケースでは StopFailure が発火せず記録が作られないので、台帳を
    # 直接走査してこの穴を埋める。中断記録が有る場合はここに来ない
    # (二重に出さない)。
    calls_root = os.environ.get("AGENT_CALLS_STATE_DIR", "")
    if not calls_root:
        sys.exit(0)
    shown, total = find_orphan_agents(calls_root, session_id, time.time())
    if not shown:
        sys.exit(0)
    out = ["", ORPHAN_HEADING]
    out.append(f"- 前のセッションで走ったまま終わったサブエージェントがいる ({total}件):")
    for agent in shown:
        out.append(f"    - {agent.role} [{agent.agent_id}] {agent.head}")
    omitted = total - len(shown)
    if omitted > 0:
        out.append(f"    - (他 {omitted} 件、上限により省略)")
    out.append(
        '- 再開するなら SendMessage(to: "<agent_id>")。届かなければ新しいサブエージェントに'
        "委譲する。メインセッションが自分で実装するのは最後の手段"
    )
    print("\n".join(out))
    for agent in shown:
        mark_orphan_presented(agent.call_path)
    sys.exit(0)
path, record, from_other_session = picked

# StopFailure の error は "unknown" を正規の値として取りうる
# (https://code.claude.com/docs/ja/hooks §StopFailure input)。空文字と同様に
# 「原因不明」へ寄せないと、見出しが英語の "(unknown)" になり、このブリーフィングが
# 守っている「日本語で原因を示す」不変条件が、最も起こりやすい経路
# (ドキュメント上の正規値そのもの)で破れる。
_raw_error_type = as_str(record.get("error_type"))
error_type = "原因不明" if _raw_error_type in ("", "unknown") else _raw_error_type
message = as_str(record.get("error_message"))[:MAX_MESSAGE_CHARS]
stamp = float_or_zero(record.get("time"))
when = time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) if stamp else "(時刻不明)"
pending = object_list(record.get("pending_agents"))
finished = object_list(record.get("finished_agents"))

out: list[str] = ["", f"## 前回ターンの中断記録 ({error_type})"]
# ここで「この記録が出ている = 新しいセッションが開始できている = 中断の原因に
# なったAPIエラーは既に解消している」と書いてはいけない。これは成立しない。
# SessionStart hook はローカルで走り、モデルへのAPIリクエストが成功したことを
# 前提にしない。制限中でも CLI は起動でき、その時点でこのブリーフィングは
# 注入される。ハーネスが毎セッション注入する文言に検証不能な断定を混ぜても、
# モデルはそれを疑う手段を持たない。行動指示は事実主張に依存せずに書けるので、
# 事実主張だけを削り行動指示は残す。
out.append(
    "- 中断の原因になったAPIエラーがまだ続いているかどうかは、この記録からは"
    "分からない。ただし「まだ制限中かもしれない」はサブエージェントへの委譲を"
    "控える理由にならない。制限が明けていれば委譲は普通に動くし、明けていなければ"
    "メインセッションが自分で実装しても同じように失敗する"
)
if from_other_session:
    # TTL (既定24h) 以内の記録は無関係より同一作業の続きである確率の方が高い。
    # 「無視してよい」を主にすると再開せずに終わらせる方向へ誘導してしまうため、
    # 「再開する」を主文にし、無視は例外的な選択として書く。
    out.append(
        "- 別セッションの記録 (session_id が一致しない)。"
        "同じ作業の続きなら下の手順で再開する。無関係だと判断できるときだけ無視してよい"
    )
out.append(f"- 中断: {when}" + (f" / {message}" if message else ""))

recorded_branch = as_str(record.get("branch"))
current_branch = os.environ.get("SESSION_START_CURRENT_BRANCH", "")
if recorded_branch and current_branch and recorded_branch != current_branch:
    out.append(f"- 中断時のブランチ: {recorded_branch} (現在は {current_branch})")

if pending:
    out.append(f"- 未完了だったサブエージェント ({len(pending)}件):")
    shown = pending[:MAX_AGENTS_SHOWN]
    for entry in shown:
        role = as_str(entry.get("subagent_type")) or "(役割不明)"
        agent_id = as_str(entry.get("agent_id"))
        head = head_text(entry.get("prompt_head"))
        # age_sec は turn-failure-record.sh が呼び出し時刻からの経過を秒で
        # 持たせたもの。1時間を超えていたら鮮度への注意を添える。
        age_sec = entry.get("age_sec")
        stale = (
            " (古い)"
            if isinstance(age_sec, (int, float)) and age_sec > STALE_AGENT_AGE_SEC
            else ""
        )
        out.append(f"    - {role} [{agent_id}] {head}{stale}")
    # pending_total は turn-failure-record.sh が MAX_AGENTS で切り捨てる前の
    # 未完了総数。ここでの表示上限 (MAX_AGENTS_SHOWN) による省略と合わせて、
    # 切り捨てが起きたこと自体を読む側が見えるようにする。
    pending_total_raw = record.get("pending_total")
    pending_total = (
        int(pending_total_raw) if isinstance(pending_total_raw, (int, float)) else len(pending)
    )
    omitted = max(pending_total, len(pending)) - len(shown)
    if omitted > 0:
        out.append(f"    - (他 {omitted} 件、上限により省略)")
if finished:
    roles = ", ".join(
        f"{as_str(e.get('agent_type')) or '(役割不明)'} [{as_str(e.get('agent_id'))}]"
        for e in finished[:MAX_AGENTS_SHOWN]
    )
    out.append(f"- このターンで完了済み ({len(finished)}件): {roles} — 再実行しない")

if pending:
    # SendMessage が届かない (エージェントが既に消えている) ケースの出口が
    # 無いと、消去法で「自分で実装する」が唯一の選択肢になってしまう。
    # 3・4 を明記して、不達時も委譲の枠内で完結できるようにする。
    out.append("- 再開の手順 (上から順に試し、成功した時点で次には進まない):")
    out.append(
        "    1. `.claude/agent-notes/` に途中成果が残っていないか先に確認する。"
        "残っていれば、次の指示はそこからの差分だけでよい"
    )
    out.append(
        '    2. 未完了エージェントに SendMessage(to: "<agent_id>") を送って再開する。'
        "同じ依頼を新規 Agent で作り直さない"
    )
    out.append(
        "    3. SendMessage が届かない (エージェントが既に消えている) 場合は、"
        "残りの作業を新しいサブエージェントに委譲する。1 のノートを踏まえ"
        "「どこまで終わっていて、次に何をするか」を書いた新しい指示を渡す"
        "(前回の依頼文の丸写しはしない)"
    )
    out.append("    4. メインセッションが自分で実装するのは最後の手段")
    out.append(
        "- 組み込みの Explore / Plan は再開できない。"
        "上記 3 と同じ扱いで、再開できるカスタムエージェントに置き換えて起動する"
    )

print("\n".join(out))
consume(path)
sys.exit(0)
PYEOF
)

printf '%s' "$raw_input" | python3 -c "$PYSCRIPT" 2>/dev/null || true

exit 0
