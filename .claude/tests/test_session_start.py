"""session-start.sh (SessionStart hook) のテスト。

turn-failure-record.sh が書いた中断記録を、再開ブリーフィングとして stdout に
出す。SessionStart の stdout はそのまま Claude のコンテキストに入るため、
ここでの文言は毎セッション自動で注入される「ハーネスの発言」になる。

このhookが守っている性質:

1. 記録が無ければ何も出さない(沈黙する)。存在しない中断の再開を促さない。
2. 一度提示した記録は二度出さない(.json.consumed へ改名)。
3. TTL (既定24h) を過ぎた記録は提示しない。別の作業の話とみなす。
4. 未完了エージェントがあるときは、SendMessage で再開する手順と、
   不達時の出口 (新規委譲) を必ず添える。出口が無いと消去法で
   「メインが自分で実装する」が唯一の選択肢になる。
5. 検証不能な事実主張を書かない。SessionStart はローカルで走るので、
   「制限は明けている」とは断定できない。
6. 中断記録が無いときに限り、台帳を走査して「走ったまま終わった」
   サブエージェントを拾う(サブエージェントだけが殺された中断では
   StopFailure が発火せず記録が作られないため)。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest

from _hookenv import (
    ENV_BRIEFING_TTL,
    ENV_CALL_TTL,
    HOOK_SESSION_START,
    hook_env,
    run_hook,
    write_json,
)

SESSION = "sess-1"


def put_record(
    recovery_dir: str,
    *,
    session_id: str = SESSION,
    error_type: str = "rate_limit",
    message: str = "You've hit your session limit",
    when: float | None = None,
    pending: list[dict[str, object]] | None = None,
    pending_total: int | None = None,
    finished: list[dict[str, object]] | None = None,
    branch: str = "",
) -> str:
    pending = pending or []
    path = os.path.join(recovery_dir, f"{session_id}.json")
    write_json(
        path,
        {
            "session_id": session_id,
            "prompt_id": "prompt-now",
            "error_type": error_type,
            "error_message": message,
            "time": time.time() if when is None else when,
            "branch": branch,
            "git_status_lines": [],
            "pending_agents": pending,
            "pending_total": pending_total if pending_total is not None else len(pending),
            "finished_agents": finished or [],
        },
    )
    return path


def agent_entry(
    agent_id: str,
    *,
    role: str = "investigator",
    head: list[str] | None = None,
    age_sec: float = 5.0,
) -> dict[str, object]:
    return {
        "agent_id": agent_id,
        "subagent_type": role,
        "prompt_id": "prompt-now",
        "prompt_head": head if head is not None else [f"{agent_id} の依頼"],
        "age_sec": age_sec,
    }


def run_session_start(
    recovery_dir: str,
    calls_dir: str,
    *,
    session_id: str = SESSION,
    extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = hook_env(recovery_dir=recovery_dir, calls_dir=calls_dir, extra=extra)
    return run_hook(HOOK_SESSION_START, {"session_id": session_id}, env)


class SilenceTests(unittest.TestCase):
    def test_no_record_produces_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_session_start(os.path.join(tmp, "rec"), os.path.join(tmp, "calls"))
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "")

    def test_expired_record_is_not_shown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            put_record(recovery, when=time.time() - 100.0)
            result = run_session_start(
                recovery, os.path.join(tmp, "calls"), extra={ENV_BRIEFING_TTL: "10"}
            )
            self.assertEqual(result.stdout.strip(), "")

    def test_broken_record_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            os.makedirs(recovery, exist_ok=True)
            with open(os.path.join(recovery, f"{SESSION}.json"), "w", encoding="utf-8") as f:
                f.write("{ broken")
            result = run_session_start(recovery, os.path.join(tmp, "calls"))
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "")


class BriefingContentTests(unittest.TestCase):
    def test_heading_shows_error_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            put_record(recovery, error_type="rate_limit")
            out = run_session_start(recovery, os.path.join(tmp, "calls")).stdout
            self.assertIn("## 前回ターンの中断記録 (rate_limit)", out)

    def test_unknown_error_type_is_localized(self) -> None:
        """性質: "unknown" は正規の値。英語のまま見出しに出さない。"""
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            put_record(recovery, error_type="unknown")
            out = run_session_start(recovery, os.path.join(tmp, "calls")).stdout
            self.assertIn("原因不明", out)
            self.assertNotIn("(unknown)", out)

    def test_empty_error_type_is_localized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            put_record(recovery, error_type="")
            out = run_session_start(recovery, os.path.join(tmp, "calls")).stdout
            self.assertIn("原因不明", out)

    def test_does_not_claim_the_limit_has_lifted(self) -> None:
        """性質5の回帰テスト。

        SessionStart はローカルで走るので、制限中でもこのブリーフィングは出る。
        「制限は解消している」と断定すると、モデルには疑う手段がない。
        """
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            put_record(recovery)
            out = run_session_start(recovery, os.path.join(tmp, "calls")).stdout
            self.assertIn("この記録からは分からない", out)
            self.assertNotIn("既に解消している", out)

    def test_branch_change_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            put_record(recovery, branch="feature/xyz")
            out = run_session_start(recovery, os.path.join(tmp, "calls")).stdout
            # 現在のブランチが取得できた環境でだけ差分行が出る。
            if "中断時のブランチ" in out:
                self.assertIn("feature/xyz", out)

    def test_message_is_included(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            put_record(recovery, message="resets 8:40pm")
            out = run_session_start(recovery, os.path.join(tmp, "calls")).stdout
            self.assertIn("resets 8:40pm", out)


class PendingAgentBriefingTests(unittest.TestCase):
    def test_pending_agents_are_listed_with_role_and_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            put_record(recovery, pending=[agent_entry("agent_1", role="implementer")])
            out = run_session_start(recovery, os.path.join(tmp, "calls")).stdout
            self.assertIn("未完了だったサブエージェント (1件)", out)
            self.assertIn("implementer [agent_1]", out)

    def test_resume_steps_include_unreachable_fallback(self) -> None:
        """性質4の回帰テスト。出口が無いと自力実装しか残らない。"""
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            put_record(recovery, pending=[agent_entry("agent_1")])
            out = run_session_start(recovery, os.path.join(tmp, "calls")).stdout
            self.assertIn("SendMessage", out)
            self.assertIn("agent-notes", out)
            self.assertIn("新しいサブエージェントに委譲", out)
            self.assertIn("最後の手段", out)

    def test_no_resume_steps_without_pending_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            put_record(recovery, pending=[])
            out = run_session_start(recovery, os.path.join(tmp, "calls")).stdout
            self.assertNotIn("再開の手順", out)

    def test_stale_agents_are_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            put_record(recovery, pending=[agent_entry("agent_old", age_sec=7200.0)])
            out = run_session_start(recovery, os.path.join(tmp, "calls")).stdout
            self.assertIn("(古い)", out)

    def test_fresh_agents_are_not_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            put_record(recovery, pending=[agent_entry("agent_new", age_sec=30.0)])
            out = run_session_start(recovery, os.path.join(tmp, "calls")).stdout
            self.assertNotIn("(古い)", out)

    def test_truncation_is_visible(self) -> None:
        """記録側 (12件上限) と表示側 (8件上限) の切り捨てを読む側に見せる。"""
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            put_record(
                recovery,
                pending=[agent_entry(f"agent_{i:02d}") for i in range(12)],
                pending_total=20,
            )
            out = run_session_start(recovery, os.path.join(tmp, "calls")).stdout
            self.assertIn("上限により省略", out)
            self.assertIn("他 12 件", out)

    def test_finished_agents_are_marked_do_not_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            put_record(
                recovery,
                finished=[{"agent_id": "agent_done", "agent_type": "reviewer"}],
            )
            out = run_session_start(recovery, os.path.join(tmp, "calls")).stdout
            self.assertIn("完了済み", out)
            self.assertIn("再実行しない", out)

    def test_builtin_agents_are_called_out_as_unresumable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            put_record(recovery, pending=[agent_entry("agent_1")])
            out = run_session_start(recovery, os.path.join(tmp, "calls")).stdout
            self.assertIn("Explore / Plan は再開できない", out)


class ConsumeTests(unittest.TestCase):
    def test_record_is_consumed_after_showing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            path = put_record(recovery)
            run_session_start(recovery, os.path.join(tmp, "calls"))
            self.assertFalse(os.path.exists(path))
            self.assertTrue(os.path.exists(f"{path}.consumed"))

    def test_second_run_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            put_record(recovery)
            first = run_session_start(recovery, calls)
            second = run_session_start(recovery, calls)
            self.assertIn("中断記録", first.stdout)
            self.assertEqual(second.stdout.strip(), "")


class OtherSessionRecordTests(unittest.TestCase):
    def test_record_from_other_session_is_shown_with_caveat(self) -> None:
        """制限後に新しいセッションを立てる運用でも拾えること。"""
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            put_record(recovery, session_id="sess-old")
            result = run_session_start(
                recovery, os.path.join(tmp, "calls"), session_id="sess-new"
            )
            self.assertIn("別セッションの記録", result.stdout)
            self.assertIn("同じ作業の続きなら", result.stdout)

    def test_exact_session_match_has_no_caveat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = os.path.join(tmp, "rec")
            put_record(recovery, session_id=SESSION)
            out = run_session_start(recovery, os.path.join(tmp, "calls")).stdout
            self.assertNotIn("別セッションの記録", out)


class OrphanAgentTests(unittest.TestCase):
    """性質6: サブエージェントだけが殺された中断は StopFailure が発火しない。"""

    def put_orphan(
        self, calls_dir: str, session_id: str, agent_id: str, *, when: float | None = None
    ) -> str:
        path = os.path.join(calls_dir, session_id, "prompt-1", f"{agent_id}.call.json")
        write_json(
            path,
            {
                "agent_id": agent_id,
                "subagent_type": "investigator",
                "norm_lines": [f"{agent_id} の依頼"],
                "time": time.time() if when is None else when,
            },
        )
        return path

    def test_orphan_from_other_session_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            self.put_orphan(calls, "sess-old", "agent_orphan")
            out = run_session_start(recovery, calls, session_id="sess-new").stdout
            self.assertIn("走ったまま終わったサブエージェント", out)
            self.assertIn("agent_orphan", out)

    def test_current_session_is_excluded(self) -> None:
        """現在のセッションのエージェントはまだ生きている可能性がある。"""
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            self.put_orphan(calls, "sess-new", "agent_live")
            out = run_session_start(recovery, calls, session_id="sess-new").stdout
            self.assertEqual(out.strip(), "")

    def test_completed_agent_is_not_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            self.put_orphan(calls, "sess-old", "agent_done")
            write_json(
                os.path.join(calls, "sess-old", "prompt-9", "agent_done.done.json"),
                {"agent_id": "agent_done", "agent_type": "investigator"},
            )
            out = run_session_start(recovery, calls, session_id="sess-new").stdout
            self.assertEqual(out.strip(), "")

    def test_orphan_scan_is_skipped_when_record_exists(self) -> None:
        """中断記録があるときは二重に出さない。"""
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            put_record(recovery, session_id="sess-new")
            self.put_orphan(calls, "sess-old", "agent_orphan")
            out = run_session_start(recovery, calls, session_id="sess-new").stdout
            self.assertIn("中断記録", out)
            self.assertNotIn("走ったまま終わったサブエージェント", out)

    def test_presented_marker_is_a_sidecar_not_a_rename(self) -> None:
        """性質: 台帳の主データ (.call.json) はリネームしない。

        リネームすると、セッションをまたいで .call.json を読む hook から
        提示済みのものだけが黙って見えなくなる。
        """
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            call_path = self.put_orphan(calls, "sess-old", "agent_orphan")
            run_session_start(recovery, calls, session_id="sess-new")

            self.assertTrue(os.path.exists(call_path), ".call.json が残っていること")
            marker = call_path[: -len(".call.json")] + ".presented.json"
            self.assertTrue(os.path.exists(marker))

    def test_orphan_is_not_presented_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            self.put_orphan(calls, "sess-old", "agent_orphan")
            first = run_session_start(recovery, calls, session_id="sess-new")
            second = run_session_start(recovery, calls, session_id="sess-new")
            self.assertIn("agent_orphan", first.stdout)
            self.assertEqual(second.stdout.strip(), "")

    def test_stale_orphan_is_excluded_by_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            self.put_orphan(calls, "sess-old", "agent_stale", when=time.time() - 100.0)
            out = run_session_start(
                recovery, calls, session_id="sess-new", extra={ENV_CALL_TTL: "10"}
            ).stdout
            self.assertEqual(out.strip(), "")

    def test_orphan_briefing_offers_delegation_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery, calls = os.path.join(tmp, "rec"), os.path.join(tmp, "calls")
            self.put_orphan(calls, "sess-old", "agent_orphan")
            out = run_session_start(recovery, calls, session_id="sess-new").stdout
            self.assertIn("SendMessage", out)
            self.assertIn("最後の手段", out)


if __name__ == "__main__":
    unittest.main()
