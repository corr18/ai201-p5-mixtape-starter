"""
tests/test_feed.py — Mixtape

Tests for the "Friends Listening Now" feed.
"""

import pytest
from datetime import datetime, timedelta, timezone
from app import create_app, db
from models import User, Song, ListeningEvent, friendships
from services.feed_service import get_friends_listening_now


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def social(app):
    """nova is friends with darius (night owl) and simone (listening now)."""
    with app.app_context():
        nova = User(username="nova", email="nova@example.com")
        darius = User(username="darius", email="darius@example.com")
        simone = User(username="simone", email="simone@example.com")
        db.session.add_all([nova, darius, simone])
        db.session.flush()
        for friend in (darius, simone):
            db.session.execute(friendships.insert().values(user_id=nova.id, friend_id=friend.id))
            db.session.execute(friendships.insert().values(user_id=friend.id, friend_id=nova.id))
        song = Song(title="Golden Hour", artist="Solange K", shared_by=nova.id)
        db.session.add(song)
        db.session.flush()

        now = datetime.now(timezone.utc)
        start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # darius: last listen was 23:30 yesterday — always < 24h ago, but not today.
        db.session.add(ListeningEvent(user_id=darius.id, song_id=song.id,
                                      listened_at=start_of_today - timedelta(minutes=30)))
        # simone: listened just now (clamped so it is never before midnight).
        db.session.add(ListeningEvent(user_id=simone.id, song_id=song.id,
                                      listened_at=max(now - timedelta(minutes=1), start_of_today)))
        db.session.commit()
        yield {"nova": nova, "darius": darius, "simone": simone}


def test_listening_now_excludes_friends_who_last_listened_yesterday(app, social):
    """
    Regression for Issue #2: a friend whose last listen was yesterday evening
    (within the past 24 hours, but on the previous calendar day) must not
    appear in Friends Listening Now.
    """
    with app.app_context():
        feed = get_friends_listening_now(social["nova"].id)
        names = [e["friend"]["username"] for e in feed]
        assert "darius" not in names


def test_listening_now_includes_friends_who_listened_today(app, social):
    with app.app_context():
        feed = get_friends_listening_now(social["nova"].id)
        names = [e["friend"]["username"] for e in feed]
        assert names == ["simone"]
