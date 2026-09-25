from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.runtime_audit import audit, git_head


HEAD = "a" * 40
OTHER_HEAD = "b" * 40


class RuntimeAuditGitHeadTests(unittest.TestCase):
    def test_exact_resolved_repository_is_process_local_safe_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            unresolved = repo / ".." / "repo"
            completed = subprocess.CompletedProcess([], 0, stdout=f"{HEAD}\n")
            with patch("tools.runtime_audit.subprocess.run", return_value=completed) as run:
                self.assertEqual(git_head(unresolved), HEAD)

        self.assertEqual(run.call_args.args[0], [
            "git", "-c", f"safe.directory={repo.resolve()}", "-C", str(repo.resolve()),
            "rev-parse", "--verify", "HEAD",
        ])

    def test_wildcard_safe_directory_is_never_used(self):
        completed = subprocess.CompletedProcess([], 0, stdout=f"{HEAD}\n")
        with patch("tools.runtime_audit.subprocess.run", return_value=completed) as run:
            git_head(Path("relative-repo"))
        command = run.call_args.args[0]
        self.assertNotIn("safe.directory=*", command)
        self.assertEqual(command[2], f"safe.directory={Path('relative-repo').resolve()}")

    def test_successful_head_returns_sha(self):
        completed = subprocess.CompletedProcess([], 0, stdout=f"{HEAD}\n")
        with patch("tools.runtime_audit.subprocess.run", return_value=completed):
            self.assertEqual(git_head(Path("repo")), HEAD)

    def test_git_failure_returns_none(self):
        completed = subprocess.CompletedProcess([], 128, stdout="")
        with patch("tools.runtime_audit.subprocess.run", return_value=completed):
            self.assertIsNone(git_head(Path("repo")))


class RuntimeAuditAcceptanceBindingTests(unittest.TestCase):
    def run_audit(self, accepted_commit: str, head: str | None) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            state = repo / "state"
            state.mkdir()
            artifact = {
                "version": 1,
                "mode": "SHADOW",
                "offline_gate": "PASS",
                "live_read_only_gate": "PASS",
                "live_run_counts": {"preflight": 5, "luna_schema": 5, "eod": 3},
                "global_shadow_tool_count": 22,
                "accepted_git_commit": accepted_commit,
            }
            (state / "reliability_acceptance.json").write_text(json.dumps(artifact), encoding="utf-8")
            with patch("tools.runtime_audit.git_head", return_value=head):
                return audit(repo, state, repo / "journal.log",
                             now=datetime.fromisoformat("2026-09-27T12:00:00+00:00"))

    @staticmethod
    def finding_codes(result: dict) -> set[str]:
        return {item["code"] for item in result["findings"]}

    def test_acceptance_invalid_when_head_cannot_resolve(self):
        result = self.run_audit(HEAD, None)
        self.assertIn("DEPLOYMENT_NOT_ACCEPTED", self.finding_codes(result))

    def test_acceptance_invalid_when_accepted_commit_differs_from_head(self):
        result = self.run_audit(OTHER_HEAD, HEAD)
        self.assertIn("DEPLOYMENT_NOT_ACCEPTED", self.finding_codes(result))

    def test_acceptance_valid_when_accepted_commit_equals_head(self):
        result = self.run_audit(HEAD, HEAD)
        self.assertNotIn("DEPLOYMENT_NOT_ACCEPTED", self.finding_codes(result))


if __name__ == "__main__":
    unittest.main()
