import pytest

from deriv_census.config import GridConfig
from deriv_census.discovery import (build_cells, duration_supported, is_excluded,
                                    select_symbols, summarise)
from deriv_census.protocol import ContractOffering, SymbolInfo


def sym(symbol="frxEURUSD", market="forex", submarket="major_pairs",
        display=None, open_=True, suspended=False, pip=1e-05):
    return SymbolInfo(symbol, display or symbol, market, submarket,
                      open_, suspended, pip)


def offering(ct="CALL", lo="15s", hi="365d"):
    return ContractOffering(ct, "callput", lo, hi, "euro_atm", "spot")


ALL = [offering(c) for c in ("CALL", "PUT", "CALLE", "PUTE")]


def test_synthetic_instruments_are_excluded_by_every_naming_route():
    """Deriv names synthetics inconsistently; all fields must be checked."""
    patterns = GridConfig().exclude_patterns
    assert is_excluded(sym("R_100", "synthetic_index", "random_index",
                           "Volatility 100 Index"), patterns)
    assert is_excluded(sym("BOOM500", "forex", "major_pairs", "Boom 500"),
                       patterns)
    assert is_excluded(sym("X", "forex", "otc_index", "Something"), patterns)
    assert not is_excluded(sym(), patterns)


def test_select_symbols_filters_by_market_and_exclusions():
    symbols = [sym(), sym("R_100", "synthetic_index", "random_index",
                          "Volatility 100"), sym("OTC_SPX", "indices", "otc")]
    assert [s.symbol for s in select_symbols(symbols, GridConfig())] == ["frxEURUSD"]


def test_explicit_allow_list_overrides_market_and_exclusion_filters():
    grid = GridConfig(symbols=["R_100"])
    symbols = [sym(), sym("R_100", "synthetic_index", "random_index",
                          "Volatility 100")]
    assert [s.symbol for s in select_symbols(symbols, grid)] == ["R_100"]


def test_closed_symbols_are_retained_so_the_census_is_not_session_biased():
    """Dropping closed markets at discovery would bias a 14-day census toward
    whichever session happened to be live at start-up."""
    symbols = [sym(open_=False)]
    assert len(select_symbols(symbols, GridConfig())) == 1


def test_duration_bounds_are_respected():
    offerings = [offering("CALL", "15s", "5m")]
    assert duration_supported(offerings, "CALL", 300)
    assert not duration_supported(offerings, "CALL", 301)
    assert not duration_supported(offerings, "CALL", 10)
    assert not duration_supported(offerings, "PUT", 60)


def test_unparseable_bounds_are_treated_permissively():
    """A wrongly included cell costs one rejected quote; a wrongly excluded
    one is a silent hole discovered only at analysis time."""
    assert duration_supported([offering("CALL", "5t", "10t")], "CALL", 120)


def test_build_cells_covers_the_configured_grid():
    grid = GridConfig(durations_seconds=[120, 300],
                      variants=["strict", "equals"], directions=["rise"])
    cells = build_cells(sym(), ALL, grid)
    assert len(cells) == 4                      # 2 variants x 2 durations
    assert {c.contract_type for c in cells} == {"CALL", "CALLE"}
    assert summarise(cells) == {"cells": 4, "symbols": 1,
                                "contract_types": 2, "durations": 2}


def test_both_directions_when_requested():
    grid = GridConfig(durations_seconds=[300], variants=["strict"],
                      directions=["rise", "fall"])
    assert {c.contract_type for c in build_cells(sym(), ALL, grid)} == {"CALL", "PUT"}


def test_unoffered_contract_types_are_skipped_not_guessed():
    grid = GridConfig(durations_seconds=[300], variants=["strict", "equals"])
    cells = build_cells(sym(), [offering("CALL")], grid)
    assert {c.contract_type for c in cells} == {"CALL"}


@pytest.mark.parametrize("seconds,expected", [
    (300, (5, "m")), (120, (2, "m")), (90, (90, "s")), (45, (45, "s"))])
def test_duration_expressed_in_the_largest_exact_unit(seconds, expected):
    grid = GridConfig(durations_seconds=[seconds], variants=["strict"])
    assert build_cells(sym(), ALL, grid)[0].duration_request == expected


def test_cell_key_is_stable_and_identifying():
    grid = GridConfig(durations_seconds=[300], variants=["strict"])
    assert build_cells(sym(), ALL, grid)[0].key == "frxEURUSD|CALL|300"


# --- active_symbols request-shape discovery ------------------------------


async def test_probe_stops_at_the_first_shape_that_returns_instruments():
    from deriv_census.discovery import resolve_active_symbols

    calls = []

    async def send(payload):
        calls.append(payload)
        if payload.get("product_type") == "basic":
            return {"active_symbols": []}          # the shape that fails live
        return {"active_symbols": [{"symbol": "frxEURUSD", "market": "forex"}]}

    symbols, probes = await resolve_active_symbols(send)
    assert [s.symbol for s in symbols] == ["frxEURUSD"]
    assert probes[0].variant == "brief+basic" and probes[0].count == 0
    assert probes[1].worked
    assert len(calls) == 2                          # stopped, did not keep going


async def test_probe_records_every_attempt_when_all_fail():
    """A bare zero reads as 'the venue offers nothing'. The attempt log is
    what turns it into a diagnosis."""
    from deriv_census.discovery import resolve_active_symbols
    from deriv_census.protocol import ACTIVE_SYMBOLS_VARIANTS

    async def send(_payload):
        return {"active_symbols": []}

    symbols, probes = await resolve_active_symbols(send)
    assert symbols == []
    assert len(probes) == len(ACTIVE_SYMBOLS_VARIANTS)
    assert not any(p.worked for p in probes)
    assert all(p.request for p in probes)           # request shape is recorded


async def test_probe_survives_an_error_on_one_shape():
    """An unrecognised parameter must not abort the remaining attempts."""
    from deriv_census.discovery import resolve_active_symbols
    from deriv_census.protocol import DerivError

    async def send(payload):
        if "landing_company_short" in payload or "landing_company" in payload:
            return {"active_symbols": [{"symbol": "frxEURUSD", "market": "forex"}]}
        raise DerivError("InputValidationFailed", "unknown field")

    symbols, probes = await resolve_active_symbols(send)
    assert [s.symbol for s in symbols] == ["frxEURUSD"]
    assert probes[0].error is not None
    assert probes[-1].worked


async def test_probe_survives_a_non_deriv_exception():
    from deriv_census.discovery import resolve_active_symbols

    calls = {"n": 0}

    async def send(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("slow")
        return {"active_symbols": [{"symbol": "frxEURUSD", "market": "forex"}]}

    symbols, probes = await resolve_active_symbols(send)
    assert symbols and "TimeoutError" in (probes[0].error or "")


async def test_contracts_for_falls_back_when_product_type_empties_the_result():
    """Live evidence: product_type='basic' on an unauthenticated connection
    returns an accepted-but-empty response. The same parameter is accepted by
    contracts_for, so the same fallback is needed here."""
    from deriv_census.discovery import resolve_contracts_for

    async def send(payload):
        if "product_type" in payload:
            return {"contracts_for": {"available": []}}
        return {"contracts_for": {"available": [
            {"contract_type": "CALL", "contract_category": "callput",
             "min_contract_duration": "15s", "max_contract_duration": "365d"}]}}

    offerings, probes = await resolve_contracts_for(send, "frxEURUSD")
    assert [o.contract_type for o in offerings] == ["CALL"]
    assert probes[0].variant == "with_product_type" and probes[0].count == 0
    assert probes[1].worked


async def test_contracts_for_reports_every_shape_when_all_fail():
    from deriv_census.discovery import resolve_contracts_for

    async def send(_payload):
        return {"contracts_for": {"available": []}}

    offerings, probes = await resolve_contracts_for(send, "frxEURUSD")
    assert offerings == []
    assert len(probes) == 2 and not any(p.worked for p in probes)


def test_product_type_can_be_omitted_from_the_request():
    from deriv_census.protocol import contracts_for
    assert "product_type" in contracts_for("frxEURUSD")
    assert "product_type" not in contracts_for("frxEURUSD", product_type=None)
