"""pending_agents_shared.py (未完了エージェント判定の2パス走査) のテスト。

このモジュールが守っている性質:

1. `.done.json` の集約は **セッション配下の全 prompt_id** を見る。
   バックグラウンドのサブエージェントは発言をまたいで生き続けるため、起動時と
   完了時で prompt_id が変わる。片方の prompt_id だけを見て突き合わせると、
   完了済みのエージェントを未完了と誤認する。
2. `time` が無い/不正なレコードは fresh 扱いにしない。SubagentStop が発火せずに
   死んだ記録が、鮮度フィルタ無しではセッションの間ずっと未完了枠を食い続ける。
3. 並べ替え・件数上限はここでは行わない(呼び出し側の関心事)。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

from _hookenv import HOOKS_DIR, write_json

sys.path.insert(0, HOOKS_DIR)

from pending_agents_shared import (  # noqa: E402
    cast_number,
    collect_done_keys,
    collect_pending_calls,
    is_fresh,
    load_json_object,
)


def call_path(session_dir: str, prompt_id: str, agent_id: str) -> str:
    return os.path.join(session_dir, prompt_id, f"{agent_id}.call.json")


def done_path(session_dir: str, prompt_id: str, agent_id: str) -> str:
    return os.path.join(session_dir, prompt_id, f"{agent_id}.done.json")


class CastNumberTests(unittest.TestCase):
    def test_accepts_int_and_float(self) -> None:
        self.assertEqual(cast_number(3), 3)
        self.assertEqual(cast_number(2.5), 2.5)

    def test_rejects_non_number(self) -> None:
        for bad in ("12", None, [], {}):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    cast_number(bad)

    def test_rejects_string_even_if_numeric(self) -> None:
        """文字列を素通しにすると外部入力が黙って数値化される。実行時に弾く。"""
        with self.assertRaises(TypeError):
            cast_number("1700000000.0")


class IsFreshTests(unittest.TestCase):
    def test_recent_call_is_fresh(self) -> None:
        now = 1000.0
        self.assertTrue(is_fresh(now - 10, now, 100.0))

    def test_old_call_is_not_fresh(self) -> None:
        now = 1000.0
        self.assertFalse(is_fresh(now - 500, now, 100.0))

    def test_missing_time_is_not_fresh(self) -> None:
        self.assertFalse(is_fresh(None, 1000.0, 100.0))

    def test_invalid_time_is_not_fresh(self) -> None:
        self.assertFalse(is_fresh("いつか", 1000.0, 100.0))


class LoadJsonObjectTests(unittest.TestCase):
    def test_returns_none_for_missing_file(self) -> None:
        self.assertIsNone(load_json_object("/nonexistent/x.json"))

    def test_returns_none_for_non_object_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("[1, 2, 3]")
            self.assertIsNone(load_json_object(path))

    def test_returns_none_for_broken_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{ broken")
            self.assertIsNone(load_json_object(path))


class CollectDoneKeysTests(unittest.TestCase):
    def test_aggregates_across_all_prompt_dirs(self) -> None:
        """起動と完了で prompt_id が変わっても完了として拾えること。"""
        with tempfile.TemporaryDirectory() as tmp:
            write_json(done_path(tmp, "prompt-A", "agent_1"), {"agent_id": "agent_1"})
            write_json(done_path(tmp, "prompt-B", "agent_2"), {"agent_id": "agent_2"})
            keys = collect_done_keys(tmp, sorted(os.listdir(tmp)))
            self.assertEqual(keys, {"agent_1", "agent_2"})

    def test_ignores_call_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_json(call_path(tmp, "prompt-A", "agent_1"), {"agent_id": "agent_1"})
            self.assertEqual(collect_done_keys(tmp, sorted(os.listdir(tmp))), set())

    def test_missing_prompt_dir_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(collect_done_keys(tmp, ["does-not-exist"]), set())


class CollectPendingCallsTests(unittest.TestCase):
    def test_call_without_done_is_pending(self) -> None:
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            write_json(
                call_path(tmp, "prompt-A", "agent_1"),
                {"agent_id": "agent_1", "subagent_type": "investigator", "time": now},
            )
            prompt_dirs = sorted(os.listdir(tmp))
            pending = collect_pending_calls(
                tmp, prompt_dirs, collect_done_keys(tmp, prompt_dirs), now, 86400.0
            )
            self.assertEqual([p.key for p in pending], ["agent_1"])

    def test_done_in_other_prompt_dir_clears_pending(self) -> None:
        """性質1の回帰テスト。ここが壊れると完了済みを再実行させてしまう。"""
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            write_json(call_path(tmp, "prompt-A", "agent_1"), {"agent_id": "agent_1", "time": now})
            write_json(done_path(tmp, "prompt-B", "agent_1"), {"agent_id": "agent_1"})
            prompt_dirs = sorted(os.listdir(tmp))
            pending = collect_pending_calls(
                tmp, prompt_dirs, collect_done_keys(tmp, prompt_dirs), now, 86400.0
            )
            self.assertEqual(pending, [])

    def test_stale_call_is_dropped(self) -> None:
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            write_json(
                call_path(tmp, "prompt-A", "agent_old"),
                {"agent_id": "agent_old", "time": now - 100000},
            )
            prompt_dirs = sorted(os.listdir(tmp))
            pending = collect_pending_calls(
                tmp, prompt_dirs, collect_done_keys(tmp, prompt_dirs), now, 86400.0
            )
            self.assertEqual(pending, [])

    def test_call_without_time_is_dropped(self) -> None:
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            write_json(call_path(tmp, "prompt-A", "agent_x"), {"agent_id": "agent_x"})
            prompt_dirs = sorted(os.listdir(tmp))
            pending = collect_pending_calls(
                tmp, prompt_dirs, collect_done_keys(tmp, prompt_dirs), now, 86400.0
            )
            self.assertEqual(pending, [])

    def test_pending_carries_call_path_and_prompt_dir(self) -> None:
        """呼び出し側が整形に使う情報が揃っていること。"""
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            path = call_path(tmp, "prompt-A", "agent_1")
            write_json(path, {"agent_id": "agent_1", "time": now})
            prompt_dirs = sorted(os.listdir(tmp))
            pending = collect_pending_calls(
                tmp, prompt_dirs, collect_done_keys(tmp, prompt_dirs), now, 86400.0
            )
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].call_path, path)
            self.assertEqual(pending[0].prompt_dir_name, "prompt-A")
            self.assertEqual(pending[0].call.get("agent_id"), "agent_1")

    def test_broken_call_json_is_skipped_without_raising(self) -> None:
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "prompt-A"), exist_ok=True)
            with open(call_path(tmp, "prompt-A", "broken"), "w", encoding="utf-8") as f:
                f.write("{ not json")
            prompt_dirs = sorted(os.listdir(tmp))
            pending = collect_pending_calls(
                tmp, prompt_dirs, collect_done_keys(tmp, prompt_dirs), now, 86400.0
            )
            self.assertEqual(pending, [])


if __name__ == "__main__":
    unittest.main()
