"""alpaca-py must not silently re-POST an order after an AMBIGUOUS failure.

relay.alpaca_client.is_definitive_rejection encodes the rule: a 4xx means the
broker created nothing (safe to retry), a 5xx or timeout means the order may
have been accepted and filled while the response was lost — and with no
idempotency key a blind re-submit opens a SECOND position.

That rule lived only in order_manager._submit_with_retry, one layer above the
SDK. alpaca-py runs its OWN retry loop underneath: DEFAULT_RETRY_EXCEPTION_CODES
is [429, 504] with 3 attempts 3s apart, so a 504 on POST /v2/orders was re-sent
up to three more times before our code saw anything. It cannot be passed in —
RESTClient.__init__ takes retry_exception_codes but TradingClient.__init__ does
not forward it — so AlpacaClient sets the attribute directly.

    .venv/bin/pytest -q tests/test_alpaca_client_retries.py
"""
from __future__ import annotations

import os

os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

from relay.alpaca_client import AlpacaClient  # noqa: E402


class _FakeRest:
    """Stands in for TradingClient, which stores retry config on _retry_codes."""

    def __init__(self):
        self._retry_codes = [429, 504]


def test_504_is_removed_so_an_order_is_never_blindly_resent():
    c = _FakeRest()
    AlpacaClient._disable_ambiguous_retries(c)
    assert 504 not in c._retry_codes, "a 504 may mean the order WAS booked"
    assert all(code < 500 for code in c._retry_codes)


def test_429_is_kept_because_it_means_nothing_was_created():
    c = _FakeRest()
    AlpacaClient._disable_ambiguous_retries(c)
    assert c._retry_codes == [429]


def test_a_renamed_sdk_attribute_does_not_break_client_construction():
    """This reaches into SDK internals on purpose; an upgrade that renames the
    attribute must degrade to the old default, never raise."""
    class Renamed:
        pass

    AlpacaClient._disable_ambiguous_retries(Renamed())  # must not raise


def test_the_shared_default_list_is_not_mutated():
    """_SAFE_RETRY_CODES is class-level; handing out the list itself would let
    one client's SDK mutate every future client's config."""
    a, b = _FakeRest(), _FakeRest()
    AlpacaClient._disable_ambiguous_retries(a)
    a._retry_codes.append(504)
    AlpacaClient._disable_ambiguous_retries(b)
    assert b._retry_codes == [429]
    assert AlpacaClient._SAFE_RETRY_CODES == [429]
