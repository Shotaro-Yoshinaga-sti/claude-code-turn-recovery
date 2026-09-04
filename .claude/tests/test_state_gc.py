"""recovery-state-gc.sh (UserPromptSubmit hook) のテスト。

このhookが守っている性質:

1. 新しい状態は消さない。GC が生きている中断記録や走行中の台帳を消すと、
   再開ブリーフィングが出ないまま作業が失われる。
2. 名前で絞って消す。利用者が同じ場所に置いた無関係なファイルを巻き込まない。
3. 提示済み (.json.consumed) も同じ基準で回収する。
4. 何が起きても exit 0 で、標準出力に何も出さない(ノンブロッキング)。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest

from _hookenv import ENV_GC_DAYS, HOOK_STATE_GC, hook_env, run_hook

# 8日前。既定の保持期間 (7日) を確実に超える。
OLD_MTIME = time.time() - 8 * 86400


def touch(path: str, *, old: bool = False, content: str = "{}") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if old:
        os.utime(path, (OLD_MTIME, OLD_MTIME))
    return path


def make_dir(path: str, *, old: bool = False) -> str:
    os.makedirs(path, exist_ok=True)
    if old:
        os.utime(path, (OLD_MTIME, OLD_MTIME))
    return path


def run_gc(
    recovery: str, calls: str, notes: str, *, extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = hook_env(recovery_dir=recovery, calls_dir=calls, notes_dir=notes, extra=extra)
    return run_hook(HOOK_STATE_GC, {"session_id": "sess-1"}, env)


class StateGcTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = self._tmp.name
        self.recovery = os.path.join(base, "recovery")
        self.calls = os.path.join(base, "agent-calls")
        self.notes = os.path.join(base, "agent-notes")
        for path in (self.recovery, self.calls, self.notes):
            os.makedirs(path, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_is_nonblocking(self) -> None:
        result = run_gc(self.recovery, self.calls, self.notes)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_missing_dirs_are_tolerated(self) -> None:
        base = self._tmp.name
        result = run_gc(
            os.path.join(base, "nope-1"),
            os.path.join(base, "nope-2"),
            os.path.join(base, "nope-3"),
        )
        self.assertEqual(result.returncode, 0)

    def test_old_session_dir_is_removed(self) -> None:
        old = make_dir(os.path.join(self.calls, "sess-old"), old=True)
        run_gc(self.recovery, self.calls, self.notes)
        self.assertFalse(os.path.exists(old))

    def test_recent_session_dir_survives(self) -> None:
        """性質1: 走行中の台帳を消すと未完了判定の材料が消える。"""
        recent = make_dir(os.path.join(self.calls, "sess-now"))
        run_gc(self.recovery, self.calls, self.notes)
        self.assertTrue(os.path.exists(recent))

    def test_nested_sidecar_is_collected_with_session_dir(self) -> None:
        session_dir = os.path.join(self.calls, "sess-old")
        touch(os.path.join(session_dir, "prompt-1", "agent_1.call.json"), old=True)
        touch(os.path.join(session_dir, "prompt-1", "agent_1.presented.json"), old=True)
        make_dir(os.path.join(session_dir, "prompt-1"), old=True)
        make_dir(session_dir, old=True)
        run_gc(self.recovery, self.calls, self.notes)
        self.assertFalse(os.path.exists(session_dir))

    def test_old_recovery_records_are_removed(self) -> None:
        old_json = touch(os.path.join(self.recovery, "sess-old.json"), old=True)
        old_consumed = touch(os.path.join(self.recovery, "sess-x.json.consumed"), old=True)
        run_gc(self.recovery, self.calls, self.notes)
        self.assertFalse(os.path.exists(old_json))
        self.assertFalse(os.path.exists(old_consumed))

    def test_recent_recovery_record_survives(self) -> None:
        recent = touch(os.path.join(self.recovery, "sess-now.json"))
        run_gc(self.recovery, self.calls, self.notes)
        self.assertTrue(os.path.exists(recent))

    def test_unrelated_files_are_not_removed(self) -> None:
        """性質2: 名前で絞る。README や設定を巻き込まない。"""
        keep = touch(os.path.join(self.recovery, "README.md"), old=True, content="memo")
        run_gc(self.recovery, self.calls, self.notes)
        self.assertTrue(os.path.exists(keep))

    def test_old_notes_are_removed_and_recent_survive(self) -> None:
        old_note = touch(os.path.join(self.notes, "20260101-investigator-a.md"), old=True)
        new_note = touch(os.path.join(self.notes, "20260904-investigator-b.md"))
        run_gc(self.recovery, self.calls, self.notes)
        self.assertFalse(os.path.exists(old_note))
        self.assertTrue(os.path.exists(new_note))

    def test_gc_days_is_configurable(self) -> None:
        note = touch(os.path.join(self.notes, "note.md"), old=True)
        run_gc(self.recovery, self.calls, self.notes, extra={ENV_GC_DAYS: "30"})
        self.assertTrue(os.path.exists(note), "保持期間を延ばしたら残ること")


if __name__ == "__main__":
    unittest.main()
