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
