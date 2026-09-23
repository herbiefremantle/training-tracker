# Training Tracker

Personal training tracker: Strava activities + your training plan, matched session by session.
FastAPI + SQLite + a plain HTML/JS frontend (Chart.js from a CDN). Multi-account (invite-only) and deployable,
but started as a single-user localhost app - see "Login" below for how accounts work.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # runtime + pytest (the Docker image uses requirements.txt only)
cp .env.example .env        # then fill in the two Strava values
```

**Strava API app** — create one at <https://www.strava.com/settings/api>:
- *Authorization Callback Domain*: `localhost`
- Copy the Client ID and Client Secret into `.env`.

## Run

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://localhost:8000>, click **Connect with Strava**, approve, then **Sync Strava**.
The first sync pulls your full history; later syncs fetch from 14 days before your latest stored
activity (picks up edits, and removes activities you deleted on Strava).

If you use a different port, set `STRAVA_REDIRECT_URI` in `.env` to match.
Tokens and data live in `training.db` (next to the code). Keep `.env` and `training.db` private.

## Use it on your phone (home Wi-Fi)

```bash
./run_lan.sh
```

It prints two addresses; open either in your phone's browser while on the **same Wi-Fi**
(`http://192.168.x.x:8000`, or `http://<your-mac>.local:8000`, which survives IP changes).
In Safari, *Share > Add to Home Screen* makes it a one-tap icon.

- The laptop must be **on, awake and running the script**. The script uses `caffeinate -i` to stop idle sleep,
  but closing the lid still sleeps the Mac. When the laptop is off, the app is unreachable.
- There is **no login**: anyone on the network can open it, sync, or edit/clear the plan. Use it on your home network only.
- **Connect with Strava from the laptop** (the login redirects to `localhost`, which on a phone means the phone itself).
  Once connected, the phone can view everything and press **Sync Strava**.
- macOS may ask "accept incoming network connections?" the first time: click **Allow**.
- If the phone can't connect: check it isn't on a guest network (they often isolate devices) and that no VPN is on.
- The address check accepts `localhost`, private-network IPs and `.local` names only, so a website can't trick your browser
  into talking to the app through a public domain name.

The plain `uvicorn ... --host 127.0.0.1` command above still listens on this computer only.

## Login

Each person gets their own account: their own username/password, their own Strava connection, their own plan and
dashboard - completely separate from everyone else's. Set `APP_PASSWORD` and the app creates the **first** account
(an admin) from it the next time it starts; every account after that comes from an **invite link** the admin creates.
There's no public sign-up page and no "forgot password" - both by design, for a small, invite-only app.

- **Unset locally, there's no login at all** (the default) - the app behaves exactly as before, one shared local
  dataset. Set `APP_PASSWORD` in `.env` (and restart) to turn login on locally too, e.g. to protect Wi-Fi mode.
- **The admin account:** `APP_PASSWORD` (8+ characters; 12+ recommended) becomes that account's password, and
  `ADMIN_USERNAME` (default `pete`) its username. This only matters the *first* time the app starts with no
  accounts yet - after that, `APP_PASSWORD` is inert, so it can be removed from `.env`/Railway if you want.
- **Inviting someone:** log in as the admin and go to the **Admin** page (only admins see this nav link), then
  click **Invite a friend**. It creates a one-time link, valid for 7 days, shown with a **Copy link** button -
  send it however you like (text, WhatsApp, email). They open it, fill in their name, email, a username and a
  password, and they're in - registering logs them straight in, same as a normal login.
- **Accounts fill up:** capped at `MAX_USERS` (default 10, matching Strava's self-serve "10 athletes" API app
  capacity - see "Deploy to Railway"). Past the cap, **Invite a friend** and redeeming an existing link both refuse.
- **Who's signed up:** the **Admin** page lists every account - name, username, email, is-admin, when they signed
  up, and when they last logged in - plus any invite links still waiting to be used. The bootstrap admin account
  (created from `APP_PASSWORD`) has no name/email on record, since nothing ever asked for one; that's expected,
  not a bug.
- **Sessions:** a signed cookie keeps you logged in for **30 days** (HttpOnly, SameSite=Lax, Secure over HTTPS).
  **Log out** is in the top bar. Set a new `SESSION_SECRET` to sign every device out at once (e.g. if you suspect
  a cookie leaked); changing your own password does not affect anyone else's session.
- **Guessing:** after 10 wrong login attempts (any account) in 10 minutes, logins are refused until the window
  passes; sessions that already exist aren't affected.

## Deploy to Railway

Files: `Dockerfile`, `railway.json`, `.dockerignore`, pinned `requirements.txt`. The app reads everything from environment
variables, so nothing secret is in the repo or the image.

**Before the first deploy**
1. **Choose a password** (8+ characters; 12+ recommended) for your own (admin) account, and generate a session
   secret - keep both in a password manager:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(24))"    # -> APP_PASSWORD
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"    # -> SESSION_SECRET
   ```
   The Docker image sets `REQUIRE_AUTH=1`, so **it refuses to start without both**: a forgotten variable fails the deploy
   instead of publishing an app nobody can lock, or one that can't tell one session from another.
2. Put the code in a GitHub repo (or use `railway up`). `.env` and `*.db` are git-ignored; check `git status` before pushing.

**In Railway**
1. New project, deploy from the repo. It builds the `Dockerfile`.
2. **Add a volume** to the service with mount path **`/data`**. The database (`/data/app.db`, including everyone's Strava
   tokens) lives there; without it everything is wiped on each deploy. The startup log says `no volume is mounted at /data`
   if it's missing.
3. **Variables:** `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `APP_PASSWORD`, `SESSION_SECRET`. Optionally
   `ADMIN_USERNAME` (your login name; default `pete`) and `MAX_USERS` (default 10 - see "Login"). Don't set `PORT`
   (Railway does), `HOST`, `FITNESS_DB`, `REQUIRE_AUTH` or `FITNESS_TODAY`.
4. **Settings > Networking > Generate Domain.** Railway then provides `RAILWAY_PUBLIC_DOMAIN`, which the app uses to accept that
   host and to build the Strava redirect URI (`https://<domain>/auth/callback`). If it isn't picked up, redeploy once, or set
   `STRAVA_REDIRECT_URI` yourself. For a custom domain add it to `ALLOWED_HOSTS` (comma-separated) too.
5. **Strava:** at strava.com/settings/api set *Authorization Callback Domain* to the Railway domain (no `https://`, no path).
   Strava allows one domain per app, so this replaces `localhost`. If you plan to invite others, also use the **self-serve
   upgrade** in that same dashboard to raise your app's athlete capacity from 1 to 10 - see "Inviting people" below.
6. Open the domain, log in with your password, **Connect with Strava**, then **Sync Strava** and re-import your plan. Your local `training.db` isn't uploaded.

**After it's up:** trigger a redeploy and confirm you're still connected with the same activity count. That proves the volume works.
Keep it to **one replica and one worker** (`railway.json` sets `numReplicas: 1`): SQLite, the sync locks, the OAuth state and
the login lockout are all in-process and assume it.

**Migrating existing data:** if you're redeploying over an already-running single-user version of this app (i.e. this exact
change), the very next startup automatically creates your admin account from `APP_PASSWORD` and moves your existing Strava
connection, activities and plan onto it - nothing to do by hand. It's covered by `tests/test_migration.py` against a copy of
the old schema, including a startup crashing partway through and being retried. Still, it's a real migration of real data on
its first run: check the deploy log for `Login: required` (not an error) and confirm your activity/plan counts match on `/api/status`
straight after.

**Inviting people:** each invite lets one person create their own account with their own Strava connection - see "Login"
above for how, and note that everyone connects through *your* Strava API app (one `STRAVA_CLIENT_ID`), so Strava's
per-app rate limit and athlete-capacity cap are shared across every account, not per person.

## Plan format

Paste from a spreadsheet or CSV, or choose a file, on the **Plan** page. Columns:
`date, session type, sport, planned distance, planned duration, notes`. A header row is optional
(with one, columns can be in any order). **Preview** shows exactly what will be imported and any bad rows.

| Field | Accepted |
|---|---|
| date | `2026-09-21`, `21/09/2026` (or month-first via the dropdown), `21 Sep 2026`, `Mon 21 Sep 2026` |
| sport | Run, Trail run, Ride/Bike, Swim, Hike/Walk, Gym/Strength, Yoga, **Rest** (rest days are never "missed") — blank is inferred from the session type ("Long run" → run) |
| distance | `10`, `10km`, `6 mi`, `800m`. A number with no unit uses whichever of **km / mi** is selected at the top of the page; a unit in the cell or the column header (`Distance (mi)`) always wins |
| duration | `90` (minutes), `1:30` (h:mm), `1:30:00`, `1h30`, `1.5h` |

Re-importing replaces only the dates present in the upload (or the whole plan, if you choose that).

## How it works

**Matching** (`app/matching.py`): key is *(local date, sport group)*. Run/TrailRun/VirtualRun are one group;
Ride/MountainBikeRide/GravelRide/… another; Walk+Hike; WeightTraining/Workout/Crossfit = gym, etc.
A planned run and a planned gym session on the same day are matched independently. If a day has several
sessions of the *same* sport, they're paired one-to-one, best fit first (by planned vs actual distance/duration),
so an activity can never satisfy two sessions.

**Status colours.** A matched session is judged on *moving time vs planned duration*, with a **±10 minute** leeway
(`DURATION_TOLERANCE_MIN` in `app/matching.py`):
**Done** (green) = within 10 min, e.g. 56 min against 50 planned; **Over** (orange ▲) = more than 10 min longer;
**Under** (orange ▼) = more than 10 min shorter. Over and Under still count as completed - they're flagged, not failed,
and show how many minutes off. **Missed** (red) = past, nothing matched; **Extra** (amber) = an activity with no planned
session; **Today** / **Upcoming** are grey. Sessions with no planned duration can't be judged and show as Done.
"Local date" is Strava's `start_date_local`, so a 23:30 run belongs to that day.

**Week card.** ‹ › step through any past or future week (Mon-Sun); **This week** jumps back. The KPI cards stay on the current week: **Distance this week** and **Time this week** show what you've logged
so far (Mon-Sun, every sport, planned or not), with the week's full planned total underneath in small text.

**Calendar.** A month grid with one dot per planned session (green ✓, orange ▲/▼, red ✕, amber + for extras, grey ring for
still to do). Click a day to open its week in the week card.

**Drill-down** (Trends & drill-down section): **Year** shows a bar per week, **Month** a bar per day, **Week** the seven
days with every activity listed (distance, time, elevation, pace, HR, load). Click a bar, a table row, a month chip or a
breadcrumb to move between levels; ‹ › step to the previous/next period. The sport dropdown filters everything in this
section (choose *All sports* to see gym sessions and rides too).

**km / mi switch** (top right, remembered in your browser) converts distances, pace (min/mi) and speed (mph).
Data is stored in km either way. Elevation stays in metres.

**Load** (`app/metrics.py`): per activity, `suffer_score` if Strava has one, otherwise
`duration_min × avg_hr / 100`; no score and no HR = 0. Charts show the 7-day and 28-day *average daily* load
(rest days count as 0). **Load ratio** = 7-day average ÷ 28-day average, flagged **high > 1.5**, **low < 0.8**.

**Trends**: distance, elevation gain and time are three separate aligned charts (not dual-axis). Pace/speed shows
per-activity average with a distance-weighted weekly (year view) or daily (month/week view) average; toggle pace ↔ speed.

## Things to know

- **Suffer score may not come from the list endpoint.** Strava documents `/athlete/activities` as returning
  summary activities, and I believe `suffer_score` is only reliably in the per-activity detail response (and only
  for Strava subscribers). So after listing, each sync looks up the detail for up to 40 activities that have heart
  rate but no score, newest first, once each. Backfilling a long history takes several syncs; the sync message
  tells you how many remain. If you have no Relative Effort, everything simply uses the HR formula.
- **Strava's rate limit is per app, shared by every account.** With just your own account it's generously within
  the default limit; once several people are syncing through the same Strava API app (see "Inviting people"),
  a burst of first-time full-history syncs could occasionally hit it - the sync banner explains and says to retry.
- **The two load sources aren't on the same scale** (suffer score is Strava's Relative Effort; the fallback is
  minutes × HR/100). If the last 28 days mix them, the dashboard says so — treat the ratio as approximate.
- Chart.js is loaded from jsDelivr, so the dashboard needs internet (Strava sync does anyway).

## Try it without Strava

```bash
FITNESS_DB=demo.db .venv/bin/python seed_demo.py                       # fake activities + demo_plan.csv
FITNESS_DB=demo.db .venv/bin/uvicorn app.main:app --port 8000          # then import demo_plan.csv on the Plan page
```
`FITNESS_TODAY=2026-09-26` pins "today" (a Saturday, so every status colour shows). The seeder refuses to run
without `FITNESS_DB`, so it can't touch your real data.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests
```
Covers accounts and invites (registration, admin-only invites, the max-accounts cap, per-account data isolation),
login (forged/expired cookies, open redirects, lockout, fail-closed startup), the one-time migration from the old
single-user database (against a frozen copy of that schema, including a crash-and-retry case), deployment config,
plan parsing, matching (incl. same-day multi-sport and the ±10 min rule), load/ratio/flags, the week / calendar /
drill-down endpoints, token refresh and rotation, pagination, incremental sync/deletion, and the OAuth callback.
