# Mixtape Bug Hunt — Submission

## AI Usage

_(written at the end — see bottom of milestone 4)_

---

## Codebase Map

### Top level

- **`app.py`**: Flask application factory. `create_app(config=None)` builds the Flask app, configures SQLAlchemy (defaults to `sqlite:///mixtape.db`, which Flask-SQLAlchemy places in `instance/`), merges in any override config (the tests pass an in-memory SQLite URI), registers the four blueprints under `/songs`, `/playlists`, `/users`, and `/feed`, and runs `db.create_all()`. The module-level `db = SQLAlchemy()` object is imported by every model and service. This is why the app must be started with `FLASK_APP=app:create_app flask run` and not `python app.py`, which would import `app` twice.
- **`models.py`**: Seven SQLAlchemy models (`User`, `Tag`, `Song`, `ListeningEvent`, `Rating`, `Playlist`, `Notification`) and three association tables:
  - `friendships(user_id, friend_id)`: a self-referential many-to-many on `User`. Friendship is stored as **two rows**, one per direction. `User.friends` is a `lazy="dynamic"` relationship, so it returns a query instead of a list.
  - `song_tags(song_id, tag_id)`: many-to-many between `Song` and `Tag`. `Song.tags` loads it with `lazy="subquery"`, and `Song.to_dict()` serializes tags as a list of names.
  - `playlist_entries(playlist_id, song_id, position, added_by, added_at)`: a many-to-many **with payload**. Besides the two foreign keys it carries an explicit `position` (NOT NULL) and `added_by` (NOT NULL), so a playlist's order lives in this table and not in insertion order. `Playlist.songs` is a plain `secondary=` relationship over this table.
  - `User` stores denormalized streak state (`listening_streak`, `last_listened_at`), so the streak is computed when a listen is written, not when it is read.
  - `Rating` has a unique constraint on `(user_id, song_id)`: one rating per user per song, which is updated in place on re-rate.
  - Every model with a timestamp defaults it to `datetime.now(timezone.utc)`. SQLite stores these as naive strings, so values read back are naive UTC datetimes.
  - Each model has a `to_dict()` that the routes return as JSON.
- **`seed_data.py`**: Drops and recreates the DB, then inserts 5 users (nova, darius, simone, kenji, aaliya) with bidirectional friendships, 10 tags, 13 songs grouped by tag count (0, 1, or 3 tags), listening events spread from ~10 minutes to ~2.5 days in the past, 3 playlists of 7 songs each (positions 1–7, written straight into `playlist_entries`), and one sample `song_added_to_playlist` notification.
- **`tests/`**: pytest suites for streaks, search, and playlists. Each one builds a fresh app on `sqlite:///:memory:` and seeds its own fixtures.

### Routes layer (`routes/`): HTTP parsing and response formatting only

| File | Endpoints | Delegates to |
|---|---|---|
| `songs.py` | `GET /songs/search?q=`, `GET /songs/<id>`, `POST /songs/<id>/rate`, `POST /songs/<id>/listen` | `search_service.search_songs/get_song`, `notification_service.rate_song`, `streak_service.record_listening_event` |
| `playlists.py` | `POST /playlists/`, `GET /playlists/<id>`, `GET /playlists/<id>/songs`, `POST /playlists/<id>/songs` | `playlist_service.*`, `notification_service.add_to_playlist` |
| `users.py` | `GET /users/<id>`, `GET /users/<id>/streak`, `GET /users/<id>/notifications[?unread_only=true]`, `POST /users/notifications/<id>/read` | `streak_service.get_streak`, `notification_service.get_notifications/mark_as_read` |
| `feed.py` | `GET /feed/<id>/listening-now`, `GET /feed/<id>/activity` | `feed_service.get_friends_listening_now/get_activity_feed` |

### Services layer (`services/`): all business logic

- **`streak_service.py`**: `record_listening_event()` creates a `ListeningEvent` stamped with "now" (UTC) and calls `update_listening_streak(user, now)`, which compares the calendar date of `now` with the date of `user.last_listened_at`. A difference of 0 days means no change, 1 day increments the streak, and anything larger resets it to 1. `get_streak()` just reads the stored column.
- **`feed_service.py`**: `get_friends_listening_now()` gets the user's friend IDs, queries their `ListeningEvent`s newer than a cutoff, orders them newest first, and keeps only the first (most recent) event per friend. `get_activity_feed()` is the same query without the cutoff, limited to N events.
- **`search_service.py`**: `search_songs(q)` runs a 2.0-style `select(Song)` with a case-insensitive `ilike` on title or artist and returns `to_dict()` for each row, which includes tags. `get_song()` looks up a song by primary key.
- **`notification_service.py`**: Holds `create_notification()`, a generic helper that inserts and commits a `Notification`, plus the two song-interaction actions that may produce notifications: `add_to_playlist()` and `rate_song()`. Also contains `get_notifications()` (newest first, with an optional unread filter) and `mark_as_read()`.
- **`playlist_service.py`**: `create_playlist()`, `get_playlist()` (metadata only), `get_user_playlists()`, and `get_playlist_songs()`, which joins `Song` to `playlist_entries` and orders by `position` ascending.

### Data flow 1: a user rates a song

1. `POST /songs/<song_id>/rate` with body `{"user_id": ..., "score": ...}` hits `rate()` in `routes/songs.py`.
2. The route checks that both fields are present (400 otherwise), casts `score` to `int`, and calls `notification_service.rate_song(user_id, song_id, score)`.
3. `rate_song()` rejects scores outside 1–5 (`ValueError`), loads the `Song` and the rater `User` (each raises `ValueError` if missing), then looks for an existing `Rating` for `(user_id, song_id)`. If one exists it updates `score`; otherwise it adds a new `Rating`. Then it commits.
4. It returns the `Rating`. The route serializes it with `to_dict()` and responds 201. Any `ValueError` becomes a 400.

### Data flow 2: a friend adds a song to a playlist (the notifying path)

1. `POST /playlists/<playlist_id>/songs` with `{"song_id", "added_by"}` goes to `add_song()` in `routes/playlists.py`, which then calls `notification_service.add_to_playlist()`.
2. That function loads the song, the adding user, and the playlist, and appends the song to `playlist.songs` if it isn't already there.
3. If the song's `shared_by` is not the adder, it calls `create_notification(user_id=song.shared_by, notification_type="song_added_to_playlist", body=...)`. The sharer later sees it through `GET /users/<id>/notifications`.

### Data flow 3: "Friends Listening Now"

`GET /feed/<user_id>/listening-now` → `feed.listening_now()` → `feed_service.get_friends_listening_now()`. That function computes a cutoff timestamp, collects `[f.id for f in user.friends]`, queries `ListeningEvent` filtered by `user_id IN friends` and `listened_at >= cutoff`, ordered by `listened_at DESC`, and walks the rows with a `seen_friends` set so each friend appears once with their latest song.

### Patterns I noticed

- **Thin routes, fat services.** Every route parses input, calls one service function, and maps `ValueError` to a 400/404. No route touches business rules.
- **Services raise `ValueError` for "not found" or "invalid"**, and routes turn it into an HTTP error.
- **Notifications are created imperatively**, inside the action that triggers them, by calling `create_notification()` directly. There are no events, signals, or model hooks, so an action notifies only if its service function makes that call.
- **Timestamps are UTC everywhere**, and "day" logic (streaks) uses the UTC calendar date.
- **Mixed query styles**: most services use the legacy `db.session.query(...)` API, while `search_service` uses the SQLAlchemy 2.0 `select()` + `db.session.execute(...).scalars()` style.
- **Association tables with extra columns** (`playlist_entries`) are written directly with `table.insert().values(...)` in seed data and tests.

---

## Root Cause Analyses
