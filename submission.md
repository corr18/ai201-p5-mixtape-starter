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

### **Issue #1 — My listening streak keeps resetting**

**How I reproduced it.** The bug depends on the day of the week, and today isn't a Sunday, so I couldn't trigger it with a live `POST /songs/<id>/listen`. Instead I called `update_listening_streak()` directly from a script with fixed datetimes that mirror kenji's report. I set up a user with `listening_streak=12` and `last_listened_at = Sat 2026-11-07 22:00 UTC`, then called it with `now = Sun 2026-11-08 09:00 UTC`. The result was **1** (expected 13). As a control, the same setup moved one day earlier (Friday to Saturday) correctly gave **13**. The existing test `test_streak_increments_on_sunday` also failed with `assert 1 == 2`. Together these confirmed that only Sunday listens were affected, as kenji said ("both times it was a Sunday").

**How I found the root cause.** I followed `GET /users/<id>/streak` → `routes/users.py::streak()` → `streak_service.get_streak()`. That function only reads `user.listening_streak`, so the wrong value had to be written earlier. The writer is `record_listening_event()` → `update_listening_streak()`. That function's docstring lists four rules, none of which mention weekdays. The `elif` that implements "listened yesterday → +1" reads `days_since_last == 1 and today.weekday() != 6`. Python's `date.weekday()` returns 0 for Monday through 6 for Sunday, so `!= 6` means "not Sunday". I was confident this was the specific cause, not just a suspicious line, because it accounts for every detail in the report. Only Sunday is affected. The streak goes to exactly 1 (it falls through to the `else` branch). Monday counts normally again, because Monday's `weekday()` is 0.

**The root cause.** In `update_listening_streak()` in `services/streak_service.py`, the "consecutive day" branch had an extra condition, `today.weekday() != 6`. When the current listen falls on a Sunday and the previous listen was Saturday (`days_since_last == 1`), that condition is False. The branch is skipped and control reaches the `else`, which runs the "more than one day has passed" rule and sets `listening_streak = 1`. Every Saturday-to-Sunday transition was treated as a missed day, which contradicts the function's documented rules.

**My fix.** I removed the `and today.weekday() != 6` clause, so the branch is now `elif days_since_last == 1:`. Whether a listen continues a streak now depends only on the calendar-day gap, which is what the docstring specifies.

**Side-effect check.**
- All existing streak tests pass: new user starts at 1, consecutive weekday increments, same-day listens don't double count, and a skipped day resets. This includes the previously failing `test_streak_increments_on_sunday`.
- I added two regression tests to `tests/test_streaks.py`. **`test_streak_increments_across_full_week`** listens every day for 14 days starting on a Monday and asserts the streak equals `day + 1` each day. It crosses the Saturday-to-Sunday boundary twice and fails on the original code on day 7. **`test_streak_resets_when_sunday_skipped`** checks the other side of the boundary: Saturday then Monday still resets to 1, so I didn't make Sunday a free pass.
- I reran my repro script: Saturday to Sunday now gives 13, and Friday to Saturday still gives 13.
- `get_streak()` and `record_listening_event()` are unchanged, and no other code reads `weekday()`.

### **Issue #2 — Friends Listening Now shows people from yesterday**

**How I reproduced it.** I recreated nova's report in the seeded DB with a small script. I deleted darius's seeded listens and inserted a single `ListeningEvent` for darius at **23:30 UTC yesterday** (2026-11-10T23:30). I ran it at 23:08 UTC on 2026-11-11, so that listen was about 23.6 hours old: within 24 hours, but on the previous calendar day. Then I called `GET /feed/<nova_id>/listening-now`. darius appeared alongside simone and kenji, with `listened_at` `2026-11-10T23:30:00`. That matches the report: a friend whose last listen was last night still shows as "listening now" the next day.

**How I found the root cause.** I followed `routes/feed.py::listening_now()` → `feed_service.get_friends_listening_now()`. The friend lookup and the "one entry per friend" dedupe loop matched what I expected, so I focused on the time filter: `ListeningEvent.listened_at >= cutoff` with `cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD` and `RECENT_THRESHOLD = timedelta(hours=24)`. I was confident this was the cause because it explains the exact phrasing in the report: "stuff from yesterday evening keeps hanging around in the feed **until the same time the next day**." A rolling 24-hour window is the only thing that ends at "the same time the next day". A query bug or the dedupe loop wouldn't behave that way.

**The root cause.** `get_friends_listening_now()` in `services/feed_service.py` defined "recent" as a **rolling 24-hour window** (`now - timedelta(hours=24)`), but the feature promises friends who have listened **today**. Any listen in the last 24 hours passed the `listened_at >= cutoff` filter, even one from the previous evening. A friend who listened at 11pm stayed in the feed until 11pm the next night, so the morning after they still looked like they were "listening now".

**My fix.** I changed the cutoff to the **start of the current calendar day in UTC**, `datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)`, and removed the `RECENT_THRESHOLD` constant, which nothing else used. I also updated the docstring to say "today (since midnight UTC)". I chose UTC midnight because the app has no per-user timezone and `streak_service` already defines a "day" as the UTC calendar date. This keeps the two "today" concepts consistent. The filter now keeps exactly the events with `listened_at` on or after today's date.

**Side-effect check.**
- **Both sides of the boundary:** after restarting the server, `GET /feed/<nova_id>/listening-now` dropped darius (23:30 yesterday) and kept simone and kenji (listens from a few minutes earlier).
- **`get_activity_feed()`**, the other function in the file, is unchanged by design. Its docstring says it is "not filtered by recency". `GET /feed/<nova_id>/activity` still returns darius's listen from last night.
- **Regression tests:** I added `tests/test_feed.py`. `test_listening_now_excludes_friends_who_last_listened_yesterday` places darius's listen at 23:30 yesterday, which is always under 24 hours old, so the old code always fails it. `test_listening_now_includes_friends_who_listened_today` checks that a listen from today is still included. I confirmed both fail on the original `feed_service.py` (by stashing my change) and pass with the fix.
- I grepped for `RECENT_THRESHOLD`, and nothing else in the codebase referenced it.
- Known trade-off: just after midnight UTC the feed is briefly empty until someone listens. That is the "listened today" behavior the issue asks for.
