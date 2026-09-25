"""Strava webhook events: the way Strava tells us an athlete revoked access or changed/deleted an activity, so
what we hold stays in step with Strava (the API policy wants revocations honoured and Strava-side changes
reflected promptly - see README "Strava API compliance").

Strava's events are *unsigned*: anyone who learns the callback URL could POST one. So nothing here trusts an
event on its own - anything that would delete data is checked against Strava first:

  * "athlete deauthorised" -> try to refresh that athlete's token. If Strava still honours it, the event was
    bogus and is ignored. If Strava refuses (or can't be asked), we delete: the deletion obligation is the one
    that has a deadline, and a wrongly-deleted connection is only a re-connect away.
  * "activity deleted/updated" -> re-fetch that one activity. Still there: refresh our copy. Gone (404/403):
    delete ours. Any other answer (rate limit, outage): do nothing - a forged event must not be able to cost
    someone history, and the next event or sync catches up.

Only ever acts on activities/athletes we actually hold, never on the demo account.
"""
import hmac
import logging
import os

import httpx

from . import strava
from .db import connect

log = logging.getLogger("uvicorn.error")


def verify_token():
    return os.environ.get("STRAVA_WEBHOOK_VERIFY_TOKEN", "").strip()


def enabled():
    """Off unless STRAVA_WEBHOOK_VERIFY_TOKEN is set - the routes then don't exist as far as anyone can tell."""
    return bool(verify_token())


def challenge_response(mode, challenge, token):
    """The JSON Strava wants echoed when it validates the callback URL, or None if this isn't a genuine
    validation request (wrong mode, or the verify token isn't ours)."""
    if not enabled() or mode != "subscribe" or not challenge:
        return None
    if not hmac.compare_digest((token or "").encode(), verify_token().encode()):
        return None
    return {"hub.challenge": challenge}


def _holders(conn, athlete_id):
    """user_ids connected to this Strava athlete - never the demo account (its athlete link is fake)."""
    return [r["user_id"] for r in conn.execute(
        "SELECT user_id FROM strava_auth WHERE athlete_id = ? AND NOT EXISTS "
        "(SELECT 1 FROM users u WHERE u.id = strava_auth.user_id AND u.is_demo = 1)", (athlete_id,))]


def process_event(event, http=None):
    """Handle one Strava event payload. Runs after the HTTP response has already been sent (Strava wants a 200
    inside two seconds and won't wait for us to call back out to it)."""
    if not isinstance(event, dict):
        return
    expected_sub = os.environ.get("STRAVA_WEBHOOK_SUBSCRIPTION_ID", "").strip()
    if expected_sub and str(event.get("subscription_id")) != expected_sub:
        log.warning("Strava webhook event for an unexpected subscription ignored")
        return
    owner, kind, aspect = event.get("owner_id"), event.get("object_type"), event.get("aspect_type")
    updates = event.get("updates") or {}
    own_client = http is None
    client = http or strava.make_client()
    try:
        with connect() as conn:
            for uid in _holders(conn, owner):
                if kind == "athlete" and str(updates.get("authorized")).lower() == "false":
                    _handle_deauthorisation(conn, client, uid)
                elif kind == "activity" and aspect in ("update", "delete"):
                    _reconcile_activity(conn, client, uid, event.get("object_id"))
    except Exception:
        log.exception("Strava webhook event failed")
    finally:
        if own_client:
            client.close()


def _handle_deauthorisation(conn, http, user_id):
    try:
        strava.access_token(conn, http, user_id, force_refresh=True)
    except (strava.StravaError, httpx.HTTPError):
        n = strava.delete_strava_data(conn, user_id)
        log.info("Strava access revoked for user %s - deleted their Strava data (%d activities)", user_id, n)
        return
    log.warning("Strava reported a deauthorisation for user %s but their token still works - ignored", user_id)


def _reconcile_activity(conn, http, user_id, activity_id):
    if not isinstance(activity_id, int):
        return
    if not conn.execute("SELECT 1 FROM activities WHERE user_id = ? AND id = ?", (user_id, activity_id)).fetchone():
        return   # not one we hold - nothing to keep in step
    try:
        detail = strava._get(conn, http, user_id, "/activities/%d" % activity_id)
    except strava.StravaError as e:
        if e.status in (403, 404):
            conn.execute("DELETE FROM activities WHERE user_id = ? AND id = ?", (user_id, activity_id))
        return   # anything else is "can't tell" - see the module docstring
    except httpx.HTTPError:
        return
    conn.execute(strava.UPSERT, strava.activity_row(detail, user_id))
