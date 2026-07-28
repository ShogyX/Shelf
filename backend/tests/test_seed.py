"""The offline-development seeder.

Its whole purpose is "run this and the frontend has something to show". That only holds if the
seeded work ends up in somebody's LIBRARY: the app is behind auth, the library is per-user, and the
frontend has no way to add an existing work to it — so a work nobody owns is invisible, and the
seeder silently did nothing useful. These pin the behaviour that makes it work.
"""
import pytest
from sqlalchemy import delete, select

from app import seed
from app.auth import hash_password
from app.db import SessionLocal, init_db
from app.models import Bookshelf, BookshelfItem, Chapter, ChapterContent, LibraryItem, User, Work


@pytest.fixture
def clean_db():
    """A DB with no works and no accounts — the state a fresh checkout starts from."""
    init_db()
    db = SessionLocal()
    for m in (BookshelfItem, Bookshelf, LibraryItem, ChapterContent, Chapter, Work, User):
        db.execute(delete(m))
    db.commit()
    db.close()
    yield


def _seeded(db) -> Work | None:
    return db.scalar(select(Work).where(Work.title.like("A Quiet Ascension%")))


def _make_user(db, username: str) -> int:
    user = User(username=username, password_hash=hash_password("devpassword"), role="admin")
    db.add(user)
    db.commit()
    return user.id


def test_seeding_before_any_account_still_creates_the_work(clean_db):
    """First run of a fresh checkout: there is no account yet. The work must exist so that creating
    an admin and re-running finishes the job (rather than the seeder refusing to do anything)."""
    db = SessionLocal()
    try:
        seed.run()
        work = _seeded(db)
        assert work is not None
        assert db.scalar(select(LibraryItem.id).where(LibraryItem.work_id == work.id)) is None
    finally:
        db.close()


def test_rerunning_after_the_account_exists_puts_it_in_the_library(clean_db):
    """The regression that made the seeder useless: the "already present" path returned early, so
    seed → create admin → seed again left the title owned by nobody and unreachable in the UI."""
    db = SessionLocal()
    try:
        seed.run()                        # work exists, no accounts
        uid = _make_user(db, "dev")
        seed.run()                        # ...now it must be adopted
        work = _seeded(db)
        assert db.scalar(select(LibraryItem.id).where(
            LibraryItem.user_id == uid, LibraryItem.work_id == work.id)) is not None
    finally:
        db.close()


def test_seeding_is_idempotent_and_covers_every_account(clean_db):
    """Safe to re-run: no duplicate works, no duplicate library rows, and a second account added
    later gets it on the next run too."""
    db = SessionLocal()
    try:
        first = _make_user(db, "dev")
        seed.run()
        seed.run()                        # twice — must not duplicate
        work = _seeded(db)
        assert len(db.scalars(select(Work).where(Work.title.like("A Quiet Ascension%"))).all()) == 1
        assert len(db.scalars(select(LibraryItem).where(
            LibraryItem.user_id == first, LibraryItem.work_id == work.id)).all()) == 1
        second = _make_user(db, "dev2")
        seed.run()
        assert db.scalar(select(LibraryItem.id).where(
            LibraryItem.user_id == second, LibraryItem.work_id == work.id)) is not None
    finally:
        db.close()


def test_seeder_never_creates_an_account(clean_db):
    """It must not mint a known-credential login — that is one `python -m app.seed` away from being
    run somewhere it shouldn't be. Populating libraries is opt-in by an account already existing."""
    db = SessionLocal()
    try:
        seed.run()
        assert db.scalar(select(User.id)) is None
    finally:
        db.close()
