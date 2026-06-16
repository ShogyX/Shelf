"""F07: the cover-cache tick must back off (skip the full-table cover_url scans) once the backlog
is empty, and resume every-tick cadence while there's work."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_cover_tick_backs_off_when_idle(monkeypatch):
    from app.ingestion import scheduler as S
    runs = []
    monkeypatch.setattr(S, "_cache_covers_batch", lambda: (runs.append(1), 0)[1])  # idle: 0 done
    S._cover_next_run_at = None
    await S.cache_images_tick()   # idle result → arms the backoff
    await S.cache_images_tick()   # within backoff window → batch skipped
    assert len(runs) == 1


@pytest.mark.asyncio
async def test_cover_tick_runs_every_tick_while_working(monkeypatch):
    from app.ingestion import scheduler as S
    runs = []
    monkeypatch.setattr(S, "_cache_covers_batch", lambda: (runs.append(1), 1)[1])  # 1 done each call
    S._cover_next_run_at = None
    await S.cache_images_tick()
    await S.cache_images_tick()
    assert len(runs) == 2   # work reported → no backoff armed
    S._cover_next_run_at = None
