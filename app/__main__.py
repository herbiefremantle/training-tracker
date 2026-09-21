"""`python -m app` - start the server on the port the platform provides.

Railway (and most hosts) hand the app its port in $PORT; binding anything else means the healthcheck and
public URL never reach it. HOST defaults to 127.0.0.1 (this computer only); the Docker image sets HOST=0.0.0.0.
"""
import os

import uvicorn


def server_config():
    host = os.environ.get("HOST", "127.0.0.1")
    raw = os.environ.get("PORT", "8000")
    try:
        port = int(raw)
    except ValueError:
        raise SystemExit("PORT must be a number, got %r" % raw)
    return host, port


if __name__ == "__main__":
    host, port = server_config()
    # one process only: the OAuth state, the sync lock and SQLite all assume a single worker
    uvicorn.run("app.main:app", host=host, port=port)
