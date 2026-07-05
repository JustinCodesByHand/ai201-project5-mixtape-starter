"""
tests/test_notifications.py — Mixtape

Tests for notification creation on song interactions.
"""

import pytest
from app import create_app, db
from models import User, Song, Notification
from services.notification_service import rate_song


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed(app):
    """A song shared by one user, plus a second user who will rate it."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Shared Song", artist="Someone", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()
        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rating_a_song_notifies_the_sharer(app, seed):
    """
    When a user rates a song shared by someone else, the person who shared
    the song should receive a notification.
    """
    with app.app_context():
        sharer_id = seed["sharer"].id
        rater_id = seed["rater"].id
        song_id = seed["song"].id

        rate_song(rater_id, song_id, 5)

        notifs = (
            db.session.query(Notification)
            .filter_by(user_id=sharer_id)
            .all()
        )
        assert len(notifs) == 1
        assert notifs[0].notification_type == "song_rated"


def test_rating_your_own_song_does_not_notify(app, seed):
    """
    A user rating their own shared song should not generate a notification
    (you don't get notified about your own action).
    """
    with app.app_context():
        sharer_id = seed["sharer"].id
        song_id = seed["song"].id

        rate_song(sharer_id, song_id, 4)

        notifs = (
            db.session.query(Notification)
            .filter_by(user_id=sharer_id)
            .all()
        )
        assert len(notifs) == 0
