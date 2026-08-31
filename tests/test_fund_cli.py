"""Config loading, report rendering and the command line."""

import pytest
import yaml

from fund_census.cli import main
from fund_census.config import load_config
from fund_census.report import render

VALID = {
    "plan": {"initial_amount": 1000, "monthly_contribution": 500,
             "horizon_years": 20, "gross_return": 0.07,
             "dividend_yield": 0.018},
    "platform": {"commission_per_trade": 1.0, "fx_spread": 0.002},
    "funds": [
        {"name": "Irish", "ter": 0.0022, "domicile": "IE"},
        {"name": "American", "ter": 0.0007, "domicile": "US"},
    ],
}


def write(tmp_path, data):
    path = tmp_path / "funds.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_loads_a_valid_config(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert [f.name for f in cfg.funds] == ["Irish", "American"]
    assert cfg.plan.horizon_years == 20
    assert cfg.platform.fx_spread == 0.002


def test_a_verified_withholding_block_is_honoured(tmp_path):
    data = dict(VALID)
    data["funds"] = [{"name": "Verified", "ter": 0.002, "domicile": "US",
                      "withholding": {"fund_level_rate": 0.15,
                                      "investor_level_rate": 0.0,
                                      "basis": "confirmed 2026-08"}}]
    cfg = load_config(write(tmp_path, data))
    model = cfg.funds[0].withholding_model()
    assert model.total_leakage == pytest.approx(0.15)
    assert model.basis == "confirmed 2026-08"


@pytest.mark.parametrize("mutate,match", [
    (lambda d: d.update(funds=[]), "no funds"),
    (lambda d: d["plan"].update(horizon_years=0), "horizon"),
    (lambda d: d["plan"].update(initial_amount=0, monthly_contribution=0),
     "nothing is being invested"),
    (lambda d: d["plan"].update(gross_return=7), "fraction"),
    (lambda d: d["plan"].update(dividend_yield=1.8), "fraction"),
    (lambda d: d["funds"].append({"name": "Irish", "ter": 0.001}), "unique"),
    (lambda d: d["funds"].append({"name": "Neg", "ter": -0.001}), "negative"),
])
def test_invalid_configs_are_rejected(tmp_path, mutate, match):
    import copy
    data = copy.deepcopy(VALID)
    mutate(data)
    with pytest.raises(ValueError, match=match):
        load_config(write(tmp_path, data))


def test_a_typo_in_a_key_fails_loudly(tmp_path):
    """Silently ignoring a misspelled key would quietly use a default and
    produce a confident, wrong comparison."""
    import copy
    data = copy.deepcopy(VALID)
    data["plan"]["horizon_year"] = 20          # missing the 's'
    with pytest.raises(ValueError, match="unknown plan keys"):
        load_config(write(tmp_path, data))
    data = copy.deepcopy(VALID)
    data["platform"]["fx_spred"] = 0.002
    with pytest.raises(ValueError, match="unknown platform keys"):
        load_config(write(tmp_path, data))


def test_report_names_the_winner_and_prices_the_difference(tmp_path):
    out = render(load_config(write(tmp_path, VALID)))
    assert "FUND COST CENSUS" in out
    assert "div tax" in out
    # The lower-fee fund must not be presented as the winner.
    assert "Choosing 'Irish'" in out
    assert "over 'American'" in out
    assert "Same index, same market, same risk" in out


def test_report_always_carries_the_verification_warning(tmp_path):
    """The withholding rates are the largest term and are unverified
    defaults. Saying so is not optional."""
    out = render(load_config(write(tmp_path, VALID)))
    assert "not verified tax" in out
    assert "qualified adviser" in out
    assert "estate tax" in out


def test_report_prices_contributing_against_hoping(tmp_path):
    out = render(load_config(write(tmp_path, VALID)))
    assert "contribution x2" in out
    assert "return +4%" in out
    assert "a decision you control" in out


def test_report_handles_a_single_fund(tmp_path):
    import copy
    data = copy.deepcopy(VALID)
    data["funds"] = data["funds"][:1]
    out = render(load_config(write(tmp_path, data)))
    assert "Choosing" not in out          # nothing to choose between
    assert "FUND COST CENSUS" in out


def test_cli_runs(tmp_path, capsys):
    assert main(["compare", "-c", str(write(tmp_path, VALID))]) == 0
    assert "FUND COST CENSUS" in capsys.readouterr().out


def test_cli_accepts_the_config_flag_before_the_subcommand(tmp_path, capsys):
    assert main(["-c", str(write(tmp_path, VALID)), "compare"]) == 0
    assert "FUND COST CENSUS" in capsys.readouterr().out


def test_cli_reports_a_missing_config(tmp_path, capsys):
    assert main(["compare", "-c", str(tmp_path / "nope.yaml")]) == 2
    assert "config not found" in capsys.readouterr().err


def test_cli_reports_a_bad_config_without_a_traceback(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"funds": []}))
    assert main(["compare", "-c", str(bad)]) == 2
    assert "no funds" in capsys.readouterr().err


def test_the_shipped_example_config_is_valid():
    """The worked example is the first thing anyone runs."""
    cfg = load_config("config/funds.yaml")
    assert len(cfg.funds) >= 2
    out = render(cfg)
    assert "FUND COST CENSUS" in out
