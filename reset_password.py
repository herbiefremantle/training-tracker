"""Set (or reset) an account's password directly in the database - for when there's no "forgot password" flow.

Run it yourself; it prompts for the new password with getpass, so it's never shown on screen, never typed into
chat, and never seen by anyone reading over your shoulder or looking at your terminal history.

    FITNESS_DB=training.db .venv/bin/python reset_password.py pete
"""
import getpass
import sys

from app import db, users

if len(sys.argv) != 2:
    sys.exit("Usage: FITNESS_DB=training.db .venv/bin/python reset_password.py <username>")
username = sys.argv[1]

db.init_db()
with db.connect() as conn:
    row = users.get_by_username(conn, username)
    if not row:
        sys.exit("No account named %r. Existing accounts: %s"
                 % (username, ", ".join(r["username"] for r in users.list_accounts(conn)) or "(none)"))

    pw1 = getpass.getpass("New password for '%s' (%d+ characters): " % (row["username"], users.MIN_PASSWORD_LENGTH))
    if len(pw1) < users.MIN_PASSWORD_LENGTH:
        sys.exit("Too short - use at least %d characters." % users.MIN_PASSWORD_LENGTH)
    pw2 = getpass.getpass("Confirm: ")
    if pw1 != pw2:
        sys.exit("Passwords didn't match - nothing changed.")

    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (users.hash_password(pw1), row["id"]))

print("Password updated for '%s'. Sessions (browser cookies) aren't tied to the password, so anywhere already "
     "logged in - yours or anyone else's - stays logged in until its cookie expires (30 days) or SESSION_SECRET "
     "changes; this only takes effect the next time someone logs in fresh." % row["username"])
