"""What the costs do to a portfolio over a holding period.

A cost of half a percent a year sounds negligible and is not. It is levied on
the entire balance every year, including on the growth that earlier years'
costs already reduced, so its effect compounds against the investor for as
long as the money is held.

This module also makes the other point that matters at small balances: the
contribution rate dominates the return rate. Doubling an 8% return to 16% is
a fantasy; doubling a monthly contribution is a decision. At a small starting
balance the second is worth far more than the first, and the projection shows
both so the comparison is not rhetorical.
"""

from __future__ import annotations

from dataclasses import dataclass

from .costs import (FundCosts, PlatformCosts, entry_cost_rate,
                    net_annual_return, total_annual_drag)


@dataclass(frozen=True)
class Plan:
    """The investor's side of the arithmetic."""

    initial_amount: float = 1000.0
    monthly_contribution: float = 0.0
    horizon_years: float = 20.0
    #: Index return before any costs. A long-run global equity assumption of
    #: 7-8% nominal is conventional; it is an assumption, not a forecast, and
    #: the comparison between funds is far less sensitive to it than the
    #: absolute numbers are.
    gross_return: float = 0.07
    #: Gross dividend yield of the index, which is what withholding bites on.
    dividend_yield: float = 0.018

    def months(self) -> int:
        return max(0, int(round(self.horizon_years * 12)))


@dataclass(frozen=True)
class Projection:
    fund_name: str
    annual_drag: float
    net_annual_return: float
    entry_cost_rate: float
    terminal_wealth: float
    total_contributed: float
    #: Terminal wealth had every cost been zero.
    frictionless_terminal: float

    @property
    def total_cost(self) -> float:
        """Money the costs removed, measured at the horizon.

        Reported at the horizon rather than as a running total because that is
        the honest figure: a fee paid in year one also removes every year of
        growth it would have earned.
        """
        return self.frictionless_terminal - self.terminal_wealth

    @property
    def cost_share(self) -> float:
        if self.frictionless_terminal <= 0:
            return 0.0
        return self.total_cost / self.frictionless_terminal

    @property
    def gain(self) -> float:
        return self.terminal_wealth - self.total_contributed


def _accumulate(plan: Plan, monthly_return: float,
                entry_cost: float) -> tuple[float, float]:
    """Run the balance forward monthly. Returns (terminal, contributed)."""
    balance = plan.initial_amount * (1.0 - entry_cost)
    contributed = plan.initial_amount
    for _ in range(plan.months()):
        balance *= (1.0 + monthly_return)
        if plan.monthly_contribution > 0:
            balance += plan.monthly_contribution * (1.0 - entry_cost)
            contributed += plan.monthly_contribution
    return balance, contributed


def project(fund: FundCosts, platform: PlatformCosts, plan: Plan) -> Projection:
    """Project one fund forward under the plan."""
    drag = total_annual_drag(fund, platform, plan.dividend_yield)
    net = net_annual_return(plan.gross_return, drag)
    monthly_net = (1.0 + net) ** (1.0 / 12.0) - 1.0

    purchase = plan.monthly_contribution or plan.initial_amount
    entry = entry_cost_rate(fund, platform, purchase)

    terminal, contributed = _accumulate(plan, monthly_net, entry)

    monthly_gross = (1.0 + plan.gross_return) ** (1.0 / 12.0) - 1.0
    frictionless, _ = _accumulate(plan, monthly_gross, 0.0)

    return Projection(
        fund_name=fund.name,
        annual_drag=drag,
        net_annual_return=net,
        entry_cost_rate=entry,
        terminal_wealth=terminal,
        total_contributed=contributed,
        frictionless_terminal=frictionless,
    )


def contribution_sensitivity(fund: FundCosts, platform: PlatformCosts,
                             plan: Plan,
                             multipliers: tuple[float, ...] = (1.0, 1.5, 2.0),
                             return_deltas: tuple[float, ...] = (0.0, 0.02, 0.04),
                             ) -> dict[str, float]:
    """Compare adding more money against earning a higher return.

    Two levers, priced side by side. One of them is a decision the investor
    controls; the other is a hope. At small balances the controllable one is
    usually worth several times the hope, and seeing that priced tends to
    settle the question faster than being told it.
    """
    out: dict[str, float] = {}
    for multiplier in multipliers:
        scaled = Plan(
            initial_amount=plan.initial_amount,
            monthly_contribution=plan.monthly_contribution * multiplier,
            horizon_years=plan.horizon_years,
            gross_return=plan.gross_return,
            dividend_yield=plan.dividend_yield)
        out[f"contribution x{multiplier:g}"] = project(
            fund, platform, scaled).terminal_wealth
    for delta in return_deltas:
        shifted = Plan(
            initial_amount=plan.initial_amount,
            monthly_contribution=plan.monthly_contribution,
            horizon_years=plan.horizon_years,
            gross_return=plan.gross_return + delta,
            dividend_yield=plan.dividend_yield)
        out[f"return +{delta:.0%}"] = project(
            fund, platform, shifted).terminal_wealth
    return out


def break_even_years(cheap: Projection, expensive: Projection,
                     switching_cost: float, plan: Plan) -> float | None:
    """How long a cheaper fund takes to repay the cost of switching to it.

    Returns ``None`` when the cheaper fund never repays the switch within the
    horizon, which is the answer whenever the annual saving is small and the
    one-off cost is not.
    """
    saving = expensive.annual_drag - cheap.annual_drag
    if saving <= 0 or switching_cost <= 0:
        return 0.0 if saving > 0 else None
    balance = plan.initial_amount
    if balance <= 0:
        return None
    years = switching_cost / (saving * balance)
    return years if years <= plan.horizon_years else None
