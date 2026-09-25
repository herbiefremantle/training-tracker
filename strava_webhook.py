"""Register (or inspect / remove) this app's Strava webhook subscription, so Strava tells the app when an
athlete revokes access or changes/deletes an activity (see app/webhook.py and README "Strava API compliance").

One subscription per Strava API app, covering every athlete who has authorised it. Strava validates the
callback URL the moment you create the subscription, by calling it - so the live app must ALREADY be deployed
with STRAVA_WEBHOOK_VERIFY_TOKEN set to the same value you use here. Order of operations:

  1. Pick a long random verify token:   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  2. Set STRAVA_WEBHOOK_VERIFY_TOKEN to it on Railway; let it redeploy.
  3. Put the same value in your local .env (along with STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET).
  4. Run:  .venv/bin/python strava_webhook.py create https://<your-domain>/strava/webhook
  5. Optional hardening: note the subscription id it prints and set STRAVA_WEBHOOK_SUBSCRIPTION_ID on Railway,
     so the app ignores events claiming to belong to any other subscription.

    .venv/bin/python strava_webhook.py view
    .venv/bin/python strava_webhook.py delete <subscription-id>
"""
import os
import sys

from dotenv import load_dotenv

from app import strava
from app.db import ROOT

load_dotenv(ROOT / ".env", override=True)

USAGE = "Usage: strava_webhook.py create <https-callback-url> | view | delete <subscription-id>"
if len(sys.argv) < 2 or sys.argv[1] not in ("create", "view", "delete"):
    sys.exit(USAGE)
if not strava.is_configured():
    sys.exit("Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env first.")

cmd = sys.argv[1]
try:
    with strava.make_client() as http:
        if cmd == "create":
            token = os.environ.get("STRAVA_WEBHOOK_VERIFY_TOKEN", "").strip()
            if len(sys.argv) != 3 or not sys.argv[2].startswith("https://") or not token:
                sys.exit(USAGE + "\n(needs an https:// URL, and STRAVA_WEBHOOK_VERIFY_TOKEN in .env)")
            print("Created:", strava.create_subscription(http, sys.argv[2], token))
        elif cmd == "view":
            print(strava.view_subscription(http) or "No subscription.")
        else:
            if len(sys.argv) != 3:
                sys.exit(USAGE)
            strava.delete_subscription(http, sys.argv[2])
            print("Deleted subscription", sys.argv[2])
except strava.StravaError as e:
    sys.exit("Failed: %s" % e)
