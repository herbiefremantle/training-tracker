import os
from datetime import date


def today():
    """Today's date. FITNESS_TODAY=YYYY-MM-DD pins it, for demos and screenshots."""
    pinned = os.environ.get("FITNESS_TODAY")
    return date.fromisoformat(pinned) if pinned else date.today()
