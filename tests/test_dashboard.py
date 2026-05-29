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

    def test_dashboard_lists_drop_in_datasets_and_strategy_configs(self) -> None:
        with self._running_server() as base_url:
            datasets_payload = self._get_json(f"{base_url}/api/datasets")
            configs_payload = self._get_json(f"{base_url}/api/configs")

        dataset_paths = {item["path"] for item in datasets_payload["datasets"]}
        config_paths = {item["path"] for item in configs_payload["configs"]}
        self.assertIn("datasets\\us_tech_100_simulated", dataset_paths)
        self.assertIn("configs\\strategies\\periodic_momentum_top10.json", config_paths)
        periodic_config = next(
            item for item in configs_payload["configs"] if item["path"] == "configs\\strategies\\periodic_momentum_top10.json"
        )
        self.assertEqual("PeriodicFactorWeight", periodic_config["payload"]["strategy_name"])

    def test_dashboard_lists_strategy_catalog_for_form_driven_config(self) -> None:
        with self._running_server() as base_url:
            payload = self._get_json(f"{base_url}/api/strategies")

        strategies = {item["name"]: item for item in payload["strategies"]}
        self.assertEqual(
            {"BuyAndHold", "MovingAverageCross", "EqualWeightRebalance", "PeriodicFactorWeight"},
            set(strategies),
        )
        periodic_param_names = {item["name"] for item in strategies["PeriodicFactorWeight"]["params"]}
        self.assertEqual({"factor_name", "rebalance_interval", "top_k"}, periodic_param_names)
        self.assertEqual("momentum", strategies["PeriodicFactorWeight"]["params"][0]["default"])

    def test_dashboard_serves_strategy_form_contract(self) -> None:
        with self._running_server() as base_url:
            html = self._get_text(f"{base_url}/")

        required_fragments = [
            'id="strategySelect"',
            'id="strategyDescription"',
            'id="strategyParamFields"',
            'id="strategyConfigJson"',
            "fetch('/api/strategies')",
            "function populateStrategySelect()",
            "function syncStrategyFormToJson()",
            "function buildConfigFromStrategyForm()",
        ]
        for fragment in required_fragments:
            self.assertIn(fragment, html)

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

    def test_dashboard_simulate_endpoint_accepts_inline_strategy_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dashboard_server.RUNS_DIR = Path(tmp) / "runs"
            with self._running_server() as base_url:
                payload = self._post_json(
                    f"{base_url}/api/simulate",
                    {
                        "dataset_path": "datasets/us_tech_100_simulated",
                        "config_json": {
                            "initial_cash": "100000",
                            "strategy_name": "PeriodicFactorWeight",
                            "strategy_params": {"factor_name": "momentum", "rebalance_interval": 5, "top_k": 10},
                            "friction": {"fee_rate": "0.0005", "slippage_per_trade": "0.01"},
                            "risk_free_rate": "0.02",
                        },
                        "run_id": "inline-config-test",
                    },
                )
            run_dir = dashboard_server.RUNS_DIR / "inline-config-test"
            manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(True, payload["success"])
            self.assertEqual("PeriodicFactorWeight", manifest["strategy_name"])
            self.assertEqual("datasets\\us_tech_100_simulated", manifest["dataset_dir"])

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

    def _get_text(self, url: str) -> str:
        with urllib.request.urlopen(url, timeout=10) as response:
            self.assertEqual(200, response.status)
            return response.read().decode("utf-8")

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

    def test_download_script_offline_smoke_outputs_simulator_ready_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset_dir = Path(tmp) / "offline_smoke"
            download = subprocess.run(
                [sys.executable, "scripts/download_data.py", "--offline-smoke", "--output-dir", str(dataset_dir)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            simulate = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "system_trading_s3.simulate",
                    str(dataset_dir),
                    "--config",
                    "tests/fixtures/sample_config.json",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(0, download.returncode, download.stderr)
        self.assertIn("wrote offline smoke dataset", download.stdout)
        self.assertEqual(0, simulate.returncode, simulate.stderr)
        self.assertIn("SIMULATION STATUS: PASS", simulate.stdout)


if __name__ == "__main__":
    unittest.main()
