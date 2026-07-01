from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from system_trading_s3 import input_audit


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


VALID_MARKET = "timestamp,symbol,price\n2026-01-01T09:30:00,SIM,100\n2026-01-02T09:30:00,SIM,101\n"


def write_file(root: Path, name: str, content: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(content, encoding="utf-8", newline="")


class InputAuditTests(unittest.TestCase):
    def test_drop_in_us_tech_dataset_passes_input_audit(self) -> None:
        result = input_audit.audit_input_dataset(ROOT / "datasets" / "us_tech_100_simulated")
        self.assertEqual(input_audit.PASS, result.status)
        self.assertEqual([], result.issues)

    def test_market_only_dataset_is_inconclusive_not_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_file(root, "market_prices.csv", VALID_MARKET)
            result = input_audit.audit_input_dataset(root)

        self.assertEqual(input_audit.INCONCLUSIVE, result.status)
        self.assertFalse(any(issue.severity == input_audit.ERROR for issue in result.issues))
        self.assertIn("optional_benchmark_prices_missing", {issue.check for issue in result.issues})
        self.assertIn("optional_factors_missing", {issue.check for issue in result.issues})
        self.assertIn("optional_dataset_manifest_missing", {issue.check for issue in result.issues})

    def test_fixture_multisymbol_price_files_are_valid_inputs(self) -> None:
        result = input_audit.audit_input_dataset(FIXTURES / "valid_multisymbol")
        self.assertEqual(input_audit.INCONCLUSIVE, result.status)
        self.assertFalse(any(issue.severity == input_audit.ERROR for issue in result.issues))
        self.assertIn("optional_dataset_manifest_missing", {issue.check for issue in result.issues})

    def test_missing_market_inputs_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = input_audit.audit_input_dataset(Path(tmp))

        self.assertEqual(input_audit.FAIL, result.status)
        self.assertIn("required_market_input_missing", {issue.check for issue in result.issues})

    def test_duplicate_market_timestamp_symbol_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_file(
                root,
                "market_prices.csv",
                "timestamp,symbol,price\n"
                "2026-01-01T09:30:00,SIM,100\n"
                "2026-01-01T09:30:00,SIM,101\n",
            )
            result = input_audit.audit_input_dataset(root)

        self.assertEqual(input_audit.FAIL, result.status)
        self.assertIn("duplicate_price_key", {issue.check for issue in result.issues})

    def test_factor_duplicate_key_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_file(root, "market_prices.csv", VALID_MARKET)
            write_file(root, "benchmark_prices.csv", "timestamp,symbol,price\n2026-01-01T09:30:00,BENCH,100\n2026-01-02T09:30:00,BENCH,101\n")
            write_file(
                root,
                "factors.csv",
                "timestamp,symbol,factor_name,factor_value\n"
                "2026-01-01T09:30:00,SIM,momentum,1\n"
                "2026-01-01T09:30:00,SIM,momentum,2\n",
            )
            result = input_audit.audit_input_dataset(root)

        self.assertEqual(input_audit.FAIL, result.status)
        self.assertIn("duplicate_factor_key", {issue.check for issue in result.issues})

    def test_manifest_missing_files_list_is_a_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_file(root, "market_prices.csv", VALID_MARKET)
            write_file(root, "dataset_manifest.json", '{"schema_version":"demo","dataset_id":"demo"}\n')
            result = input_audit.audit_input_dataset(root)

        self.assertEqual(input_audit.INCONCLUSIVE, result.status)
        self.assertIn("manifest_files_invalid", {issue.check for issue in result.issues})


class InputAuditCliTests(unittest.TestCase):
    def test_json_output_is_structured(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "system_trading_s3.input_audit", "--json", str(ROOT / "datasets" / "us_tech_100_simulated")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(input_audit.PASS, payload["status"])
        self.assertEqual([], payload["issues"])

    def test_strict_promotes_input_gaps_to_exit_one(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "system_trading_s3.input_audit", "--strict", str(FIXTURES / "valid_minimal")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(1, completed.returncode)
        self.assertIn("STATUS: INCONCLUSIVE", completed.stdout)


if __name__ == "__main__":
    unittest.main()
