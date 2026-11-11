"""
tests/test_notifications.py — Mixtape

Tests for notifications triggered by friends interacting with shared songs.
"""

import pytest
from app import create_app, db
from models import User, Song, Rating
from services.notification_service import rate_song, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def people(app):
    """aaliya shares a song; kenji is the friend who rates it."""
    with app.app_context():
        aaliya = User(username="aaliya", email="aaliya@example.com")
        kenji = User(username="kenji", email="kenji@example.com")
        db.session.add_all([aaliya, kenji])
        db.session.flush()
        song = Song(title="Golden Hour", artist="Solange K", shared_by=aaliya.id)
        db.session.add(song)
        db.session.commit()
        yield {"aaliya": aaliya, "kenji": kenji, "song": song}


def test_rating_a_friends_song_notifies_the_sharer(app, people):
    """
    Regression for Issue #4: rating someone else's shared song must create a
    'song_rated' notification for the sharer, just like adding it to a playlist does.
    """
    with app.app_context():
        rate_song(people["kenji"].id, people["song"].id, 5)
        notifs = get_notifications(people["aaliya"].id)
        assert len(notifs) == 1
        assert notifs[0]["type"] == "song_rated"
        assert "kenji" in notifs[0]["body"] and "Golden Hour" in notifs[0]["body"]


def test_rating_own_song_does_not_notify(app, people):
    """Mirrors add_to_playlist: no self-notifications."""
    with app.app_context():
        rate_song(people["aaliya"].id, people["song"].id, 4)
        assert get_notifications(people["aaliya"].id) == []


def test_rating_is_still_saved(app, people):
    with app.app_context():
        rate_song(people["kenji"].id, people["song"].id, 3)
        rate_song(people["kenji"].id, people["song"].id, 5)  # re-rate updates in place
        ratings = db.session.query(Rating).filter_by(song_id=people["song"].id).all()
        assert [r.score for r in ratings] == [5]
