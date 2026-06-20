"""Route outcome — the result of asking ONE acquisition route to fulfil a title.

`acquire.acquire` walks the priority chain; each route either starts a download (MATCHED), has nothing
to offer (NO_MATCH), exhausted its candidates (EXHAUSTED), was temporarily unreachable (UNAVAILABLE),
or errored (ERROR). Today the loop tracks only a ``last_err`` string; this type lets each route block
report a structured outcome so the worst non-matched reason can be threaded into the response without
changing the matched-path return dict or the bottom-of-loop ledger gating (Wave A is a pure refactor).

The enum values reuse the ``ledger.REASONS`` vocabulary where they overlap (no_match) so a route's
reason maps straight onto a ContentRequest.failure_reason.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Outcome(str, Enum):
    MATCHED = "matched"          # a job/hook was started for this title
    NO_MATCH = "no_match"        # the route ran but found no confident candidate
    EXHAUSTED = "exhausted"      # candidates were tried but none verified/usable
    UNAVAILABLE = "unavailable"  # the route's backend was temporarily unreachable (transient)
    ERROR = "error"             # the route raised an unexpected error


@dataclass(frozen=True)
class RouteResult:
    outcome: Outcome
    job: Any = None              # the DownloadJob (or hook Work) created on a match — internal plumbing
    status: str | None = None    # the public status word for a match: hooked|grabbed|downloading
    retry_at: Any = None         # an optional retry hint (unused in Wave A)
    reason: str | None = None    # a human-readable detail for the response / logs
    route: str | None = None     # the route key that produced this result

    @property
    def matched(self) -> bool:
        return self.outcome is Outcome.MATCHED
