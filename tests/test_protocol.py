import pytest

from deriv_census import protocol as p
from deriv_census.protocol import DerivError, duration_to_seconds


def test_proposal_uses_stake_basis_so_payout_is_the_free_variable():
    msg = p.proposal("frxEURUSD", "CALL", 5, "m", 10.0)
    assert msg["basis"] == "stake"      # payout floats; that is the measurement
    assert msg["amount"] == 10.0
    assert msg["contract_type"] == "CALL"
    assert msg["duration"] == 5 and msg["duration_unit"] == "m"
    assert msg["subscribe"] == 1


def test_proposal_can_be_unsubscribed_for_one_shot_probes():
    assert "subscribe" not in p.proposal("frxEURUSD", "CALL", 5, "m", 10.0,
                                         subscribe=False)


def test_no_request_can_authenticate_or_trade():
    """The census must be structurally incapable of placing a trade."""
    builders = [p.ping(), p.active_symbols(), p.contracts_for("frxEURUSD"),
                p.proposal("frxEURUSD", "CALL", 5, "m", 10.0),
                p.ticks("frxEURUSD"), p.forget("x"), p.forget_all("proposal")]
    forbidden = {"authorize", "buy", "sell", "api_token", "token",
                 "sell_contract", "buy_contract_for_multiple_accounts"}
    for msg in builders:
        assert not forbidden & set(msg), f"{msg} touches a trading endpoint"


@pytest.mark.parametrize("text,expected", [
    ("15s", 15), ("1m", 60), ("5m", 300), ("2h", 7200), ("365d", 31_536_000),
    ("5t", None), (None, None), ("", None), ("garbage", None), ("xm", None),
])
def test_duration_parsing(text, expected):
    assert duration_to_seconds(text) == expected


def test_parse_active_symbols_normalises_flags_and_pip():
    parsed = p.parse_active_symbols({"active_symbols": [
        {"symbol": "frxEURUSD", "display_name": "EUR/USD", "market": "forex",
         "submarket": "major_pairs", "exchange_is_open": 1,
         "is_trading_suspended": 0, "pip": 1e-05},
        {"symbol": "R_50", "display_name": "Volatility 50", "market": "synthetic_index",
         "submarket": "random_index", "exchange_is_open": 1,
         "is_trading_suspended": 1, "pip": 0.001},
    ]})
    eurusd, r50 = parsed
    assert eurusd.tradeable is True
    assert eurusd.pip_decimals == 5          # needed for exact tie comparison
    assert r50.tradeable is False            # suspended
    assert r50.pip_decimals == 3


def test_parse_active_symbols_tolerates_missing_optional_fields():
    parsed = p.parse_active_symbols({"active_symbols": [{"symbol": "X"}]})
    assert parsed[0].symbol == "X"
    assert parsed[0].pip is None and parsed[0].pip_decimals is None
    assert parsed[0].tradeable is False


def test_parse_contracts_for_extracts_types_and_bounds():
    parsed = p.parse_contracts_for({"contracts_for": {"available": [
        {"contract_type": "CALL", "contract_category": "callput",
         "min_contract_duration": "15s", "max_contract_duration": "365d"}]}})
    assert parsed[0].contract_type == "CALL"
    assert parsed[0].min_duration == "15s"


def test_parse_handles_empty_payloads():
    assert p.parse_active_symbols({}) == []
    assert p.parse_contracts_for({}) == []
    assert p.parse_active_symbols({"active_symbols": None}) == []


def test_error_classification_drives_cooldown_versus_permanent_drop():
    assert DerivError("RateLimit", "slow down").is_rate_limit
    assert DerivError("InvalidSymbol", "no").is_permanent
    # A closed market must NOT be permanent - it reopens within the run.
    assert not DerivError("MarketIsClosed", "closed").is_permanent
    assert not DerivError("RateLimit", "slow").is_permanent


def test_variant_mapping_is_total_over_contract_types():
    for variant, (rise, fall) in p.CONTRACT_TYPES.items():
        assert p.VARIANT_OF_CONTRACT_TYPE[rise] == variant
        assert p.VARIANT_OF_CONTRACT_TYPE[fall] == variant
