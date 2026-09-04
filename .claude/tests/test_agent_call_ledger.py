"""agent-call-record.sh (PostToolUse:Agent) / agent-call-complete.sh (SubagentStop)
の台帳テスト。

この2つが書く `.call.json` / `.done.json` が、未完了エージェント判定の唯一の材料。
守っている性質:

1. 起動は `tool_response.agentId` で keying する。PostToolUse は
   `tool_input.prompt` と `agentId` の両方を持つ唯一のイベントで、
   「どの agent_id がどんな依頼で起動されたか」はここでしか確定できない。
2. 完了は SubagentStop で記録する。PostToolUse は起動が返った時点で発火するため、
   完了の代わりに使うと並列で走っている兄弟を互いに完了済みとみなしてしまう。
3. agent_id が取れない入力は黙って諦める(紐付けられない記録を残さない)。
4. 何が起きても exit 0 で、標準出力に何も出さない(ノンブロッキング)。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from _hookenv import (
    ENV_CALLS_DISABLE,
    HOOK_AGENT_CALL_COMPLETE,
    HOOK_AGENT_CALL_RECORD,
    hook_env,
    read_json,
    run_hook,
)


def call_payload(
    *,
    session_id: str = "sess-1",
    prompt_id: str = "prompt-1",
    agent_id: str = "agent_1",
    subagent_type: str = "investigator",
    prompt: str = "認証まわりのエラー処理を洗い出す",
) -> dict[str, object]:
    return {
        "session_id": session_id,
        "prompt_id": prompt_id,
        "tool_use_id": "toolu_1",
        "tool_input": {"subagent_type": subagent_type, "prompt": prompt},
        "tool_response": {"agentId": agent_id, "status": "async_launched"},
    }


def done_payload(
    *,
    session_id: str = "sess-1",
    prompt_id: str = "prompt-2",
    agent_id: str = "agent_1",
    agent_type: str = "investigator",
    output: str = "結論: 応答形式は3種類",
) -> dict[str, object]:
    return {
        "session_id": session_id,
        "prompt_id": prompt_id,
        "agent_id": agent_id,
        "agent_type": agent_type,
        "last_assistant_message": output,
    }


class AgentCallRecordTests(unittest.TestCase):
    def test_writes_call_json_keyed_by_agent_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = hook_env(calls_dir=tmp)
            result = run_hook(HOOK_AGENT_CALL_RECORD, call_payload(), env)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

            path = os.path.join(tmp, "sess-1", "prompt-1", "agent_1.call.json")
            record = read_json(path)
            self.assertEqual(record["agent_id"], "agent_1")
            self.assertEqual(record["subagent_type"], "investigator")
            self.assertEqual(record["norm_lines"], ["認証まわりのエラー処理を洗い出す"])
            self.assertIsInstance(record["time"], float)

    def test_normalizes_prompt_lines(self) -> None:
        """空行と前後の空白を落として比較しやすい形にすること。"""
        with tempfile.TemporaryDirectory() as tmp:
            env = hook_env(calls_dir=tmp)
            run_hook(
                HOOK_AGENT_CALL_RECORD,
                call_payload(prompt="  一行目  \n\n\t二行目\n   \n"),
                env,
            )
            record = read_json(os.path.join(tmp, "sess-1", "prompt-1", "agent_1.call.json"))
            self.assertEqual(record["norm_lines"], ["一行目", "二行目"])

    def test_skips_when_agent_id_missing(self) -> None:
        """agent_id が無いと .done.json と紐付けられない。記録を残さない。"""
        with tempfile.TemporaryDirectory() as tmp:
            env = hook_env(calls_dir=tmp)
            payload = call_payload()
            payload["tool_response"] = {"status": "async_launched"}
            result = run_hook(HOOK_AGENT_CALL_RECORD, payload, env)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(os.listdir(tmp), [])

    def test_skips_when_session_id_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = hook_env(calls_dir=tmp)
            payload = call_payload()
            del payload["session_id"]
            result = run_hook(HOOK_AGENT_CALL_RECORD, payload, env)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(os.listdir(tmp), [])

    def test_missing_prompt_id_falls_back_to_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = hook_env(calls_dir=tmp)
            payload = call_payload()
            del payload["prompt_id"]
            run_hook(HOOK_AGENT_CALL_RECORD, payload, env)
            self.assertTrue(
                os.path.exists(os.path.join(tmp, "sess-1", "no-prompt-id", "agent_1.call.json"))
            )

    def test_path_separators_in_ids_do_not_escape_state_dir(self) -> None:
        """ID は safe_name で正規化され、状態ディレクトリの外に書かないこと。"""
        with tempfile.TemporaryDirectory() as tmp:
            env = hook_env(calls_dir=tmp)
            run_hook(
                HOOK_AGENT_CALL_RECORD,
                call_payload(session_id="../escape", agent_id="../../evil"),
                env,
            )
            written = [
                os.path.join(root, name)
                for root, _dirs, files in os.walk(tmp)
                for name in files
            ]
            self.assertEqual(len(written), 1)
            self.assertTrue(os.path.realpath(written[0]).startswith(os.path.realpath(tmp)))

    def test_disable_env_skips_recording(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = hook_env(calls_dir=tmp, extra={ENV_CALLS_DISABLE: "1"})
            result = run_hook(HOOK_AGENT_CALL_RECORD, call_payload(), env)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(os.listdir(tmp), [])

    def test_empty_stdin_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = hook_env(calls_dir=tmp)
            result = run_hook(HOOK_AGENT_CALL_RECORD, {}, env)
            self.assertEqual(result.returncode, 0)


class AgentCallCompleteTests(unittest.TestCase):
    def test_writes_done_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = hook_env(calls_dir=tmp)
            result = run_hook(HOOK_AGENT_CALL_COMPLETE, done_payload(), env)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

            record = read_json(os.path.join(tmp, "sess-1", "prompt-2", "agent_1.done.json"))
            self.assertEqual(record["agent_id"], "agent_1")
            self.assertEqual(record["agent_type"], "investigator")
            self.assertEqual(record["output_lines"], ["結論: 応答形式は3種類"])

    def test_skips_when_agent_id_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = hook_env(calls_dir=tmp)
            payload = done_payload()
            del payload["agent_id"]
            result = run_hook(HOOK_AGENT_CALL_COMPLETE, payload, env)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(os.listdir(tmp), [])

    def test_marks_truncation_for_long_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = hook_env(calls_dir=tmp)
            long_output = "\n".join(f"行{i}" for i in range(500))
            run_hook(HOOK_AGENT_CALL_COMPLETE, done_payload(output=long_output), env)
            record = read_json(os.path.join(tmp, "sess-1", "prompt-2", "agent_1.done.json"))
            self.assertEqual(len(record["output_lines"]), 400)
            self.assertTrue(record["output_truncated"])

    def test_disable_env_skips_recording(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = hook_env(calls_dir=tmp, extra={ENV_CALLS_DISABLE: "1"})
            result = run_hook(HOOK_AGENT_CALL_COMPLETE, done_payload(), env)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(os.listdir(tmp), [])


class LedgerContractTests(unittest.TestCase):
    """書く側と読む側が同じ場所・同じキーを使っていることの behavioral な検証。

    起動と完了は別々の hook・別々の prompt_id で書かれる。この結合点がずれると
    「完了しているのに未完了として提示される」という形で静かに壊れるため、
    実際に両方を走らせて突き合わせる。
    """

    def test_call_and_done_share_key_across_prompt_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = hook_env(calls_dir=tmp)
            run_hook(HOOK_AGENT_CALL_RECORD, call_payload(prompt_id="prompt-1"), env)
            run_hook(HOOK_AGENT_CALL_COMPLETE, done_payload(prompt_id="prompt-9"), env)

            session_dir = os.path.join(tmp, "sess-1")
            self.assertTrue(
                os.path.exists(os.path.join(session_dir, "prompt-1", "agent_1.call.json"))
            )
            self.assertTrue(
                os.path.exists(os.path.join(session_dir, "prompt-9", "agent_1.done.json"))
            )

    def test_default_state_dir_is_derived_from_hook_location(self) -> None:
        """環境変数を渡さないとき、両hookが同じ既定ディレクトリを組み立てること。"""
        import re

        for hook_path in (HOOK_AGENT_CALL_RECORD, HOOK_AGENT_CALL_COMPLETE):
            with self.subTest(hook=os.path.basename(hook_path)):
                with open(hook_path, encoding="utf-8") as f:
                    source = f.read()
                self.assertTrue(
                    re.search(
                        r'AGENT_CALLS_STATE_DIR:-\$REPO_ROOT/\.claude/agent-calls',
                        source,
                    ),
                    "既定の状態ディレクトリの組み立てが2つのhookで一致していません",
                )


if __name__ == "__main__":
    unittest.main()
