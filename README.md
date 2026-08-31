# Deriv Payout Census

A two-week measurement that decides whether a short-horizon binary options
strategy on Deriv is mathematically viable **before any code is written for it
and before any capital is committed**.

It answers one question:

> What directional edge would a model need, just to break even?

and compares that against what short-horizon FX models actually achieve out of
sample. If the answer is "more than any model delivers", the project is over
for the cost of two weeks and one script, rather than for the cost of a full
build.

The package is read-only with respect to the venue. It has no authentication
path, sends no token, and cannot place a trade.

---

## Why this and not a backtest

A fixed-payout binary pays `b` on a win and costs the full stake on a loss, so
break-even is not a coin flip:

```
p_be = 1 / (1 + b)
```

At a payout of 0.80 you must win 55.6% of the time merely to break even. The
excess over a fair coin is the house margin, `m = p_be - 0.5`, and it is the
edge you must manufacture before you earn anything at all.

Two facts make this worth measuring rather than assuming.

**The payout is not a constant.** It is quoted per contract, live, and it moves.
A strategy is viable at `b = 0.95` and hopeless at `b = 0.80`, and the
difference between those is not visible from documentation.

**Ties are a first-order cost.** On strict Rise/Fall an exit tick exactly equal
to the entry tick is a *loss*, not a push. The required edge is therefore

```
strict:  e = p_be / (1 - p_tie) - 0.5
equals:  e = (p_be - p_tie) / (1 - p_tie) - 0.5
```

At `b = 0.95` the raw margin is 1.28pp — but at a measured 2.8% tie rate the
required edge is **2.76pp**. Ties more than double the hurdle. A census that
measured payout alone would understate the requirement by half.

Deriv quotes both variants: `CALL`/`PUT` where a tie loses, and `CALLE`/`PUTE`
where a tie wins at a reduced payout. Which is better is genuinely close and
depends on the measured tie rate, so this tool measures both.

### The yardstick

| Payout `b` | Break-even | Margin | Required edge @2.8% ties | Required IC |
|-----------:|-----------:|-------:|-------------------------:|------------:|
| 0.30 | 76.92% | 26.92pp | 29.14pp | 0.811 |
| 0.50 | 66.67% | 16.67pp | 18.59pp | 0.484 |
| 0.70 | 58.82% |  8.82pp | 10.52pp | 0.267 |
| 0.80 | 55.56% |  5.56pp |  7.16pp | 0.180 |
| 0.90 | 52.63% |  2.63pp |  4.15pp | 0.104 |
| 0.95 | 51.28% |  1.28pp |  2.76pp | 0.069 |
| 0.98 | 50.51% |  0.51pp |  1.96pp | 0.049 |

A well-built model on short-horizon FX, measured honestly out of sample,
delivers an information coefficient of roughly **0.03–0.08** — a directional
edge of about **1.2–3.2pp**. An IC above 0.10 at these horizons is more often
a symptom of leakage than of skill.

Read the two together: viability needs `b` around **0.95 or better**. Below
roughly 0.90 there is no model that wins.

---

## Install

**Windows:** double-click `setup.bat`. It installs everything, runs the
self-test, and measures the payout. Use `setup.bat` rather than `setup.ps1`
directly -- Windows blocks unsigned PowerShell scripts downloaded from the
internet, and the `.bat` wrapper clears that mark first.

**macOS / Linux:**

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Use

```bash
# 1. Validate the live API before committing two weeks. Run during market hours.
.venv/bin/census preflight

#    Optionally capture the raw exchanges as evidence:
.venv/bin/census preflight --dump-raw capture.json

# 2. Capture. Runs for the configured 14 days; safe to interrupt and resume.
.venv/bin/census run

# 3. Watch it fill up. Open http://127.0.0.1:8765 and leave it.
.venv/bin/census serve

# 4. Verdict.
.venv/bin/census analyse

# Optional: Parquet copies of the raw streams for your own analysis.
.venv/bin/census export
```

### The monitor

`census serve` is a standard-library HTTP server — no framework, no build step,
nothing extra to install on the machine that has to stay up for a fortnight. It
binds to localhost only.

It shows the live verdict and per-cell economics, plus a header answering the
question that matters at 3am: **is the capture still alive?** A run whose
heartbeat has stopped shows as `stalled` rather than quietly serving a stale
page. JSON is at `/api/health`, `/api/verdict` and `/api/cells` if you want to
alert on it.

The page reuses the same renderer as the CLI, so the dashboard and the terminal
cannot disagree about the numbers. The report is rebuilt at most once a minute,
because building it reads the whole capture and that reaches gigabytes — a
dashboard that re-read several GB per browser refresh would compete with the
capture it is meant to observe.

`preflight` exists because a fourteen-day run that discovers on day fourteen
that a field name changed is fourteen days lost. It checks every assumption the
package makes about the wire format — contract availability, duration bounds,
the payout field, pip size, Rise/Fall payout symmetry, the tick stream — and
reports each as PASS or FAIL. **Do not start a run until it passes.**

`--dump-raw` writes every request/response pair verbatim to a JSON file. That
file is the evidence the wire format matches what `protocol.py` parses, and it
is shareable: it carries only public market data and the application id,
because this client has no authentication path that could produce anything
else. Failed exchanges are recorded too — an error response is exactly what
explains a failed check.

Register your own application id at <https://api.deriv.com/> and pass it as
`DERIV_APP_ID` rather than committing it.

## The pre-registered decision rule

Set in `config/census.yaml` **before** the run, on the required edge of the
best-performing cell:

| Median required edge | Verdict | Action |
|---|---|---|
| ≤ 1.5pp | **GO** | Proceed to modelling — scoped to that one cell only |
| ≤ 3.0pp | **CONDITIONAL** | Proceed only with a pre-registered IC target and a hard kill gate |
| > 3.0pp | **STOP** | No achievable model wins. Do not spend the build |

Cells below the configured minimum sample size report `INSUFFICIENT_DATA`
rather than a verdict.

Write the rule down before you see the data. That is the entire point.

---

## What gets captured

| Stream | Content |
|---|---|
| `data/proposals/` | Every quoted payout, with spot, timestamps and subscription id |
| `data/ticks/` | Continuous quote stream for the tick-covered symbols |
| `data/events/` | Run start/stop, discovery, reconnects, dropped cells, heartbeats |

Newline-delimited JSON, partitioned by UTC date. JSONL rather than Parquet on
the write path is deliberate: a fourteen-day unattended run *will* be
interrupted, and a truncated JSONL file loses its last line where a truncated
columnar file can lose the whole partition. Raw payloads are kept alongside
parsed fields, so a wire-format surprise is recoverable rather than fatal.

The event stream makes a run auditable after the fact — how long it truly ran,
how many reconnects, what was skipped and why. A census whose coverage cannot
be reconstructed is not evidence.

### Two subtleties the capture gets right

**Ties are compared at the pip grid, not at float precision.** Two quotes of
`1.10345` can differ in the last bit after a JSON round trip. Comparing raw
floats would report a tie rate of zero and flip the verdict, so quotes are
rounded to the feed's own precision first.

**Confidence intervals use a non-overlapping subsample.** Consecutive entry
ticks produce heavily autocorrelated settlement samples. They are unbiased for
the point estimate, so all of them are used for it — but intervals are computed
from samples spaced a full duration apart, and both counts are reported so the
difference is visible rather than buried.

---

## Settlement convention, and a trap it exposes

A Rise/Fall contract settles the exit tick against the **entry tick** — the
first tick after the contract starts — not against the quote visible at
decision time. This package reproduces that convention exactly.

The same subtlety is a live trap for the strategy itself. A backtest labelled
off the decision-time price awards itself the first fraction of a second of
every move, and that fraction correlates with signal strength. The bias
survives a look-ahead audit, because no future data is used. Measuring the
convention here is the first defence against it.

## Tests

```bash
.venv/bin/pytest
```

150 tests. The client, runner and analysis are exercised end to end against an
in-process fake Deriv server over a real socket — not against mocks that would
agree with whatever the code does. Covered: request/response correlation under
concurrency, streaming subscriptions and their release, permanent versus
transient error handling, slow-consumer backpressure, reconnection, closed
markets, interrupted captures, truncated files, frozen feeds, and every
economic identity in `stats.py`.
