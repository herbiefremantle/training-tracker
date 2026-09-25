"""The privacy policy page (/privacy). Written to describe what this app actually does, and built from the
running configuration where the honest answer depends on it - e.g. it only says revocations on Strava's side are
picked up automatically if the Strava webhook is really switched on (app/webhook.py), rather than promising it
unconditionally.

PRIVACY_CONTACT_EMAIL and PRIVACY_OPERATOR_NAME identify who runs this deployment; a real, monitored contact
address is something Strava's API policy and UK GDPR both expect, so main.py logs a warning at startup when
the email is missing on a deployment that requires login. This text is a plain-English description, not legal
advice - have it read over before relying on it commercially.
"""
import html
import os

from . import auth, webhook

UPDATED = "25 September 2026"


def contact_email():
    return os.environ.get("PRIVACY_CONTACT_EMAIL", "").strip()


def operator_name():
    return os.environ.get("PRIVACY_OPERATOR_NAME", "").strip() or "the person who runs this app"


def _contact_html():
    email = contact_email()
    if email:
        e = html.escape(email, quote=True)
        return '<a href="mailto:%s">%s</a>' % (e, e)
    return "the administrator who gave you access to this app"


def privacy_html():
    who, contact = html.escape(operator_name()), _contact_html()
    revocation = (
        "If you revoke this app's access from Strava's own settings, Strava notifies us and we delete the Strava "
        "data we hold about you automatically."
        if webhook.enabled() else
        "If you revoke this app's access from Strava's own settings, we lose the ability to read your data; please "
        "also use <b>Disconnect Strava</b> on the Account page (or contact us) so that what we already hold is deleted."
    )
    return """<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy policy - Training Tracker</title>
%s
<link rel="stylesheet" href="/static/style.css"></head>
<body><main class="legal">
<p><a href="/">&larr; Back to Training Tracker</a></p>
<h1>Privacy policy</h1>
<p class="muted small">Last updated %s</p>

<p>Training Tracker compares your Strava activities with a training plan. This page explains what personal
data it holds about you, why, who else can see it, and how to get rid of it. It is run by %s, who is the
controller of your data. Questions or requests: %s.</p>

<h2>What we hold</h2>
<ul>
<li><b>Your account:</b> username, first and last name, email address, and your password (stored only as a salted
hash - never in a readable form), plus when you joined and were last active.</li>
<li><b>Your Strava data</b>, only if you connect Strava: your Strava athlete ID and name, and for each of your
activities - including ones you have marked private, because the app asks for Strava's <i>read</i> and
<i>activity:read_all</i> permissions - its name, date and start time, sport type, distance, moving time,
elevation gain, average and maximum heart rate and speed, Strava's effort score and workout type. We also keep
the access tokens Strava gives us, so the app can fetch your activities on your behalf.</li>
<li><b>Your training plan:</b> the sessions you type, paste, upload or choose from the ready-made plans (dates,
sport, distance, duration, notes).</li>
<li><b>In your browser:</b> one essential session cookie that keeps you logged in (30 days), and two
preferences (km or miles, and the colour theme) stored in your browser's local storage. Neither is used for
tracking.</li>
<li><b>Server logs:</b> our hosting provider keeps standard logs of requests to the site, which include IP
addresses. We add no analytics, advertising or tracking of our own.</li>
</ul>

<h2>Why, and on what basis</h2>
<p>To show you your own dashboard, week-by-week comparison and trends - the service you signed up for. We use
your <b>consent</b> to connect to Strava (you can withdraw it at any time by disconnecting), and it is
necessary to provide the account you asked for. We do not use your data for anything else.</p>

<h2>Who can see it</h2>
<ul>
<li><b>Only you</b> can view your activities and plan in the app. Nobody else's account can see them, and the app
never shows one person's Strava data to another.</li>
<li>The administrator can see the account list (name, username, email, when you were last active) to manage
access. The app gives them no way to browse your activities, but as the person running the service they have
technical access to its database.</li>
<li>We do not sell your data, use it for advertising or profiling, or use Strava data to train or run any AI or
machine-learning system.</li>
</ul>

<h2>Who processes it for us</h2>
<ul>
<li><b>Strava</b> - the source of your activity data, under your own agreement with Strava.</li>
<li><b>Railway</b> - our hosting provider, which stores the database. Railway is a US company, so your data may
be processed outside the UK and EEA.</li>
<li><b>jsDelivr</b> - a content network that serves the charting library to your browser; it sees your IP
address and browser details when you open the dashboard, but none of your account or training data.</li>
</ul>

<h2>How long we keep it</h2>
<p>Your account and plan are kept until you delete your account. Strava activities we have synced are kept for
as long as you stay connected, so your history and trends keep working. You can end that at any time:</p>
<ul>
<li><b>Disconnect Strava</b> (Account page) - revokes the app's access at Strava and immediately deletes your
synced activities, tokens and Strava link. Your plan and account stay.</li>
<li><b>Delete my account</b> (Account page) - does the above and removes everything else we hold about you.</li>
</ul>
<p>%s Deletion from the live database is immediate; if our hosting provider keeps backups, they age out on its
normal schedule. If you ask us to delete your data any other way, we will do so within 30 days.</p>

<h2>Your rights</h2>
<p>Under UK GDPR you can ask to see your data (the Account page has a <b>Download my data</b> button), have it
corrected, have it deleted, restrict or object to how it is used, or take it elsewhere. Use the Account page for
the self-service ones, or contact %s for anything else - we will reply within one month. You can also complain
to the Information Commissioner's Office at <a href="https://ico.org.uk">ico.org.uk</a>.</p>

<h2>Security</h2>
<p>The site is served over HTTPS, passwords are hashed, session cookies are HttpOnly, and logins are
rate-limited. No system is perfectly secure, but we keep what we hold to the minimum the app needs.</p>

<h2>Children</h2>
<p>Training Tracker is not intended for anyone under 16.</p>

<h2>The demo account</h2>
<p>If a demo login is offered, it contains only made-up sample data and is not connected to any real Strava
account.</p>

<h2>Changes</h2>
<p>If this policy changes, the date at the top of this page will change with it.</p>

<p class="muted small">Training Tracker is an independent app that uses the Strava API. It is not endorsed by or
affiliated with Strava.</p>
</main></body></html>""" % (auth._HEAD_EXTRA, UPDATED, who, contact, revocation, contact)
