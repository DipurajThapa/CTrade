"""Typed configuration for a fund cost comparison, loaded from YAML.

Every input is something the investor can look up on a factsheet or a broker's
fee schedule. Nothing here requires market data or a network connection, which
is deliberate: the comparison must be runnable from anywhere, by anyone, in
a minute.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .costs import FundCosts, PlatformCosts, WithholdingModel
from .projection import Plan


@dataclass
class ComparisonConfig:
    plan: Plan = field(default_factory=Plan)
    platform: PlatformCosts = field(default_factory=PlatformCosts)
    funds: list[FundCosts] = field(default_factory=list)

    def validate(self) -> None:
        if not self.funds:
            raise ValueError("no funds to compare")
        if self.plan.horizon_years <= 0:
            raise ValueError("plan.horizon_years must be positive")
        if self.plan.initial_amount < 0 or self.plan.monthly_contribution < 0:
            raise ValueError("amounts must not be negative")
        if self.plan.initial_amount <= 0 and self.plan.monthly_contribution <= 0:
            raise ValueError("nothing is being invested")
        if not -1.0 < self.plan.gross_return < 1.0:
            raise ValueError("plan.gross_return should be a fraction, e.g. 0.07")
        if not 0.0 <= self.plan.dividend_yield < 1.0:
            raise ValueError("plan.dividend_yield should be a fraction")
        names = [f.name for f in self.funds]
        if len(set(names)) != len(names):
            raise ValueError("fund names must be unique")
        for fund in self.funds:
            if fund.ter < 0 or fund.spread < 0:
                raise ValueError(f"{fund.name}: costs must not be negative")

    def to_dict(self) -> dict[str, Any]:
        return {"plan": asdict(self.plan),
                "platform": asdict(self.platform),
                "funds": [asdict(f) for f in self.funds]}


def _fund_from(raw: dict[str, Any]) -> FundCosts:
    withholding = None
    if "withholding" in raw and raw["withholding"] is not None:
        w = raw["withholding"]
        withholding = WithholdingModel(
            fund_level_rate=float(w["fund_level_rate"]),
            investor_level_rate=float(w["investor_level_rate"]),
            basis=w.get("basis", "supplied in config"))
    return FundCosts(
        name=raw["name"],
        ter=float(raw["ter"]),
        domicile=raw.get("domicile", "IE"),
        tracking_difference=float(raw.get("tracking_difference", 0.0)),
        spread=float(raw.get("spread", 0.0005)),
        withholding=withholding)


def load_config(path: str | Path) -> ComparisonConfig:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top level must be a mapping")

    known_plan = set(Plan.__dataclass_fields__)
    plan_raw = raw.get("plan") or {}
    unknown = set(plan_raw) - known_plan
    if unknown:
        raise ValueError(f"unknown plan keys: {sorted(unknown)}")

    known_platform = set(PlatformCosts.__dataclass_fields__)
    platform_raw = raw.get("platform") or {}
    unknown = set(platform_raw) - known_platform
    if unknown:
        raise ValueError(f"unknown platform keys: {sorted(unknown)}")

    cfg = ComparisonConfig(
        plan=Plan(**plan_raw),
        platform=PlatformCosts(**platform_raw),
        funds=[_fund_from(f) for f in raw.get("funds", []) or []])
    cfg.validate()
    return cfg
