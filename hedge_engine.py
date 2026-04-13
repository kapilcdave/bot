#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


@dataclass(frozen=True)
class HedgeBook:
    yes_shares: float
    yes_avg_price: float
    no_shares: float
    no_avg_price: float

    @property
    def yes_cost(self) -> float:
        return self.yes_shares * self.yes_avg_price

    @property
    def no_cost(self) -> float:
        return self.no_shares * self.no_avg_price

    @property
    def total_cost(self) -> float:
        return self.yes_cost + self.no_cost

    @property
    def pnl_if_yes(self) -> float:
        return self.yes_shares - self.total_cost

    @property
    def pnl_if_no(self) -> float:
        return self.no_shares - self.total_cost

    @property
    def dominant_side(self) -> str:
        return "yes" if self.yes_shares >= self.no_shares else "no"

    @property
    def weak_side(self) -> str:
        return "no" if self.dominant_side == "yes" else "yes"

    @property
    def dominant_shares(self) -> float:
        return self.yes_shares if self.dominant_side == "yes" else self.no_shares

    @property
    def weak_shares(self) -> float:
        return self.no_shares if self.dominant_side == "yes" else self.yes_shares

    @property
    def imbalance_shares(self) -> float:
        return max(0.0, self.dominant_shares - self.weak_shares)


@dataclass(frozen=True)
class HedgeInputs:
    current_yes_price: float
    current_no_price: float
    elapsed_seconds: float
    total_seconds: float
    volatility: float
    cumulative_volume_delta_flips: int
    short_term_momentum_flips: int
    orderbook_imbalance_flips: int
    pair_cost: float
    mid_price: float = 0.5
    flow_decay: float = 1.0


@dataclass(frozen=True)
class HedgeConfig:
    risk_aversion: float = 0.12
    time_pressure_power: float = 2.0
    discount_floor: float = 0.0
    discount_ceiling: float = 0.06
    max_flow_flips_for_full_score: int = 6
    urgency_threshold: float = 0.72
    cooldown_seconds: float = 6.0
    max_hedges_per_session: int = 4
    hedge_budget_dollars: float = 12.0
    max_single_hedge_cost_fraction: float = 0.35
    target_max_loss_fraction: float = 0.25
    min_hedge_shares: float = 1.0
    max_hedge_shares: float = 500.0
    urgency_weights: Mapping[str, float] = field(
        default_factory=lambda: {
            "inventory": 0.34,
            "time": 0.18,
            "volatility": 0.16,
            "flow": 0.18,
            "discount": 0.14,
        }
    )
    component_floors: Mapping[str, float] = field(
        default_factory=lambda: {
            "inventory": 0.15,
            "time": 0.05,
            "volatility": 0.05,
            "flow": 0.05,
            "discount": 0.05,
        }
    )


@dataclass
class HedgeState:
    hedges_fired: int = 0
    budget_spent: float = 0.0
    last_hedge_ts: float = -1e18


@dataclass(frozen=True)
class HedgeDecision:
    fire: bool
    hedge_side: str
    hedge_shares: float
    hedge_price: float
    hedge_cost: float
    urgency: float
    capped_by_budget: bool
    capped_by_profit: bool
    blocked_reason: str | None
    components: Dict[str, float]
    diagnostics: Dict[str, float]


class HedgeEngine:
    def __init__(self, config: HedgeConfig | None = None):
        self.config = config or HedgeConfig()

    def reservation_price_shift(self, book: HedgeBook, inputs: HedgeInputs) -> float:
        imbalance = book.imbalance_shares
        if imbalance <= 0:
            return 0.0
        horizon = clamp(1.0 - (inputs.elapsed_seconds / max(inputs.total_seconds, 1.0)), 0.0, 1.0)
        sigma2 = max(inputs.volatility, 0.0) ** 2
        return self.config.risk_aversion * sigma2 * horizon * imbalance

    def inventory_component(self, book: HedgeBook, inputs: HedgeInputs) -> float:
        total_shares = max(book.yes_shares + book.no_shares, 1.0)
        imbalance_ratio = book.imbalance_shares / total_shares
        reservation_shift = self.reservation_price_shift(book, inputs)
        return clamp(0.65 * imbalance_ratio + 0.35 * reservation_shift, 0.0, 1.0)

    def time_component(self, inputs: HedgeInputs) -> float:
        progress = clamp(inputs.elapsed_seconds / max(inputs.total_seconds, 1.0), 0.0, 1.0)
        return progress ** self.config.time_pressure_power

    def volatility_component(self, inputs: HedgeInputs) -> float:
        sigma2 = max(inputs.volatility, 0.0) ** 2
        return clamp(sigma2 / 0.04, 0.0, 1.0)

    def flow_component(self, inputs: HedgeInputs) -> float:
        flips = (
            max(inputs.cumulative_volume_delta_flips, 0)
            + max(inputs.short_term_momentum_flips, 0)
            + max(inputs.orderbook_imbalance_flips, 0)
        )
        normalized = flips / max(self.config.max_flow_flips_for_full_score, 1)
        return clamp(normalized * max(inputs.flow_decay, 0.0), 0.0, 1.0)

    def discount_component(self, inputs: HedgeInputs) -> float:
        # For Kalshi binaries, a two-sided lock gets more attractive as YES+NO drops below $1.
        discount = clamp(1.0 - inputs.pair_cost, 0.0, 1.0)
        span = max(self.config.discount_ceiling - self.config.discount_floor, 1e-9)
        normalized = (discount - self.config.discount_floor) / span
        return clamp(normalized, 0.0, 1.0)

    def urgency_components(self, book: HedgeBook, inputs: HedgeInputs) -> Dict[str, float]:
        return {
            "inventory": self.inventory_component(book, inputs),
            "time": self.time_component(inputs),
            "volatility": self.volatility_component(inputs),
            "flow": self.flow_component(inputs),
            "discount": self.discount_component(inputs),
        }

    def urgency_score(self, components: Mapping[str, float]) -> float:
        weighted = 0.0
        for name, weight in self.config.urgency_weights.items():
            weighted += weight * clamp(components.get(name, 0.0), 0.0, 1.0)
        return clamp(weighted, 0.0, 1.0)

    def _component_gate(self, components: Mapping[str, float]) -> bool:
        for name, floor in self.config.component_floors.items():
            if components.get(name, 0.0) < floor:
                return False
        return True

    def hedge_side_and_price(self, book: HedgeBook, inputs: HedgeInputs) -> tuple[str, float]:
        if book.dominant_side == "yes":
            return "no", inputs.current_no_price
        return "yes", inputs.current_yes_price

    def target_hedge_shares(self, book: HedgeBook, inputs: HedgeInputs) -> float:
        protected_loss = min(book.pnl_if_yes, book.pnl_if_no)
        open_profit = max(book.pnl_if_yes, book.pnl_if_no)
        price = inputs.current_no_price if book.dominant_side == "yes" else inputs.current_yes_price
        if price <= 0:
            return 0.0

        allowed_loss = max(0.0, open_profit * self.config.target_max_loss_fraction)
        deficit = max(0.0, allowed_loss - protected_loss)
        if deficit <= 0:
            return 0.0

        shares = deficit / max(1.0 - price, 1e-9)
        shares = clamp(shares, self.config.min_hedge_shares, self.config.max_hedge_shares)
        return shares

    def decide(self, book: HedgeBook, inputs: HedgeInputs, state: HedgeState, now_ts: float) -> HedgeDecision:
        components = self.urgency_components(book, inputs)
        urgency = self.urgency_score(components)
        hedge_side, hedge_price = self.hedge_side_and_price(book, inputs)

        if state.hedges_fired >= self.config.max_hedges_per_session:
            return HedgeDecision(
                fire=False,
                hedge_side=hedge_side,
                hedge_shares=0.0,
                hedge_price=hedge_price,
                hedge_cost=0.0,
                urgency=urgency,
                capped_by_budget=False,
                capped_by_profit=False,
                blocked_reason="session_cap",
                components=dict(components),
                diagnostics={"budget_remaining": self.config.hedge_budget_dollars - state.budget_spent},
            )

        if now_ts - state.last_hedge_ts < self.config.cooldown_seconds:
            return HedgeDecision(
                fire=False,
                hedge_side=hedge_side,
                hedge_shares=0.0,
                hedge_price=hedge_price,
                hedge_cost=0.0,
                urgency=urgency,
                capped_by_budget=False,
                capped_by_profit=False,
                blocked_reason="cooldown",
                components=dict(components),
                diagnostics={"budget_remaining": self.config.hedge_budget_dollars - state.budget_spent},
            )

        if urgency < self.config.urgency_threshold:
            return HedgeDecision(
                fire=False,
                hedge_side=hedge_side,
                hedge_shares=0.0,
                hedge_price=hedge_price,
                hedge_cost=0.0,
                urgency=urgency,
                capped_by_budget=False,
                capped_by_profit=False,
                blocked_reason="urgency_below_threshold",
                components=dict(components),
                diagnostics={"budget_remaining": self.config.hedge_budget_dollars - state.budget_spent},
            )

        if not self._component_gate(components):
            return HedgeDecision(
                fire=False,
                hedge_side=hedge_side,
                hedge_shares=0.0,
                hedge_price=hedge_price,
                hedge_cost=0.0,
                urgency=urgency,
                capped_by_budget=False,
                capped_by_profit=False,
                blocked_reason="component_gate",
                components=dict(components),
                diagnostics={"budget_remaining": self.config.hedge_budget_dollars - state.budget_spent},
            )

        target_shares = self.target_hedge_shares(book, inputs)
        if target_shares <= 0:
            return HedgeDecision(
                fire=False,
                hedge_side=hedge_side,
                hedge_shares=0.0,
                hedge_price=hedge_price,
                hedge_cost=0.0,
                urgency=urgency,
                capped_by_budget=False,
                capped_by_profit=False,
                blocked_reason="no_deficit_to_hedge",
                components=dict(components),
                diagnostics={"budget_remaining": self.config.hedge_budget_dollars - state.budget_spent},
            )

        open_profit = max(book.pnl_if_yes, book.pnl_if_no)
        budget_remaining = max(0.0, self.config.hedge_budget_dollars - state.budget_spent)
        max_cost_from_profit = max(0.0, open_profit * self.config.max_single_hedge_cost_fraction)
        allowed_cost = min(budget_remaining, max_cost_from_profit)
        uncapped_cost = target_shares * hedge_price
        capped_by_budget = budget_remaining < uncapped_cost
        capped_by_profit = max_cost_from_profit < uncapped_cost

        if hedge_price <= 0 or allowed_cost <= 0:
            return HedgeDecision(
                fire=False,
                hedge_side=hedge_side,
                hedge_shares=0.0,
                hedge_price=hedge_price,
                hedge_cost=0.0,
                urgency=urgency,
                capped_by_budget=capped_by_budget,
                capped_by_profit=capped_by_profit,
                blocked_reason="budget_exhausted",
                components=dict(components),
                diagnostics={"budget_remaining": budget_remaining, "target_shares": target_shares},
            )

        hedge_shares = min(target_shares, allowed_cost / hedge_price)
        hedge_shares = clamp(hedge_shares, 0.0, self.config.max_hedge_shares)
        hedge_cost = hedge_shares * hedge_price

        if hedge_shares < self.config.min_hedge_shares:
            return HedgeDecision(
                fire=False,
                hedge_side=hedge_side,
                hedge_shares=0.0,
                hedge_price=hedge_price,
                hedge_cost=0.0,
                urgency=urgency,
                capped_by_budget=capped_by_budget,
                capped_by_profit=capped_by_profit,
                blocked_reason="below_min_size",
                components=dict(components),
                diagnostics={"budget_remaining": budget_remaining, "target_shares": target_shares},
            )

        return HedgeDecision(
            fire=True,
            hedge_side=hedge_side,
            hedge_shares=hedge_shares,
            hedge_price=hedge_price,
            hedge_cost=hedge_cost,
            urgency=urgency,
            capped_by_budget=capped_by_budget,
            capped_by_profit=capped_by_profit,
            blocked_reason=None,
            components=dict(components),
            diagnostics={
                "budget_remaining": budget_remaining,
                "target_shares": target_shares,
                "reservation_shift": self.reservation_price_shift(book, inputs),
                "pnl_if_yes": book.pnl_if_yes,
                "pnl_if_no": book.pnl_if_no,
            },
        )

    def apply_decision(self, state: HedgeState, decision: HedgeDecision, now_ts: float) -> None:
        if not decision.fire:
            return
        state.hedges_fired += 1
        state.budget_spent += decision.hedge_cost
        state.last_hedge_ts = now_ts
