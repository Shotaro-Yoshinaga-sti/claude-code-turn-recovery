"""settings.json と hook スクリプトの登録契約テスト。

このハーネスは5つの hook が別々のイベントで動いて初めて成立する。1つでも
登録が抜けると、エラーは出ないまま「中断記録が作られない」「作られても
提示されない」という形で静かに壊れる。壊れ方が沈黙なので、登録そのものを
機械的に検査する。

- StopFailure が無い → 中断が記録されない
- SessionStart が無い → 記録されても再開ブリーフィングが出ない
- PostToolUse(Agent) が無い → 「誰が走っていたか」が分からない
- SubagentStop が無い → 完了済みのエージェントを未完了と誤認し続ける
- UserPromptSubmit が無い → 状態が無限に溜まる
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest

from _hookenv import HOOKS_DIR, SETTINGS_PATH

EXPECTED = {
    "SessionStart": ("session-start.sh", None),
    "UserPromptSubmit": ("recovery-state-gc.sh", None),
    "PostToolUse": ("agent-call-record.sh", "Agent"),
    "SubagentStop": ("agent-call-complete.sh", None),
    "StopFailure": ("turn-failure-record.sh", None),
}


def load_settings() -> dict[str, object]:
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, dict)
    return {str(k): v for k, v in data.items()}


def entries_for(settings: dict[str, object], event: str) -> list[dict[str, object]]:
    hooks = settings.get("hooks")
    assert isinstance(hooks, dict)
    raw = hooks.get(event)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


class SettingsContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings()

    def test_all_hooks_are_registered(self) -> None:
        for event, (script, _matcher) in EXPECTED.items():
            with self.subTest(event=event):
                commands = [
                    str(hook.get("command", ""))
                    for entry in entries_for(self.settings, event)
                    for hook in entry.get("hooks", [])
                    if isinstance(hook, dict)
                ]
                self.assertTrue(
                    any(script in command for command in commands),
                    f"{event} に {script} が登録されていません",
                )

    def test_matchers_are_correct(self) -> None:
        """PostToolUse は Agent に絞る。全ツールに発火させない。"""
        for event, (script, matcher) in EXPECTED.items():
            if matcher is None:
                continue
            with self.subTest(event=event):
                found = [
                    entry.get("matcher")
                    for entry in entries_for(self.settings, event)
                    for hook in entry.get("hooks", [])
                    if isinstance(hook, dict) and script in str(hook.get("command", ""))
                ]
                self.assertEqual(found, [matcher])

    def test_stop_failure_has_no_matcher(self) -> None:
        """rate_limit 以外でも作業は飛ぶ。matcher で絞らないこと。"""
        for entry in entries_for(self.settings, "StopFailure"):
            self.assertNotIn("matcher", entry)

    def test_commands_use_project_dir_variable(self) -> None:
        """絶対パス決め打ちにしない(チェックアウト先が異なる環境で壊れる)。"""
        hooks = self.settings.get("hooks")
        assert isinstance(hooks, dict)
        for event, raw in hooks.items():
            assert isinstance(raw, list)
            for entry in raw:
                for hook in entry.get("hooks", []):
                    command = str(hook.get("command", ""))
                    with self.subTest(event=event, command=command):
                        self.assertIn("$CLAUDE_PROJECT_DIR", command)

    def test_referenced_scripts_exist_and_are_executable(self) -> None:
        for _event, (script, _matcher) in EXPECTED.items():
            path = os.path.join(HOOKS_DIR, script)
            with self.subTest(script=script):
                self.assertTrue(os.path.exists(path), f"{script} がありません")
                self.assertTrue(os.access(path, os.X_OK), f"{script} に実行権限がありません")

    def test_shared_module_is_present(self) -> None:
        self.assertTrue(os.path.exists(os.path.join(HOOKS_DIR, "pending_agents_shared.py")))

    def test_auto_background_tasks_is_enabled(self) -> None:
        """バックグラウンドのサブエージェントは失敗しても最後の出力が親に渡る。

        フォアグラウンドはまだ何も出力していなければ全損になるため、
        長い作業を自動でバックグラウンドへ移す設定を既定で入れている。
        """
        env = self.settings.get("env")
        assert isinstance(env, dict)
        self.assertEqual(env.get("CLAUDE_AUTO_BACKGROUND_TASKS"), "1")


class ShellSyntaxTests(unittest.TestCase):
    def test_all_hooks_parse(self) -> None:
        for name in sorted(os.listdir(HOOKS_DIR)):
            if not name.endswith(".sh"):
                continue
            with self.subTest(script=name):
                result = subprocess.run(
                    ["bash", "-n", os.path.join(HOOKS_DIR, name)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
