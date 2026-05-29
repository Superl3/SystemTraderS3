"""Thin paper-trading simulation loop for simulated market data."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Protocol

from system_trading_s3 import audit


PASS = "PASS"
FAIL = "FAIL"
DEFAULT_INITIAL_CASH = Decimal("100000")
ONE_UNIT = Decimal("1")
ZERO = Decimal("0")
STRATEGY_NAME = "buy_and_hold_one_unit"
DEFAULT_RUN_ID = "default"
SIMULATION_PRESET_NAME = "market_follow"
RUN_ARTIFACT_SCHEMA_VERSION = "mvp2.run_artifacts.v1"
ARTIFACT_FILES = [
    "run_manifest.json",
    "equity_curve.csv",
    "trades.csv",
    "orders.csv",
    "fills.csv",
    "account_summary.json",
    "audit_summary.json",
]


class SimulationInputError(Exception):
    """Raised when simulation input data is invalid."""


class SimulationExecutionError(Exception):
    """Raised when the simulated account cannot apply a fill."""


class SimulationExportError(Exception):
    """Raised when deterministic run artifacts cannot be exported."""


class SimulationConfigError(Exception):
    """Raised when a simulation config file is invalid."""


@dataclass(frozen=True)
class MarketPriceEvent:
    timestamp: datetime
    symbol: str
    price: Decimal


@dataclass(frozen=True)
class BenchmarkSnapshot:
    price: Decimal | None
    equity: Decimal | None


@dataclass(frozen=True)
class Order:
    side: str
    symbol: str
    quantity: Decimal
    price: Decimal


@dataclass(frozen=True)
class MarketState:
    timestamp: datetime
    symbol: str
    price: Decimal


@dataclass(frozen=True)
class AccountState:
    cash: Decimal
    positions: dict[str, Decimal]


@dataclass(frozen=True)
class Fill:
    timestamp: datetime
    side: str
    symbol: str
    quantity: Decimal
    price: Decimal
    order_id: str = ""
    fill_id: str = ""
    fee: Decimal = ZERO
    slippage: Decimal = ZERO


@dataclass(frozen=True)
class OrderRecord:
    order_id: str
    timestamp: datetime
    order: Order
    status: str


@dataclass(frozen=True)
class AccountSnapshot:
    timestamp: datetime
    equity: Decimal
    cash: Decimal
    position_value: Decimal
    symbol: str
    position_quantity: Decimal
    last_price: Decimal
    benchmark_price: Decimal | None = None
    benchmark_equity: Decimal | None = None


@dataclass(frozen=True)
class SimulationResult:
    status: str
    dataset: str
    audit_status: str
    strategy_name: str
    initial_cash: Decimal
    final_cash: Decimal | None
    final_positions: dict[str, Decimal]
    order_count: int
    fill_count: int
    final_equity: Decimal | None
    friction: FrictionModel
    risk_free_rate: Decimal
    total_fees: Decimal
    total_slippage: Decimal
    fills: list[Fill]
    orders: list[OrderRecord]
    equity_curve: list[AccountSnapshot]
    warnings: list[str]
    error: str | None = None


@dataclass(frozen=True)
class FrictionModel:
    fee_rate: Decimal = ZERO
    slippage_per_trade: Decimal = ZERO


@dataclass(frozen=True)
class SimulationConfig:
    initial_cash: Decimal
    strategy_name: str
    strategy_params: dict[str, object]
    friction: FrictionModel = FrictionModel()
    risk_free_rate: Decimal = ZERO


class Strategy(Protocol):
    name: str

    def on_data(self, market_state: MarketState, account_state: AccountState) -> list[Order]:
        ...

    def on_finish(self, final_event: MarketPriceEvent, account: SimulatedAccount) -> list[Order]:
        ...


class DataFeed:
    """Validated one-symbol simulated price feed."""

    def __init__(self, events: list[MarketPriceEvent]) -> None:
        self._events = events

    @classmethod
    def from_dataset(cls, dataset_dir: Path | str) -> "DataFeed":
        path = Path(dataset_dir) / "market_prices.csv"
        if not path.exists():
            raise SimulationInputError("market_prices.csv is missing.")
        if not path.is_file():
            raise SimulationInputError("market_prices.csv must be a file.")

        events = _read_market_prices(path, file_name="market_prices.csv")
        if len(events) < 2:
            raise SimulationInputError("market_prices.csv must contain at least two price rows.")
        _validate_one_symbol_price_events(events, "market_prices.csv")

        return cls(events)

    def __iter__(self) -> Iterable[MarketPriceEvent]:
        return iter(self._events)

    @property
    def events(self) -> list[MarketPriceEvent]:
        return list(self._events)


class BenchmarkFeed:
    """Optional one-symbol benchmark price feed aligned to market events."""

    def __init__(self, snapshots: dict[datetime, BenchmarkSnapshot], warnings: list[str]) -> None:
        self._snapshots = snapshots
        self.warnings = warnings

    @classmethod
    def from_dataset(
        cls,
        dataset_dir: Path | str,
        market_events: list[MarketPriceEvent],
        initial_cash: Decimal,
    ) -> "BenchmarkFeed":
        path = Path(dataset_dir) / "benchmark_prices.csv"
        if not path.exists():
            return cls(
                snapshots={event.timestamp: BenchmarkSnapshot(price=None, equity=None) for event in market_events},
                warnings=["benchmark_prices.csv missing; benchmark-relative metrics unavailable."],
            )
        if not path.is_file():
            return cls(
                snapshots={event.timestamp: BenchmarkSnapshot(price=None, equity=None) for event in market_events},
                warnings=["benchmark_prices.csv is not a file; benchmark-relative metrics unavailable."],
            )

        try:
            benchmark_events = _read_market_prices(path, file_name="benchmark_prices.csv")
            _validate_one_symbol_price_events(benchmark_events, "benchmark_prices.csv")
            return cls(
                snapshots=_align_benchmark_to_market(market_events, benchmark_events, initial_cash),
                warnings=[],
            )
        except SimulationInputError as exc:
            return cls(
                snapshots={event.timestamp: BenchmarkSnapshot(price=None, equity=None) for event in market_events},
                warnings=[f"{exc} Benchmark-relative metrics unavailable."],
            )

    def snapshot_for(self, timestamp: datetime) -> BenchmarkSnapshot:
        return self._snapshots.get(timestamp, BenchmarkSnapshot(price=None, equity=None))


class SimulatedAccount:
    def __init__(self, initial_cash: Decimal) -> None:
        if not initial_cash.is_finite():
            raise SimulationInputError("initial cash must be finite.")
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions: dict[str, Decimal] = {}

    def apply_fill(self, fill: Fill) -> None:
        notional = fill.quantity * fill.price
        cash_cost = fill.fee + fill.slippage
        current_position = self.positions.get(fill.symbol, Decimal("0"))

        if fill.side == "buy":
            if self.cash < notional + cash_cost:
                raise SimulationExecutionError(
                    f"Insufficient cash to buy {format_decimal(fill.quantity)} {fill.symbol} at {format_decimal(fill.price)}."
                )
            self.cash -= notional + cash_cost
            self.positions[fill.symbol] = current_position + fill.quantity
            return

        if fill.side == "sell":
            if current_position < fill.quantity:
                raise SimulationExecutionError(
                    f"Insufficient position to sell {format_decimal(fill.quantity)} {fill.symbol}."
                )
            self.cash += notional - cash_cost
            new_position = current_position - fill.quantity
            if new_position == 0:
                self.positions.pop(fill.symbol, None)
            else:
                self.positions[fill.symbol] = new_position
            return

        raise SimulationExecutionError(f"Unsupported order side: {fill.side}.")

    def final_equity(self, last_prices: dict[str, Decimal]) -> Decimal:
        equity = self.cash
        for symbol in sorted(self.positions):
            if symbol not in last_prices:
                raise SimulationExecutionError(f"Missing final price for open position: {symbol}.")
            equity += self.positions[symbol] * last_prices[symbol]
        return equity


class ExecutionSimulator:
    def __init__(self, friction: FrictionModel = FrictionModel()) -> None:
        if friction.fee_rate < 0 or friction.slippage_per_trade < 0:
            raise SimulationInputError("friction values must be nonnegative.")
        self.friction = friction

    def fill(self, timestamp: datetime, order: Order, order_id: str = "", fill_id: str = "") -> Fill:
        notional = order.quantity * order.price
        fee = notional * self.friction.fee_rate
        slippage = self.friction.slippage_per_trade
        return Fill(
            timestamp=timestamp,
            side=order.side,
            symbol=order.symbol,
            quantity=order.quantity,
            price=order.price,
            order_id=order_id,
            fill_id=fill_id,
            fee=fee,
            slippage=slippage,
        )


class BuyAndHoldOneUnitStrategy:
    name = STRATEGY_NAME

    def __init__(self) -> None:
        self._bought = False

    def on_event(self, event: MarketPriceEvent, account: SimulatedAccount) -> list[Order]:
        del account
        if self._bought:
            return []
        self._bought = True
        return [Order(side="buy", symbol=event.symbol, quantity=ONE_UNIT, price=event.price)]

    def on_data(self, market_state: MarketState, account_state: AccountState) -> list[Order]:
        del account_state
        event = MarketPriceEvent(timestamp=market_state.timestamp, symbol=market_state.symbol, price=market_state.price)
        return self.on_event(event, SimulatedAccount(Decimal("0")))

    def on_finish(self, final_event: MarketPriceEvent, account: SimulatedAccount) -> list[Order]:
        held_quantity = account.positions.get(final_event.symbol, Decimal("0"))
        if held_quantity <= 0:
            return []
        return [Order(side="sell", symbol=final_event.symbol, quantity=held_quantity, price=final_event.price)]


class BuyAndHoldStrategy:
    name = "BuyAndHold"

    def __init__(self, quantity: Decimal = ONE_UNIT) -> None:
        if quantity <= 0:
            raise SimulationConfigError("BuyAndHold quantity must be positive.")
        self.quantity = quantity
        self._bought = False

    def on_data(self, market_state: MarketState, account_state: AccountState) -> list[Order]:
        del account_state
        if self._bought:
            return []
        self._bought = True
        return [Order(side="buy", symbol=market_state.symbol, quantity=self.quantity, price=market_state.price)]

    def on_finish(self, final_event: MarketPriceEvent, account: SimulatedAccount) -> list[Order]:
        del final_event, account
        return []


class MovingAverageCrossStrategy:
    name = "MovingAverageCross"

    def __init__(self, short_window: int = 2, long_window: int = 3, quantity: Decimal = ONE_UNIT) -> None:
        if short_window <= 0:
            raise SimulationConfigError("MovingAverageCross short_window must be positive.")
        if long_window <= short_window:
            raise SimulationConfigError("MovingAverageCross long_window must be greater than short_window.")
        if quantity <= 0:
            raise SimulationConfigError("MovingAverageCross quantity must be positive.")
        self.short_window = short_window
        self.long_window = long_window
        self.quantity = quantity
        self._prices: list[Decimal] = []

    def on_data(self, market_state: MarketState, account_state: AccountState) -> list[Order]:
        self._prices.append(market_state.price)
        if len(self._prices) < self.long_window:
            return []

        short_avg = sum(self._prices[-self.short_window:]) / Decimal(self.short_window)
        long_avg = sum(self._prices[-self.long_window:]) / Decimal(self.long_window)
        held_quantity = account_state.positions.get(market_state.symbol, Decimal("0"))

        if short_avg > long_avg and held_quantity <= 0:
            return [Order(side="buy", symbol=market_state.symbol, quantity=self.quantity, price=market_state.price)]
        if short_avg < long_avg and held_quantity > 0:
            return [Order(side="sell", symbol=market_state.symbol, quantity=held_quantity, price=market_state.price)]
        return []

    def on_finish(self, final_event: MarketPriceEvent, account: SimulatedAccount) -> list[Order]:
        del final_event, account
        return []


STRATEGY_REGISTRY = {
    BuyAndHoldStrategy.name: BuyAndHoldStrategy,
    MovingAverageCrossStrategy.name: MovingAverageCrossStrategy,
}


class SimulationEngine:
    def __init__(
        self,
        feed: DataFeed,
        benchmark_feed: BenchmarkFeed,
        account: SimulatedAccount,
        strategy: Strategy,
        execution: ExecutionSimulator,
    ) -> None:
        self.feed = feed
        self.benchmark_feed = benchmark_feed
        self.account = account
        self.strategy = strategy
        self.execution = execution
        self.order_count = 0
        self.orders: list[OrderRecord] = []
        self.fills: list[Fill] = []
        self.equity_curve: list[AccountSnapshot] = []
        self.last_prices: dict[str, Decimal] = {}

    def run(self) -> None:
        events = self.feed.events
        final_event = events[-1]

        for event in events:
            self.last_prices[event.symbol] = event.price
            self._execute_orders(event, self.strategy.on_data(_market_state(event), _account_state(self.account)))
            if event == final_event:
                self._execute_orders(final_event, self.strategy.on_finish(final_event, self.account))
            self._record_account_snapshot(event)

    def _execute_orders(self, event: MarketPriceEvent, orders: list[Order]) -> None:
        for order in orders:
            order_id = f"O{len(self.orders) + 1:06d}"
            fill_id = f"F{len(self.fills) + 1:06d}"
            self.orders.append(OrderRecord(order_id=order_id, timestamp=event.timestamp, order=order, status="filled"))
            self.order_count += 1
            fill = self.execution.fill(event.timestamp, order, order_id=order_id, fill_id=fill_id)
            self.account.apply_fill(fill)
            self.fills.append(fill)

    def _record_account_snapshot(self, event: MarketPriceEvent) -> None:
        position_quantity = self.account.positions.get(event.symbol, Decimal("0"))
        position_value = position_quantity * event.price
        benchmark_snapshot = self.benchmark_feed.snapshot_for(event.timestamp)
        self.equity_curve.append(
            AccountSnapshot(
                timestamp=event.timestamp,
                equity=self.account.cash + position_value,
                cash=self.account.cash,
                position_value=position_value,
                symbol=event.symbol,
                position_quantity=position_quantity,
                last_price=event.price,
                benchmark_price=benchmark_snapshot.price,
                benchmark_equity=benchmark_snapshot.equity,
            )
        )


def run_simulation(
    dataset_dir: Path | str,
    initial_cash: Decimal = DEFAULT_INITIAL_CASH,
    strategy: Strategy | None = None,
    friction: FrictionModel = FrictionModel(),
    risk_free_rate: Decimal = ZERO,
) -> SimulationResult:
    dataset_path = Path(dataset_dir)
    audit_status = _audit_status_for_context(dataset_path)
    strategy_name = strategy.name if strategy is not None else STRATEGY_NAME

    try:
        feed = DataFeed.from_dataset(dataset_path)
        benchmark_feed = BenchmarkFeed.from_dataset(dataset_path, feed.events, initial_cash)
        account = SimulatedAccount(initial_cash)
        selected_strategy = strategy if strategy is not None else BuyAndHoldOneUnitStrategy()
        engine = SimulationEngine(feed, benchmark_feed, account, selected_strategy, ExecutionSimulator(friction))
        engine.run()
        final_equity = account.final_equity(engine.last_prices)
        total_fees = sum((fill.fee for fill in engine.fills), ZERO)
        total_slippage = sum((fill.slippage for fill in engine.fills), ZERO)
        return SimulationResult(
            status=PASS,
            dataset=str(dataset_path),
            audit_status=audit_status,
            strategy_name=selected_strategy.name,
            initial_cash=initial_cash,
            final_cash=account.cash,
            final_positions=dict(sorted(account.positions.items())),
            order_count=engine.order_count,
            fill_count=len(engine.fills),
            final_equity=final_equity,
            friction=friction,
            risk_free_rate=risk_free_rate,
            total_fees=total_fees,
            total_slippage=total_slippage,
            fills=list(engine.fills),
            orders=list(engine.orders),
            equity_curve=list(engine.equity_curve),
            warnings=list(benchmark_feed.warnings),
        )
    except (SimulationInputError, SimulationExecutionError) as exc:
        return SimulationResult(
            status=FAIL,
            dataset=str(dataset_path),
            audit_status=audit_status,
            strategy_name=strategy_name,
            initial_cash=initial_cash,
            final_cash=None,
            final_positions={},
            order_count=0,
            fill_count=0,
            final_equity=None,
            friction=friction,
            risk_free_rate=risk_free_rate,
            total_fees=ZERO,
            total_slippage=ZERO,
            fills=[],
            orders=[],
            equity_curve=[],
            warnings=[],
            error=str(exc),
        )


def format_result(result: SimulationResult) -> str:
    lines = [
        f"SIMULATION STATUS: {result.status}",
        f"DATASET: {result.dataset}",
        f"MVP0 AUDIT STATUS: {result.audit_status}",
        f"STRATEGY: {result.strategy_name}",
        f"INITIAL CASH: {format_decimal(result.initial_cash)}",
        f"FINAL CASH: {_format_optional_decimal(result.final_cash)}",
        f"FINAL POSITIONS: {format_positions(result.final_positions)}",
        f"ORDER COUNT: {result.order_count}",
        f"FILL COUNT: {result.fill_count}",
        f"FINAL EQUITY: {_format_optional_decimal(result.final_equity)}",
    ]
    if result.error is not None:
        lines.append(f"ERROR: {result.error}")
    if result.warnings:
        lines.extend(f"WARNING: {warning}" for warning in result.warnings)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a thin simulated paper-trading loop.")
    parser.add_argument("dataset_dir", type=Path, help="Directory containing market_prices.csv.")
    parser.add_argument(
        "--initial-cash",
        default=str(DEFAULT_INITIAL_CASH),
        type=_parse_initial_cash,
        help="Initial simulated cash. Defaults to 100000.",
    )
    parser.add_argument("--export-dir", type=Path, help="Write deterministic run artifacts to this directory.")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing non-empty export directory.")
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID, help="Deterministic run identifier for exported artifacts.")
    parser.add_argument("--config", type=Path, help="JSON run config with initial_cash, strategy_name, and strategy_params.")
    args = parser.parse_args(argv)

    if not args.dataset_dir.exists() or not args.dataset_dir.is_dir():
        print(f"dataset_dir must be an existing directory: {args.dataset_dir}", file=sys.stderr)
        return 2

    try:
        config = load_simulation_config(args.config) if args.config is not None else None
        selected_initial_cash = config.initial_cash if config is not None else args.initial_cash
        selected_strategy = create_strategy(config.strategy_name, config.strategy_params) if config is not None else None
        selected_friction = config.friction if config is not None else FrictionModel()
        selected_risk_free_rate = config.risk_free_rate if config is not None else ZERO
        result = run_simulation(
            args.dataset_dir,
            selected_initial_cash,
            selected_strategy,
            selected_friction,
            selected_risk_free_rate,
        )
    except SimulationConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive CLI boundary.
        print(f"Internal error: {exc}", file=sys.stderr)
        return 2

    print(format_result(result))
    if args.export_dir is not None:
        if result.status != PASS:
            return 1
        try:
            export_run_artifacts(
                result=result,
                dataset_dir=args.dataset_dir,
                export_dir=args.export_dir,
                run_id=args.run_id,
                overwrite=args.overwrite,
            )
        except SimulationExportError as exc:
            print(f"EXPORT ERROR: {exc}", file=sys.stderr)
            return 1
    return 0 if result.status == PASS else 1


def export_run_artifacts(
    result: SimulationResult,
    dataset_dir: Path | str,
    export_dir: Path | str,
    run_id: str = DEFAULT_RUN_ID,
    overwrite: bool = False,
) -> None:
    if result.status != PASS:
        raise SimulationExportError("simulation must pass before artifacts can be exported.")

    target = Path(export_dir)
    if target.exists() and not target.is_dir():
        raise SimulationExportError(f"export path exists and is not a directory: {target}")
    if target.exists() and any(target.iterdir()) and not overwrite:
        raise SimulationExportError(f"export directory is non-empty: {target}")

    parent = target.parent if target.parent != Path("") else Path(".")
    parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=str(parent)))
    try:
        _write_run_artifacts(temp_root, result, Path(dataset_dir), run_id)
        if target.exists():
            shutil.rmtree(target)
        temp_root.replace(target)
    except Exception as exc:
        shutil.rmtree(temp_root, ignore_errors=True)
        if isinstance(exc, SimulationExportError):
            raise
        raise SimulationExportError(str(exc)) from exc


def load_simulation_config(path: Path | str) -> SimulationConfig:
    config_path = Path(path)
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SimulationConfigError(f"Could not read config JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise SimulationConfigError("Config must be a JSON object.")

    initial_cash_value = payload.get("initial_cash")
    strategy_name = payload.get("strategy_name")
    strategy_params = payload.get("strategy_params")
    friction_payload = payload.get("friction", {})
    risk_free_rate = _decimal_config_value(payload, "risk_free_rate", ZERO, "Config risk_free_rate")

    if not isinstance(initial_cash_value, str):
        raise SimulationConfigError("Config initial_cash must be a decimal string.")
    initial_cash = audit._parse_decimal(initial_cash_value)
    if initial_cash is None or initial_cash < 0:
        raise SimulationConfigError("Config initial_cash must be a finite nonnegative decimal.")
    if not isinstance(strategy_name, str) or not strategy_name.strip():
        raise SimulationConfigError("Config strategy_name must be a non-empty string.")
    if not isinstance(strategy_params, dict):
        raise SimulationConfigError("Config strategy_params must be an object.")
    friction = _parse_friction_config(friction_payload)
    return SimulationConfig(
        initial_cash=initial_cash,
        strategy_name=strategy_name.strip(),
        strategy_params=strategy_params,
        friction=friction,
        risk_free_rate=risk_free_rate,
    )


def create_strategy(strategy_name: str, strategy_params: dict[str, object]) -> Strategy:
    strategy_class = STRATEGY_REGISTRY.get(strategy_name)
    if strategy_class is None:
        available = ", ".join(sorted(STRATEGY_REGISTRY))
        raise SimulationConfigError(f"Unknown strategy_name {strategy_name!r}. Available strategies: {available}.")

    if strategy_class is BuyAndHoldStrategy:
        quantity = _decimal_param(strategy_params, "quantity", ONE_UNIT)
        return BuyAndHoldStrategy(quantity=quantity)
    if strategy_class is MovingAverageCrossStrategy:
        short_window = _int_param(strategy_params, "short_window", 2)
        long_window = _int_param(strategy_params, "long_window", 3)
        quantity = _decimal_param(strategy_params, "quantity", ONE_UNIT)
        return MovingAverageCrossStrategy(short_window=short_window, long_window=long_window, quantity=quantity)

    raise SimulationConfigError(f"Strategy {strategy_name!r} is registered but cannot be constructed.")


def _read_market_prices(path: Path, file_name: str) -> list[MarketPriceEvent]:
    rows: list[MarketPriceEvent] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            try:
                raw_headers = next(reader)
            except StopIteration:
                raise SimulationInputError(f"{file_name} is empty.")

            headers = [header.strip() for header in raw_headers]
            for required in ["timestamp", "symbol", "price"]:
                if required not in headers:
                    raise SimulationInputError(f"{file_name} missing required header: {required}.")

            for row_number, raw_row in enumerate(reader, start=2):
                if not raw_row or all(audit._is_blank(cell) for cell in raw_row):
                    continue
                if len(raw_row) > len(headers):
                    raise SimulationInputError(f"{file_name} row {row_number} has more fields than headers.")
                row = dict(zip(headers, raw_row + [""] * (len(headers) - len(raw_row))))

                timestamp_text = row.get("timestamp", "")
                symbol = row.get("symbol", "").strip()
                price_text = row.get("price", "")

                if audit._is_blank(timestamp_text):
                    raise SimulationInputError(f"{file_name} row {row_number} missing timestamp.")
                if symbol == "":
                    raise SimulationInputError(f"{file_name} row {row_number} missing symbol.")
                if audit._is_blank(price_text):
                    raise SimulationInputError(f"{file_name} row {row_number} missing price.")

                timestamp = audit._parse_timestamp(timestamp_text, "datetime")
                if not isinstance(timestamp, datetime):
                    raise SimulationInputError(f"{file_name} row {row_number} has invalid ISO datetime.")

                price = audit._parse_decimal(price_text)
                if price is None:
                    raise SimulationInputError(f"{file_name} row {row_number} has invalid finite price.")

                rows.append(MarketPriceEvent(timestamp=timestamp, symbol=symbol, price=price))
    except UnicodeDecodeError as exc:
        raise SimulationInputError(f"{file_name} decode error: {exc}") from exc
    except csv.Error as exc:
        raise SimulationInputError(f"{file_name} parse error: {exc}") from exc
    return rows


def _validate_one_symbol_price_events(events: list[MarketPriceEvent], file_name: str) -> None:
    symbols = sorted({event.symbol for event in events})
    if len(symbols) != 1:
        raise SimulationInputError(f"{file_name} must contain exactly one symbol.")

    previous: datetime | None = None
    for event in events:
        if previous is not None:
            if not audit._timestamps_are_comparable(previous, event.timestamp):
                raise SimulationInputError(f"{file_name} cannot mix timezone-aware and timezone-naive timestamps.")
            if event.timestamp <= previous:
                raise SimulationInputError(f"{file_name} timestamps must be strictly increasing.")
        previous = event.timestamp


def _align_benchmark_to_market(
    market_events: list[MarketPriceEvent],
    benchmark_events: list[MarketPriceEvent],
    initial_cash: Decimal,
) -> dict[datetime, BenchmarkSnapshot]:
    snapshots: dict[datetime, BenchmarkSnapshot] = {}
    benchmark_index = 0
    latest_price: Decimal | None = None
    base_price: Decimal | None = None

    for market_event in market_events:
        while benchmark_index < len(benchmark_events) and benchmark_events[benchmark_index].timestamp <= market_event.timestamp:
            latest_price = benchmark_events[benchmark_index].price
            if base_price is None:
                base_price = latest_price
            benchmark_index += 1
        if latest_price is None or base_price is None or base_price == 0:
            snapshots[market_event.timestamp] = BenchmarkSnapshot(price=None, equity=None)
        else:
            snapshots[market_event.timestamp] = BenchmarkSnapshot(
                price=latest_price,
                equity=initial_cash * latest_price / base_price,
            )
    return snapshots


def _write_run_artifacts(export_dir: Path, result: SimulationResult, dataset_dir: Path, run_id: str) -> None:
    _write_json(export_dir / "run_manifest.json", _manifest_payload(result, dataset_dir, run_id))
    _write_equity_curve(export_dir / "equity_curve.csv", result)
    _write_trades(export_dir / "trades.csv", result)
    _write_orders(export_dir / "orders.csv", result)
    _write_fills(export_dir / "fills.csv", result)
    _write_json(export_dir / "account_summary.json", _account_summary_payload(result))

    audit_result = audit.audit_dataset(export_dir)
    _write_json(export_dir / "audit_summary.json", _audit_summary_payload(audit_result))


def _manifest_payload(result: SimulationResult, dataset_dir: Path, run_id: str) -> dict[str, object]:
    return {
        "schema_version": RUN_ARTIFACT_SCHEMA_VERSION,
        "run_id": run_id,
        "dataset_dir": str(dataset_dir),
        "strategy_name": result.strategy_name,
        "initial_cash": format_decimal(result.initial_cash),
        "risk_free_rate": format_decimal(result.risk_free_rate),
        "input_files": _input_files_payload(result),
        "friction": {
            "fee_rate": format_decimal(_result_fee_rate(result)),
            "slippage_per_trade": format_decimal(_result_slippage_per_trade(result)),
            "total_fees": format_decimal(result.total_fees),
            "total_slippage": format_decimal(result.total_slippage),
        },
        "simulation_assumptions": [
            "immediate fills",
            _fee_assumption(result),
            _slippage_assumption(result),
            "one symbol only",
            "one deterministic strategy selected from a static registry or default legacy strategy",
        ],
        "warnings": list(result.warnings),
        "generated_at_policy": "omitted_for_determinism",
    }


def _account_summary_payload(result: SimulationResult) -> dict[str, object]:
    return {
        "initial_cash": format_decimal(result.initial_cash),
        "final_cash": _format_optional_decimal(result.final_cash),
        "final_equity": _format_optional_decimal(result.final_equity),
        "final_positions": {symbol: format_decimal(quantity) for symbol, quantity in sorted(result.final_positions.items())},
        "order_count": result.order_count,
        "fill_count": result.fill_count,
        "trade_count": len(result.fills),
        "total_fees": format_decimal(result.total_fees),
        "total_slippage": format_decimal(result.total_slippage),
        "status": result.status,
    }


def _audit_summary_payload(audit_result: audit.AuditResult) -> dict[str, object]:
    error_count = sum(1 for issue in audit_result.issues if issue.severity == audit.ERROR)
    gap_count = sum(1 for issue in audit_result.issues if issue.severity == audit.GAP)
    return {
        "audit_status": audit_result.status,
        "optional_gaps_only": error_count == 0 and gap_count > 0,
        "required_generated_outputs_valid": error_count == 0,
        "issues": [issue.to_dict() for issue in audit_result.issues],
    }


def _write_equity_curve(path: Path, result: SimulationResult) -> None:
    headers = [
        "timestamp",
        "equity",
        "cash",
        "position_value",
        "symbol",
        "position_quantity",
        "last_price",
        "benchmark_price",
        "benchmark_equity",
    ]
    rows = [
        [
            snapshot.timestamp.date().isoformat(),
            format_decimal(snapshot.equity),
            format_decimal(snapshot.cash),
            format_decimal(snapshot.position_value),
            snapshot.symbol,
            format_decimal(snapshot.position_quantity),
            format_decimal(snapshot.last_price),
            _format_optional_csv_decimal(snapshot.benchmark_price),
            _format_optional_csv_decimal(snapshot.benchmark_equity),
        ]
        for snapshot in result.equity_curve
    ]
    _write_csv(path, headers, rows)


def _write_trades(path: Path, result: SimulationResult) -> None:
    headers = [
        "timestamp",
        "trade_id",
        "strategy",
        "side",
        "quantity",
        "price",
        "cost",
        "realized_pnl",
        "symbol",
        "strategy_name",
        "preset_name",
        "order_id",
        "fill_id",
    ]
    buy_lots: dict[str, list[dict[str, Decimal]]] = {}
    rows: list[list[str]] = []
    for index, fill in enumerate(result.fills, start=1):
        realized_pnl = ""
        fill_cost = fill.fee + fill.slippage
        if fill.side == "buy":
            buy_lots.setdefault(fill.symbol, []).append(
                {"quantity": fill.quantity, "price": fill.price, "cost": fill_cost}
            )
        elif fill.side == "sell":
            realized = _realized_pnl_for_sell(fill, buy_lots.get(fill.symbol, []))
            if realized is not None:
                realized_pnl = format_decimal(realized)
        rows.append(
            [
                fill.timestamp.isoformat(),
                f"T{index:06d}",
                SIMULATION_PRESET_NAME,
                fill.side,
                format_decimal(fill.quantity),
                format_decimal(fill.price),
                format_decimal(fill_cost),
                realized_pnl,
                fill.symbol,
                result.strategy_name,
                SIMULATION_PRESET_NAME,
                fill.order_id,
                fill.fill_id,
            ]
        )
    _write_csv(path, headers, rows)


def _write_orders(path: Path, result: SimulationResult) -> None:
    headers = ["order_id", "timestamp", "symbol", "side", "quantity", "requested_price", "status"]
    rows = [
        [
            record.order_id,
            record.timestamp.isoformat(),
            record.order.symbol,
            record.order.side,
            format_decimal(record.order.quantity),
            format_decimal(record.order.price),
            record.status,
        ]
        for record in result.orders
    ]
    _write_csv(path, headers, rows)


def _write_fills(path: Path, result: SimulationResult) -> None:
    headers = ["fill_id", "order_id", "timestamp", "symbol", "side", "quantity", "fill_price", "fee", "slippage"]
    rows = [
        [
            fill.fill_id,
            fill.order_id,
            fill.timestamp.isoformat(),
            fill.symbol,
            fill.side,
            format_decimal(fill.quantity),
            format_decimal(fill.price),
            format_decimal(fill.fee),
            format_decimal(fill.slippage),
        ]
        for fill in result.fills
    ]
    _write_csv(path, headers, rows)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)


def _input_files_payload(result: SimulationResult) -> list[str]:
    input_files = ["market_prices.csv"]
    if any(snapshot.benchmark_price is not None for snapshot in result.equity_curve):
        input_files.append("benchmark_prices.csv")
    return input_files


def _market_state(event: MarketPriceEvent) -> MarketState:
    return MarketState(timestamp=event.timestamp, symbol=event.symbol, price=event.price)


def _account_state(account: SimulatedAccount) -> AccountState:
    return AccountState(cash=account.cash, positions=dict(sorted(account.positions.items())))


def _decimal_param(params: dict[str, object], key: str, default: Decimal) -> Decimal:
    value = params.get(key, str(default))
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        raise SimulationConfigError(f"Strategy parameter {key} must be a decimal string.")
    parsed = audit._parse_decimal(value)
    if parsed is None:
        raise SimulationConfigError(f"Strategy parameter {key} must be a finite decimal.")
    return parsed


def _parse_friction_config(value: object) -> FrictionModel:
    if value is None:
        return FrictionModel()
    if not isinstance(value, dict):
        raise SimulationConfigError("Config friction must be an object.")
    fee_rate = _decimal_config_value(value, "fee_rate", ZERO, "Config friction.fee_rate")
    slippage_per_trade = _decimal_config_value(value, "slippage_per_trade", ZERO, "Config friction.slippage_per_trade")
    if fee_rate < 0:
        raise SimulationConfigError("Config friction.fee_rate must be nonnegative.")
    if slippage_per_trade < 0:
        raise SimulationConfigError("Config friction.slippage_per_trade must be nonnegative.")
    return FrictionModel(fee_rate=fee_rate, slippage_per_trade=slippage_per_trade)


def _decimal_config_value(params: dict[str, object], key: str, default: Decimal, label: str) -> Decimal:
    value = params.get(key, str(default))
    if isinstance(value, bool):
        raise SimulationConfigError(f"{label} must be a decimal number or string.")
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        raise SimulationConfigError(f"{label} must be a decimal number or string.")
    parsed = audit._parse_decimal(value)
    if parsed is None:
        raise SimulationConfigError(f"{label} must be finite.")
    return parsed


def _realized_pnl_for_sell(fill: Fill, lots: list[dict[str, Decimal]]) -> Decimal | None:
    remaining = fill.quantity
    realized = ZERO
    exit_cost = fill.fee + fill.slippage
    while remaining > 0 and lots:
        lot = lots[0]
        lot_quantity = lot["quantity"]
        matched = min(remaining, lot_quantity)
        buy_cost = _allocated_cost(lot["cost"], lot_quantity, matched)
        sell_cost = _allocated_cost(exit_cost, fill.quantity, matched)
        realized += (fill.price - lot["price"]) * matched - buy_cost - sell_cost
        remaining -= matched
        lot["quantity"] = lot_quantity - matched
        lot["cost"] -= buy_cost
        if lot["quantity"] == 0:
            lots.pop(0)
    if remaining > 0:
        return None
    return realized


def _allocated_cost(total_cost: Decimal, total_quantity: Decimal, matched_quantity: Decimal) -> Decimal:
    if total_quantity == 0:
        return ZERO
    return total_cost * matched_quantity / total_quantity


def _result_fee_rate(result: SimulationResult) -> Decimal:
    return result.friction.fee_rate


def _result_slippage_per_trade(result: SimulationResult) -> Decimal:
    return result.friction.slippage_per_trade


def _fee_assumption(result: SimulationResult) -> str:
    if result.friction.fee_rate == 0:
        return "zero fee"
    return "configured fee rate"


def _slippage_assumption(result: SimulationResult) -> str:
    if result.friction.slippage_per_trade == 0:
        return "zero slippage"
    return "configured fixed slippage per fill"


def _int_param(params: dict[str, object], key: str, default: int) -> int:
    value = params.get(key, default)
    if not isinstance(value, int):
        raise SimulationConfigError(f"Strategy parameter {key} must be an integer.")
    return value


def _audit_status_for_context(dataset_path: Path) -> str:
    try:
        return audit.audit_dataset(dataset_path).status
    except Exception:
        return "ERROR"


def _parse_initial_cash(value: str) -> Decimal:
    parsed = audit._parse_decimal(value)
    if parsed is None or parsed < 0:
        raise argparse.ArgumentTypeError("initial cash must be a finite nonnegative decimal.")
    return parsed


def format_decimal(value: Decimal) -> str:
    return format(value, "f")


def _format_optional_decimal(value: Decimal | None) -> str:
    if value is None:
        return "UNAVAILABLE"
    return format_decimal(value)


def _format_optional_csv_decimal(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format_decimal(value)


def format_positions(positions: dict[str, Decimal]) -> str:
    if not positions:
        return "none"
    return ", ".join(f"{symbol}:{format_decimal(quantity)}" for symbol, quantity in sorted(positions.items()))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
