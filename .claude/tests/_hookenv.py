"""hookテストが共有する環境変数名とパス定義。

各テストが環境変数名を文字列リテラルで持つと、hook側のリネーム時に一部の
テストだけ追随し損ねる。その取りこぼしは「テストが一時ディレクトリへ書いている
つもりで実運用の状態ディレクトリを汚染する」という形で表面化する。中断記録は
次のセッション開始時にそのままコンテキストへ注入されるため、汚染すると
存在しない中断の再開ブリーフィングが出る。環境変数名は必ずここから参照すること。
"""

from __future__ import annotations

import json
import os
import subprocess

CLAUDE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS_DIR = os.path.join(CLAUDE_DIR, "hooks")
SETTINGS_PATH = os.path.join(CLAUDE_DIR, "settings.json")

# hook が自身の位置 (<root>/.claude/hooks/) から `cd ... && pwd` で導出する
# リポジトリルート。テストは固定パスではなくこの値の下にパスを組み立てること
# (そうしないとチェックアウト先が異なる CI で照合が外れる)。
REPO_ROOT = os.path.realpath(os.path.dirname(CLAUDE_DIR))

# hook側と共有する契約。変更時は .claude/hooks/ の各スクリプトも合わせて直すこと。
ENV_RECOVERY_DIR = "TURN_RECOVERY_STATE_DIR"
ENV_CALLS_DIR = "AGENT_CALLS_STATE_DIR"
ENV_NOTES_DIR = "AGENT_NOTES_DIR"
ENV_CALL_TTL = "TURN_RECOVERY_CALL_TTL_SEC"
ENV_BRIEFING_TTL = "TURN_RECOVERY_TTL_SEC"
ENV_RECOVERY_DISABLE = "TURN_RECOVERY_DISABLE"
ENV_CALLS_DISABLE = "AGENT_CALLS_DISABLE"
ENV_GC_DAYS = "HOOK_STATE_GC_DAYS"

# テストが決して書き込んではならない実運用の状態ディレクトリ。
REAL_RECOVERY_DIR = os.path.realpath(os.path.join(CLAUDE_DIR, "recovery"))
REAL_CALLS_DIR = os.path.realpath(os.path.join(CLAUDE_DIR, "agent-calls"))
REAL_NOTES_DIR = os.path.realpath(os.path.join(CLAUDE_DIR, "agent-notes"))

HOOK_AGENT_CALL_RECORD = os.path.join(HOOKS_DIR, "agent-call-record.sh")
HOOK_AGENT_CALL_COMPLETE = os.path.join(HOOKS_DIR, "agent-call-complete.sh")
HOOK_TURN_FAILURE_RECORD = os.path.join(HOOKS_DIR, "turn-failure-record.sh")
HOOK_SESSION_START = os.path.join(HOOKS_DIR, "session-start.sh")
HOOK_STATE_GC = os.path.join(HOOKS_DIR, "recovery-state-gc.sh")


def hook_env(
    *,
    recovery_dir: str | None = None,
    calls_dir: str | None = None,
    notes_dir: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """hookをテスト用の一時ディレクトリに向ける環境変数を組み立てる。"""
    guarded = (
        (recovery_dir, REAL_RECOVERY_DIR),
        (calls_dir, REAL_CALLS_DIR),
        (notes_dir, REAL_NOTES_DIR),
    )
    for given, real in guarded:
        if given is not None and os.path.realpath(given) == real:
            raise AssertionError(
                f"テストが実運用の状態ディレクトリ ({real}) を指しています。"
                "一時ディレクトリを渡してください。"
            )

    env = dict(os.environ)
    if recovery_dir is not None:
        env[ENV_RECOVERY_DIR] = recovery_dir
    if calls_dir is not None:
        env[ENV_CALLS_DIR] = calls_dir
    if notes_dir is not None:
        env[ENV_NOTES_DIR] = notes_dir
    if extra:
        env.update(extra)
    return env


def run_hook(
    hook_path: str, payload: dict[str, object], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """hook に JSON ペイロードを stdin から流し込んで実行する。"""
    return subprocess.run(
        ["bash", hook_path],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def write_json(path: str, data: dict[str, object]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def read_json(path: str) -> dict[str, object]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, dict)
    return {str(k): v for k, v in data.items()}
