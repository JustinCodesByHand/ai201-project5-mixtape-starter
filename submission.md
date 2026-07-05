# Mixtape — Codebase Map

Submission doc for Project 5: Mixtape Bug Hunt. This is the map of how the app is built, how data flows through it, and where to look when tracing a feature.

Mixtape is a social music app: friends share songs, build collaborative playlists, rate tracks, and keep listening streaks. It's a Flask + SQLAlchemy JSON API backed by SQLite.

---

## Architecture at a glance

The app is a clean three-layer stack. Every request flows the same direction:

```
HTTP request
   │
   ▼
routes/*.py        ← thin HTTP layer: parse request, validate presence, call a service, jsonify result
   │
   ▼
services/*.py      ← all business logic lives here (streaks, feeds, search, notifications, playlists)
   │
   ▼
models.py          ← SQLAlchemy ORM models + association tables
   │
   ▼
app.py (db)        ← SQLite via SQLAlchemy
```

The rule the codebase follows: **routes never touch the database directly** (one small exception below), and **all logic lives in services**. Per the README, the bugs live in the `services/` layer — routes are just pass-through.

---

## The main files and what each does

### Core

| File | Role |
|------|------|
| `app.py` | Flask application factory. `create_app()` sets config (SQLite at `mixtape.db`), inits the `db` object, registers the four route blueprints under URL prefixes (`/songs`, `/playlists`, `/users`, `/feed`), and calls `db.create_all()`. The shared `db = SQLAlchemy()` instance also lives here — everything imports `from app import db`. |
| `models.py` | Every DB entity. Models: `User`, `Tag`, `Song`, `ListeningEvent`, `Rating`, `Playlist`, `Notification`. Three association tables: `friendships` (symmetric M:N users), `song_tags` (M:N), `playlist_entries` (M:N with `position`, `added_by`, `added_at`). IDs are UUID strings. Every model has a `to_dict()` for JSON serialization. |
| `seed_data.py` | Populates the DB with test data. |
| `requirements.txt` | Deps (Flask, flask-sqlalchemy, pytest). |

### Routes (HTTP layer — thin)

| File | Prefix | Endpoints |
|------|--------|-----------|
| `routes/songs.py` | `/songs` | `GET /search`, `GET /<id>`, `POST /<id>/rate`, `POST /<id>/listen` |
| `routes/playlists.py` | `/playlists` | `POST /`, `GET /<id>`, `GET /<id>/songs`, `POST /<id>/songs` |
| `routes/users.py` | `/users` | `GET /<id>`, `GET /<id>/streak`, `GET /<id>/notifications`, `POST /notifications/<id>/read`. Only route that touches `db`/`User` directly — the plain `get_user` lookup. |
| `routes/feed.py` | `/feed` | `GET /<id>/listening-now`, `GET /<id>/activity` |

Routes all follow one shape: read JSON/query params → check required fields present (400 if missing) → call service in `try/except ValueError` → return `jsonify` + status code. Services raise `ValueError` for "not found" / bad input; routes translate that to 404 or 400.

### Services (logic layer — where bugs live)

| File | Key functions | Owns |
|------|---------------|------|
| `services/streak_service.py` | `record_listening_event`, `update_listening_streak`, `get_streak` | Listening-streak counting on consecutive calendar days. |
| `services/feed_service.py` | `get_friends_listening_now`, `get_activity_feed` | Recent-friend-activity feeds (24h `RECENT_THRESHOLD`). |
| `services/search_service.py` | `search_songs`, `get_song` | Title/artist `ILIKE` search with tag join. |
| `services/notification_service.py` | `create_notification`, `add_to_playlist`, `rate_song`, `get_notifications`, `mark_as_read` | Ratings + notification creation/retrieval. Also owns the playlist-add write path. |
| `services/playlist_service.py` | `create_playlist`, `get_playlist_songs`, `get_playlist`, `get_user_playlists` | Playlist CRUD/retrieval, ordered by `position`. |

### Tests

`tests/test_streaks.py`, `tests/test_search.py`, `tests/test_playlists.py` — pytest, run with `pytest tests/`.

---

## Data flow: sharing a song → notification

The requested walkthrough. "Sharing a song into a playlist" is what triggers a notification, and it crosses three services — a good example of the full stack.

**Scenario:** User B adds a song (originally shared by User A) to a collaborative playlist. User A should get notified.

```
POST /playlists/<playlist_id>/songs   { song_id, added_by }
   │
   ▼  routes/playlists.py :: add_song()
        validates song_id + added_by present → else 400
        calls notification_service.add_to_playlist(playlist_id, song_id, added_by)
   │
   ▼  services/notification_service.py :: add_to_playlist()
        1. db.session.get(Song, song_id)   → 404 (ValueError) if missing
        2. db.session.get(User, added_by)  → 404 if missing
        3. db.session.get(Playlist, id)    → 404 if missing
        4. if song not in playlist.songs: playlist.songs.append(song); commit
        5. if song.shared_by != added_by:                    ← don't notify self
              create_notification(
                  user_id  = song.shared_by,                 ← the ORIGINAL sharer (User A)
                  type     = "song_added_to_playlist",
                  body     = "<adder> added your song '<title>' to '<playlist>'.")
   │
   ▼  services/notification_service.py :: create_notification()
        new Notification row → db.session.add → commit
   │
   ▼  models.py :: Notification  (user_id → User A, read=False, created_at=now)
```

User A later reads it:

```
GET /users/<A>/notifications?unread_only=true
   → routes/users.py → notification_service.get_notifications()
   → query Notification by user_id, filter read=False, order desc(created_at)
   → list of to_dict()
```

**Key design point:** the notification recipient is `song.shared_by` (the person who *originally shared* the song), not the playlist owner. The self-check (`song.shared_by != added_by`) suppresses notifying yourself about your own action.

The **rating** path is the parallel case — same file, same pattern, but note: `rate_song()` saves the rating and does **not** call `create_notification`. That matches open Issue #4 ("notified when added to playlist but not when rated"). So this diagram shows the working half of the notification feature; the rating half is the missing half.

---

## Patterns worth noticing

1. **Strict layering.** Route = HTTP + validation only; service = logic; model = data. Trace any bug from route → the one service it calls (README's exercise). The one deliberate leak: `routes/users.py` does a trivial `User` lookup inline.

2. **`ValueError` as the "not found / bad input" signal.** Services never return HTTP codes — they raise `ValueError`. Routes catch it and choose 404 vs 400. Uniform across every service.

3. **`to_dict()` everywhere.** Serialization is a model responsibility, not a route/service one. Services return `list[dict]`, never ORM objects (except the two functions that return a freshly created `Rating`/`Playlist` for the route to serialize).

4. **UUID string PKs + M:N association tables.** No integer autoincrement. `playlist_entries` is the interesting one — it carries `position`, `added_by`, `added_at`, so playlist ordering is data on the join row, not on the song.

5. **`db.session.get(Model, id)` for by-PK lookups**, `db.session.query(...).filter(...)` for everything else. Consistent split.

6. **Cross-service imports are lazy (inside functions).** `add_to_playlist` imports `Playlist` and `get_playlist_songs` inside the function body to dodge circular imports — a pattern to watch when adding new service-to-service calls.

7. **Timezone-aware UTC throughout.** `datetime.now(timezone.utc)`. `streak_service` even re-attaches `tzinfo=utc` to naive DB values before comparing — SQLite drops tz info on round-trip, so this defends against that.

---

## Where to look (quick index)

- **Understand the data →** start in `models.py`. The three association tables explain most relationships.
- **Understand a request →** open the matching `routes/*.py`, read the one-line service call, jump to that service.
- **Streak bug (Issue #1) →** `streak_service.py :: update_listening_streak`. The `today.weekday() != 6` (Sunday) branch on line 73 is the suspicious part — it skips incrementing on Sundays.
- **Feed shows stale people (Issue #2) →** `feed_service.py`. `RECENT_THRESHOLD = 24h` is a rolling window, not "today" — someone who listened 23h ago (yesterday) still shows.
- **Duplicate search results (Issue #3) →** `search_service.py :: search_songs`. The `outerjoin(song_tags)` multiplies rows per tag and there's no `.distinct()` / no `DISTINCT`.
- **Rating notification missing (Issue #4) →** `notification_service.py :: rate_song`. It commits the rating but never calls `create_notification` (contrast with `add_to_playlist`).
- **Last playlist song missing (Issue #5) →** `playlist_service.py :: get_playlist_songs`. Line 66 returns `songs[:-1]` — drops the last element.

> Above are the leads I found while mapping the code, keyed to the five README issues. Each is isolated to a single service function, consistent with "bugs live in `services/`."

---

# Root Cause Analysis

Each entry uses five fields: issue, how reproduced, how the root cause was found, the precise root cause, and the fix + side-effect check. Debugging followed a strict discipline: **no fix before a confirmed root cause, and a failing test (RED) before every fix.**

## Issue #1 — "My listening streak keeps resetting"

**How I reproduced it.** `tests/test_streaks.py` already contained `test_streak_increments_on_sunday`: it calls `update_listening_streak` with a fixed Saturday (`2024-06-15`) then a fixed Sunday (`2024-06-16`) and asserts the streak becomes 2. Running `pytest tests/test_streaks.py -v` from the project root, that one test failed with `assert 1 == 2` while the other four passed — the streak reset to 1 on Sunday instead of incrementing. The fixed calendar dates (not `datetime.now()`) make the failure deterministic; with real "today" it would only fail on actual Sundays.

**How I found the root cause.** I opened `services/streak_service.py` and traced `update_listening_streak` line by line for the Sat→Sun input. The branch chain at lines 70–76 decides the streak change. I compared the code against the function's own docstring spec (lines 46–50), which lists four cases: never-listened→1, same-day→no change, yesterday→+1, gap→reset. Three branches matched the spec; the "listened yesterday" branch did not. That mismatch — code carrying a condition the spec never mentions — is the moment I was confident it was the cause, not just a suspicious area.

**The root cause.** The consecutive-day branch was `elif days_since_last == 1 and today.weekday() != 6:`. Python's `datetime.weekday()` returns 6 for Sunday. So when a user listened yesterday and today is Sunday, `days_since_last == 1` is true (should increment) but `today.weekday() != 6` evaluates `6 != 6` → False, so the whole `elif` is False and control falls to the `else` branch, which sets `listening_streak = 1`. The extra Sunday guard turned every "yesterday → today is Sunday" transition into a reset. Nothing in the streak rules justifies special-casing Sunday; the condition was simply wrong.

**My fix and side-effect check.** Changed line 73 to `elif days_since_last == 1:`, removing the `and today.weekday() != 6` clause, so any consecutive day increments regardless of weekday. This fixes the root cause because the reset was caused solely by that spurious condition short-circuiting the increment branch. Side-effect check: re-ran the full `tests/test_streaks.py` — all 5 pass, including the previously-passing new-user, same-day-no-double-count, consecutive-day, and skipped-day-reset cases, confirming the other three branches still behave correctly. Commit `67beb7b`.

## Issue #3 — "The same song keeps showing up twice in search" (investigated — not reproducible)

**How I reproduced it (attempted).** `tests/test_search.py` seeds three songs with 0, 1, and 3 tags and asserts each appears exactly once. I predicted `test_search_no_duplicates_multi_tag_song` (3-tag song) would fail. Running the suite, it **passed** — all 5 green. Rather than trust the theory over the evidence, I gathered data directly: (a) a probe script issuing the same query two ways, and (b) a scripted query against the real seeded database (`instance/mixtape.db`, created via `python seed_data.py`). Both environments agreed.

**How I found the root cause.** I read `services/search_service.py :: search_songs` and noticed the query did `query(Song).outerjoin(song_tags, ...)` but selected no `song_tags` columns and filtered only on `Song.title`/`Song.artist`. The probe measured two things: `query(Song.id, song_tags.c.tag_id)...` (raw columns) returned **3 rows** for the 3-tag song — the join genuinely multiplies rows — but `query(Song)...` returned **1** object. That contrast was the decisive moment: the duplication mechanism is real at the SQL level, yet the ORM collapses it before it reaches the caller.

**The root cause (of the *non*-bug).** The `outerjoin(song_tags)` is dead code: it produces N raw rows for a song with N tags, but because the query requests the `Song` *entity* (not raw columns), SQLAlchemy's identity map deduplicates results by primary key, so `.all()` yields exactly one `Song` per id. Tags are not sourced from this join at all — they load via the `Song.tags` relationship inside `to_dict()`. So the reported duplicate never occurs on this code; the join only makes the query *look* buggy.

**My fix and side-effect check.** Since there was no functional defect, this is a `refactor:`, not a `fix:`. I removed the `.outerjoin(...)` and the now-unused `Tag`/`song_tags` imports, leaving `query(Song).filter(or_(title ilike, artist ilike)).all()`. This is safe because the join contributed nothing to output. Side-effect check: ran the full suite — `test_search.py` 5/5 and `test_streaks.py` 5/5 pass; the 2 failures that remain are in `test_playlists.py` (Issue #5, pre-existing and unrelated). Commit `354eb62`.

## Issue #5 — "The last song in a playlist never shows up"

**How I reproduced it.** `tests/test_playlists.py` seeds a playlist with 5 songs (`Track 1`..`Track 5`, positions 1–5). Running `pytest tests/test_playlists.py -v` from the project root, two tests failed: `test_playlist_returns_all_songs` asserted `len(songs) == 5` but got 4, and `test_playlist_returns_songs_in_order` asserted the titles were `["Track 1"..."Track 5"]` but the actual list was missing the last item — pytest reported `Right contains one more item: 'Track 5'`. So the function was dropping exactly the final song of the ordered list.

**How I found the root cause.** I opened `services/playlist_service.py :: get_playlist_songs` and read it end to end. The query is correct — it joins `playlist_entries`, filters by playlist, and orders by `playlist_entries.position` ascending, so `songs` is the full list in the right order. The bug had to be after the query, in what happened to that list before returning. The final line was `return [song.to_dict() for song in songs[:-1]]`. That was the moment it was clear — the query was sound; the return slice was mangling the result.

**The root cause.** The return statement sliced the ordered list with `songs[:-1]`. In Python, `list[:-1]` means "every element except the last," so the highest-position song (Track 5) was silently discarded on every call. Two visible symptoms — a count of 4 instead of 5, and a title list missing its last entry — were the same single cause: one off-by-one slice. (There was no ordering/reversal bug; the `order_by(asc(position))` already guaranteed order, so only the last-element drop was ever wrong.)

**My fix and side-effect check.** Changed the return to `return [song.to_dict() for song in songs]` — no slice, return the full ordered list, matching the docstring ("returns all songs in the playlist"). An intermediate attempt used `songs[::1]`, which happens to work because it is a whole-list no-op slice, but that reads as deliberate logic; the plain iteration is clearer. This fixes the root cause because the drop was caused solely by the `[:-1]` slice. Side-effect check: `tests/test_playlists.py` now 3/3 (count is 5, order is Track 1..5, empty playlist still returns `[]`), and the full suite is 13/13 green. Commit `dbb91ab`.

## Issue #4 — "I got notified when a friend added my song to a playlist but not when they rated it"

**How I reproduced it.** No test existed for notifications, so I wrote `tests/test_notifications.py`: seed a song shared by user A, have user B rate it, and assert user A receives one notification of type `song_rated`; plus a second test that a user rating their own song gets no notification. Running it from the project root, `test_rating_a_song_notifies_the_sharer` failed with `assert 0 == 1` (no notification row created), reproducing the bug; the self-rating test passed trivially (nothing was ever notified yet).

**How I found the root cause.** I opened `services/notification_service.py` and compared the two interaction handlers side by side. `add_to_playlist` (the feature that *does* notify) ends by calling `create_notification(user_id=song.shared_by, notification_type="song_added_to_playlist", ...)` guarded by `song.shared_by != added_by_user_id`. `rate_song`, directly below it, loads the song and rater, saves the `Rating`, commits — and then returns. The working sibling function made the omission obvious: `rate_song` had no `create_notification` call at all.

**The root cause.** `rate_song` never created a notification. It persisted the rating and returned, so the song's sharer was never informed. This was a *missing step*, not incorrect logic — the notification half of the rating feature was simply absent, while the parallel playlist-add path had it.

**My fix and side-effect check.** After the `db.session.commit()` in `rate_song`, I added a `create_notification` call mirroring `add_to_playlist`: recipient `song.shared_by`, type `song_rated`, body naming the rater, song title, and score, guarded by `if song.shared_by != user_id` so users aren't notified about rating their own shared song. This addresses the root cause by supplying the missing notification step. Side-effect check: both new notification tests pass (2/2), and the full suite is 15/15 green, confirming rating persistence and the playlist-add notification path still work. Commit `330df62`.

---

## Summary of changes

| Issue | Verdict | Commit |
|-------|---------|--------|
| #1 streak resets on Sunday | fixed | `67beb7b` |
| #3 duplicate search results | investigated — not reproducible; dead-code refactor | `354eb62` |
| #4 no notification on rating | fixed | `330df62` |
| #5 last playlist song missing | fixed | `dbb91ab` |

Three genuine bug fixes (#1, #4, #5) plus one investigation that found no defect and removed the misleading dead code (#3). Every fix followed the same discipline: reproduce with a failing test (RED) → confirm the specific root cause → make the smallest change at the source → re-verify the fix and the full suite (GREEN) before committing. Full suite: 15/15 passing.
