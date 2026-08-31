"""Economics of a fixed-payout binary contract.

Every quantity the census reports is defined here, with its derivation. Nothing
in this module touches the network, the clock or the filesystem: it is pure
arithmetic over measured inputs, so it can be tested exhaustively.

Notation
--------
``s``       stake paid to open the contract
``P``       gross payout returned on a win (stake included)
``b``       payout fraction, the net profit per unit staked on a win
``p_be``    break-even win probability
``m``       house margin expressed in probability units
``p_tie``   probability the exit tick exactly equals the entry tick
``e``       directional edge: P(correct | outcome is not a tie) = 0.5 + e

The contract variants
---------------------
Deriv quotes two families of Rise/Fall contract that differ only in how an
exact tie settles:

``CALL`` / ``PUT``      strict.  A Rise needs exit > entry.  A tie LOSES.
``CALLE`` / ``PUTE``    "equals". A Rise needs exit >= entry. A tie WINS.

The equals variant is quoted at a lower payout. Which is better is an empirical
question, because it trades payout against the measured tie rate, and the tie
rate at 1-5 minute horizons is not negligible. Resolving that trade is one of
the two things this census exists to do.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Sequence

from scipy.stats import norm

Variant = Literal["strict", "equals"]

__all__ = [
    "payout_fraction",
    "breakeven_probability",
    "house_margin",
    "required_edge",
    "required_information_coefficient",
    "expected_value",
    "kelly_fraction",
    "wilson_interval",
    "bootstrap_median_interval",
    "ContractEconomics",
    "evaluate",
]


# ---------------------------------------------------------------------------
# Payout algebra
# ---------------------------------------------------------------------------


def payout_fraction(stake: float, payout: float) -> float:
    """Net profit per unit staked on a win.

    Deriv's ``proposal`` response quotes ``payout`` gross of the stake, so a
    $10 stake returning $19.50 is a payout fraction of 0.95, not 1.95.
    """
    if stake <= 0:
        raise ValueError(f"stake must be positive, got {stake}")
    if payout < 0:
        raise ValueError(f"payout must be non-negative, got {payout}")
    return (payout - stake) / stake


def breakeven_probability(b: float) -> float:
    """Win probability at which a fixed-payout binary has zero expectancy.

    Staking 1 unit returns ``b`` on a win and costs 1 on a loss::

        E = w*b - (1 - w) = 0   =>   w = 1 / (1 + b)

    This is the single most important number in fixed-payout trading, and it is
    strictly above 0.5 for any ``b < 1``. A coin flip is a losing trade.
    """
    if b <= -1:
        raise ValueError(f"payout fraction must exceed -1, got {b}")
    return 1.0 / (1.0 + b)


def house_margin(b: float) -> float:
    """Break-even probability in excess of a fair coin: ``p_be - 0.5``.

    For an at-the-money Rise/Fall on a driftless underlying the true
    probability is approximately 0.5, so this is exactly the edge the trader
    must manufacture merely to break even. It is the toll, in probability
    units, and it is the headline diagnostic of this census.
    """
    return breakeven_probability(b) - 0.5


# ---------------------------------------------------------------------------
# Required edge, including the settlement-tie term
# ---------------------------------------------------------------------------


def required_edge(b: float, p_tie: float, variant: Variant = "strict") -> float:
    """Smallest directional edge ``e`` that makes the contract break even.

    ``e`` is defined on the non-tie outcomes: conditional on a signal,
    ``P(direction correct | not a tie) = 0.5 + e``. Defining it this way keeps
    the model's skill separate from the venue's tie convention, so the same
    model can be evaluated against both contract variants.

    Strict variant (a tie loses)::

        P(win) = (1 - p_tie) * (0.5 + e)
        set equal to p_be:   e = p_be / (1 - p_tie) - 0.5

    Equals variant (a tie wins)::

        P(win) = (1 - p_tie) * (0.5 + e) + p_tie
        set equal to p_be:   e = (p_be - p_tie) / (1 - p_tie) - 0.5

    The strict form shows why ties matter so much: dividing by ``1 - p_tie``
    inflates the requirement multiplicatively. At b = 0.95 the raw margin is
    1.28pp, but a 2.8% tie rate raises the required edge to 2.76pp -- it more
    than doubles. A census that measured payout alone would miss this entirely.

    Returns a value that may exceed 0.5, which signals that the contract cannot
    be won at any skill level: even a perfect directional forecaster loses.
    """
    if not 0.0 <= p_tie <= 1.0:
        raise ValueError(f"p_tie must lie in [0, 1], got {p_tie}")
    if variant not in ("strict", "equals"):
        raise ValueError(f"unknown variant {variant!r}")
    p_be = breakeven_probability(b)
    if p_tie == 1.0:
        # Every settlement is a tie: a frozen feed. Strict contracts can never
        # win, equals contracts always do. Both are degenerate rather than
        # tradeable, and callers surface them as a data fault.
        return math.inf if variant == "strict" else -math.inf
    if variant == "strict":
        return p_be / (1.0 - p_tie) - 0.5
    return (p_be - p_tie) / (1.0 - p_tie) - 0.5


def required_information_coefficient(
    b: float, p_tie: float, variant: Variant = "strict"
) -> float:
    """Required edge restated as a normal-quantile information coefficient.

    Modelling the horizon return as ``N(mu, sigma^2 T)`` and the signal as a
    forecast of ``mu``, the probability of calling direction correctly is
    ``Phi(IC)`` where ``IC = mu / (sigma * sqrt(T))``. Inverting::

        IC_required = Phi^-1(0.5 + e_required)

    IC is the unit quantitative researchers actually work in, so this is the
    number to compare against what a model can realistically deliver. For
    short-horizon FX, an out-of-sample IC of 0.03-0.08 is a good result and
    0.10+ is more often a symptom of leakage than of skill.

    Returns ``+inf`` when the required edge is unattainable (>= 0.5).
    """
    e = required_edge(b, p_tie, variant)
    if e == math.inf:
        return math.inf
    if e == -math.inf:
        return -math.inf
    p = 0.5 + e
    if p >= 1.0:
        return math.inf
    if p <= 0.0:
        return -math.inf
    return float(norm.ppf(p))


# ---------------------------------------------------------------------------
# Expectancy and sizing
# ---------------------------------------------------------------------------


def win_probability(e: float, p_tie: float, variant: Variant = "strict") -> float:
    """Probability the contract settles in the money, given edge ``e``."""
    if not 0.0 <= p_tie <= 1.0:
        raise ValueError(f"p_tie must lie in [0, 1], got {p_tie}")
    directional = (1.0 - p_tie) * (0.5 + e)
    if variant == "strict":
        p = directional
    elif variant == "equals":
        p = directional + p_tie
    else:
        raise ValueError(f"unknown variant {variant!r}")
    return min(max(p, 0.0), 1.0)


def expected_value(
    b: float, e: float, p_tie: float, variant: Variant = "strict"
) -> float:
    """Expectancy per unit staked.

    ``EV = P(win) * b - (1 - P(win)) = P(win) * (1 + b) - 1``

    A positive value is necessary but not sufficient to trade: the live system
    must additionally clear an edge buffer covering calibration error and the
    payout drift between quote capture and fill.
    """
    p = win_probability(e, p_tie, variant)
    return p * (1.0 + b) - 1.0


def kelly_fraction(
    b: float, e: float, p_tie: float, variant: Variant = "strict"
) -> float:
    """Growth-optimal stake fraction, floored at zero.

    ``f* = (b*p - (1-p)) / b``, the standard Kelly criterion for a binary
    payoff. Reported for reference only. Full Kelly is not investable here:
    establishing an edge of this size takes thousands of trades, which leaves
    the win-rate estimate with roughly a 40% relative standard error, and the
    Kelly optimum then sits well inside its own error bar. Live sizing should
    use a quarter of this at most.
    """
    if b <= 0:
        return 0.0
    p = win_probability(e, p_tie, variant)
    return max(0.0, (b * p - (1.0 - p)) / b)


# ---------------------------------------------------------------------------
# Interval estimation
# ---------------------------------------------------------------------------


def wilson_interval(successes: int, trials: int, confidence: float = 0.95
                    ) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Preferred over the normal approximation because the tie rate is a small
    proportion, where the Wald interval misbehaves badly and can produce
    negative lower bounds. Used for ``p_tie``, whose uncertainty propagates
    directly into the required edge.
    """
    if trials <= 0:
        return (0.0, 1.0)
    if not 0 <= successes <= trials:
        raise ValueError(f"successes {successes} outside [0, {trials}]")
    z = float(norm.ppf(0.5 + confidence / 2.0))
    n = float(trials)
    phat = successes / n
    denom = 1.0 + z * z / n
    centre = (phat + z * z / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt(phat * (1.0 - phat) / n + z * z / (4.0 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def bootstrap_median_interval(
    values: Sequence[float],
    confidence: float = 0.95,
    resamples: int = 2000,
    seed: int = 20260101,
) -> tuple[float, float]:
    """Percentile bootstrap interval for the median of an observed sample.

    The payout sample is autocorrelated (successive quotes on one cell move
    together), so this understates uncertainty. It is reported as an indicative
    band, and the decision rule is deliberately built on a robust central
    statistic rather than on the interval.
    """
    import numpy as np

    arr = np.asarray([v for v in values if v is not None and math.isfinite(v)],
                     dtype=float)
    if arr.size == 0:
        return (math.nan, math.nan)
    if arr.size == 1:
        return (float(arr[0]), float(arr[0]))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, arr.size, size=(resamples, arr.size))
    medians = np.median(arr[idx], axis=1)
    lo = float(np.quantile(medians, (1.0 - confidence) / 2.0))
    hi = float(np.quantile(medians, 1.0 - (1.0 - confidence) / 2.0))
    return (lo, hi)


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractEconomics:
    """Every derived economic quantity for one (payout, tie rate, variant)."""

    payout_fraction: float
    tie_rate: float
    variant: Variant
    breakeven_probability: float
    house_margin: float
    required_edge: float
    required_ic: float
    ev_at_zero_edge: float

    def ev_at(self, edge: float) -> float:
        return expected_value(self.payout_fraction, edge, self.tie_rate, self.variant)


def evaluate(b: float, p_tie: float, variant: Variant = "strict") -> ContractEconomics:
    """Compute the full economic picture for one quoted contract."""
    return ContractEconomics(
        payout_fraction=b,
        tie_rate=p_tie,
        variant=variant,
        breakeven_probability=breakeven_probability(b),
        house_margin=house_margin(b),
        required_edge=required_edge(b, p_tie, variant),
        required_ic=required_information_coefficient(b, p_tie, variant),
        ev_at_zero_edge=expected_value(b, 0.0, p_tie, variant),
    )
