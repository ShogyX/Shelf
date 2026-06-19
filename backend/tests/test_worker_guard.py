"""SEC-S2: the single-worker guard refuses an env that would split the in-process security state
(brute-force lockout / login rate-limit / request-stats counters) across multiple workers."""
from __future__ import annotations

import pytest

from app.__main__ import _assert_single_worker


def test_allows_single_worker():
    # Unset or =1 is the supported configuration → no-op.
    _assert_single_worker({})
    _assert_single_worker({"WEB_CONCURRENCY": "1"})
    _assert_single_worker({"SHELF_WORKERS": "1"})


def test_refuses_multi_worker():
    for env in ({"WEB_CONCURRENCY": "2"}, {"SHELF_WORKERS": "4"}, {"WEB_CONCURRENCY": "8"},
                {"WEB_CONCURRENCY": "abc"}):  # unparseable → fail closed with the clean message
        with pytest.raises(SystemExit):
            _assert_single_worker(env)
