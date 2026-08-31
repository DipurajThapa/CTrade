"""Typed configuration, loaded from YAML.

Two principles drive the defaults:

1. Every rate-limit number is a configurable guess, not a fact. Deriv publishes
   its limits and they change; the defaults here are deliberately conservative
   so a fourteen-day run does not get the app id throttled. Raise them only
   after checking the current published limits.
2. The decision thresholds are pre-registered. They live in the config file so
   they are written down, version-controlled and timestamped BEFORE the data is
   seen. Changing them after looking at results is the single easiest way to
   turn this census into an expensive way of confirming what you hoped.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

DEFAULT_ENDPOINT = "wss://ws.derivws.com/websockets/v3"


@dataclass
class ConnectionConfig:
    endpoint: str = DEFAULT_ENDPOINT
    app_id: str = "1089"
    #: Path to a CA bundle. Needed only behind a TLS-terminating proxy.
    ca_bundle: str | None = None
    open_timeout_s: float = 20.0
    request_timeout_s: float = 20.0
    ping_interval_s: float = 30.0
    #: Reconnect backoff, seconds. Capped, with jitter applied at use.
    backoff_initial_s: float = 2.0
    backoff_max_s: float = 120.0

    def url(self) -> str:
        return f"{self.endpoint}?app_id={self.app_id}"


@dataclass
class RateLimitConfig:
    #: Requests per minute across all message types. Conservative default.
    requests_per_minute: int = 60
    #: Simultaneously open proposal streams. Deriv caps concurrent streams.
    max_concurrent_proposals: int = 12
    #: Simultaneously open tick streams (one per symbol under measurement).
    max_concurrent_ticks: int = 8


@dataclass
class GridConfig:
    """Which (symbol, duration, contract type) cells to measure."""

    markets: list[str] = field(default_factory=lambda: ["forex"])
    #: Explicit allow-list. Empty means "every tradeable symbol in `markets`".
    symbols: list[str] = field(default_factory=list)
    #: Exclude synthetic and OTC instruments, whose statistics say nothing
    #: about a real-market strategy. Matched as case-insensitive substrings
    #: against the symbol code, display name and submarket.
    exclude_patterns: list[str] = field(
        default_factory=lambda: ["synthetic", "volatility", "boom", "crash",
                                 "jump", "step", "range break", "otc"])
    durations_seconds: list[int] = field(default_factory=lambda: [120, 180, 300])
    variants: list[str] = field(default_factory=lambda: ["strict", "equals"])
    #: Rise only. Deriv quotes Rise and Fall symmetrically, so sampling both
    #: doubles the request budget for almost no additional information. The
    #: preflight check verifies the symmetry holds before the run relies on it.
    directions: list[str] = field(default_factory=lambda: ["rise"])
    stake: float = 10.0
    currency: str = "USD"


@dataclass
class SamplingConfig:
    #: How long to hold one batch of proposal streams before rotating. Longer
    #: dwell measures payout drift in more detail; shorter dwell covers the
    #: grid more often. 90s is a compromise that yields both.
    dwell_seconds: float = 90.0
    #: Pause between rotations, to stay clear of burst limits.
    rotation_pause_seconds: float = 2.0
    #: Stop after this many days. The default is the pre-registered horizon.
    duration_days: float = 14.0
    #: Re-run instrument discovery this often, to pick up session opens/closes.
    rediscover_every_minutes: float = 60.0


@dataclass
class DecisionConfig:
    """Pre-registered decision rule. Set before the run; do not tune after.

    Thresholds are on REQUIRED EDGE -- the directional skill needed to break
    even, inclusive of the settlement-tie penalty -- not on the raw house
    margin. Margin alone understates the hurdle, materially so at short
    durations where ties are common.

    Calibration of the thresholds: a well-built model on short-horizon FX,
    measured out of sample after purged walk-forward and deflation for
    selection bias, plausibly delivers an information coefficient of 0.03-0.08,
    which is a directional edge of roughly 1.2-3.2pp. A hurdle at the top of
    that band is a coin flip on the model being world-class; a hurdle above it
    is unreachable.
    """

    go_max_required_edge: float = 0.015
    conditional_max_required_edge: float = 0.030
    #: Minimum observations before a cell's verdict is reported at all.
    min_proposals_per_cell: int = 200
    min_settlement_samples_per_cell: int = 500


@dataclass
class StorageConfig:
    root: str = "data"
    #: Flush the write buffer at least this often, so a crash loses seconds.
    flush_interval_s: float = 5.0


@dataclass
class CensusConfig:
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    grid: GridConfig = field(default_factory=GridConfig)
    sampling: SamplingConfig = field(default_factory=SamplingConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> None:
        g = self.grid
        if not g.durations_seconds:
            raise ValueError("grid.durations_seconds is empty")
        if any(d <= 0 for d in g.durations_seconds):
            raise ValueError("grid durations must be positive")
        bad = set(g.variants) - {"strict", "equals"}
        if bad:
            raise ValueError(f"unknown grid.variants: {sorted(bad)}")
        bad = set(g.directions) - {"rise", "fall"}
        if bad:
            raise ValueError(f"unknown grid.directions: {sorted(bad)}")
        if g.stake <= 0:
            raise ValueError("grid.stake must be positive")
        if self.rate_limit.requests_per_minute <= 0:
            raise ValueError("rate_limit.requests_per_minute must be positive")
        if self.rate_limit.max_concurrent_proposals <= 0:
            raise ValueError("rate_limit.max_concurrent_proposals must be positive")
        if self.sampling.dwell_seconds <= 0:
            raise ValueError("sampling.dwell_seconds must be positive")
        d = self.decision
        if not 0 < d.go_max_required_edge <= d.conditional_max_required_edge:
            raise ValueError(
                "decision thresholds must satisfy 0 < go <= conditional")


def _merge(base: Any, override: dict[str, Any]) -> None:
    for key, value in override.items():
        if not hasattr(base, key):
            raise ValueError(f"unknown config key: {key}")
        current = getattr(base, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge(current, value)
        else:
            setattr(base, key, value)


def load_config(path: str | Path | None = None) -> CensusConfig:
    """Load configuration, applying environment overrides.

    ``DERIV_APP_ID`` and ``DERIV_ENDPOINT`` override the file, so an app id can
    be supplied by environment rather than committed to source control.
    """
    cfg = CensusConfig()
    if path is not None:
        raw = yaml.safe_load(Path(path).read_text()) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: top level must be a mapping")
        _merge(cfg, raw)
    if os.environ.get("DERIV_APP_ID"):
        cfg.connection.app_id = os.environ["DERIV_APP_ID"]
    if os.environ.get("DERIV_ENDPOINT"):
        cfg.connection.endpoint = os.environ["DERIV_ENDPOINT"]
    if os.environ.get("DERIV_CA_BUNDLE"):
        cfg.connection.ca_bundle = os.environ["DERIV_CA_BUNDLE"]
    cfg.validate()
    return cfg
