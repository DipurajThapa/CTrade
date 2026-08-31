import pytest
import yaml

from deriv_census.config import CensusConfig, load_config


def test_defaults_are_valid_and_conservative():
    cfg = CensusConfig()
    cfg.validate()
    assert cfg.grid.markets == ["forex"]
    assert 1 not in cfg.grid.durations_seconds      # 1s makes no sense
    assert cfg.decision.go_max_required_edge <= cfg.decision.conditional_max_required_edge


def test_yaml_overrides_merge_without_clobbering_siblings(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({
        "grid": {"durations_seconds": [180], "stake": 5.0},
        "decision": {"go_max_required_edge": 0.01}}))
    cfg = load_config(path)
    assert cfg.grid.durations_seconds == [180]
    assert cfg.grid.stake == 5.0
    assert cfg.grid.markets == ["forex"]            # untouched sibling
    assert cfg.decision.go_max_required_edge == 0.01
    assert cfg.decision.conditional_max_required_edge == 0.030


def test_unknown_keys_are_rejected_rather_than_silently_ignored(tmp_path):
    """A typo in a 14-day run's config must fail loudly at start, not quietly
    disable the setting for two weeks."""
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({"grid": {"duration_seconds": [180]}}))
    with pytest.raises(ValueError, match="unknown config key"):
        load_config(path)


def test_environment_overrides_keep_credentials_out_of_source_control(monkeypatch):
    monkeypatch.setenv("DERIV_APP_ID", "99999")
    monkeypatch.setenv("DERIV_ENDPOINT", "wss://example.test/ws")
    cfg = load_config(None)
    assert cfg.connection.app_id == "99999"
    assert cfg.connection.url() == "wss://example.test/ws?app_id=99999"


@pytest.mark.parametrize("mutate,match", [
    (lambda c: setattr(c.grid, "durations_seconds", []), "empty"),
    (lambda c: setattr(c.grid, "durations_seconds", [-1]), "positive"),
    (lambda c: setattr(c.grid, "variants", ["sideways"]), "variants"),
    (lambda c: setattr(c.grid, "directions", ["up"]), "directions"),
    (lambda c: setattr(c.grid, "stake", 0), "stake"),
    (lambda c: setattr(c.sampling, "dwell_seconds", 0), "dwell"),
    (lambda c: setattr(c.decision, "go_max_required_edge", 0.9), "thresholds"),
])
def test_invalid_configs_are_rejected(mutate, match):
    cfg = CensusConfig()
    mutate(cfg)
    with pytest.raises(ValueError, match=match):
        cfg.validate()
