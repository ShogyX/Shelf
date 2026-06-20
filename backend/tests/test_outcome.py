"""The route-outcome type: enum values align with the ledger reason vocabulary; .matched property."""
from __future__ import annotations

from app.ingestion import ledger
from app.ingestion.outcome import Outcome, RouteResult


def test_outcome_values_align_with_ledger_reasons():
    # NO_MATCH must map straight onto a ContentRequest.failure_reason.
    assert Outcome.NO_MATCH.value == "no_match"
    assert Outcome.NO_MATCH.value in ledger.REASONS
    assert Outcome.ERROR.value in ledger.REASONS
    # str(Enum) members compare equal to their string value (str mixin).
    assert Outcome.MATCHED == "matched"


def test_route_result_matched_property():
    ok = RouteResult(Outcome.MATCHED, job=object(), status="downloading", route="torrent")
    assert ok.matched is True
    assert ok.status == "downloading" and ok.route == "torrent"
    for o in (Outcome.NO_MATCH, Outcome.EXHAUSTED, Outcome.UNAVAILABLE, Outcome.ERROR):
        assert RouteResult(o).matched is False


def test_route_result_is_frozen():
    import dataclasses
    import pytest
    rr = RouteResult(Outcome.NO_MATCH)
    with pytest.raises(dataclasses.FrozenInstanceError):
        rr.outcome = Outcome.MATCHED  # type: ignore[misc]
