"""Thin paper-trading simulation loop for simulated market data."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any, Iterable, Protocol

from system_trading_s3 import audit
from system_trading_s3 import input_audit


PASS = "PASS"
FAIL = "FAIL"
DEFAULT_INITIAL_CASH = Decimal("100000")
ONE_UNIT = Decimal("1")
ZERO = Decimal("0")
LEGACY_STRATEGY_NAME = "buy_and_hold_one_unit"
STRATEGY_NAME = "RoundTripBuyAndHold"
DEFAULT_RUN_ID = "default"
SIMULATION_PRESET_NAME = "market_follow"
RUN_ARTIFACT_SCHEMA_VERSION = "mvp2.run_artifacts.v1"
ARTIFACT_FILES = [
    "run_manifest.json",
    "equity_curve.csv",
    "trades.csv",
    "orders.csv",
    "order_events.csv",
    "risk_events.csv",
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
    prices: dict[str, Decimal] = field(default_factory=dict)
    factor_data: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class FactorEvent:
    timestamp: datetime
    symbol: str
    factor_name: str
    factor_value: float


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


WeightValue = Decimal | int | float | str


@dataclass(frozen=True)
class TargetWeights:
    weights: dict[str, WeightValue]


StrategyIntent = list[Order] | TargetWeights | dict[str, WeightValue] | None


@dataclass(frozen=True)
class MarketState:
    timestamp: datetime
    symbol: str
    price: Decimal
    prices: dict[str, Decimal] = field(default_factory=dict)
    factor_data: dict[str, dict[str, float]] = field(default_factory=dict)


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
class OrderEventRecord:
    event_id: str
    order_id: str
    timestamp: datetime
    event_type: str
    status: str
    filled_quantity: Decimal
    remaining_quantity: Decimal
    message: str = ""


@dataclass(frozen=True)
class RiskEventRecord:
    event_id: str
    timestamp: datetime
    order_id: str
    symbol: str
    rule: str
    action: str
    original_quantity: Decimal
    adjusted_quantity: Decimal
    message: str


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
    prices: dict[str, Decimal] = field(default_factory=dict)
    positions: dict[str, Decimal] = field(default_factory=dict)
    factor_data: dict[str, dict[str, float]] = field(default_factory=dict)


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
    execution: ExecutionConfig
    risk_free_rate: Decimal
    risk: RiskConfig
    total_fees: Decimal
    total_slippage: Decimal
    fills: list[Fill]
    orders: list[OrderRecord]
    order_events: list[OrderEventRecord]
    risk_events: list[RiskEventRecord]
    equity_curve: list[AccountSnapshot]
    input_files: list[str]
    warnings: list[str]
    error: str | None = None


@dataclass(frozen=True)
class FrictionModel:
    fee_rate: Decimal = ZERO
    slippage_per_trade: Decimal = ZERO


@dataclass(frozen=True)
class ExecutionConfig:
    max_fill_quantity: Decimal | None = None
    partial_fill_policy: str = "cancel_remainder"


@dataclass(frozen=True)
class OpenOrder:
    order_id: str
    order: Order
    remaining_quantity: Decimal


@dataclass(frozen=True)
class RiskConfig:
    max_position_weight: Decimal | None = None
    min_cash_buffer: Decimal = ZERO
    max_order_notional: Decimal | None = None
    cooldown_periods: int = 0
    max_drawdown_pct: Decimal | None = None


@dataclass(frozen=True)
class SimulationConfig:
    initial_cash: Decimal
    strategy_name: str
    strategy_params: dict[str, object]
    friction: FrictionModel = FrictionModel()
    execution: ExecutionConfig = ExecutionConfig()
    risk_free_rate: Decimal = ZERO
    risk: RiskConfig = RiskConfig()


@dataclass(frozen=True)
class RiskDecision:
    order: Order | None
    events: list[RiskEventRecord]


@dataclass(frozen=True)
class StrategyParamSpec:
    name: str
    param_type: str
    default: object
    label: str

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "type": self.param_type,
            "default": self.default,
            "label": self.label,
        }


@dataclass(frozen=True)
class StrategyPreset:
    name: str
    description: str
    strategy_class: type
    params: tuple[StrategyParamSpec, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "params": [param.to_payload() for param in self.params],
        }


class Strategy(Protocol):
    name: str

    def on_data(self, market_state: MarketState, account_state: AccountState) -> StrategyIntent:
        ...

    def on_finish(self, final_event: MarketPriceEvent, account: SimulatedAccount) -> list[Order]:
        ...


class DataFeed:
    """Validated deterministic multi-symbol simulated price feed."""

    def __init__(self, events: list[MarketPriceEvent], input_files: list[str], warnings: list[str]) -> None:
        self._events = events
        self._input_files = input_files
        self.warnings = warnings

    @classmethod
    def from_dataset(cls, dataset_dir: Path | str) -> "DataFeed":
        root = Path(dataset_dir)
        path = root / "market_prices.csv"
        if path.exists():
            if not path.is_file():
                raise SimulationInputError("market_prices.csv must be a file.")
            raw_events = _read_market_prices(path, file_name="market_prices.csv")
            input_files = ["market_prices.csv"]
        else:
            price_files = _market_price_files(root)
            if not price_files:
                raise SimulationInputError("market_prices.csv is missing.")
            raw_events = []
            input_files = []
            for price_file in price_files:
                raw_events.extend(_read_market_prices(price_file, file_name=price_file.name))
                input_files.append(price_file.name)

        events = _merge_market_events(raw_events)
        if len(events) < 2:
            raise SimulationInputError("market price inputs must contain at least two synchronized price rows.")
        factor_path = root / "factors.csv"
        warnings: list[str] = []
        if factor_path.exists():
            if not factor_path.is_file():
                raise SimulationInputError("factors.csv must be a file.")
            factor_events = _read_factors(factor_path)
            events = _attach_factors(events, factor_events)
            input_files.append("factors.csv")

        return cls(events, input_files, warnings)

    def __iter__(self) -> Iterable[MarketPriceEvent]:
        return iter(self._events)

    @property
    def events(self) -> list[MarketPriceEvent]:
        return list(self._events)

    @property
    def input_files(self) -> list[str]:
        return list(self._input_files)


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
    def __init__(self, friction: FrictionModel = FrictionModel(), execution: ExecutionConfig = ExecutionConfig()) -> None:
        if friction.fee_rate < 0 or friction.slippage_per_trade < 0:
            raise SimulationInputError("friction values must be nonnegative.")
        if execution.max_fill_quantity is not None and execution.max_fill_quantity <= 0:
            raise SimulationInputError("execution.max_fill_quantity must be positive when provided.")
        if execution.partial_fill_policy not in {"cancel_remainder", "carry_forward"}:
            raise SimulationInputError("execution.partial_fill_policy must be cancel_remainder or carry_forward.")
        self.friction = friction
        self.execution = execution

    def fill(self, timestamp: datetime, order: Order, order_id: str = "", fill_id: str = "") -> Fill:
        quantity = order.quantity
        if self.execution.max_fill_quantity is not None:
            quantity = min(quantity, self.execution.max_fill_quantity)
        notional = quantity * order.price
        fee = notional * self.friction.fee_rate
        slippage = self.friction.slippage_per_trade
        return Fill(
            timestamp=timestamp,
            side=order.side,
            symbol=order.symbol,
            quantity=quantity,
            price=order.price,
            order_id=order_id,
            fill_id=fill_id,
            fee=fee,
            slippage=slippage,
        )


class PortfolioRebalancer:
    """Convert target weights into deterministic integer market orders."""

    def __init__(self, friction: FrictionModel = FrictionModel()) -> None:
        if friction.fee_rate < 0 or friction.slippage_per_trade < 0:
            raise SimulationInputError("friction values must be nonnegative.")
        self.friction = friction

    def orders_for_target_weights(
        self,
        target_weights: TargetWeights | dict[str, WeightValue],
        account_state: AccountState,
        market_state: MarketState,
    ) -> list[Order]:
        prices = _state_prices(market_state)
        weights = _normalize_target_weights(target_weights, prices)
        equity = _portfolio_equity(account_state.cash, account_state.positions, prices)
        if equity <= 0:
            return []

        cash = account_state.cash
        positions = dict(sorted(account_state.positions.items()))
        orders: list[Order] = []

        for symbol in sorted(positions):
            if symbol not in prices:
                continue
            price = prices[symbol]
            quantity = positions.get(symbol, ZERO)
            target_value = equity * weights.get(symbol, ZERO)
            current_value = quantity * price
            if quantity <= 0 or current_value <= target_value:
                continue
            sell_quantity = min(quantity, _floor_decimal((current_value - target_value) / price))
            if sell_quantity <= 0:
                continue
            orders.append(Order(side="sell", symbol=symbol, quantity=sell_quantity, price=price))
            cash += self._sell_proceeds(sell_quantity, price)
            remaining = quantity - sell_quantity
            if remaining == 0:
                positions.pop(symbol, None)
            else:
                positions[symbol] = remaining

        for symbol in sorted(weights):
            if symbol not in prices:
                continue
            price = prices[symbol]
            target_value = equity * weights[symbol]
            current_value = positions.get(symbol, ZERO) * price
            if target_value <= current_value:
                continue
            desired_quantity = _floor_decimal((target_value - current_value) / price)
            if desired_quantity <= 0:
                continue
            affordable_quantity = self._max_affordable_quantity(cash, price)
            buy_quantity = min(desired_quantity, affordable_quantity)
            if buy_quantity <= 0:
                continue
            orders.append(Order(side="buy", symbol=symbol, quantity=buy_quantity, price=price))
            cash -= self._buy_cost(buy_quantity, price)
            positions[symbol] = positions.get(symbol, ZERO) + buy_quantity

        return orders

    def _buy_cost(self, quantity: Decimal, price: Decimal) -> Decimal:
        notional = quantity * price
        return notional + (notional * self.friction.fee_rate) + self.friction.slippage_per_trade

    def _sell_proceeds(self, quantity: Decimal, price: Decimal) -> Decimal:
        notional = quantity * price
        return notional - (notional * self.friction.fee_rate) - self.friction.slippage_per_trade

    def _max_affordable_quantity(self, cash: Decimal, price: Decimal) -> Decimal:
        if price <= 0:
            raise SimulationExecutionError("Cannot rebalance using nonpositive prices.")
        cash_after_fixed_cost = cash - self.friction.slippage_per_trade
        if cash_after_fixed_cost <= 0:
            return ZERO
        per_share_cost = price * (ONE_UNIT + self.friction.fee_rate)
        return _floor_decimal(cash_after_fixed_cost / per_share_cost)


class RiskRuleEngine:
    """Apply common base risk rules before simulated execution."""

    def __init__(self, risk: RiskConfig = RiskConfig(), friction: FrictionModel = FrictionModel()) -> None:
        self.risk = risk
        self.friction = friction
        self.cooldown_remaining = 0
        self.peak_equity: Decimal | None = None

    def review_order(
        self,
        event_id_prefix: str,
        timestamp: datetime,
        order_id: str,
        order: Order,
        account_state: AccountState,
        market_state: MarketState,
    ) -> RiskDecision:
        if order.side != "buy":
            return RiskDecision(order=order, events=[])

        kill_switch_event = self._drawdown_kill_switch_event(event_id_prefix, timestamp, order_id, order, account_state, market_state)
        if kill_switch_event is not None:
            return RiskDecision(order=None, events=[kill_switch_event])

        cooldown_event = self._cooldown_event(event_id_prefix, timestamp, order_id, order)
        if cooldown_event is not None:
            return RiskDecision(order=None, events=[cooldown_event])

        adjusted_quantity = order.quantity
        events: list[RiskEventRecord] = []

        adjusted_quantity = self._apply_max_order_notional(
            event_id_prefix, timestamp, order_id, order, adjusted_quantity, events
        )
        adjusted_quantity = self._apply_max_position_weight(
            event_id_prefix, timestamp, order_id, order, account_state, market_state, adjusted_quantity, events
        )
        adjusted_quantity = self._apply_min_cash_buffer(
            event_id_prefix, timestamp, order_id, order, account_state, adjusted_quantity, events
        )

        if adjusted_quantity <= 0:
            if not events:
                events.append(
                    self._event(
                        event_id_prefix,
                        timestamp,
                        order_id,
                        order,
                        "risk_rejected",
                        "rejected",
                        order.quantity,
                        ZERO,
                        "Order rejected by risk rules.",
                    )
                )
            return RiskDecision(order=None, events=events)
        if adjusted_quantity != order.quantity:
            self._start_cooldown()
            return RiskDecision(
                order=Order(side=order.side, symbol=order.symbol, quantity=adjusted_quantity, price=order.price),
                events=events,
            )
        self._start_cooldown()
        return RiskDecision(order=order, events=events)

    def _drawdown_kill_switch_event(
        self,
        event_id_prefix: str,
        timestamp: datetime,
        order_id: str,
        order: Order,
        account_state: AccountState,
        market_state: MarketState,
    ) -> RiskEventRecord | None:
        if self.risk.max_drawdown_pct is None:
            return None
        equity = _portfolio_equity(account_state.cash, account_state.positions, _state_prices(market_state))
        if equity <= 0:
            drawdown_pct = Decimal("100")
        else:
            if self.peak_equity is None or equity > self.peak_equity:
                self.peak_equity = equity
            if self.peak_equity is None or self.peak_equity <= 0:
                return None
            drawdown_pct = (self.peak_equity - equity) / self.peak_equity * Decimal("100")
        if drawdown_pct < self.risk.max_drawdown_pct:
            return None
        return self._event(
            event_id_prefix,
            timestamp,
            order_id,
            order,
            "max_drawdown_pct",
            "rejected",
            order.quantity,
            ZERO,
            "Buy order rejected by drawdown kill switch.",
        )

    def _cooldown_event(
        self,
        event_id_prefix: str,
        timestamp: datetime,
        order_id: str,
        order: Order,
    ) -> RiskEventRecord | None:
        if self.risk.cooldown_periods <= 0 or self.cooldown_remaining <= 0:
            return None
        self.cooldown_remaining -= 1
        return self._event(
            event_id_prefix,
            timestamp,
            order_id,
            order,
            "cooldown_periods",
            "rejected",
            order.quantity,
            ZERO,
            "Buy order rejected during configured cooldown period.",
        )

    def _start_cooldown(self) -> None:
        if self.risk.cooldown_periods > 0:
            self.cooldown_remaining = self.risk.cooldown_periods

    def _apply_max_order_notional(
        self,
        event_id_prefix: str,
        timestamp: datetime,
        order_id: str,
        order: Order,
        current_quantity: Decimal,
        events: list[RiskEventRecord],
    ) -> Decimal:
        if self.risk.max_order_notional is None:
            return current_quantity
        max_quantity = _floor_decimal(self.risk.max_order_notional / order.price)
        if current_quantity <= max_quantity:
            return current_quantity
        adjusted = max(max_quantity, ZERO)
        events.append(
            self._event(
                event_id_prefix,
                timestamp,
                order_id,
                order,
                "max_order_notional",
                "adjusted" if adjusted > 0 else "rejected",
                current_quantity,
                adjusted,
                "Buy order quantity capped by max_order_notional.",
            )
        )
        return adjusted

    def _apply_max_position_weight(
        self,
        event_id_prefix: str,
        timestamp: datetime,
        order_id: str,
        order: Order,
        account_state: AccountState,
        market_state: MarketState,
        current_quantity: Decimal,
        events: list[RiskEventRecord],
    ) -> Decimal:
        if self.risk.max_position_weight is None:
            return current_quantity
        prices = _state_prices(market_state)
        equity = _portfolio_equity(account_state.cash, account_state.positions, prices)
        if equity <= 0:
            return ZERO
        current_position_value = account_state.positions.get(order.symbol, ZERO) * order.price
        max_position_value = equity * self.risk.max_position_weight
        allowed_additional_value = max_position_value - current_position_value
        max_quantity = _floor_decimal(allowed_additional_value / order.price)
        if current_quantity <= max_quantity:
            return current_quantity
        adjusted = max(max_quantity, ZERO)
        events.append(
            self._event(
                event_id_prefix,
                timestamp,
                order_id,
                order,
                "max_position_weight",
                "adjusted" if adjusted > 0 else "rejected",
                current_quantity,
                adjusted,
                "Buy order quantity capped by max_position_weight.",
            )
        )
        return adjusted

    def _apply_min_cash_buffer(
        self,
        event_id_prefix: str,
        timestamp: datetime,
        order_id: str,
        order: Order,
        account_state: AccountState,
        current_quantity: Decimal,
        events: list[RiskEventRecord],
    ) -> Decimal:
        if self.risk.min_cash_buffer <= 0:
            return current_quantity
        spendable_cash = account_state.cash - self.risk.min_cash_buffer - self.friction.slippage_per_trade
        if spendable_cash <= 0:
            adjusted = ZERO
        else:
            adjusted = min(current_quantity, _floor_decimal(spendable_cash / (order.price * (ONE_UNIT + self.friction.fee_rate))))
        if adjusted == current_quantity:
            return current_quantity
        events.append(
            self._event(
                event_id_prefix,
                timestamp,
                order_id,
                order,
                "min_cash_buffer",
                "adjusted" if adjusted > 0 else "rejected",
                current_quantity,
                adjusted,
                "Buy order quantity capped to preserve min_cash_buffer.",
            )
        )
        return adjusted

    def _event(
        self,
        event_id_prefix: str,
        timestamp: datetime,
        order_id: str,
        order: Order,
        rule: str,
        action: str,
        original_quantity: Decimal,
        adjusted_quantity: Decimal,
        message: str,
    ) -> RiskEventRecord:
        return RiskEventRecord(
            event_id=event_id_prefix,
            timestamp=timestamp,
            order_id=order_id,
            symbol=order.symbol,
            rule=rule,
            action=action,
            original_quantity=original_quantity,
            adjusted_quantity=adjusted_quantity,
            message=message,
        )


class BuyAndHoldOneUnitStrategy:
    name = STRATEGY_NAME

    def __init__(self) -> None:
        self._bought_symbols: set[str] = set()

    def on_event(self, event: MarketPriceEvent, account: SimulatedAccount) -> list[Order]:
        del account
        orders: list[Order] = []
        for symbol, price in sorted(_event_prices(event).items()):
            if symbol in self._bought_symbols:
                continue
            self._bought_symbols.add(symbol)
            orders.append(Order(side="buy", symbol=symbol, quantity=ONE_UNIT, price=price))
        return orders

    def on_data(self, market_state: MarketState, account_state: AccountState) -> list[Order]:
        del account_state
        event = MarketPriceEvent(
            timestamp=market_state.timestamp,
            symbol=market_state.symbol,
            price=market_state.price,
            prices=dict(sorted(_state_prices(market_state).items())),
        )
        return self.on_event(event, SimulatedAccount(Decimal("0")))

    def on_finish(self, final_event: MarketPriceEvent, account: SimulatedAccount) -> list[Order]:
        prices = _event_prices(final_event)
        orders: list[Order] = []
        for symbol, held_quantity in sorted(account.positions.items()):
            if held_quantity <= 0 or symbol not in prices:
                continue
            orders.append(Order(side="sell", symbol=symbol, quantity=held_quantity, price=prices[symbol]))
        return orders


class BuyAndHoldStrategy:
    name = "BuyAndHold"

    def __init__(self, quantity: Decimal = ONE_UNIT, target_symbol: str | None = None) -> None:
        if quantity <= 0:
            raise SimulationConfigError("BuyAndHold quantity must be positive.")
        self.quantity = quantity
        self.target_symbol = target_symbol
        self._bought_symbols: set[str] = set()

    def on_data(self, market_state: MarketState, account_state: AccountState) -> list[Order]:
        del account_state
        orders: list[Order] = []
        for symbol, price in sorted(_target_prices(_state_prices(market_state), self.target_symbol).items()):
            if symbol in self._bought_symbols:
                continue
            self._bought_symbols.add(symbol)
            orders.append(Order(side="buy", symbol=symbol, quantity=self.quantity, price=price))
        return orders

    def on_finish(self, final_event: MarketPriceEvent, account: SimulatedAccount) -> list[Order]:
        del final_event, account
        return []


class MovingAverageCrossStrategy:
    name = "MovingAverageCross"

    def __init__(
        self,
        short_window: int = 2,
        long_window: int = 3,
        quantity: Decimal = ONE_UNIT,
        target_symbol: str | None = None,
    ) -> None:
        if short_window <= 0:
            raise SimulationConfigError("MovingAverageCross short_window must be positive.")
        if long_window <= short_window:
            raise SimulationConfigError("MovingAverageCross long_window must be greater than short_window.")
        if quantity <= 0:
            raise SimulationConfigError("MovingAverageCross quantity must be positive.")
        self.short_window = short_window
        self.long_window = long_window
        self.quantity = quantity
        self.target_symbol = target_symbol
        self._prices: dict[str, list[Decimal]] = {}

    def on_data(self, market_state: MarketState, account_state: AccountState) -> list[Order]:
        orders: list[Order] = []
        for symbol, price in sorted(_target_prices(_state_prices(market_state), self.target_symbol).items()):
            prices = self._prices.setdefault(symbol, [])
            prices.append(price)
            if len(prices) < self.long_window:
                continue

            short_avg = sum(prices[-self.short_window:]) / Decimal(self.short_window)
            long_avg = sum(prices[-self.long_window:]) / Decimal(self.long_window)
            held_quantity = account_state.positions.get(symbol, Decimal("0"))

            if short_avg > long_avg and held_quantity <= 0:
                orders.append(Order(side="buy", symbol=symbol, quantity=self.quantity, price=price))
            elif short_avg < long_avg and held_quantity > 0:
                orders.append(Order(side="sell", symbol=symbol, quantity=held_quantity, price=price))
        return orders

    def on_finish(self, final_event: MarketPriceEvent, account: SimulatedAccount) -> list[Order]:
        del final_event, account
        return []


class EqualWeightRebalanceStrategy:
    name = "EqualWeightRebalance"

    def __init__(self) -> None:
        self._emitted = False

    def on_data(self, market_state: MarketState, account_state: AccountState) -> StrategyIntent:
        del account_state
        if self._emitted:
            return []
        prices = _state_prices(market_state)
        if not prices:
            return []
        self._emitted = True
        weight = ONE_UNIT / Decimal(len(prices))
        return TargetWeights({symbol: weight for symbol in sorted(prices)})

    def on_finish(self, final_event: MarketPriceEvent, account: SimulatedAccount) -> list[Order]:
        del final_event, account
        return []


class PeriodicFactorWeightStrategy:
    name = "PeriodicFactorWeight"

    def __init__(self, factor_name: str, rebalance_interval: int = 20, top_k: int = 1) -> None:
        if not factor_name.strip():
            raise SimulationConfigError("PeriodicFactorWeight factor_name must be non-empty.")
        if rebalance_interval <= 0:
            raise SimulationConfigError("PeriodicFactorWeight rebalance_interval must be positive.")
        if top_k <= 0:
            raise SimulationConfigError("PeriodicFactorWeight top_k must be positive.")
        self.factor_name = factor_name.strip()
        self.rebalance_interval = rebalance_interval
        self.top_k = top_k
        self._tick_index = 0

    def on_data(self, market_state: MarketState, account_state: AccountState) -> StrategyIntent:
        del account_state
        tick_index = self._tick_index
        self._tick_index += 1
        if tick_index % self.rebalance_interval != 0:
            return None

        candidates: list[tuple[float, str]] = []
        for symbol in sorted(_state_prices(market_state)):
            factor_value = market_state.factor_data.get(symbol, {}).get(self.factor_name)
            if factor_value is None:
                continue
            candidates.append((factor_value, symbol))
        if not candidates:
            return None

        selected = sorted(candidates, key=lambda item: (-item[0], item[1]))[: self.top_k]
        weight = ONE_UNIT / Decimal(len(selected))
        return TargetWeights({symbol: weight for _, symbol in sorted(selected, key=lambda item: item[1])})

    def on_finish(self, final_event: MarketPriceEvent, account: SimulatedAccount) -> list[Order]:
        del final_event, account
        return []


STRATEGY_PRESETS = {
    BuyAndHoldOneUnitStrategy.name: StrategyPreset(
        name=BuyAndHoldOneUnitStrategy.name,
        description="Buy one unit of each available symbol once, then liquidate held positions at the final tick.",
        strategy_class=BuyAndHoldOneUnitStrategy,
        params=(),
    ),
    BuyAndHoldStrategy.name: StrategyPreset(
        name=BuyAndHoldStrategy.name,
        description="Buy a fixed quantity once for each available symbol or a configured target symbol.",
        strategy_class=BuyAndHoldStrategy,
        params=(
            StrategyParamSpec("quantity", "decimal", "1", "Quantity"),
            StrategyParamSpec("target_symbol", "text", "", "Target Symbol"),
        ),
    ),
    MovingAverageCrossStrategy.name: StrategyPreset(
        name=MovingAverageCrossStrategy.name,
        description="Trade a deterministic short/long simple moving average cross.",
        strategy_class=MovingAverageCrossStrategy,
        params=(
            StrategyParamSpec("short_window", "integer", 2, "Short Window"),
            StrategyParamSpec("long_window", "integer", 3, "Long Window"),
            StrategyParamSpec("quantity", "decimal", "1", "Quantity"),
            StrategyParamSpec("target_symbol", "text", "", "Target Symbol"),
        ),
    ),
    EqualWeightRebalanceStrategy.name: StrategyPreset(
        name=EqualWeightRebalanceStrategy.name,
        description="Emit equal target weights across all currently available symbols on the first tick.",
        strategy_class=EqualWeightRebalanceStrategy,
        params=(),
    ),
    PeriodicFactorWeightStrategy.name: StrategyPreset(
        name=PeriodicFactorWeightStrategy.name,
        description="Every N ticks, target equal weights in the top-K symbols for a configured factor.",
        strategy_class=PeriodicFactorWeightStrategy,
        params=(
            StrategyParamSpec("factor_name", "text", "momentum", "Factor Name"),
            StrategyParamSpec("rebalance_interval", "integer", 5, "Rebalance Interval"),
            StrategyParamSpec("top_k", "integer", 10, "Top K"),
        ),
    ),
}

STRATEGY_REGISTRY = {name: preset.strategy_class for name, preset in STRATEGY_PRESETS.items()}
STRATEGY_ALIASES = {LEGACY_STRATEGY_NAME: BuyAndHoldOneUnitStrategy.name}


def strategy_catalog() -> list[dict[str, object]]:
    return [STRATEGY_PRESETS[name].to_payload() for name in sorted(STRATEGY_PRESETS)]


def registered_strategy_names() -> list[str]:
    return sorted(STRATEGY_PRESETS)


class SimulationEngine:
    def __init__(
        self,
        feed: DataFeed,
        benchmark_feed: BenchmarkFeed,
        account: SimulatedAccount,
        strategy: Strategy,
        execution: ExecutionSimulator,
        risk: RiskConfig = RiskConfig(),
    ) -> None:
        self.feed = feed
        self.benchmark_feed = benchmark_feed
        self.account = account
        self.strategy = strategy
        self.execution = execution
        self.rebalancer = PortfolioRebalancer(execution.friction)
        self.risk = RiskRuleEngine(risk, execution.friction)
        self.order_attempt_count = 0
        self.order_count = 0
        self.orders: list[OrderRecord] = []
        self.order_events: list[OrderEventRecord] = []
        self.risk_events: list[RiskEventRecord] = []
        self.fills: list[Fill] = []
        self.open_orders: list[OpenOrder] = []
        self.equity_curve: list[AccountSnapshot] = []
        self.last_prices: dict[str, Decimal] = {}

    def run(self) -> None:
        events = self.feed.events
        final_event = events[-1]

        for event in events:
            self.last_prices.update(_event_prices(event))
            self._execute_open_orders(event)
            market_state = _market_state(event)
            self._execute_intent(event, market_state, self.strategy.on_data(market_state, _account_state(self.account)))
            if event == final_event:
                self._execute_orders(final_event, self.strategy.on_finish(final_event, self.account))
                self._cancel_open_orders(final_event.timestamp)
            self._record_account_snapshot(event)

    def _execute_intent(self, event: MarketPriceEvent, market_state: MarketState, intent: StrategyIntent) -> None:
        if intent is None:
            return
        if intent == {}:
            return
        if isinstance(intent, TargetWeights) or isinstance(intent, dict):
            orders = self.rebalancer.orders_for_target_weights(intent, _account_state(self.account), market_state)
        else:
            orders = intent
        self._execute_orders(event, orders)

    def _execute_orders(self, event: MarketPriceEvent, orders: list[Order]) -> None:
        for order in orders:
            self.order_attempt_count += 1
            order_id = f"O{self.order_attempt_count:06d}"
            decision = self.risk.review_order(
                f"R{len(self.risk_events) + 1:06d}",
                event.timestamp,
                order_id,
                order,
                _account_state(self.account),
                _market_state(event),
            )
            for risk_event in decision.events:
                self._record_risk_event(risk_event)
            if decision.order is None:
                continue
            reviewed_order = decision.order
            self._record_order_event(event.timestamp, order_id, "accepted", "accepted", ZERO, reviewed_order.quantity)
            self.orders.append(OrderRecord(order_id=order_id, timestamp=event.timestamp, order=reviewed_order, status="accepted"))
            self.order_count += 1
            self._fill_order(event.timestamp, order_id, reviewed_order)

    def _execute_open_orders(self, event: MarketPriceEvent) -> None:
        if not self.open_orders:
            return
        open_orders = self.open_orders
        self.open_orders = []
        next_open_orders: list[OpenOrder] = []
        prices = _event_prices(event)
        for open_order in open_orders:
            price = prices.get(open_order.order.symbol)
            if price is None:
                next_open_orders.append(open_order)
                continue
            residual_order = Order(
                side=open_order.order.side,
                symbol=open_order.order.symbol,
                quantity=open_order.remaining_quantity,
                price=price,
            )
            remaining_quantity = self._fill_order(
                event.timestamp,
                open_order.order_id,
                residual_order,
                carry_remaining=False,
                cancel_remaining=False,
            )
            if remaining_quantity > 0:
                next_open_orders.append(OpenOrder(open_order.order_id, open_order.order, remaining_quantity))
        self.open_orders = next_open_orders

    def _fill_order(
        self,
        timestamp: datetime,
        order_id: str,
        order: Order,
        carry_remaining: bool = True,
        cancel_remaining: bool = True,
    ) -> Decimal:
        fill_id = f"F{len(self.fills) + 1:06d}"
        fill = self.execution.fill(timestamp, order, order_id=order_id, fill_id=fill_id)
        remaining_quantity = order.quantity - fill.quantity
        self.account.apply_fill(fill)
        self.fills.append(fill)
        if remaining_quantity == 0:
            total_filled = self._filled_quantity_for_order(order_id)
            original_quantity = self._order_record_for(order_id).order.quantity
            self._replace_order_status(order_id, "filled")
            self._record_order_event(timestamp, order_id, "filled", "filled", total_filled, ZERO)
            if total_filled != original_quantity:
                raise SimulationExecutionError(f"Order {order_id} filled quantity does not match original quantity.")
            return ZERO

        self._replace_order_status(order_id, "partially_filled")
        self._record_order_event(
            timestamp,
            order_id,
            "partially_filled",
            "partially_filled",
            fill.quantity,
            remaining_quantity,
            "Order partially filled by deterministic max_fill_quantity.",
        )
        if self.execution.execution.partial_fill_policy == "carry_forward" and carry_remaining:
            self.open_orders.append(OpenOrder(order_id, self._order_record_for(order_id).order, remaining_quantity))
        elif cancel_remaining:
            self._record_order_event(
                timestamp,
                order_id,
                "cancelled",
                "partially_filled",
                ZERO,
                remaining_quantity,
                "Remaining quantity cancelled after deterministic partial fill.",
            )
        return remaining_quantity

    def _cancel_open_orders(self, timestamp: datetime) -> None:
        for open_order in self.open_orders:
            self._record_order_event(
                timestamp,
                open_order.order_id,
                "cancelled",
                "partially_filled",
                ZERO,
                open_order.remaining_quantity,
                "Remaining quantity cancelled at final tick after carry-forward partial fills.",
            )
        self.open_orders = []

    def _order_record_for(self, order_id: str) -> OrderRecord:
        for record in self.orders:
            if record.order_id == order_id:
                return record
        raise SimulationExecutionError(f"Unknown order_id: {order_id}.")

    def _filled_quantity_for_order(self, order_id: str) -> Decimal:
        return sum((fill.quantity for fill in self.fills if fill.order_id == order_id), ZERO)

    def _replace_order_status(self, order_id: str, status: str) -> None:
        self.orders = [
            OrderRecord(record.order_id, record.timestamp, record.order, status if record.order_id == order_id else record.status)
            for record in self.orders
        ]

    def _record_risk_event(self, record: RiskEventRecord) -> None:
        event_id = f"R{len(self.risk_events) + 1:06d}"
        self.risk_events.append(
            RiskEventRecord(
                event_id=event_id,
                timestamp=record.timestamp,
                order_id=record.order_id,
                symbol=record.symbol,
                rule=record.rule,
                action=record.action,
                original_quantity=record.original_quantity,
                adjusted_quantity=record.adjusted_quantity,
                message=record.message,
            )
        )

    def _record_order_event(
        self,
        timestamp: datetime,
        order_id: str,
        event_type: str,
        status: str,
        filled_quantity: Decimal,
        remaining_quantity: Decimal,
        message: str = "",
    ) -> None:
        event_id = f"E{len(self.order_events) + 1:06d}"
        self.order_events.append(
            OrderEventRecord(
                event_id=event_id,
                order_id=order_id,
                timestamp=timestamp,
                event_type=event_type,
                status=status,
                filled_quantity=filled_quantity,
                remaining_quantity=remaining_quantity,
                message=message,
            )
        )

    def _record_account_snapshot(self, event: MarketPriceEvent) -> None:
        prices = _event_prices(event)
        positions = dict(sorted(self.account.positions.items()))
        position_value = sum((quantity * prices[symbol] for symbol, quantity in positions.items() if symbol in prices), ZERO)
        display_symbol = event.symbol if len(prices) == 1 else "PORTFOLIO"
        display_quantity = positions.get(event.symbol, ZERO) if len(prices) == 1 else sum(positions.values(), ZERO)
        display_price = event.price if len(prices) == 1 else ZERO
        benchmark_snapshot = self.benchmark_feed.snapshot_for(event.timestamp)
        self.equity_curve.append(
            AccountSnapshot(
                timestamp=event.timestamp,
                equity=self.account.cash + position_value,
                cash=self.account.cash,
                position_value=position_value,
                symbol=display_symbol,
                position_quantity=display_quantity,
                last_price=display_price,
                benchmark_price=benchmark_snapshot.price,
                benchmark_equity=benchmark_snapshot.equity,
                prices=dict(sorted(prices.items())),
                positions=positions,
                factor_data={symbol: dict(sorted(values.items())) for symbol, values in sorted(event.factor_data.items())},
            )
        )


def run_simulation(
    dataset_dir: Path | str,
    initial_cash: Decimal = DEFAULT_INITIAL_CASH,
    strategy: Strategy | None = None,
    friction: FrictionModel = FrictionModel(),
    risk_free_rate: Decimal = ZERO,
    risk: RiskConfig = RiskConfig(),
    execution: ExecutionConfig = ExecutionConfig(),
) -> SimulationResult:
    dataset_path = Path(dataset_dir)
    audit_status = _audit_status_for_context(dataset_path)
    strategy_name = strategy.name if strategy is not None else STRATEGY_NAME

    try:
        feed = DataFeed.from_dataset(dataset_path)
        benchmark_feed = BenchmarkFeed.from_dataset(dataset_path, feed.events, initial_cash)
        account = SimulatedAccount(initial_cash)
        selected_strategy = strategy if strategy is not None else BuyAndHoldOneUnitStrategy()
        engine = SimulationEngine(feed, benchmark_feed, account, selected_strategy, ExecutionSimulator(friction, execution), risk)
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
            execution=execution,
            risk_free_rate=risk_free_rate,
            risk=risk,
            total_fees=total_fees,
            total_slippage=total_slippage,
            fills=list(engine.fills),
            orders=list(engine.orders),
            order_events=list(engine.order_events),
            risk_events=list(engine.risk_events),
            equity_curve=list(engine.equity_curve),
            input_files=feed.input_files + (["benchmark_prices.csv"] if not benchmark_feed.warnings else []),
            warnings=list(feed.warnings) + list(benchmark_feed.warnings),
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
            execution=execution,
            risk_free_rate=risk_free_rate,
            risk=risk,
            total_fees=ZERO,
            total_slippage=ZERO,
            fills=[],
            orders=[],
            order_events=[],
            risk_events=[],
            equity_curve=[],
            input_files=[],
            warnings=[],
            error=str(exc),
        )


def format_result(result: SimulationResult) -> str:
    lines = [
        f"SIMULATION STATUS: {result.status}",
        f"DATASET: {result.dataset}",
        f"INPUT AUDIT STATUS: {result.audit_status}",
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
        selected_execution = config.execution if config is not None else ExecutionConfig()
        selected_risk_free_rate = config.risk_free_rate if config is not None else ZERO
        selected_risk = config.risk if config is not None else RiskConfig()
        result = run_simulation(
            args.dataset_dir,
            selected_initial_cash,
            selected_strategy,
            selected_friction,
            selected_risk_free_rate,
            selected_risk,
            selected_execution,
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

    return simulation_config_from_dict(payload)


def simulation_config_from_dict(payload: dict[str, object]) -> SimulationConfig:
    _reject_unknown_config_keys(
        payload,
        {"initial_cash", "strategy_name", "strategy_params", "friction", "execution", "risk_free_rate", "risk"},
        "Config",
    )
    initial_cash_value = payload.get("initial_cash")
    strategy_name = payload.get("strategy_name")
    strategy_params = payload.get("strategy_params")
    friction_payload = payload.get("friction", {})
    execution_payload = payload.get("execution", {})
    risk_free_rate = _decimal_config_value(payload, "risk_free_rate", ZERO, "Config risk_free_rate")
    risk_payload = payload.get("risk", {})

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
    execution = _parse_execution_config(execution_payload)
    risk = _parse_risk_config(risk_payload)
    return SimulationConfig(
        initial_cash=initial_cash,
        strategy_name=strategy_name.strip(),
        strategy_params=strategy_params,
        friction=friction,
        execution=execution,
        risk_free_rate=risk_free_rate,
        risk=risk,
    )


def create_strategy(strategy_name: str, strategy_params: dict[str, object]) -> Strategy:
    canonical_name = _canonical_strategy_name(strategy_name)
    strategy_class = STRATEGY_REGISTRY.get(canonical_name)
    if strategy_class is None:
        available = ", ".join(sorted(STRATEGY_REGISTRY))
        aliases = ", ".join(f"{alias}->{target}" for alias, target in sorted(STRATEGY_ALIASES.items()))
        alias_message = f" Accepted aliases: {aliases}." if aliases else ""
        raise SimulationConfigError(f"Unknown strategy_name {strategy_name!r}. Available strategies: {available}.{alias_message}")

    if strategy_class is BuyAndHoldOneUnitStrategy:
        _reject_unknown_params(strategy_params, set())
        return BuyAndHoldOneUnitStrategy()
    if strategy_class is BuyAndHoldStrategy:
        quantity = _decimal_param(strategy_params, "quantity", ONE_UNIT)
        target_symbol = _optional_str_param(strategy_params, "target_symbol")
        return BuyAndHoldStrategy(quantity=quantity, target_symbol=target_symbol)
    if strategy_class is EqualWeightRebalanceStrategy:
        _reject_unknown_params(strategy_params, set())
        return EqualWeightRebalanceStrategy()
    if strategy_class is MovingAverageCrossStrategy:
        short_window = _int_param(strategy_params, "short_window", 2)
        long_window = _int_param(strategy_params, "long_window", 3)
        quantity = _decimal_param(strategy_params, "quantity", ONE_UNIT)
        target_symbol = _optional_str_param(strategy_params, "target_symbol")
        return MovingAverageCrossStrategy(
            short_window=short_window,
            long_window=long_window,
            quantity=quantity,
            target_symbol=target_symbol,
        )
    if strategy_class is PeriodicFactorWeightStrategy:
        factor_name = _required_str_param(strategy_params, "factor_name")
        rebalance_interval = _int_param(strategy_params, "rebalance_interval", 20)
        top_k = _int_param(strategy_params, "top_k", 1)
        _reject_unknown_params(strategy_params, {"factor_name", "rebalance_interval", "top_k"})
        return PeriodicFactorWeightStrategy(
            factor_name=factor_name,
            rebalance_interval=rebalance_interval,
            top_k=top_k,
        )

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


def _read_factors(path: Path) -> list[FactorEvent]:
    rows: list[FactorEvent] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            try:
                raw_headers = next(reader)
            except StopIteration:
                raise SimulationInputError("factors.csv is empty.")

            headers = [header.strip() for header in raw_headers]
            for required in ["timestamp", "symbol", "factor_name", "factor_value"]:
                if required not in headers:
                    raise SimulationInputError(f"factors.csv missing required header: {required}.")

            seen: set[tuple[datetime, str, str]] = set()
            for row_number, raw_row in enumerate(reader, start=2):
                if not raw_row or all(audit._is_blank(cell) for cell in raw_row):
                    continue
                if len(raw_row) > len(headers):
                    raise SimulationInputError(f"factors.csv row {row_number} has more fields than headers.")
                row = dict(zip(headers, raw_row + [""] * (len(headers) - len(raw_row))))
                timestamp_text = row.get("timestamp", "")
                symbol = row.get("symbol", "").strip()
                factor_name = row.get("factor_name", "").strip()
                factor_value_text = row.get("factor_value", "")
                if audit._is_blank(timestamp_text):
                    raise SimulationInputError(f"factors.csv row {row_number} missing timestamp.")
                if symbol == "":
                    raise SimulationInputError(f"factors.csv row {row_number} missing symbol.")
                if factor_name == "":
                    raise SimulationInputError(f"factors.csv row {row_number} missing factor_name.")
                if audit._is_blank(factor_value_text):
                    raise SimulationInputError(f"factors.csv row {row_number} missing factor_value.")

                timestamp = audit._parse_timestamp(timestamp_text, "datetime")
                if not isinstance(timestamp, datetime):
                    raise SimulationInputError(f"factors.csv row {row_number} has invalid ISO datetime.")
                factor_decimal = audit._parse_decimal(factor_value_text)
                if factor_decimal is None:
                    raise SimulationInputError(f"factors.csv row {row_number} has invalid finite factor_value.")
                key = (timestamp, symbol, factor_name)
                if key in seen:
                    raise SimulationInputError(
                        f"factors.csv row {row_number} duplicate timestamp/symbol/factor_name."
                    )
                seen.add(key)
                rows.append(
                    FactorEvent(
                        timestamp=timestamp,
                        symbol=symbol,
                        factor_name=factor_name,
                        factor_value=float(factor_decimal),
                    )
                )
    except UnicodeDecodeError as exc:
        raise SimulationInputError(f"factors.csv decode error: {exc}") from exc
    except csv.Error as exc:
        raise SimulationInputError(f"factors.csv parse error: {exc}") from exc
    if rows:
        _validate_comparable_factor_timestamps(rows, "factors.csv")
    return rows


def _attach_factors(events: list[MarketPriceEvent], factor_events: list[FactorEvent]) -> list[MarketPriceEvent]:
    if not factor_events:
        return events
    sorted_factors = sorted(factor_events, key=lambda item: (item.timestamp, item.symbol, item.factor_name))
    current: dict[str, dict[str, float]] = {}
    factor_index = 0
    enriched: list[MarketPriceEvent] = []
    for event in events:
        while factor_index < len(sorted_factors) and sorted_factors[factor_index].timestamp <= event.timestamp:
            factor = sorted_factors[factor_index]
            current.setdefault(factor.symbol, {})[factor.factor_name] = factor.factor_value
            factor_index += 1
        prices = _event_prices(event)
        factor_data = {
            symbol: dict(sorted(current.get(symbol, {}).items()))
            for symbol in sorted(prices)
            if current.get(symbol)
        }
        enriched.append(
            MarketPriceEvent(
                timestamp=event.timestamp,
                symbol=event.symbol,
                price=event.price,
                prices=prices,
                factor_data=factor_data,
            )
        )
    return enriched


def _market_price_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.glob("*_prices.csv")
        if path.name not in {"benchmark_prices.csv", "market_prices.csv"} and path.is_file()
    )


def _merge_market_events(raw_events: list[MarketPriceEvent]) -> list[MarketPriceEvent]:
    if not raw_events:
        return []

    _validate_comparable_timestamps(raw_events, "market price inputs")
    events_by_timestamp: dict[datetime, list[MarketPriceEvent]] = {}
    seen_keys: set[tuple[datetime, str]] = set()
    for event in raw_events:
        key = (event.timestamp, event.symbol)
        if key in seen_keys:
            raise SimulationInputError(
                f"market price inputs contain duplicate timestamp/symbol: {event.timestamp.isoformat()} {event.symbol}."
            )
        seen_keys.add(key)
        events_by_timestamp.setdefault(event.timestamp, []).append(event)

    current_prices: dict[str, Decimal] = {}
    merged: list[MarketPriceEvent] = []
    for timestamp in sorted(events_by_timestamp):
        for event in sorted(events_by_timestamp[timestamp], key=lambda item: item.symbol):
            current_prices[event.symbol] = event.price
        prices = dict(sorted(current_prices.items()))
        primary_symbol = sorted(prices)[0]
        merged.append(
            MarketPriceEvent(
                timestamp=timestamp,
                symbol=primary_symbol,
                price=prices[primary_symbol],
                prices=prices,
            )
        )
    return merged


def _validate_one_symbol_price_events(events: list[MarketPriceEvent], file_name: str) -> None:
    symbols = sorted({event.symbol for event in events})
    if len(symbols) != 1:
        raise SimulationInputError(f"{file_name} must contain exactly one symbol.")

    _validate_comparable_timestamps(events, file_name)
    previous_by_symbol: dict[str, datetime] = {}
    for event in events:
        previous = previous_by_symbol.get(event.symbol)
        if previous is not None and event.timestamp <= previous:
            raise SimulationInputError(f"{file_name} timestamps must be strictly increasing.")
        previous_by_symbol[event.symbol] = event.timestamp


def _validate_comparable_timestamps(events: list[MarketPriceEvent], file_name: str) -> None:
    first = events[0].timestamp
    for event in events[1:]:
        if not audit._timestamps_are_comparable(first, event.timestamp):
            raise SimulationInputError(f"{file_name} cannot mix timezone-aware and timezone-naive timestamps.")


def _validate_comparable_factor_timestamps(events: list[FactorEvent], file_name: str) -> None:
    first = events[0].timestamp
    for event in events[1:]:
        if not audit._timestamps_are_comparable(first, event.timestamp):
            raise SimulationInputError(f"{file_name} cannot mix timezone-aware and timezone-naive timestamps.")


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
    _write_benchmark_returns(export_dir / "benchmark.csv", result)
    _write_factor_exposure_series(export_dir / "factor_exposure.csv", result)
    _write_trades(export_dir / "trades.csv", result)
    _write_orders(export_dir / "orders.csv", result)
    _write_order_events(export_dir / "order_events.csv", result)
    _write_risk_events(export_dir / "risk_events.csv", result)
    _write_fills(export_dir / "fills.csv", result)
    _write_json(export_dir / "account_summary.json", _account_summary_payload(result))

    audit_result = audit.audit_dataset(export_dir)
    _write_json(export_dir / "audit_summary.json", _audit_summary_payload(audit_result))


def _manifest_payload(result: SimulationResult, dataset_dir: Path, run_id: str) -> dict[str, object]:
    return {
        "schema_version": RUN_ARTIFACT_SCHEMA_VERSION,
        "run_id": run_id,
        "dataset_dir": str(dataset_dir),
        "input_audit_status": result.audit_status,
        "strategy_name": result.strategy_name,
        "strategy_preset": _strategy_preset_payload(result.strategy_name),
        "initial_cash": format_decimal(result.initial_cash),
        "risk_free_rate": format_decimal(result.risk_free_rate),
        "input_files": _input_files_payload(result),
        "friction": {
            "fee_rate": format_decimal(_result_fee_rate(result)),
            "slippage_per_trade": format_decimal(_result_slippage_per_trade(result)),
            "total_fees": format_decimal(result.total_fees),
            "total_slippage": format_decimal(result.total_slippage),
        },
        "execution": {
            "max_fill_quantity": _format_optional_decimal(result.execution.max_fill_quantity),
            "partial_fill_enabled": result.execution.max_fill_quantity is not None,
            "partial_fill_policy": result.execution.partial_fill_policy,
        },
        "risk": {
            "max_position_weight": _format_optional_decimal(result.risk.max_position_weight),
            "min_cash_buffer": format_decimal(result.risk.min_cash_buffer),
            "max_order_notional": _format_optional_decimal(result.risk.max_order_notional),
            "cooldown_periods": result.risk.cooldown_periods,
            "max_drawdown_pct": _format_optional_decimal(result.risk.max_drawdown_pct),
            "risk_event_count": len(result.risk_events),
        },
        "simulation_assumptions": [
            _execution_assumption(result),
            "order lifecycle events emitted as accepted, filled, partially_filled, or cancelled",
            "common base risk rules applied before simulated execution",
            _fee_assumption(result),
            _slippage_assumption(result),
            "multi-symbol portfolio accounting with forward-filled prices",
            "one deterministic strategy selected from a source-of-truth preset catalog",
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
        "last_prices",
        "position_quantities",
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
            _format_decimal_mapping(snapshot.prices),
            _format_decimal_mapping(snapshot.positions),
        ]
        for snapshot in result.equity_curve
    ]
    _write_csv(path, headers, rows)


def _write_benchmark_returns(path: Path, result: SimulationResult) -> None:
    rows: list[list[str]] = []
    for previous, current in zip(result.equity_curve, result.equity_curve[1:]):
        if previous.benchmark_equity is None or current.benchmark_equity is None:
            continue
        if previous.benchmark_equity <= 0:
            continue
        benchmark_return = (current.benchmark_equity - previous.benchmark_equity) / previous.benchmark_equity
        rows.append([current.timestamp.isoformat(), format_decimal(benchmark_return)])
    if rows:
        _write_csv(path, ["timestamp", "benchmark_return"], rows)


def _write_factor_exposure_series(path: Path, result: SimulationResult) -> None:
    rows: list[list[str]] = []
    for snapshot in result.equity_curve:
        factor_names = sorted({name for values in snapshot.factor_data.values() for name in values})
        positions = {symbol: quantity for symbol, quantity in snapshot.positions.items() if quantity > 0}
        if not positions:
            continue
        for factor_name in factor_names:
            weighted_total = ZERO
            value_total = ZERO
            for symbol, quantity in positions.items():
                price = snapshot.prices.get(symbol)
                raw_factor_value = snapshot.factor_data.get(symbol, {}).get(factor_name)
                if price is None or price <= 0 or raw_factor_value is None:
                    continue
                market_value = quantity * price
                weighted_total += Decimal(str(raw_factor_value)) * market_value
                value_total += market_value
            if value_total > 0:
                rows.append([snapshot.timestamp.isoformat(), factor_name, _format_exposure_decimal(weighted_total / value_total)])
    if rows:
        _write_csv(path, ["timestamp", "factor", "exposure"], rows)


def _format_exposure_decimal(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001")), "f")


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


def _write_order_events(path: Path, result: SimulationResult) -> None:
    headers = [
        "event_id",
        "order_id",
        "timestamp",
        "event_type",
        "status",
        "filled_quantity",
        "remaining_quantity",
        "message",
    ]
    rows = [
        [
            record.event_id,
            record.order_id,
            record.timestamp.isoformat(),
            record.event_type,
            record.status,
            format_decimal(record.filled_quantity),
            format_decimal(record.remaining_quantity),
            record.message,
        ]
        for record in result.order_events
    ]
    _write_csv(path, headers, rows)


def _write_risk_events(path: Path, result: SimulationResult) -> None:
    headers = [
        "event_id",
        "timestamp",
        "order_id",
        "symbol",
        "rule",
        "action",
        "original_quantity",
        "adjusted_quantity",
        "message",
    ]
    rows = [
        [
            record.event_id,
            record.timestamp.isoformat(),
            record.order_id,
            record.symbol,
            record.rule,
            record.action,
            format_decimal(record.original_quantity),
            format_decimal(record.adjusted_quantity),
            record.message,
        ]
        for record in result.risk_events
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
    return list(result.input_files)


def _market_state(event: MarketPriceEvent) -> MarketState:
    return MarketState(
        timestamp=event.timestamp,
        symbol=event.symbol,
        price=event.price,
        prices=_event_prices(event),
        factor_data={symbol: dict(sorted(values.items())) for symbol, values in sorted(event.factor_data.items())},
    )


def _account_state(account: SimulatedAccount) -> AccountState:
    return AccountState(cash=account.cash, positions=dict(sorted(account.positions.items())))


def _event_prices(event: MarketPriceEvent) -> dict[str, Decimal]:
    if event.prices:
        return dict(sorted(event.prices.items()))
    return {event.symbol: event.price}


def _state_prices(market_state: MarketState) -> dict[str, Decimal]:
    if market_state.prices:
        return dict(sorted(market_state.prices.items()))
    return {market_state.symbol: market_state.price}


def _target_prices(prices: dict[str, Decimal], target_symbol: str | None) -> dict[str, Decimal]:
    if target_symbol is None:
        return dict(sorted(prices.items()))
    if target_symbol not in prices:
        return {}
    return {target_symbol: prices[target_symbol]}


def _portfolio_equity(cash: Decimal, positions: dict[str, Decimal], prices: dict[str, Decimal]) -> Decimal:
    return cash + sum((quantity * prices[symbol] for symbol, quantity in positions.items() if symbol in prices), ZERO)


def _normalize_target_weights(
    target_weights: TargetWeights | dict[str, WeightValue],
    prices: dict[str, Decimal],
) -> dict[str, Decimal]:
    raw_weights = target_weights.weights if isinstance(target_weights, TargetWeights) else target_weights
    weights: dict[str, Decimal] = {}
    for symbol, raw_weight in raw_weights.items():
        if symbol not in prices:
            raise SimulationExecutionError(f"Target weight references unknown symbol: {symbol}.")
        weight = _parse_weight(symbol, raw_weight)
        if weight < 0:
            raise SimulationExecutionError(f"Target weight for {symbol} must be nonnegative.")
        if weight != 0:
            weights[symbol] = weight
    total_weight = sum(weights.values(), ZERO)
    if total_weight > ONE_UNIT:
        raise SimulationExecutionError("Target weights must sum to 1.0 or less.")
    return dict(sorted(weights.items()))


def _parse_weight(symbol: str, value: WeightValue) -> Decimal:
    if isinstance(value, bool):
        raise SimulationExecutionError(f"Target weight for {symbol} must be numeric.")
    if isinstance(value, (int, float)):
        value = str(value)
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str):
        parsed = audit._parse_decimal(value)
        if parsed is None:
            raise SimulationExecutionError(f"Target weight for {symbol} must be finite.")
    else:
        raise SimulationExecutionError(f"Target weight for {symbol} must be numeric.")
    if not parsed.is_finite():
        raise SimulationExecutionError(f"Target weight for {symbol} must be finite.")
    return parsed


def _floor_decimal(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding=ROUND_FLOOR)


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


def _optional_str_param(params: dict[str, object], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SimulationConfigError(f"Strategy parameter {key} must be a non-empty string when provided.")
    return value.strip()


def _required_str_param(params: dict[str, object], key: str) -> str:
    value = _optional_str_param(params, key)
    if value is None:
        raise SimulationConfigError(f"Strategy parameter {key} is required.")
    return value


def _reject_unknown_params(params: dict[str, object], allowed_keys: set[str]) -> None:
    unknown = sorted(key for key in params if key not in allowed_keys)
    if unknown:
        raise SimulationConfigError(f"Unknown strategy parameter(s): {', '.join(unknown)}.")


def _parse_friction_config(value: object) -> FrictionModel:
    if value is None:
        return FrictionModel()
    if not isinstance(value, dict):
        raise SimulationConfigError("Config friction must be an object.")
    _reject_unknown_config_keys(value, {"fee_rate", "slippage_per_trade"}, "Config friction")
    fee_rate = _decimal_config_value(value, "fee_rate", ZERO, "Config friction.fee_rate")
    slippage_per_trade = _decimal_config_value(value, "slippage_per_trade", ZERO, "Config friction.slippage_per_trade")
    if fee_rate < 0:
        raise SimulationConfigError("Config friction.fee_rate must be nonnegative.")
    if slippage_per_trade < 0:
        raise SimulationConfigError("Config friction.slippage_per_trade must be nonnegative.")
    return FrictionModel(fee_rate=fee_rate, slippage_per_trade=slippage_per_trade)


def _parse_execution_config(value: object) -> ExecutionConfig:
    if value is None:
        return ExecutionConfig()
    if not isinstance(value, dict):
        raise SimulationConfigError("Config execution must be an object.")
    _reject_unknown_config_keys(value, {"max_fill_quantity", "partial_fill_policy"}, "Config execution")
    max_fill_quantity = _optional_decimal_config_value(value, "max_fill_quantity", "Config execution.max_fill_quantity")
    partial_fill_policy = _str_config_value(value, "partial_fill_policy", "cancel_remainder", "Config execution.partial_fill_policy")
    if max_fill_quantity is not None and max_fill_quantity <= 0:
        raise SimulationConfigError("Config execution.max_fill_quantity must be positive.")
    if partial_fill_policy not in {"cancel_remainder", "carry_forward"}:
        raise SimulationConfigError("Config execution.partial_fill_policy must be cancel_remainder or carry_forward.")
    return ExecutionConfig(max_fill_quantity=max_fill_quantity, partial_fill_policy=partial_fill_policy)


def _parse_risk_config(value: object) -> RiskConfig:
    if value is None:
        return RiskConfig()
    if not isinstance(value, dict):
        raise SimulationConfigError("Config risk must be an object.")
    _reject_unknown_config_keys(
        value,
        {"max_position_weight", "min_cash_buffer", "max_order_notional", "cooldown_periods", "max_drawdown_pct"},
        "Config risk",
    )
    max_position_weight = _optional_decimal_config_value(value, "max_position_weight", "Config risk.max_position_weight")
    min_cash_buffer = _decimal_config_value(value, "min_cash_buffer", ZERO, "Config risk.min_cash_buffer")
    max_order_notional = _optional_decimal_config_value(value, "max_order_notional", "Config risk.max_order_notional")
    cooldown_periods = _int_config_value(value, "cooldown_periods", 0, "Config risk.cooldown_periods")
    max_drawdown_pct = _optional_decimal_config_value(value, "max_drawdown_pct", "Config risk.max_drawdown_pct")
    if max_position_weight is not None and (max_position_weight <= 0 or max_position_weight > 1):
        raise SimulationConfigError("Config risk.max_position_weight must be greater than 0 and less than or equal to 1.")
    if min_cash_buffer < 0:
        raise SimulationConfigError("Config risk.min_cash_buffer must be nonnegative.")
    if max_order_notional is not None and max_order_notional <= 0:
        raise SimulationConfigError("Config risk.max_order_notional must be positive.")
    if cooldown_periods < 0:
        raise SimulationConfigError("Config risk.cooldown_periods must be nonnegative.")
    if max_drawdown_pct is not None and (max_drawdown_pct <= 0 or max_drawdown_pct > 100):
        raise SimulationConfigError("Config risk.max_drawdown_pct must be greater than 0 and less than or equal to 100.")
    return RiskConfig(
        max_position_weight=max_position_weight,
        min_cash_buffer=min_cash_buffer,
        max_order_notional=max_order_notional,
        cooldown_periods=cooldown_periods,
        max_drawdown_pct=max_drawdown_pct,
    )


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


def _optional_decimal_config_value(params: dict[str, object], key: str, label: str) -> Decimal | None:
    if key not in params or params[key] is None:
        return None
    return _decimal_config_value(params, key, ZERO, label)


def _int_config_value(params: dict[str, object], key: str, default: int, label: str) -> int:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SimulationConfigError(f"{label} must be an integer.")
    return value


def _str_config_value(params: dict[str, object], key: str, default: str, label: str) -> str:
    value = params.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise SimulationConfigError(f"{label} must be a non-empty string.")
    return value.strip()


def _reject_unknown_config_keys(params: dict[str, object], allowed_keys: set[str], label: str) -> None:
    unknown = sorted(str(key) for key in params if key not in allowed_keys)
    if unknown:
        raise SimulationConfigError(f"{label} has unknown key(s): {', '.join(unknown)}.")


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


def _strategy_preset_payload(strategy_name: str) -> dict[str, object]:
    preset = STRATEGY_PRESETS.get(_canonical_strategy_name(strategy_name))
    if preset is None:
        return {"name": strategy_name, "description": "Custom strategy object.", "params": []}
    return preset.to_payload()


def _canonical_strategy_name(strategy_name: str) -> str:
    return STRATEGY_ALIASES.get(strategy_name, strategy_name)


def _execution_assumption(result: SimulationResult) -> str:
    if result.execution.max_fill_quantity is None:
        return "immediate full fills"
    if result.execution.partial_fill_policy == "carry_forward":
        return "deterministic partial fills capped by max_fill_quantity with residual orders carried across market ticks"
    return "deterministic partial fills capped by max_fill_quantity"


def _int_param(params: dict[str, object], key: str, default: int) -> int:
    value = params.get(key, default)
    if not isinstance(value, int):
        raise SimulationConfigError(f"Strategy parameter {key} must be an integer.")
    return value


def _audit_status_for_context(dataset_path: Path) -> str:
    try:
        return input_audit.audit_input_dataset(dataset_path).status
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


def _format_decimal_mapping(values: dict[str, Decimal]) -> str:
    return json.dumps({key: format_decimal(value) for key, value in sorted(values.items())}, sort_keys=True, separators=(",", ":"))


def format_positions(positions: dict[str, Decimal]) -> str:
    if not positions:
        return "none"
    return ", ".join(f"{symbol}:{format_decimal(quantity)}" for symbol, quantity in sorted(positions.items()))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
