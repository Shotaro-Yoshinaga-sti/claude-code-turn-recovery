"""turn-failure-record.sh (StopFailure hook) のテスト。

APIエラーでターンが打ち切られたとき、失われた作業を .claude/recovery/ に
記録する。次のセッション開始時に session-start.sh がこれを読む。

このhookが守っている性質:

1. 「.call.json はあるが .done.json がない」= 中断時に走っていたエージェント。
   この判定を誤ると、再開時に完了済みの調査まで流し直してトークンを二重に捨てる。
2. 未完了の判定はセッション配下の *全* prompt_id を見る。バックグラウンドの
   サブエージェントは発言をまたいで生き続けるため、直近の prompt_id だけを
   見ると取りこぼす。
3. 未完了の切り捨ては呼び出し時刻の降順で行う。prompt_id は UUID で発行順とは
   無関係なので、辞書順のまま切ると「いま走っている呼び出し」が枠から漏れる。
4. error / last_assistant_message の抽出はキー名のゆれを吸収する。
5. 何が起きても exit 0 で、書けないときは何も残さない(fail-open)。
   StopFailure は stdout も終了コードも無視されるイベントなので、
   失敗を通知する手段がなく、静かに諦めるしかない。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from _hookenv import (
    ENV_CALL_TTL,
    ENV_RECOVERY_DISABLE,
    HOOK_TURN_FAILURE_RECORD,
    hook_env,
    read_json,
    run_hook,
    write_json,
)

SESSION = "sess-1"


def failure_payload(
    *,
    session_id: str = SESSION,
    prompt_id: str = "prompt-now",
    error: str | None = "rate_limit",
    message: str | None = "You've hit your session limit",
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": session_id,
        "prompt_id": prompt_id,
        "hook_event_name": "StopFailure",
    }
    if error is not None:
        payload["error"] = error
    if message is not None:
        payload["last_assistant_message"] = message
    if extra:
        payload.update(extra)
    return payload


def put_call(
    calls_dir: str,
    prompt_id: str,
    agent_id: str,
    *,
    session_id: str = SESSION,
    subagent_type: str = "investigator",
    when: float | None = None,
    lines: list[str] | None = None,
) -> None:
    write_json(
        os.path.join(calls_dir, session_id, prompt_id, f"{agent_id}.call.json"),
        {
            "agent_id": agent_id,
            "subagent_type": subagent_type,
            "norm_lines": lines if lines is not None else [f"{agent_id} の依頼"],
            "time": time.time() if when is None else when,
        },
    )


def put_done(
    calls_dir: str,
    prompt_id: str,
    agent_id: str,
    *,
    session_id: str = SESSION,
    agent_type: str = "investigator",
) -> None:
    write_json(
        os.path.join(calls_dir, session_id, prompt_id, f"{agent_id}.done.json"),
        {
            "agent_id": agent_id,
            "agent_type": agent_type,
            "output_lines": [f"{agent_id} の報告"],
            "time": time.time(),
        },
    )


def record_path(recovery_dir: str, session_id: str = SESSION) -> str:
    return os.path.join(recovery_dir, f"{session_id}.json")


class BasicRecordTests(unittest.TestCase):
    def test_writes_record_with_error_type_and_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            env = hook_env(recovery_dir=recovery, calls_dir=calls)
            result = run_hook(HOOK_TURN_FAILURE_RECORD, failure_payload(), env)

            self.assertEqual(result.returncode, 0)
            record = read_json(record_path(recovery))
            self.assertEqual(record["error_type"], "rate_limit")
            self.assertEqual(record["error_message"], "You've hit your session limit")
            self.assertEqual(record["session_id"], SESSION)
            self.assertEqual(record["prompt_id"], "prompt-now")

    def test_stdout_is_empty(self) -> None:
        """StopFailure の stdout は無視される。何も出さない設計であること。"""
        with tempfile.TemporaryDirectory() as tmp:
            env = hook_env(
                recovery_dir=os.path.join(tmp, "rec"), calls_dir=os.path.join(tmp, "calls")
            )
            result = run_hook(HOOK_TURN_FAILURE_RECORD, failure_payload(), env)
            self.assertEqual(result.stdout, "")

    def test_no_session_id_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            env = hook_env(recovery_dir=recovery, calls_dir=os.path.join(tmp, "calls"))
            payload = failure_payload()
            del payload["session_id"]
            result = run_hook(HOOK_TURN_FAILURE_RECORD, payload, env)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(os.path.exists(recovery))

    def test_disable_env_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            env = hook_env(
                recovery_dir=recovery,
                calls_dir=os.path.join(tmp, "calls"),
                extra={ENV_RECOVERY_DISABLE: "1"},
            )
            result = run_hook(HOOK_TURN_FAILURE_RECORD, failure_payload(), env)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(os.path.exists(recovery))

    def test_empty_stdin_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = hook_env(
                recovery_dir=os.path.join(tmp, "rec"), calls_dir=os.path.join(tmp, "calls")
            )
            result = run_hook(HOOK_TURN_FAILURE_RECORD, {}, env)
            self.assertEqual(result.returncode, 0)

    def test_record_is_replaced_atomically(self) -> None:
        """2回目の中断で上書きされ、一時ファイルが残らないこと。"""
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            env = hook_env(recovery_dir=recovery, calls_dir=calls)
            run_hook(HOOK_TURN_FAILURE_RECORD, failure_payload(error="overloaded"), env)
            run_hook(HOOK_TURN_FAILURE_RECORD, failure_payload(error="rate_limit"), env)

            self.assertEqual(read_json(record_path(recovery))["error_type"], "rate_limit")
            leftovers = [n for n in os.listdir(recovery) if n.startswith(".tmp-")]
            self.assertEqual(leftovers, [])


class ErrorFieldExtractionTests(unittest.TestCase):
    def test_error_details_is_used_when_message_absent(self) -> None:
        """last_assistant_message も error_details も optional。片方だけ来る中断がある。"""
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            env = hook_env(recovery_dir=recovery, calls_dir=os.path.join(tmp, "calls"))
            run_hook(
                HOOK_TURN_FAILURE_RECORD,
                failure_payload(message=None, extra={"error_details": "詳細のみ"}),
                env,
            )
            self.assertEqual(read_json(record_path(recovery))["error_message"], "詳細のみ")

    def test_error_object_form_is_accepted(self) -> None:
        """将来ペイロードが dict 形式に変わっても種別を拾えること。"""
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            env = hook_env(recovery_dir=recovery, calls_dir=os.path.join(tmp, "calls"))
            run_hook(
                HOOK_TURN_FAILURE_RECORD,
                failure_payload(
                    error=None,
                    message=None,
                    extra={"error": {"type": "server_error", "message": "落ちた"}},
                ),
                env,
            )
            record = read_json(record_path(recovery))
            self.assertEqual(record["error_type"], "server_error")
            self.assertEqual(record["error_message"], "落ちた")

    def test_rate_limit_is_inferred_from_message(self) -> None:
        """種別キーが1つも埋まらなくても、本文から大まかに推定する。"""
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            env = hook_env(recovery_dir=recovery, calls_dir=os.path.join(tmp, "calls"))
            run_hook(
                HOOK_TURN_FAILURE_RECORD,
                failure_payload(error=None, message="429 Too Many Requests"),
                env,
            )
            self.assertEqual(read_json(record_path(recovery))["error_type"], "rate_limit")

    def test_unknown_error_type_is_kept_as_is(self) -> None:
        """"unknown" は正規の値。日本語への読み替えは提示側 (session-start) の責務。"""
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            env = hook_env(recovery_dir=recovery, calls_dir=os.path.join(tmp, "calls"))
            run_hook(HOOK_TURN_FAILURE_RECORD, failure_payload(error="unknown"), env)
            self.assertEqual(read_json(record_path(recovery))["error_type"], "unknown")

    def test_payload_keys_are_recorded_for_diagnosis(self) -> None:
        """ペイロードの形が変わったときに気付けるよう、生のキー一覧を残す。"""
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            env = hook_env(recovery_dir=recovery, calls_dir=os.path.join(tmp, "calls"))
            run_hook(
                HOOK_TURN_FAILURE_RECORD,
                failure_payload(extra={"effort": {"level": "high"}}),
                env,
            )
            record = read_json(record_path(recovery))
            self.assertIn("effort", record["payload_keys"])
            self.assertIn("effort", record["payload_preview"])

    def test_payload_preview_excludes_already_stored_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            env = hook_env(recovery_dir=recovery, calls_dir=os.path.join(tmp, "calls"))
            run_hook(HOOK_TURN_FAILURE_RECORD, failure_payload(), env)
            preview = read_json(record_path(recovery))["payload_preview"]
            assert isinstance(preview, dict)
            for key in ("session_id", "prompt_id", "error", "last_assistant_message"):
                self.assertNotIn(key, preview)


class PendingAgentTests(unittest.TestCase):
    def test_call_without_done_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            put_call(calls, "prompt-now", "agent_live")
            env = hook_env(recovery_dir=recovery, calls_dir=calls)
            run_hook(HOOK_TURN_FAILURE_RECORD, failure_payload(), env)

            record = read_json(record_path(recovery))
            pending = record["pending_agents"]
            assert isinstance(pending, list)
            self.assertEqual([p["agent_id"] for p in pending], ["agent_live"])
            self.assertEqual(record["pending_total"], 1)

    def test_done_in_earlier_prompt_id_is_not_pending(self) -> None:
        """性質2の回帰テスト。起動と完了の prompt_id は日常的に食い違う。"""
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            put_call(calls, "prompt-old", "agent_done")
            put_done(calls, "prompt-now", "agent_done")
            env = hook_env(recovery_dir=recovery, calls_dir=calls)
            run_hook(HOOK_TURN_FAILURE_RECORD, failure_payload(), env)

            self.assertEqual(read_json(record_path(recovery))["pending_agents"], [])

    def test_pending_carries_role_and_prompt_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            put_call(
                calls,
                "prompt-now",
                "agent_1",
                subagent_type="implementer",
                lines=["A を実装する", "B のテストも足す"],
            )
            env = hook_env(recovery_dir=recovery, calls_dir=calls)
            run_hook(HOOK_TURN_FAILURE_RECORD, failure_payload(), env)

            pending = read_json(record_path(recovery))["pending_agents"]
            assert isinstance(pending, list)
            self.assertEqual(pending[0]["subagent_type"], "implementer")
            self.assertEqual(pending[0]["prompt_head"], ["A を実装する", "B のテストも足す"])
            self.assertIsInstance(pending[0]["age_sec"], float)

    def test_stale_call_is_excluded_by_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            put_call(calls, "prompt-now", "agent_stale", when=time.time() - 100.0)
            env = hook_env(
                recovery_dir=recovery, calls_dir=calls, extra={ENV_CALL_TTL: "10"}
            )
            run_hook(HOOK_TURN_FAILURE_RECORD, failure_payload(), env)
            self.assertEqual(read_json(record_path(recovery))["pending_agents"], [])

    def test_other_session_calls_are_ignored(self) -> None:
        """このhookは現在のセッションだけを見る(孤児走査は session-start の担当)。"""
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            put_call(calls, "prompt-now", "agent_other", session_id="sess-other")
            env = hook_env(recovery_dir=recovery, calls_dir=calls)
            run_hook(HOOK_TURN_FAILURE_RECORD, failure_payload(), env)
            self.assertEqual(read_json(record_path(recovery))["pending_agents"], [])

    def test_newest_calls_survive_the_cap(self) -> None:
        """性質3の回帰テスト。

        辞書順で後ろに来る prompt_id に live な呼び出しを置き、辞書順で前に来る
        prompt_id を古い呼び出しで埋める。時刻順に切っていなければ live が落ちる。
        """
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            old = time.time() - 3600.0
            for i in range(15):
                put_call(calls, "aaa-old-prompt", f"agent_old_{i:02d}", when=old + i)
            put_call(calls, "zzz-now-prompt", "agent_live", when=time.time())

            env = hook_env(recovery_dir=recovery, calls_dir=calls)
            run_hook(HOOK_TURN_FAILURE_RECORD, failure_payload(prompt_id="zzz-now-prompt"), env)

            record = read_json(record_path(recovery))
            pending = record["pending_agents"]
            assert isinstance(pending, list)
            self.assertEqual(len(pending), 12)
            self.assertEqual(pending[0]["agent_id"], "agent_live")
            self.assertEqual(record["pending_total"], 16)

    def test_call_without_time_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            write_json(
                os.path.join(calls, SESSION, "prompt-now", "agent_notime.call.json"),
                {"agent_id": "agent_notime", "subagent_type": "investigator"},
            )
            env = hook_env(recovery_dir=recovery, calls_dir=calls)
            result = run_hook(HOOK_TURN_FAILURE_RECORD, failure_payload(), env)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(read_json(record_path(recovery))["pending_agents"], [])

    def test_missing_calls_dir_still_writes_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            env = hook_env(recovery_dir=recovery, calls_dir=os.path.join(tmp, "nope"))
            run_hook(HOOK_TURN_FAILURE_RECORD, failure_payload(), env)
            record = read_json(record_path(recovery))
            self.assertEqual(record["pending_agents"], [])
            self.assertEqual(record["error_type"], "rate_limit")


class FinishedAgentTests(unittest.TestCase):
    def test_finished_agents_come_from_current_prompt_id(self) -> None:
        """「このターンで完了済み」は再実行させないための情報。"""
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            put_done(calls, "prompt-now", "agent_done", agent_type="reviewer")
            env = hook_env(recovery_dir=recovery, calls_dir=calls)
            run_hook(HOOK_TURN_FAILURE_RECORD, failure_payload(prompt_id="prompt-now"), env)

            finished = read_json(record_path(recovery))["finished_agents"]
            assert isinstance(finished, list)
            self.assertEqual([f["agent_id"] for f in finished], ["agent_done"])
            self.assertEqual(finished[0]["agent_type"], "reviewer")

    def test_finished_agents_exclude_other_prompt_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            put_done(calls, "prompt-old", "agent_done")
            env = hook_env(recovery_dir=recovery, calls_dir=calls)
            run_hook(HOOK_TURN_FAILURE_RECORD, failure_payload(prompt_id="prompt-now"), env)
            self.assertEqual(read_json(record_path(recovery))["finished_agents"], [])


class GitContextTests(unittest.TestCase):
    def test_branch_field_exists(self) -> None:
        """再開時に「中断時と違うブランチにいる」を検出できるようにする。"""
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            env = hook_env(recovery_dir=recovery, calls_dir=os.path.join(tmp, "calls"))
            run_hook(HOOK_TURN_FAILURE_RECORD, failure_payload(), env)
            record = read_json(record_path(recovery))
            self.assertIn("branch", record)
            self.assertIsInstance(record["branch"], str)
            self.assertIsInstance(record["git_status_lines"], list)


if __name__ == "__main__":
    unittest.main()
