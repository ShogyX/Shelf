"""Insights aggregation endpoints: zero-filled daily series + overview/vt shapes (admin-gated)."""
from __future__ import annotations

from sqlalchemy import delete


def test_stats_endpoints():
    from fastapi.testclient import TestClient
    from app.db import SessionLocal, init_db
    from app.main import app
    from app.models import User, UserSession

    init_db()
    db = SessionLocal()
    for m in (UserSession, User):
        db.execute(delete(m))
    db.commit()
    db.close()

    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "adminpw1"})  # admin bypass

        a = c.get("/api/stats/acquisitions?days=14")
        assert a.status_code == 200, a.text
        days = a.json()["days"]
        assert len(days) == 14  # continuous, zero-filled
        assert all({"date", "imported", "failed", "acquire_s"} <= set(d) for d in days)
        assert days[0]["date"] < days[-1]["date"]  # oldest → newest

        g = c.get("/api/stats/library-growth?days=30").json()
        assert len(g["days"]) == 30 and "total" in g
        assert all("total" in d for d in g["days"])  # cumulative line present

        o = c.get("/api/stats/overview").json()
        assert {"downloaded_30d", "success_rate", "avg_acquire_s", "titles_in_library", "spark"} <= set(o)
        assert len(o["spark"]["downloaded"]) == 14 and len(o["spark"]["titles"]) == 14

        v = c.get("/api/stats/vt-usage")
        assert v.status_code == 200 and isinstance(v.json(), dict)

        # hit_rate is now exposed per route in the pipeline stats.
        p = c.get("/api/stats/pipeline").json()
        assert "by_route" in p["downloads"]
        assert all("hit_rate" in r for r in p["downloads"]["by_route"])

        # admin-gated: a fresh client with no session is rejected.
        assert TestClient(app).get("/api/stats/overview").status_code in (401, 403)
