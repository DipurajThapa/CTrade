"""Deriv payout census.

Measures the two quantities that decide whether a short-horizon binary options
strategy on Deriv can work at all:

1. the distribution of the quoted payout fraction ``b``, which fixes the
   break-even win probability at ``1 / (1 + b)``; and
2. the empirical settlement-tie rate, which on strict Rise/Fall contracts is a
   direct, and often larger than expected, subtraction from the win rate.

Together they give the directional edge a model must produce merely to break
even. Comparing that against what short-horizon FX models actually achieve out
of sample is the entire decision.

The package is read-only with respect to the venue: it has no authentication
path and cannot place a trade.
"""

__version__ = "1.0.0"
