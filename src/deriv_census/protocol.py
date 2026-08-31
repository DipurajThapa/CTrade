"""Deriv WebSocket API v3: message construction and response parsing.

Kept pure and separate from transport so the wire format can be tested without
a socket, and so a field-name surprise on the live API is a one-file fix.

The census uses only public, unauthenticated, read-only calls:

``ping``            keepalive
``active_symbols``  instrument discovery
``contracts_for``   available contract types and duration bounds per symbol
``proposal``        a price quote -- the measurement itself
``ticks``           the quote stream, used to measure settlement outcomes
``forget``          release a subscription

No call in this module can open a position, move money, or read account state.
There is no authentication path here at all: the client never sends a token,
so a leaked config file cannot be used to trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CONTRACT_TYPES: dict[str, tuple[str, str]] = {
    # variant -> (rise contract type, fall contract type)
    "strict": ("CALL", "PUT"),
    "equals": ("CALLE", "PUTE"),
}

VARIANT_OF_CONTRACT_TYPE: dict[str, str] = {
    "CALL": "strict", "PUT": "strict",
    "CALLE": "equals", "PUTE": "equals",
}


class DerivError(RuntimeError):
    """An ``error`` object returned by the API."""

    def __init__(self, code: str, message: str, echo: dict[str, Any] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.echo = echo or {}

    @property
    def is_rate_limit(self) -> bool:
        return self.code in {"RateLimit", "RateLimitExceeded"}

    @property
    def is_permanent(self) -> bool:
        """True for errors that will not resolve by retrying the same request.

        Used by the sampler to drop a cell from the grid permanently instead of
        burning request budget on it for the rest of a fourteen-day run.
        """
        return self.code in {
            "InvalidSymbol", "OfferingsValidationError", "ContractValidationError",
            "InputValidationFailed", "InvalidContractType", "PermissionDenied",
            "InvalidAppID", "ContractCreationFailure",
        }


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


def ping() -> dict[str, Any]:
    return {"ping": 1}


def active_symbols(product_type: str = "basic") -> dict[str, Any]:
    return {"active_symbols": "brief", "product_type": product_type}


#: Request shapes to try when discovering instruments, most specific first.
#:
#: Deriv accepts several parameter combinations here and which one returns a
#: populated list has varied -- with the account's landing company, with
#: whether ``product_type`` is supplied, and over time. A single hardcoded
#: shape that returns an empty list is indistinguishable from "the venue
#: offers nothing", which is a bad failure: it reads as a market condition
#: when it is really a request that needs a different parameter.
#:
#: So the shape is discovered rather than assumed. Every attempt and its
#: response is recorded, which turns an opaque zero into a diagnosis.
ACTIVE_SYMBOLS_VARIANTS: list[tuple[str, dict[str, Any]]] = [
    ("brief+basic",
     {"active_symbols": "brief", "product_type": "basic"}),
    ("brief",
     {"active_symbols": "brief"}),
    ("full",
     {"active_symbols": "full"}),
    ("brief+svg",
     {"active_symbols": "brief", "landing_company_short": "svg"}),
    ("brief+basic+svg",
     {"active_symbols": "brief", "product_type": "basic",
      "landing_company_short": "svg"}),
    ("brief+landing_company_svg",
     {"active_symbols": "brief", "landing_company": "svg"}),
]


def website_status() -> dict[str, Any]:
    """Site status, and crucially the country Deriv thinks you are in.

    Deriv scopes its offerings by jurisdiction. An ``active_symbols`` list
    that comes back empty for every request shape is far more likely to mean
    "nothing is offered to this country" than "the request was malformed",
    and ``clients_country`` is what distinguishes the two. Unauthenticated.
    """
    return {"website_status": 1}


def landing_company(country: str) -> dict[str, Any]:
    """Which Deriv entities serve a country, and what each may offer.

    If no company covers the country, or none offers the relevant product,
    that is the answer: the venue is unavailable from there, and no amount of
    request tuning changes it.
    """
    return {"landing_company": country}


#: Deriv's symbol codes for the major FX pairs.
#:
#: Used only when discovery returns nothing. Asking for a price directly is
#: strictly more informative than a second opinion on the symbol list: a quote
#: is the measurement the census exists to take, and an error carries a reason
#: code. Either outcome beats an empty list.
FALLBACK_FX_SYMBOLS: list[str] = [
    "frxEURUSD", "frxGBPUSD", "frxUSDJPY", "frxAUDUSD",
    "frxUSDCHF", "frxUSDCAD", "frxEURGBP",
]


def contracts_for(symbol: str, currency: str = "USD",
                  product_type: str | None = "basic") -> dict[str, Any]:
    """Available contract types and duration bounds for one symbol.

    ``product_type`` is optional because it is not harmless. On an
    unauthenticated connection ``product_type="basic"`` makes
    ``active_symbols`` return an empty list -- accepted, no error, just
    nothing -- and the same parameter is accepted here. Sending it by
    default and falling back to omitting it keeps the common case terse
    without letting one parameter silently empty the grid.
    """
    msg: dict[str, Any] = {"contracts_for": symbol, "currency": currency}
    if product_type is not None:
        msg["product_type"] = product_type
    return msg


#: Request shapes to try for ``contracts_for``, most specific first. Same
#: reasoning as ACTIVE_SYMBOLS_VARIANTS: an empty offering list is not
#: evidence that a symbol offers no contracts.
CONTRACTS_FOR_VARIANTS: list[tuple[str, str | None]] = [
    ("with_product_type", "basic"),
    ("plain", None),
]


def proposal(symbol: str, contract_type: str, duration: int, duration_unit: str,
             stake: float, currency: str = "USD",
             subscribe: bool = True) -> dict[str, Any]:
    """A price quote for one contract.

    ``basis="stake"`` fixes what we pay and lets the payout float, which is the
    orientation we want: the payout is the quantity under measurement. Asking
    with ``basis="payout"`` instead would fix the payout and float the price,
    hiding the very variable the census exists to observe.

    ``subscribe=True`` turns a single request into a live stream of re-quotes.
    That is both far kinder to the rate limit than polling and strictly more
    informative, because the re-quote sequence measures payout drift -- the
    quantity that sets the live system's edge buffer.
    """
    msg: dict[str, Any] = {
        "proposal": 1,
        "amount": round(float(stake), 2),
        "basis": "stake",
        "contract_type": contract_type,
        "currency": currency,
        "duration": int(duration),
        "duration_unit": duration_unit,
        "symbol": symbol,
    }
    if subscribe:
        msg["subscribe"] = 1
    return msg


def ticks(symbol: str) -> dict[str, Any]:
    return {"ticks": symbol, "subscribe": 1}


def forget(subscription_id: str) -> dict[str, Any]:
    return {"forget": subscription_id}


def forget_all(*types: str) -> dict[str, Any]:
    return {"forget_all": list(types) if len(types) > 1 else types[0]}


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    display_name: str
    market: str
    submarket: str
    exchange_is_open: bool
    is_trading_suspended: bool
    pip: float | None

    @property
    def tradeable(self) -> bool:
        return self.exchange_is_open and not self.is_trading_suspended

    @property
    def pip_decimals(self) -> int | None:
        """Decimal places implied by the pip size, e.g. 0.00001 -> 5.

        Needed to compare quotes exactly when measuring the tie rate: float
        equality on parsed decimals is unreliable, so quotes are rounded to
        this precision before comparison.
        """
        if not self.pip or self.pip <= 0:
            return None
        import math as _m
        return max(0, int(round(-_m.log10(self.pip))))


def parse_active_symbols(payload: dict[str, Any]) -> list[SymbolInfo]:
    out: list[SymbolInfo] = []
    for row in payload.get("active_symbols", []) or []:
        pip = row.get("pip")
        out.append(SymbolInfo(
            symbol=row["symbol"],
            display_name=row.get("display_name", row["symbol"]),
            market=row.get("market", ""),
            submarket=row.get("submarket", ""),
            exchange_is_open=bool(row.get("exchange_is_open", 0)),
            is_trading_suspended=bool(row.get("is_trading_suspended", 0)),
            pip=float(pip) if pip not in (None, "") else None,
        ))
    return out


@dataclass(frozen=True)
class ContractOffering:
    contract_type: str
    contract_category: str
    min_duration: str | None
    max_duration: str | None
    barrier_category: str | None
    start_type: str | None


def parse_contracts_for(payload: dict[str, Any]) -> list[ContractOffering]:
    block = payload.get("contracts_for") or {}
    out: list[ContractOffering] = []
    for row in block.get("available", []) or []:
        out.append(ContractOffering(
            contract_type=row.get("contract_type", ""),
            contract_category=row.get("contract_category", ""),
            min_duration=row.get("min_contract_duration"),
            max_duration=row.get("max_contract_duration"),
            barrier_category=row.get("barrier_category"),
            start_type=row.get("start_type"),
        ))
    return out


DURATION_SECONDS = {"t": None, "s": 1, "m": 60, "h": 3600, "d": 86400}


def duration_to_seconds(text: str | None) -> int | None:
    """Parse Deriv's compact duration strings such as ``"15s"``, ``"5m"``.

    Returns ``None`` for tick-denominated durations, which have no fixed
    wall-clock length and are therefore excluded from the census grid.
    """
    if not text:
        return None
    text = str(text).strip()
    if not text:
        return None
    unit = text[-1]
    if unit not in DURATION_SECONDS:
        return None
    mult = DURATION_SECONDS[unit]
    if mult is None:
        return None
    try:
        return int(float(text[:-1]) * mult)
    except ValueError:
        return None
