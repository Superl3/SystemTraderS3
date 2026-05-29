from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from system_trading_s3 import audit


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


BASE_EQUITY = "timestamp,equity\n2026-01-01,100000\n2026-01-02,100100\n"
BASE_TRADES = (
    "timestamp,trade_id,strategy,side,quantity,price,cost\n"
    "2026-01-01T09:30:00,T1,market_follow,buy,10,100,1\n"
    "2026-01-02T09:30:00,T2,trend_following,sell,-5,101,1\n"
)
BASE_BENCHMARK = "timestamp,benchmark_return\n2026-01-01T00:00:00,0.001\n2026-01-02T00:00:00,0.002\n"
BASE_FACTOR = "timestamp,factor,exposure\n2026-01-01T00:00:00,market,1.0\n"


def write_dataset(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for file_name, content in files.items():
        (root / file_name).write_text(content, encoding="utf-8", newline="")


def complete_files(**overrides: str) -> dict[str, str]:
    files = {
        "equity_curve.csv": BASE_EQUITY,
        "trades.csv": BASE_TRADES,
        "benchmark.csv": BASE_BENCHMARK,
        "factor_exposure.csv": BASE_FACTOR,
    }
    files.update(overrides)
    return files


class AuditDatasetTests(unittest.TestCase):
    def test_valid_complete_passes(self) -> None:
        result = audit.audit_dataset(FIXTURES / "valid_complete")
        self.assertEqual(audit.PASS, result.status)
        self.assertEqual([], result.issues)

    def test_valid_minimal_is_inconclusive_for_optional_gaps(self) -> None:
        result = audit.audit_dataset(FIXTURES / "valid_minimal")
        self.assertEqual(audit.INCONCLUSIVE, result.status)
        self.assertFalse(any(issue.severity == audit.ERROR for issue in result.issues))
        self.assertIn("optional_file_missing", {issue.check for issue in result.issues})
        self.assertIn("optional_cost_fields_missing", {issue.check for issue in result.issues})

    def test_blank_cost_column_is_still_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dataset(
                root,
                {
                    "equity_curve.csv": BASE_EQUITY,
                    "trades.csv": (
                        "timestamp,trade_id,strategy,side,quantity,price,cost\n"
                        "2026-01-01T09:30:00,T1,market_follow,buy,10,100,\n"
                    ),
                },
            )
            result = audit.audit_dataset(root)
        self.assertEqual(audit.INCONCLUSIVE, result.status)
        self.assertIn("optional_cost_fields_missing", {issue.check for issue in result.issues})

    def test_missing_required_files_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = audit.audit_dataset(Path(tmp))
        self.assertEqual(audit.FAIL, result.status)
        self.assertIn("required_file_missing", {issue.check for issue in result.issues})

    def test_missing_required_header_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dataset(root, complete_files(**{"equity_curve.csv": "timestamp\n2026-01-01\n"}))
            result = audit.audit_dataset(root)
        self.assertEqual(audit.FAIL, result.status)
        self.assertIn("required_header_missing", {issue.check for issue in result.issues})

    def test_invalid_numeric_and_date_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dataset(
                root,
                complete_files(
                    **{
                        "equity_curve.csv": "timestamp,equity\nnot-a-date,abc\n",
                    }
                ),
            )
            result = audit.audit_dataset(root)
        checks = {issue.check for issue in result.issues}
        self.assertEqual(audit.FAIL, result.status)
        self.assertIn("invalid_timestamp", checks)
        self.assertIn("invalid_numeric", checks)

    def test_basic_iso_date_without_dashes_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dataset(root, complete_files(**{"equity_curve.csv": "timestamp,equity\n20260101,100000\n"}))
            result = audit.audit_dataset(root)
        self.assertEqual(audit.FAIL, result.status)
        self.assertIn("invalid_timestamp", {issue.check for issue in result.issues})

    def test_timezone_mixing_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dataset(
                root,
                complete_files(
                    **{
                        "trades.csv": (
                            "timestamp,trade_id,strategy,side,quantity,price,cost\n"
                            "2026-01-01T09:30:00,T1,market_follow,buy,10,100,1\n"
                            "2026-01-02T09:30:00Z,T2,market_follow,buy,10,100,1\n"
                        )
                    }
                ),
            )
            result = audit.audit_dataset(root)
        self.assertEqual(audit.FAIL, result.status)
        self.assertIn("mixed_timezone_mode", {issue.check for issue in result.issues})

    def test_unsorted_equity_timestamp_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dataset(root, complete_files(**{"equity_curve.csv": "timestamp,equity\n2026-01-02,1\n2026-01-01,2\n"}))
            result = audit.audit_dataset(root)
        self.assertEqual(audit.FAIL, result.status)
        self.assertIn("timestamp_order", {issue.check for issue in result.issues})

    def test_duplicate_equity_timestamp_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dataset(root, complete_files(**{"equity_curve.csv": "timestamp,equity\n2026-01-01,1\n2026-01-01,2\n"}))
            result = audit.audit_dataset(root)
        self.assertEqual(audit.FAIL, result.status)
        self.assertIn("duplicate_key", {issue.check for issue in result.issues})

    def test_duplicate_trade_composite_key_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dataset(
                root,
                complete_files(
                    **{
                        "trades.csv": (
                            "timestamp,trade_id,strategy,side,quantity,price,cost\n"
                            "2026-01-01T09:30:00,T1,market_follow,buy,10,100,1\n"
                            "2026-01-01T09:30:00,T1,market_follow,buy,11,101,1\n"
                        )
                    }
                ),
            )
            result = audit.audit_dataset(root)
        self.assertEqual(audit.FAIL, result.status)
        self.assertIn("duplicate_key", {issue.check for issue in result.issues})

    def test_same_timestamp_multi_trade_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dataset(
                root,
                complete_files(
                    **{
                        "trades.csv": (
                            "timestamp,trade_id,strategy,side,quantity,price,cost\n"
                            "2026-01-01T09:30:00,T1,market_follow,buy,10,100,1\n"
                            "2026-01-01T09:30:00,T2,market_follow,sell,-3,100,1\n"
                        )
                    }
                ),
            )
            result = audit.audit_dataset(root)
        self.assertEqual(audit.PASS, result.status)

    def test_unknown_strategy_enum_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dataset(root, complete_files(**{"trades.csv": BASE_TRADES.replace("market_follow", "alpha_magic")}))
            result = audit.audit_dataset(root)
        self.assertEqual(audit.FAIL, result.status)
        self.assertIn("unknown_enum", {issue.check for issue in result.issues})

    def test_enum_whitespace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dataset(root, complete_files(**{"trades.csv": BASE_TRADES.replace("market_follow", " market_follow ")}))
            result = audit.audit_dataset(root)
        self.assertEqual(audit.FAIL, result.status)
        self.assertIn("enum_whitespace", {issue.check for issue in result.issues})

    def test_missing_trade_alternative_group_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dataset(
                root,
                complete_files(
                    **{
                        "trades.csv": (
                            "timestamp,trade_id,strategy,side,cost\n"
                            "2026-01-01T09:30:00,T1,market_follow,buy,1\n"
                        )
                    }
                ),
            )
            result = audit.audit_dataset(root)
        self.assertEqual(audit.FAIL, result.status)
        self.assertIn("trade_readiness_group", {issue.check for issue in result.issues})

    def test_unknown_extra_columns_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dataset(
                root,
                complete_files(
                    **{
                        "equity_curve.csv": "timestamp,equity,extra_note\n2026-01-01,100000,ok\n",
                        "trades.csv": (
                            "timestamp,trade_id,strategy,side,quantity,price,cost,extra_note\n"
                            "2026-01-01T09:30:00,T1,market_follow,buy,10,100,1,ok\n"
                        ),
                    }
                ),
            )
            result = audit.audit_dataset(root)
        self.assertEqual(audit.PASS, result.status)

    def test_zero_row_required_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dataset(root, complete_files(**{"equity_curve.csv": "timestamp,equity\n"}))
            result = audit.audit_dataset(root)
        self.assertEqual(audit.FAIL, result.status)
        self.assertIn("required_file_empty", {issue.check for issue in result.issues})

    def test_one_row_required_files_pass_when_otherwise_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dataset(
                root,
                complete_files(
                    **{
                        "equity_curve.csv": "timestamp,equity\n2026-01-01,100000\n",
                        "trades.csv": (
                            "timestamp,trade_id,strategy,side,quantity,price,cost\n"
                            "2026-01-01T09:30:00,T1,cash_pause,buy,1,100,0\n"
                        ),
                        "benchmark.csv": "timestamp,benchmark_return\n2026-01-01T00:00:00,0\n",
                        "factor_exposure.csv": "timestamp,factor,exposure\n2026-01-01T00:00:00,market,0\n",
                    }
                ),
            )
            result = audit.audit_dataset(root)
        self.assertEqual(audit.PASS, result.status)

    def test_bom_and_crlf_inputs_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.mkdir(parents=True, exist_ok=True)
            (root / "equity_curve.csv").write_bytes(b"\xef\xbb\xbftimestamp,equity\r\n2026-01-01,100000\r\n")
            (root / "trades.csv").write_bytes(
                b"timestamp,trade_id,strategy,side,quantity,price,cost\r\n"
                b"2026-01-01T09:30:00,T1,market_follow,buy,10,100,1\r\n"
            )
            (root / "benchmark.csv").write_bytes(b"timestamp,benchmark_return\r\n2026-01-01T00:00:00,0\r\n")
            (root / "factor_exposure.csv").write_bytes(b"timestamp,factor,exposure\r\n2026-01-01T00:00:00,market,1\r\n")
            result = audit.audit_dataset(root)
        self.assertEqual(audit.PASS, result.status)


class ContractTests(unittest.TestCase):
    def test_schema_contracts_are_audit_source(self) -> None:
        contracts = {contract["file_name"]: contract for contract in audit.load_contracts()}
        self.assertEqual(["timestamp", "equity"], contracts["equity_curve.csv"]["required_headers"])
        self.assertEqual(["timestamp", "trade_id", "strategy", "side"], contracts["trades.csv"]["required_headers"])
        self.assertEqual(["timestamp", "benchmark_return"], contracts["benchmark.csv"]["required_headers"])
        self.assertEqual(["timestamp", "factor", "exposure"], contracts["factor_exposure.csv"]["required_headers"])
        self.assertIn("volatility_breakout", contracts["trades.csv"]["enum_fields"]["strategy"])


class CliTests(unittest.TestCase):
    def test_strict_promotes_inconclusive_to_exit_one(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "system_trading_s3.audit", "--strict", str(FIXTURES / "valid_minimal")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(1, completed.returncode)
        self.assertIn("STATUS: INCONCLUSIVE", completed.stdout)

    def test_json_output_is_structured(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "system_trading_s3.audit", "--json", str(FIXTURES / "valid_complete")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertEqual(audit.PASS, payload["status"])
        self.assertEqual([], payload["issues"])

    def test_invalid_dataset_path_exits_two(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "system_trading_s3.audit", str(ROOT / "README.md")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("dataset_dir must be an existing directory", completed.stderr)


if __name__ == "__main__":
    unittest.main()
