#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Iterable, Sequence


def fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def fmt_signed_money(value: float) -> str:
    return f"{value:+,.2f}"


def fmt_percent(value: float) -> str:
    return f"{value:.1f}%"


def safe_div(numerator: float, denominator: float) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


@dataclass(frozen=True)
class SidePosition:
    side: str
    shares: float
    avg_price: float

    @property
    def cost(self) -> float:
        return self.shares * self.avg_price

    @property
    def payout(self) -> float:
        return self.shares

    @property
    def market_value(self) -> float:
        return self.shares * self.avg_price


@dataclass(frozen=True)
class PendingOrder:
    label: str
    shares: float
    side: str
    price: float

    def render(self) -> str:
        return f"{self.label}: {self.shares:.1f} {self.side.upper()} @ {self.price:.2f}"


@dataclass(frozen=True)
class OracleState:
    pulse: float
    rolling_range_points: int
    rolling_span_minutes: float
    rhr_guard_delta: float
    rhr_multiplier: float
    rhr_blocked: int
    obi: float
    obi_threshold: float
    ptd_count: int
    ptd_ticks: int
    flip_doom: str
    oracle_price: float
    pts_vs_reference: float
    reference_price: float
    leader: str
    leader_position_yes: float
    leader_position_no: float


@dataclass(frozen=True)
class BinanceEngineState:
    stream_ok: bool
    age_ms: int
    buffer: int
    target_buffer: int
    buffer_ratio: int
    natural_delta: float
    current_delta: float
    stretch: float
    calibration: str
    last_action: str


@dataclass(frozen=True)
class StrategyState:
    strategy_name: str
    guard_state: str
    exec_delta: float
    elapsed: str
    remaining: str
    progress: float
    current_yes: float
    current_no: float
    entry_target: str
    hedge_target: str
    parity_mode: str | None = None
    hard_lock_active: bool = False
    trading_halted: bool = False


@dataclass(frozen=True)
class SessionStats:
    session_theo_pnl: float
    session_fee_adj_pnl: float
    entered: int
    wins: int
    locks: int
    losses: int
    win_rate: float
    all_time_theo: float
    all_time_theo_entered: int
    all_time_win_rate: float
    all_time_true: float
    all_time_true_resolved: int
    pending: int
    wallet: float
    redemptions: int
    true_losses: int


@dataclass(frozen=True)
class Snapshot:
    market: str
    tick: int
    leader: str
    total_shares: float
    yes: SidePosition
    no: SidePosition
    pending: Sequence[PendingOrder]
    oracle: OracleState
    binance: BinanceEngineState
    strategy: StrategyState
    stats: SessionStats

    @property
    def total_cost(self) -> float:
        return self.yes.cost + self.no.cost

    @property
    def profit_if_yes_wins(self) -> float:
        return self.yes.payout - self.total_cost

    @property
    def profit_if_no_wins(self) -> float:
        return self.no.payout - self.total_cost

    @property
    def pair_shares(self) -> float:
        return min(self.yes.shares, self.no.shares)

    @property
    def unmatched_yes(self) -> float:
        return max(0.0, self.yes.shares - self.no.shares)

    @property
    def unmatched_no(self) -> float:
        return max(0.0, self.no.shares - self.yes.shares)

    @property
    def yes_break_even_shares(self) -> float:
        if self.strategy.current_yes <= 0:
            return 0.0
        return max(0.0, -self.profit_if_yes_wins / self.strategy.current_yes)

    @property
    def no_break_even_shares(self) -> float:
        if self.strategy.current_no <= 0:
            return 0.0
        return max(0.0, -self.profit_if_no_wins / self.strategy.current_no)

    def render(self) -> str:
        lines: list[str] = []
        lines.append("=" * 78)
        lines.append(f"Market: {self.market}")
        lines.append(
            f"Strategy: {self.strategy.strategy_name} | GUARD: {self.strategy.guard_state} | "
            f"EXEC Δ: {self.strategy.exec_delta:.1f}"
        )
        lines.append("")
        lines.append(
            f"TIME: {self.strategy.elapsed} elapsed | {self.strategy.remaining} remaining | "
            f"{fmt_percent(self.strategy.progress * 100)} complete"
        )
        lines.append("=" * 78)
        lines.append("POSITIONS:")
        lines.append(
            f"  YES: {self.yes.shares:.1f} shares @ {self.yes.avg_price:.2f} avg = {fmt_money(self.yes.cost)} cost"
        )
        lines.append(
            f"  NO:  {self.no.shares:.1f} shares @ {self.no.avg_price:.2f} avg = {fmt_money(self.no.cost)} cost"
        )
        lines.append("PENDING:")
        for order in self.pending:
            lines.append(f"  {order.render()}")
        lines.append("")
        lines.append("MARKET (Oracle):")
        lines.append(
            f"  Pulse: {fmt_signed_money(self.oracle.pulse)} Rolling Range "
            f"({'UP' if self.oracle.pulse >= 0 else 'DOWN'} {self.oracle.rolling_range_points} pts, "
            f"{self.oracle.rolling_span_minutes:.1f}m span)"
        )
        lines.append(
            f"  RHR Guard: {fmt_signed_money(self.oracle.rhr_guard_delta)} required "
            f"({self.oracle.rhr_multiplier:.2f}x RHR) | {self.oracle.rhr_blocked} blocked"
        )
        lines.append(
            f"  OBI Guard: {self.oracle.obi:+.2f} (threshold: {self.oracle.obi_threshold:+.2f})"
        )
        lines.append(f"  PTD Doom: CLEAR ({self.oracle.ptd_count}/{self.oracle.ptd_ticks} ticks stable)")
        lines.append(f"  Flip Doom: {self.oracle.flip_doom}")
        lines.append(
            f"  Oracle Price: {fmt_money(self.oracle.oracle_price)} "
            f"({fmt_signed_money(self.oracle.pts_vs_reference)} vs ref {fmt_money(self.oracle.reference_price)})"
        )
        lines.append(
            f"  Leader: {self.oracle.leader} | Position: "
            f"{self.oracle.leader_position_yes:.0f}% YES / {self.oracle.leader_position_no:.0f}% NO"
        )
        lines.append("")
        lines.append("BINANCE ENGINE:")
        lines.append(
            f"  Stream: {'OK' if self.binance.stream_ok else 'DOWN'} (age: {self.binance.age_ms}ms) | "
            f"Buffer: {self.binance.buffer}/{self.binance.target_buffer} ({self.binance.buffer_ratio}%)"
        )
        lines.append(
            f"  Delta: {fmt_signed_money(self.binance.natural_delta)} natural | "
            f"{fmt_signed_money(self.binance.current_delta)} current | "
            f"Stretch: {fmt_signed_money(self.binance.stretch)}"
        )
        lines.append(
            f"  Calibration: {self.binance.calibration} | Last Action: {self.binance.last_action}"
        )
        lines.append("")
        lines.append(
            f"Current Price: YES {self.strategy.current_yes:.2f} | NO {self.strategy.current_no:.2f}"
        )
        lines.append(f"Entry Target: {self.strategy.entry_target}")
        lines.append(f"Hedge Target: {self.strategy.hedge_target}")
        if self.strategy.parity_mode:
            lines.append(f"Parity Mode: {self.strategy.parity_mode}")
        lines.append("")
        lock_state = "HARD LOCK ACTIVE" if self.strategy.hard_lock_active else "UNLOCKED"
        lines.append(f"POTENTIAL PROFIT: [{lock_state}]")
        lines.append(f"  If YES wins: {fmt_signed_money(self.profit_if_yes_wins)} ({self.yes.shares:.1f} shares)")
        lines.append(f"  If NO wins:  {fmt_signed_money(self.profit_if_no_wins)} ({self.no.shares:.1f} shares)")
        lines.append("BREAKEVEN DEFICIT:")
        lines.append(
            f"  YES needs: {self.yes_break_even_shares:.1f} shares to breakeven @ {self.strategy.current_yes:.2f}"
        )
        lines.append(
            f"  NO needs:  {self.no_break_even_shares:.1f} shares to breakeven @ {self.strategy.current_no:.2f}"
        )
        if self.strategy.trading_halted:
            lines.append("  HARD PROFIT LOCK (Trading Halted)")
        lines.append("")
        lines.append("STATS:")
        lines.append(
            f"  Session P&L: {fmt_signed_money(self.stats.session_theo_pnl)} (theo) | "
            f"{fmt_signed_money(self.stats.session_fee_adj_pnl)} (fee-adj) | {self.stats.entered} entered"
        )
        lines.append(
            f"  Wins: {self.stats.wins} | Locks: {self.stats.locks} | Losses: {self.stats.losses} | "
            f"Win Rate: {fmt_percent(self.stats.win_rate)}"
        )
        lines.append(
            f"  All-Time (Theo): {fmt_signed_money(self.stats.all_time_theo)} "
            f"({self.stats.all_time_theo_entered} entered) | Win Rate: {fmt_percent(self.stats.all_time_win_rate)}"
        )
        lines.append(
            f"  All-Time (True): {fmt_signed_money(self.stats.all_time_true)} "
            f"({self.stats.all_time_true_resolved} resolved) | Pending: {self.stats.pending}"
        )
        lines.append(
            f"  Wallet: {fmt_money(self.stats.wallet)} | Redemptions: {self.stats.redemptions} | "
            f"True Losses: {self.stats.true_losses}"
        )
        lines.append("")
        lines.append(
            f"Tick {self.tick} | Y:{self.strategy.current_yes:.2f} N:{self.strategy.current_no:.2f} | "
            f"Leader: {self.leader} | Shares: {self.total_shares:.1f}"
        )
        lines.append("=" * 78)
        return "\n".join(lines)


def sample_btc_snapshot() -> Snapshot:
    return Snapshot(
        market="Bitcoin Up or Down - January 18, 4:30AM-4:45AM ET",
        tick=45,
        leader="YES",
        total_shares=200.6,
        yes=SidePosition(side="yes", shares=103.2, avg_price=0.67),
        no=SidePosition(side="no", shares=97.4, avg_price=0.28),
        pending=[
            PendingOrder(label="GTC (Standard)", shares=12.8, side="yes", price=0.67),
            PendingOrder(label="GTC (Standard)", shares=85.7, side="no", price=0.27),
            PendingOrder(label="GTC (Batching)", shares=3.7, side="no", price=0.30),
        ],
        oracle=OracleState(
            pulse=60.09,
            rolling_range_points=132,
            rolling_span_minutes=10.0,
            rhr_guard_delta=27.04,
            rhr_multiplier=0.45,
            rhr_blocked=15,
            obi=-0.06,
            obi_threshold=-0.30,
            ptd_count=39,
            ptd_ticks=39,
            flip_doom="OK",
            oracle_price=95202.43,
            pts_vs_reference=57.05,
            reference_price=95145.38,
            leader="YES",
            leader_position_yes=51,
            leader_position_no=49,
        ),
        binance=BinanceEngineState(
            stream_ok=True,
            age_ms=0,
            buffer=450,
            target_buffer=300,
            buffer_ratio=150,
            natural_delta=44.79,
            current_delta=50.58,
            stretch=5.78,
            calibration="LOCKED",
            last_action="NORMAL",
        ),
        strategy=StrategyState(
            strategy_name="Incremental Pair",
            guard_state="ESCAPED",
            exec_delta=39.0,
            elapsed="03:45",
            remaining="11:14",
            progress=0.25,
            current_yes=0.67,
            current_no=0.32,
            entry_target="Buy YES @ 0.67 (FAK)",
            hedge_target="Bid NO @ 0.27 (GTC)",
            hard_lock_active=True,
            trading_halted=True,
        ),
        stats=SessionStats(
            session_theo_pnl=3.80,
            session_fee_adj_pnl=3.80,
            entered=2,
            wins=0,
            locks=2,
            losses=0,
            win_rate=100.0,
            all_time_theo=79.79,
            all_time_theo_entered=94,
            all_time_win_rate=86.2,
            all_time_true=102.25,
            all_time_true_resolved=98,
            pending=1,
            wallet=1258.50,
            redemptions=1,
            true_losses=0,
        ),
    )


def sample_xrp_snapshot() -> Snapshot:
    return Snapshot(
        market="XRP Up or Down - January 17, 4:15PM-4:30PM ET",
        tick=177,
        leader="YES",
        total_shares=238.3,
        yes=SidePosition(side="yes", shares=119.4, avg_price=0.73),
        no=SidePosition(side="no", shares=119.0, avg_price=0.24),
        pending=[],
        oracle=OracleState(
            pulse=0.0,
            rolling_range_points=142,
            rolling_span_minutes=10.0,
            rhr_guard_delta=0.0,
            rhr_multiplier=0.45,
            rhr_blocked=129,
            obi=-0.04,
            obi_threshold=-0.30,
            ptd_count=117,
            ptd_ticks=117,
            flip_doom="OK",
            oracle_price=2.07,
            pts_vs_reference=0.0,
            reference_price=2.07,
            leader="YES",
            leader_position_yes=50,
            leader_position_no=50,
        ),
        binance=BinanceEngineState(
            stream_ok=True,
            age_ms=0,
            buffer=450,
            target_buffer=300,
            buffer_ratio=150,
            natural_delta=0.0,
            current_delta=0.0,
            stretch=0.0,
            calibration="LOCKED",
            last_action="NORMAL",
        ),
        strategy=StrategyState(
            strategy_name="Incremental Pair",
            guard_state="ESCAPED",
            exec_delta=60.5,
            elapsed="13:19",
            remaining="01:40",
            progress=0.887,
            current_yes=0.84,
            current_no=0.05,
            entry_target="Buy YES @ 0.84 (FAK)",
            hedge_target="Bid NO @ 0.10 (GTC)",
            parity_mode="ACTIVATED (Verified - Seeking breakeven via GTCs)",
            hard_lock_active=True,
            trading_halted=True,
        ),
        stats=SessionStats(
            session_theo_pnl=0.0,
            session_fee_adj_pnl=0.0,
            entered=0,
            wins=0,
            locks=0,
            losses=0,
            win_rate=0.0,
            all_time_theo=5.51,
            all_time_theo_entered=4,
            all_time_win_rate=100.0,
            all_time_true=5.51,
            all_time_true_resolved=9,
            pending=1,
            wallet=1003.09,
            redemptions=0,
            true_losses=0,
        ),
    )


def iter_samples(names: Iterable[str]) -> list[Snapshot]:
    lookup = {
        "btc": sample_btc_snapshot(),
        "xrp": sample_xrp_snapshot(),
    }
    return [lookup[name] for name in names]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reverse-engineered state scaffold for the screenshot-based incremental pair bot."
    )
    parser.add_argument(
        "--sample",
        choices=["btc", "xrp", "both"],
        default="both",
        help="Render one of the built-in snapshots inferred from the screenshots.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    names = ["btc", "xrp"] if args.sample == "both" else [args.sample]
    for index, snapshot in enumerate(iter_samples(names)):
        if index:
            print()
        print(snapshot.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
