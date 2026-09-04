"""turn-failure-record.sh (StopFailure) / session-start.sh (SessionStart) が
共有する「未完了エージェント」判定の2パス走査。

agent-call-record.sh が起動時に `.call.json` を、agent-call-complete.sh が
完了時に `.done.json` を書く。「`.call.json` はあるが対応する `.done.json` が
セッション内のどこにも無い」= まだ走っている(または走ったまま終わった)
エージェント、という判定を2つの hook がそれぞれ独立に実装すると、片方だけ直した
ときに再開ブリーフィング(session-start.sh)と中断記録(turn-failure-record.sh)で
未完了の判定が食い違う。

移植元のプロジェクトでは実際にこの事故が起きた。`.done.json` 側の集約範囲だけを
セッション全体へ広げる修正が session-start.sh にしか反映されず、
turn-failure-record.sh は `.call.json` 側が現在の prompt_id スコープのまま
取り残された結果、いま走っているエージェントが pending から脱落した。

各 hook は自身の位置 (<root>/.claude/hooks/) を `PENDING_AGENTS_HOOKS_DIR` として
環境変数に渡し、python ヒアドキュメント側で `sys.path` に追加してこのモジュールを
import する。

このモジュールが持つのはあくまで2パス判定そのもの(パス1: done_keys の集約、
パス2: .call.json の未完了抽出、TTL鮮度判定)であって、以下は呼び出し側の
関心事として意図的に持たせていない:
  - 対象セッションの選び方
    (turn-failure-record.sh は現在のセッションのみ、
     session-start.sh は現在のセッションを除く全セッション)
  - 抽出後の並べ替え・件数上限・出力の形
"""

from __future__ import annotations

import json
import os


def cast_number(value: object) -> float | int:
    """`value` が実際に int/float であることを実行時検証する。

    `float(value)` は引数が `object` 型のままだと型チェッカがエラーにする。
    `# type: ignore` で握りつぶさず isinstance で型を絞ってから渡す。
    """
    if isinstance(value, (int, float)):
        return value
    raise TypeError("number expected")


def is_fresh(call_time: object, now: float, ttl_sec: float) -> bool:
    """`.call.json` の `time` が `now` から見て `ttl_sec` 以内かどうか。

    `time` が無い/不正な値のレコードは fresh 扱いにしない
    (SubagentStop が発火せずに死んだエージェントの記録が、鮮度フィルタ無しでは
    セッションの間ずっと未完了枠を食い続けてしまうため)。
    """
    try:
        return (now - float(cast_number(call_time))) <= ttl_sec
    except (TypeError, ValueError):
        return False


def load_json_object(path: str) -> dict[str, object] | None:
    try:
        with open(path, encoding="utf-8") as f:
            data: object = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): v for k, v in data.items()}


def collect_done_keys(session_dir: str, prompt_dir_names: list[str]) -> set[str]:
    """パス1: セッション全体の `.done.json` キーを1つの集合へ集約する。

    バックグラウンドのサブエージェントは発言をまたいで生き続けるため、
    `.call.json` (起動時の prompt_id) と `.done.json` (完了時の prompt_id) は
    日常的に別の prompt_id ディレクトリになる。片方の prompt_id だけを見て
    突き合わせると、既に完了しているエージェントを未完了と誤認する。
    """
    done_keys: set[str] = set()
    for prompt_dir_name in prompt_dir_names:
        prompt_dir = os.path.join(session_dir, prompt_dir_name)
        try:
            names = os.listdir(prompt_dir)
        except Exception:
            continue
        done_keys.update(n[: -len(".done.json")] for n in names if n.endswith(".done.json"))
    return done_keys


class PendingCall:
    """未完了と判定された `.call.json` 1件。呼び出し側が必要な形に整形する。"""

    def __init__(
        self,
        prompt_dir_name: str,
        key: str,
        call: dict[str, object],
        call_path: str,
    ) -> None:
        self.prompt_dir_name = prompt_dir_name
        self.key = key
        self.call = call
        self.call_path = call_path


def collect_pending_calls(
    session_dir: str,
    prompt_dir_names: list[str],
    done_keys: set[str],
    now: float,
    ttl_sec: float,
) -> list[PendingCall]:
    """パス2: 各 prompt_id の `.call.json` を見て、`done_keys` に無く、
    かつ鮮度フィルタ (`is_fresh`) を通ったものだけを返す。

    prompt_dir_names の順序、および結果の並べ替え・件数上限は呼び出し側の
    関心事なのでここでは行わない(呼び出し側で呼び出し時刻等に基づいて
    並べ替えること)。
    """
    pending: list[PendingCall] = []
    for prompt_dir_name in prompt_dir_names:
        prompt_dir = os.path.join(session_dir, prompt_dir_name)
        try:
            names = sorted(os.listdir(prompt_dir))
        except Exception:
            continue
        for name in names:
            if not name.endswith(".call.json"):
                continue
            key = name[: -len(".call.json")]
            if key in done_keys:
                continue
            call_path = os.path.join(prompt_dir, name)
            call = load_json_object(call_path)
            if call is None:
                continue
            if not is_fresh(call.get("time"), now, ttl_sec):
                continue
            pending.append(PendingCall(prompt_dir_name, key, call, call_path))
    return pending
