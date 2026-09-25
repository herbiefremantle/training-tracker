"""Strava OAuth + activity sync.

Every function that talks to Strava takes an httpx.Client so tests can inject a mock transport, and a user_id so
each account's tokens and activities stay separate - everyone connects through the same STRAVA_CLIENT_ID/SECRET
(one Strava API app), each getting their own stored tokens under their own account.
"""
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from . import sports
from .db import get_meta, set_meta

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
REVOKE_URL = "https://www.strava.com/oauth/revoke"
API = "https://www.strava.com/api/v3"
SUBSCRIPTIONS_URL = API + "/push_subscriptions"

PAGE_SIZE = 200
RESYNC_OVERLAP_S = 14 * 86400   # re-fetch the last 2 weeks each sync to pick up edits/deletes
ENRICH_PER_SYNC = 40            # detail lookups per sync (Strava allows ~100 requests / 15 min per app, all accounts)


class StravaError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status   # the HTTP status Strava answered with, when there was one - lets callers tell
                               # "Strava says this access is gone" (400/401) from "Strava is having a bad day"


def make_client():
    return httpx.Client(timeout=30)


def client_id():
    return os.environ.get("STRAVA_CLIENT_ID", "").strip()


def client_secret():
    return os.environ.get("STRAVA_CLIENT_SECRET", "").strip()


def is_configured():
    return bool(client_id() and client_secret())


def redirect_uri():
    """STRAVA_REDIRECT_URI if set; else the public Railway domain (https); else localhost for local use."""
    explicit = os.environ.get("STRAVA_REDIRECT_URI", "").strip()
    if explicit:
        return explicit
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if domain:
        return "https://%s/auth/callback" % domain
    return "http://localhost:8000/auth/callback"


def authorize_url(state):
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": client_id(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": "read,activity:read_all",
        "state": state,
    })


# ---- tokens ---------------------------------------------------------------------------------

def _token_request(http, payload):
    r = http.post(TOKEN_URL, data={"client_id": client_id(), "client_secret": client_secret(), **payload})
    if r.status_code != 200:
        raise StravaError("Strava rejected the token request (%s). If you revoked access, "
                          "reconnect with Strava." % r.status_code, status=r.status_code)
    return r.json()


def _save_tokens(conn, user_id, tok, scope=None):
    athlete = tok.get("athlete") or {}
    name = " ".join(x for x in (athlete.get("firstname"), athlete.get("lastname")) if x) or None
    old = conn.execute("SELECT * FROM strava_auth WHERE user_id = ?", (user_id,)).fetchone()
    conn.execute(
        """INSERT INTO strava_auth (user_id, athlete_id, athlete_name, access_token, refresh_token, expires_at, scope)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             athlete_id = COALESCE(excluded.athlete_id, athlete_id),
             athlete_name = COALESCE(excluded.athlete_name, athlete_name),
             access_token = excluded.access_token,
             refresh_token = excluded.refresh_token,   -- Strava may rotate this on every refresh
             expires_at = excluded.expires_at,
             scope = COALESCE(excluded.scope, scope)""",
        (user_id, athlete.get("id"), name, tok["access_token"], tok["refresh_token"], tok["expires_at"],
         scope if scope is not None else (old["scope"] if old else None)),
    )
    conn.commit()


def exchange_code(conn, http, user_id, code, scope):
    tok = _token_request(http, {"code": code, "grant_type": "authorization_code"})
    _save_tokens(conn, user_id, tok, scope=scope)


def access_token(conn, http, user_id, force_refresh=False):
    row = conn.execute("SELECT * FROM strava_auth WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        raise StravaError("Not connected to Strava yet.")
    if force_refresh or row["expires_at"] - 60 <= time.time():
        tok = _token_request(http, {"grant_type": "refresh_token", "refresh_token": row["refresh_token"]})
        _save_tokens(conn, user_id, tok)
        return tok["access_token"]
    return row["access_token"]


def _get(conn, http, user_id, path, params=None):
    """GET against the Strava API; refreshes once on 401."""
    for attempt in (0, 1):
        token = access_token(conn, http, user_id, force_refresh=bool(attempt))
        r = http.get(API + path, params=params, headers={"Authorization": "Bearer " + token})
        if r.status_code == 401 and attempt == 0:
            continue
        break
    if r.status_code == 429:
        raise StravaError("Strava rate limit hit (shared by everyone using this app - 200 requests / 15 min, "
                          "2,000 / day). Try again later.", status=429)
    if r.status_code == 401:
        raise StravaError("Strava refused the stored credentials. Reconnect with Strava.", status=401)
    if r.status_code != 200:
        raise StravaError("Strava API error %s on %s" % (r.status_code, path), status=r.status_code)
    return r.json()


# ---- disconnecting: revoke at Strava, delete what we synced ---------------------------------

def revoke(conn, http, user_id):
    """Ask Strava to invalidate this account's tokens (POST /oauth/revoke, HTTP Basic with the app's own
    credentials - revoking the refresh token also kills its access tokens). Returns True once Strava confirms,
    False if it couldn't be done (not configured, network trouble, Strava said no), None if there was nothing
    to revoke. Never raises: callers use this on the way to deleting local data, which must go ahead either
    way - the honest thing is to report the outcome, not to refuse to delete because Strava was unreachable."""
    row = conn.execute("SELECT refresh_token FROM strava_auth WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return None
    if not is_configured():
        return False
    try:
        r = http.post(REVOKE_URL, data={"token": row["refresh_token"]}, auth=(client_id(), client_secret()))
    except httpx.HTTPError:
        return False
    return r.status_code == 200


def delete_strava_data(conn, user_id):
    """Everything we hold that came from Strava for this account: the synced activities, the stored tokens and
    athlete link, and the last-sync marker. Their training plan is their own input, not Strava data, so it
    stays. Returns how many activities were removed."""
    n = conn.execute("DELETE FROM activities WHERE user_id = ?", (user_id,)).rowcount
    conn.execute("DELETE FROM strava_auth WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM meta WHERE user_id = ? AND key = 'last_sync'", (user_id,))
    return n


# ---- webhook subscription (one per app; covers every athlete who has authorised it) --------------

def _subscription_call(http, method, url, **kw):
    try:
        r = http.request(method, url, **kw)
    except httpx.HTTPError as e:
        raise StravaError("Couldn't reach Strava: %s" % e)
    if r.status_code not in (200, 201, 204):
        raise StravaError("Strava answered %s: %s" % (r.status_code, r.text[:300]), status=r.status_code)
    return r.json() if r.content else None


def create_subscription(http, callback_url, verify_token):
    """Strava immediately GETs callback_url to validate it (see app/webhook.py), so the app must already be
    deployed with the same verify token before this is called."""
    return _subscription_call(http, "POST", SUBSCRIPTIONS_URL, data={
        "client_id": client_id(), "client_secret": client_secret(),
        "callback_url": callback_url, "verify_token": verify_token})


def view_subscription(http):
    return _subscription_call(http, "GET", SUBSCRIPTIONS_URL,
                              params={"client_id": client_id(), "client_secret": client_secret()})


def delete_subscription(http, subscription_id):
    return _subscription_call(http, "DELETE", "%s/%s" % (SUBSCRIPTIONS_URL, subscription_id),
                              params={"client_id": client_id(), "client_secret": client_secret()})


# ---- activities -----------------------------------------------------------------------------

def _epoch(iso_utc):
    return int(datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())


def activity_row(a, user_id):
    sport_type = a.get("sport_type") or a.get("type") or ""
    local = a.get("start_date_local") or a["start_date"]
    return {
        "id": a["id"],
        "user_id": user_id,
        # start_date_local is wall-clock time at the activity, labelled 'Z' - take the date as-is
        "date": local[:10],
        "start_epoch": _epoch(a["start_date"]),
        "name": a.get("name"),
        "sport_type": sport_type,
        "sport_group": sports.group_from_strava(sport_type),
        "distance": a.get("distance"),
        "moving_time": a.get("moving_time"),
        "average_heartrate": a.get("average_heartrate"),
        "max_heartrate": a.get("max_heartrate"),
        "average_speed": a.get("average_speed"),
        "max_speed": a.get("max_speed"),
        "total_elevation_gain": a.get("total_elevation_gain"),
        "suffer_score": a.get("suffer_score"),
        "workout_type": a.get("workout_type"),
    }


UPSERT = """
INSERT INTO activities (id, user_id, date, start_epoch, name, sport_type, sport_group, distance, moving_time,
    average_heartrate, max_heartrate, average_speed, max_speed, total_elevation_gain, suffer_score, workout_type)
VALUES (:id, :user_id, :date, :start_epoch, :name, :sport_type, :sport_group, :distance, :moving_time,
    :average_heartrate, :max_heartrate, :average_speed, :max_speed, :total_elevation_gain, :suffer_score, :workout_type)
ON CONFLICT(user_id, id) DO UPDATE SET
    date = excluded.date, start_epoch = excluded.start_epoch, name = excluded.name,
    sport_type = excluded.sport_type, sport_group = excluded.sport_group,
    distance = excluded.distance, moving_time = excluded.moving_time,
    average_heartrate = excluded.average_heartrate, max_heartrate = excluded.max_heartrate,
    average_speed = excluded.average_speed, max_speed = excluded.max_speed,
    total_elevation_gain = excluded.total_elevation_gain,
    -- the list endpoint may omit suffer_score; don't wipe one fetched from the detail endpoint
    suffer_score = COALESCE(excluded.suffer_score, activities.suffer_score),
    workout_type = excluded.workout_type
"""


def _fetch_all(conn, http, user_id, after):
    out, page = [], 1
    while True:
        batch = _get(conn, http, user_id, "/athlete/activities",
                     {"after": after, "per_page": PAGE_SIZE, "page": page})
        out.extend(batch)
        if len(batch) < PAGE_SIZE:
            return out
        page += 1


def _enrich_suffer_scores(conn, http, user_id):
    """Best-effort: ask the detail endpoint for suffer_score where the list didn't supply one.

    Newest first, capped per sync so we stay inside Strava's rate limit; each activity is only
    ever looked up once (detail_checked), so athletes without Relative Effort don't burn requests.
    Returns (looked_up, remaining)."""
    todo = conn.execute(
        "SELECT id FROM activities WHERE user_id = ? AND suffer_score IS NULL AND average_heartrate IS NOT NULL "
        "AND detail_checked = 0 ORDER BY start_epoch DESC", (user_id,)).fetchall()
    done = 0
    for row in todo[:ENRICH_PER_SYNC]:
        try:
            detail = _get(conn, http, user_id, "/activities/%d" % row["id"])
        except StravaError:
            break  # rate limited or transient - the rest waits for the next sync
        conn.execute("UPDATE activities SET suffer_score = COALESCE(?, suffer_score), detail_checked = 1 "
                     "WHERE user_id = ? AND id = ?", (detail.get("suffer_score"), user_id, row["id"]))
        conn.commit()
        done += 1
    return done, len(todo) - done


def sync(conn, http, user_id):
    latest = conn.execute("SELECT MAX(start_epoch) AS m FROM activities WHERE user_id = ?", (user_id,)).fetchone()["m"]
    after = max(0, latest - RESYNC_OVERLAP_S) if latest else 0

    fetched = _fetch_all(conn, http, user_id, after)   # raises before we touch the DB if anything fails

    existing = {r["id"] for r in conn.execute(
        "SELECT id FROM activities WHERE user_id = ? AND start_epoch > ?", (user_id, after))}
    ids = set()
    for a in fetched:
        conn.execute(UPSERT, activity_row(a, user_id))
        ids.add(a["id"])
    # anything in the re-fetched window that Strava no longer returns was deleted (or made private)
    gone = existing - ids
    for aid in gone:
        conn.execute("DELETE FROM activities WHERE user_id = ? AND id = ?", (user_id, aid))
    conn.commit()

    looked_up, remaining = _enrich_suffer_scores(conn, http, user_id)
    set_meta(conn, user_id, "last_sync", datetime.now().isoformat(timespec="seconds"))
    conn.commit()
    return {
        "fetched": len(fetched),
        "new": len(ids - existing),
        "removed": len(gone),
        "suffer_lookups": looked_up,
        "suffer_lookups_remaining": remaining,
        "full_history": after == 0,
    }
