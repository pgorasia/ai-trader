from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from tools.startup_smoke import MCP_DISABLE_OVERRIDE, startup_smoke


ROOT = Path(__file__).resolve().parents[1]


class StartupSmokeTests(unittest.TestCase):
    def test_actual_daemon_initialization_reaches_readiness_offline(self):
        result = startup_smoke(ROOT)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["mode"], "SHADOW")
        self.assertFalse(result["network_used"])
        self.assertFalse(result["brokerage_used"])
        self.assertEqual(result["mcp_override"], MCP_DISABLE_OVERRIDE)

    def test_required_cli_exercises_startup_path(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "tools/startup_smoke.py"), "--repo", str(ROOT)],
            cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('"status": "PASS"', completed.stdout)


if __name__ == "__main__":
    unittest.main()
