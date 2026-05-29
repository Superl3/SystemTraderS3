from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import HTTPServer
from pathlib import Path

from dashboard import server as dashboard_server


ROOT = Path(__file__).resolve().parents[1]


class DashboardApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_runs_dir = dashboard_server.RUNS_DIR

    def tearDown(self) -> None:
        dashboard_server.RUNS_DIR = self._original_runs_dir

    def test_dashboard_lists_committed_demo_run(self) -> None:
        dashboard_server.RUNS_DIR = ROOT / "runs"
        with self._running_server() as base_url:
            payload = self._get_json(f"{base_url}/api/runs")
        runs_by_id = {item["run_id"]: item for item in payload["runs"]}
        run_ids = set(runs_by_id)
        self.assertIn("demo-run", run_ids)
        self.assertEqual("INCONCLUSIVE", runs_by_id["demo-run"]["audit_status"])
        self.assertEqual("tests\\fixtures\\valid_multisymbol", runs_by_id["demo-run"]["dataset"])

    def test_dashboard_simulate_endpoint_exports_and_metrics_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dashboard_server.RUNS_DIR = Path(tmp) / "runs"
            with self._running_server() as base_url:
                payload = self._post_json(
                    f"{base_url}/api/simulate",
                    {
                        "dataset_name": "valid_multisymbol",
                        "config_name": "sample_config.json",
                        "run_id": "dashboard-test",
                    },
                )
            run_dir = dashboard_server.RUNS_DIR / "dashboard-test"
            self.assertEqual(True, payload["success"])
            self.assertTrue((run_dir / "run_manifest.json").is_file())
            self.assertTrue((run_dir / "metrics.json").is_file())

    def _running_server(self):
        testcase = self

        class ServerContext:
            def __enter__(self) -> str:
                class QuietDashboardHandler(dashboard_server.DashboardHandler):
                    def log_message(self, format, *args) -> None:
                        return

                self.server = HTTPServer(("127.0.0.1", 0), QuietDashboardHandler)
                self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
                self.thread.start()
                host, port = self.server.server_address
                return f"http://{host}:{port}"

            def __exit__(self, exc_type, exc, tb) -> None:
                self.server.shutdown()
                self.server.server_close()
                self.thread.join(timeout=5)

        del testcase
        return ServerContext()

    def _get_json(self, url: str) -> dict:
        with urllib.request.urlopen(url, timeout=10) as response:
            self.assertEqual(200, response.status)
            return json.loads(response.read().decode("utf-8"))

    def _post_json(self, url: str, payload: dict) -> dict:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(200, response.status)
            return json.loads(response.read().decode("utf-8"))


class DownloadDataScriptTests(unittest.TestCase):
    def test_download_script_help_does_not_require_optional_dependencies(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/download_data.py", "--help"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, completed.returncode)
        self.assertIn("Download Yahoo Finance prices", completed.stdout)


if __name__ == "__main__":
    unittest.main()
