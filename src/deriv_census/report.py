"""Render a census report as terminal text or a standalone HTML file."""

from __future__ import annotations

import html
import json
from pathlib import Path

from .analysis import CONDITIONAL, GO, INSUFFICIENT, STOP, CensusReport

VERDICT_BLURB = {
    GO: "Proceed to modelling, scoped to the winning cell only.",
    CONDITIONAL: "Proceed only with a pre-registered IC target and a hard kill gate.",
    STOP: "Do not spend the build. No achievable model wins at this payout.",
    INSUFFICIENT: "Not enough data yet. Continue capture.",
}


def _pct(value: float, places: int = 2) -> str:
    if value is None or value != value:
        return "n/a"
    if value in (float("inf"), float("-inf")):
        return "unreachable"
    return f"{value * 100:.{places}f}%"


def render_text(report: CensusReport, limit: int = 25) -> str:
    lines: list[str] = []
    add = lines.append

    add("=" * 100)
    add(f"DERIV PAYOUT CENSUS  --  {report.generated_at}")
    add("=" * 100)
    add("")
    add(f"VERDICT: {report.overall_verdict}")
    add(f"  {VERDICT_BLURB.get(report.overall_verdict, '')}")
    add("")
    for chunk in _wrap(report.rationale, 96):
        add(f"  {chunk}")
    add("")

    c = report.coverage
    add("-" * 100)
    add("COVERAGE")
    add(f"  proposals {c['proposal_records']:,} over {c['proposal_span_hours']}h "
        f"across {c['cells']} cells / {c['symbols_quoted']} symbols")
    add(f"  ticks     {c['tick_records']:,} over {c['tick_span_hours']}h "
        f"across {c['symbols_ticked']} symbols")
    d = report.drift
    if d.get("n"):
        add(f"  payout drift between consecutive quotes: median "
            f"{d['p50']:.4f}, p95 {d['p95']:.4f}, max {d['max']:.4f} "
            f"({d['share_nonzero'] * 100:.1f}% of re-quotes moved)")
    add("")

    add("-" * 100)
    add("PER-CELL ECONOMICS  (sorted by required edge, best first)")
    add("")
    header = (f"{'symbol':<12}{'type':<7}{'dur':>5}  {'n':>7}  {'payout':>8}"
              f"  {'p_be':>8}  {'margin':>8}  {'tie':>7}  {'req.edge':>9}"
              f"  {'req.IC':>7}  {'verdict':<16}")
    add(header)
    add("-" * len(header))
    for cell in report.cells[:limit]:
        add(f"{cell.symbol:<12}{cell.contract_type:<7}{cell.duration_s:>4}s"
            f"  {cell.n_proposals:>7,}"
            f"  {cell.b_median:>8.4f}"
            f"  {_pct(cell.breakeven_probability):>8}"
            f"  {_pct(cell.house_margin):>8}"
            f"  {_pct(cell.tie_rate):>7}"
            f"  {_pct(cell.required_edge):>9}"
            f"  {cell.required_ic:>7.3f}"
            f"  {cell.verdict:<16}")
    if len(report.cells) > limit:
        add(f"... {len(report.cells) - limit} further cells omitted")
    add("")

    flagged = [c for c in report.cells[:limit] if c.notes]
    if flagged:
        add("-" * 100)
        add("NOTES")
        for cell in flagged:
            add(f"  {cell.symbol} {cell.contract_type} {cell.duration_s}s")
            for note in cell.notes:
                for chunk in _wrap(note, 90):
                    add(f"      {chunk}")
        add("")

    add("-" * 100)
    add("PRE-REGISTERED DECISION RULE")
    add(f"  GO           required edge <= {_pct(report.decision.go_max_required_edge)}")
    add(f"  CONDITIONAL  required edge <= {_pct(report.decision.conditional_max_required_edge)}")
    add(f"  STOP         above that")
    add(f"  minimum samples per cell: {report.decision.min_proposals_per_cell} proposals, "
        f"{report.decision.min_settlement_samples_per_cell} settlements")
    add("")
    add("  Required edge is the directional skill needed to break even, on the")
    add("  non-tie outcomes, inclusive of the settlement-tie penalty. Compare it")
    add("  against 1.2-3.2pp, which is what an out-of-sample information")
    add("  coefficient of 0.03-0.08 delivers on short-horizon FX.")
    add("=" * 100)
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words, out, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


VERDICT_COLOUR = {GO: "#1a7f37", CONDITIONAL: "#9a6700",
                  STOP: "#cf222e", INSUFFICIENT: "#57606a"}


def render_html(report: CensusReport) -> str:
    rows = []
    for cell in report.cells:
        colour = VERDICT_COLOUR.get(cell.verdict, "#57606a")
        rows.append(
            "<tr>"
            f"<td>{html.escape(cell.symbol)}</td>"
            f"<td>{html.escape(cell.contract_type)}</td>"
            f"<td class='n'>{cell.duration_s}s</td>"
            f"<td class='n'>{cell.n_proposals:,}</td>"
            f"<td class='n'>{cell.b_median:.4f}</td>"
            f"<td class='n'>{_pct(cell.breakeven_probability)}</td>"
            f"<td class='n'>{_pct(cell.house_margin)}</td>"
            f"<td class='n'>{_pct(cell.tie_rate)}</td>"
            f"<td class='n'><b>{_pct(cell.required_edge)}</b></td>"
            f"<td class='n'>{cell.required_ic:.3f}</td>"
            f"<td style='color:{colour};font-weight:600'>{cell.verdict}</td>"
            "</tr>")

    colour = VERDICT_COLOUR.get(report.overall_verdict, "#57606a")
    coverage = html.escape(json.dumps(report.coverage, indent=2))
    drift = html.escape(json.dumps(report.drift, indent=2))
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Deriv Payout Census</title>
<style>
 body {{ font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        margin: 0 auto; max-width: 1100px; padding: 32px 20px; color: #1f2328; }}
 h1 {{ font-size: 22px; margin: 0 0 4px; }}
 .verdict {{ font-size: 30px; font-weight: 700; color: {colour}; margin: 18px 0 6px; }}
 .rationale {{ background: #f6f8fa; border-left: 4px solid {colour};
               padding: 12px 16px; border-radius: 4px; }}
 table {{ border-collapse: collapse; width: 100%; margin-top: 12px; font-size: 13px; }}
 th, td {{ border-bottom: 1px solid #d0d7de; padding: 6px 8px; text-align: left; }}
 th {{ background: #f6f8fa; font-weight: 600; }}
 td.n {{ text-align: right; font-variant-numeric: tabular-nums; }}
 pre {{ background: #f6f8fa; padding: 12px; border-radius: 4px; overflow-x: auto; }}
 .muted {{ color: #57606a; font-size: 13px; }}
</style></head><body>
<h1>Deriv Payout Census</h1>
<div class="muted">generated {html.escape(report.generated_at)}</div>
<div class="verdict">{html.escape(report.overall_verdict)}</div>
<div class="rationale">{html.escape(report.rationale)}</div>

<h2>Per-cell economics</h2>
<table><thead><tr>
<th>Symbol</th><th>Type</th><th>Duration</th><th>Quotes</th><th>Payout b</th>
<th>Break-even</th><th>Margin</th><th>Tie rate</th><th>Required edge</th>
<th>Required IC</th><th>Verdict</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>

<p class="muted">Required edge is the directional skill needed to break even on
non-tie outcomes, inclusive of the settlement-tie penalty. Compare against
1.2&ndash;3.2pp, the edge implied by an out-of-sample information coefficient of
0.03&ndash;0.08 on short-horizon FX.</p>

<h2>Coverage</h2><pre>{coverage}</pre>
<h2>Payout drift between consecutive quotes</h2><pre>{drift}</pre>
</body></html>"""


def write_html(report: CensusReport, destination: str | Path) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(report), encoding="utf-8")
    return path
