"""Render a fund cost comparison as terminal text."""

from __future__ import annotations

from .config import ComparisonConfig
from .costs import annual_drag
from .projection import Projection, contribution_sensitivity, project


def _money(value: float) -> str:
    return f"{value:,.0f}"


def render(cfg: ComparisonConfig) -> str:
    projections = sorted(
        (project(f, cfg.platform, cfg.plan) for f in cfg.funds),
        key=lambda p: -p.terminal_wealth)
    by_name = {f.name: f for f in cfg.funds}
    plan = cfg.plan

    lines: list[str] = []
    add = lines.append
    add("=" * 92)
    add("FUND COST CENSUS")
    add("=" * 92)
    add("")
    add(f"  Starting with {_money(plan.initial_amount)}, adding "
        f"{_money(plan.monthly_contribution)} a month, for "
        f"{plan.horizon_years:g} years.")
    add(f"  Assuming the index returns {plan.gross_return:.1%} a year gross, "
        f"with a {plan.dividend_yield:.2%} dividend yield.")
    add("")

    add("-" * 92)
    add("WHERE THE MONEY GOES  (annual, as a share of everything you hold)")
    add("")
    header = (f"{'fund':<30}{'fee':>9}{'tracking':>10}{'div tax':>10}"
              f"{'platform':>10}{'TOTAL':>10}")
    add(header)
    add("-" * len(header))
    for p in projections:
        d = annual_drag(by_name[p.fund_name], cfg.platform, plan.dividend_yield)
        add(f"{p.fund_name[:29]:<30}{d['ter']:>9.3%}"
            f"{d['tracking_difference']:>10.3%}{d['dividend_withholding']:>10.3%}"
            f"{d['platform_fee']:>10.3%}{p.annual_drag:>10.3%}")
    add("")
    add("  'div tax' is dividend withholding, set by where the fund is")
    add("  domiciled and your tax residency. It appears on no factsheet and")
    add("  is frequently larger than the fee everybody compares.")
    add("")

    add("-" * 92)
    add(f"WHAT THAT COSTS YOU OVER {plan.horizon_years:g} YEARS")
    add("")
    header = (f"{'fund':<30}{'net return':>12}{'final value':>14}"
              f"{'costs paid':>13}{'% lost':>9}")
    add(header)
    add("-" * len(header))
    for p in projections:
        add(f"{p.fund_name[:29]:<30}{p.net_annual_return:>12.3%}"
            f"{_money(p.terminal_wealth):>14}{_money(p.total_cost):>13}"
            f"{p.cost_share:>9.1%}")
    add("")
    if len(projections) >= 2:
        best, worst = projections[0], projections[-1]
        gap = best.terminal_wealth - worst.terminal_wealth
        add(f"  Choosing '{best.fund_name}' over '{worst.fund_name}'")
        add(f"  is worth {_money(gap)} over {plan.horizon_years:g} years.")
        add("  Same index, same market, same risk. Only the costs differ.")
        add("")

    add("-" * 92)
    add("THE LEVER THAT ACTUALLY MOVES THE NUMBER")
    add("")
    best_fund = by_name[projections[0].fund_name]
    sensitivity = contribution_sensitivity(best_fund, cfg.platform, plan)
    baseline = sensitivity["contribution x1"]
    for label, value in sensitivity.items():
        delta = value - baseline
        marker = "" if abs(delta) < 1 else f"   ({delta:+,.0f})"
        add(f"  {label:<24}{_money(value):>14}{marker}")
    add("")
    add("  Adding more is a decision you control. A higher return is a hope,")
    add("  and 2-4 extra percentage points a year is not a small hope: it is")
    add("  more than most professional managers deliver over a decade.")
    add("")

    add("=" * 92)
    add("BEFORE YOU ACT ON ANY OF THIS")
    add("")
    add("  The withholding rates are modelling defaults, not verified tax")
    add("  advice, and they depend on your tax residency rather than where")
    add("  you happen to live. They are also the largest single term above.")
    add("  Confirm them with a qualified adviser before choosing a fund.")
    add("")
    add("  Also worth asking about: US-domiciled holdings above roughly")
    add("  $60,000 can expose a non-US person to US estate tax at rates up")
    add("  to 40%. Funds domiciled elsewhere generally avoid it. That is not")
    add("  a cost this tool models, and it can exceed everything that it does.")
    add("=" * 92)
    return "\n".join(lines)
