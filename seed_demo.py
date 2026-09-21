"""Fill a *separate* database with fake activities and a plan so you can see the dashboard
without Strava credentials:

    FITNESS_DB=demo.db python seed_demo.py
    FITNESS_DB=demo.db uvicorn app.main:app --port 8000

Optionally pin the date:  FITNESS_TODAY=2026-09-26 (a Saturday shows every status colour).
"""
import os
import random
import sys
from datetime import timedelta

from app import clock, db, strava

if not os.environ.get("FITNESS_DB"):
    sys.exit("Refusing to seed your real database - set FITNESS_DB=demo.db first.")

rng = random.Random(7)
today = clock.today()
db.init_db()
this_monday = today - timedelta(days=today.weekday())

WEEK = [  # weekday, sport_type, session type, km, minutes, planned?
    (1, "Run", "Intervals", 9, 55), (1, "WeightTraining", "Strength", 0, 45),
    (3, "Run", "Tempo", 11, 62), (4, "Run", "Easy", 6, 38),
    (5, "TrailRun", "Long run", 22, 175), (6, "Hike", "Power hike", 9, 110),
]


def make_activity(aid, day, sport, km, mins, week_idx, hr=True, score_ratio=0.5):
    speed = (km * 1000) / (mins * 60) if km else 0
    climb = {"TrailRun": 55, "Hike": 65, "Run": 12}.get(sport, 0) * km * rng.uniform(0.8, 1.25)
    start = "%sT%02d:%02d:00Z" % (day.isoformat(), rng.choice([6, 7, 17]), rng.randint(0, 59))
    a = {"id": aid, "name": "%s %s" % (day.strftime("%a"), sport), "sport_type": sport,
         "distance": km * 1000.0, "moving_time": mins * 60, "start_date": start, "start_date_local": start,
         "average_speed": round(speed, 2), "max_speed": round(speed * rng.uniform(1.3, 1.9) if speed else 0, 2),
         "total_elevation_gain": round(climb), "workout_type": 2 if sport == "TrailRun" else 0}
    if hr:
        avg = rng.randint(128, 158) if sport != "WeightTraining" else rng.randint(102, 118)
        a.update(average_heartrate=avg, max_heartrate=avg + rng.randint(18, 32))
        if rng.random() < score_ratio:
            a["suffer_score"] = round(mins * avg / 100 * rng.uniform(0.7, 1.1))
    return a


activities, plan_lines, aid = [], ["date,session type,sport,planned distance,planned duration,notes"], 1000
for w in range(-16, 2):                       # 16 weeks back, this week, next week
    monday = this_monday + timedelta(weeks=w)
    ramp = 0.75 + 0.03 * (w + 16)             # gradual build...
    if (w + 16) % 4 == 3:
        ramp *= 0.7                           # ...with a down week every 4th
    for wd, sport, stype, km, mins in WEEK:
        day = monday + timedelta(days=wd)
        km_p, mins_p = round(km * ramp, 1), round(mins * ramp)
        if w >= -3:                           # only the recent weeks have a written plan
            plan_lines.append("%s,%s,%s,%s,%d,%s" % (
                day.isoformat(), stype, {"TrailRun": "Trail run", "WeightTraining": "Gym"}.get(sport, sport),
                ("%gkm" % km_p) if km_p else "", mins_p, "hilly, keep it steady" if stype == "Long run" else ""))
        if day > today or rng.random() < 0.12:     # future, or a missed session
            continue
        aid += 1
        activities.append(make_activity(aid, day, sport, round(km_p * rng.uniform(0.9, 1.08), 1),
                                        round(mins_p * rng.uniform(0.92, 1.08)), w,
                                        hr=rng.random() > 0.1, score_ratio=0.5))
    # an unplanned extra now and then
    extra_day = monday + timedelta(days=2)
    if w % 3 == 0 and extra_day <= today:
        aid += 1
        activities.append(make_activity(aid, extra_day, "Ride", 28.0, 75, w, score_ratio=0.0))

with db.connect() as conn:
    for a in activities:
        conn.execute(strava.UPSERT, strava.activity_row(a))
    conn.execute("INSERT INTO auth VALUES (1, 1, 'Demo Athlete', 'x', 'x', 0, 'read,activity:read_all') "
                 "ON CONFLICT(id) DO NOTHING")
    db.set_meta(conn, "last_sync", today.isoformat() + "T07:00:00")

print("Seeded %d activities into %s" % (len(activities), db.db_path()))
open("demo_plan.csv", "w").write("\n".join(plan_lines) + "\n")
print("Wrote demo_plan.csv - paste or upload it on the Plan page.")
