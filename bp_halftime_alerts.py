#!/usr/bin/env python3
"""Alert when NBA players are well over or under their projected minutes pace.

Reads a projected-minutes CSV (from bp_projected_minutes.py or a Claude pull),
polls ESPN's public live box scores, and when each game reaches halftime
compares each player's minutes with half their projection.

A player is flagged when BOTH hold (defaults, change with flags):
  |actual - expected| >= 4 min   and   |actual - expected| >= 25% of expected
Players projected under 12 min are skipped (too noisy to bet on).

Usage:
    python3 bp_halftime_alerts.py                      # today's CSV, watch until all games pass halftime
    python3 bp_halftime_alerts.py --csv nba_projected_minutes_2026-10-27.csv
    python3 bp_halftime_alerts.py --check-now          # check every live/finished game once, at its current pace

No extra packages needed.
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
# ESPN team codes that differ from BettingPros'.
ESPN_TO_BP = {"WSH": "WAS", "GS": "GSW", "UTAH": "UTH", "PHX": "PHO", "NY": "NYK",
              "SA": "SAS", "NO": "NOP"}


def norm(name):
    # ESPN and BettingPros spell some names differently (accents, Jr., III).
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z ]", "", s)
    return " ".join(w for w in s.split() if w not in {"jr", "sr", "ii", "iii", "iv"})


def get(url):
    with urlopen(url, timeout=20) as r:
        return json.load(r)


def elapsed_minutes(period, clock):
    """Game minutes played so far, from the period number and the clock left in it."""
    if ":" in clock:
        m, s = clock.split(":")
        left = int(m) + float(s) / 60
    else:
        left = float(clock or 0) / 60
    if period <= 4:
        return (period - 1) * 12 + (12 - left)
    return 48 + (period - 5) * 5 + (5 - left)


def load_projections(path):
    proj = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r["proj_min"]:
                proj[norm(r["player"])] = r
    return proj


def box_players(event_id):
    d = get(f"{ESPN}/summary?event={event_id}")
    out = []
    for team in d.get("boxscore", {}).get("players", []):
        abbr = team["team"]["abbreviation"]
        st = team["statistics"][0]
        keys = st["keys"]
        for a in st["athletes"]:
            stats = dict(zip(keys, a.get("stats") or []))
            mins = stats.get("minutes", "0")
            out.append({
                "name": a["athlete"]["displayName"],
                "team": abbr,
                "min": float(mins) if mins.replace(".", "", 1).isdigit() else 0.0,
                "pts": stats.get("points", ""),
                "pf": int(stats["fouls"]) if stats.get("fouls", "").isdigit() else 0,
                "dnp": a.get("didNotPlay", False),
            })
    return out


def check_game(game, proj, args, frac):
    """Return alert rows for one game, given the share of regulation elapsed."""
    comp = game["competitions"][0]
    score = {c["team"]["abbreviation"]: int(c.get("score") or 0) for c in comp["competitors"]}
    margin = abs(max(score.values()) - min(score.values())) if len(score) == 2 else 0
    alerts, seen = [], set()
    for p in box_players(game["id"]):
        r = proj.get(norm(p["name"]))
        if not r:
            continue
        seen.add(norm(p["name"]))
        full = float(r["proj_min"])
        if full < args.min_proj:
            continue
        expected = full * frac
        diff = p["min"] - expected
        if abs(diff) >= args.min_diff and abs(diff) >= args.min_pct * expected:
            notes = []
            if p["pf"] >= 3:
                notes.append(f"{p['pf']} fouls")
            if margin >= 15:
                notes.append(f"blowout {margin}")
            if p["dnp"] or p["min"] == 0:
                notes.append("not played")
            alerts.append({
                "game": game["shortName"], "player": p["name"], "team": p["team"],
                "dir": "OVER" if diff > 0 else "UNDER", "min": p["min"],
                "expected": round(expected, 1), "diff": round(diff, 1), "proj": full,
                "pts": p["pts"], "notes": ", ".join(notes),
            })
    # Projected regulars on these teams who are not in the box score at all (late scratch).
    teams = {ESPN_TO_BP.get(t, t) for t in score}
    for key, r in proj.items():
        full = float(r["proj_min"])
        if key in seen or full < args.min_proj or r["team"] not in teams:
            continue
        alerts.append({
            "game": game["shortName"], "player": r["player"], "team": r["team"],
            "dir": "UNDER", "min": 0.0, "expected": round(full * frac, 1),
            "diff": round(-full * frac, 1), "proj": full, "pts": "",
            "notes": "not in box score",
        })
    return alerts, margin


def notify(title, msg):
    # Phone push via ntfy when NTFY_TOPIC is set (the topic name is the only secret).
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        req = Request(f"https://ntfy.sh/{topic}", data=msg.encode(), method="POST",
                      headers={"Title": title.encode("ascii", "ignore").decode(), "Priority": "high"})
        try:
            urlopen(req, timeout=20).close()
        except OSError as e:
            print(f"ntfy failed: {e}")
    # macOS notification banner with a sound; harmless no-op elsewhere.
    if sys.platform == "darwin":
        script = f'display notification {json.dumps(msg)} with title {json.dumps(title)} sound name "Glass"'
        subprocess.run(["osascript", "-e", script], capture_output=True)


def report(game, alerts, margin, label):
    print(f"\n=== {game['shortName']} · {label} · margin {margin} ===")
    if not alerts:
        print("  nobody way off pace")
        return
    alerts = sorted(alerts, key=lambda a: -abs(a["diff"]))
    for a in alerts:
        print(f"  {a['dir']:5} {a['player']:<24} {a['team']:<4} {a['min']:>4.0f} min "
              f"vs {a['expected']:>4.1f} exp ({a['diff']:+.1f}) · proj {a['proj']:.1f}"
              f"{' · ' + a['notes'] if a['notes'] else ''}")
    body = "\n".join(f"{a['dir']} {a['player']} ({a['team']}) {a['min']:.0f} vs {a['expected']:.1f} "
                     f"({a['diff']:+.1f}){' · ' + a['notes'] if a['notes'] else ''}" for a in alerts)
    notify(f"Minutes alert: {game['shortName']} · margin {margin}", body)


def log_alerts(path, alerts, label):
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "check", "game", "player", "team", "dir", "min", "expected", "diff", "proj", "pts", "notes"])
        now = datetime.now().strftime("%H:%M:%S")
        for a in alerts:
            w.writerow([now, label, a["game"], a["player"], a["team"], a["dir"], a["min"],
                        a["expected"], a["diff"], a["proj"], a["pts"], a["notes"]])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", help="slate date YYYY-MM-DD (default: today, US Eastern)")
    ap.add_argument("--csv", help="projected-minutes CSV (default: nba_projected_minutes_<date>.csv)")
    ap.add_argument("--min-diff", type=float, default=4.0, help="minimum minutes off pace (default 4)")
    ap.add_argument("--min-pct", type=float, default=0.25, help="minimum share of expected minutes off pace (default 0.25)")
    ap.add_argument("--min-proj", type=float, default=12.0, help="ignore players projected under this (default 12)")
    ap.add_argument("--interval", type=int, default=60, help="seconds between polls (default 60)")
    ap.add_argument("--check-now", action="store_true", help="check live/finished games once at their current pace, then exit")
    args = ap.parse_args()

    date = args.date or datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    csv_path = Path(args.csv or f"nba_projected_minutes_{date}.csv")
    if not csv_path.exists():
        sys.exit(f"{csv_path} not found: pull the projected minutes for {date} first.")
    proj = load_projections(csv_path)
    log_path = Path(f"minutes_alerts_{date}.csv")
    print(f"Loaded {len(proj)} projections from {csv_path}. Alerts also logged to {log_path}.")

    done = set()
    while True:
        games = get(f"{ESPN}/scoreboard?dates={date.replace('-', '')}")["events"]
        pending = 0
        for g in games:
            st = g["status"]
            name = st["type"]["name"]
            if args.check_now:
                if st["type"]["state"] == "pre":
                    continue
                if st["type"]["state"] == "post":
                    # Finished games sometimes report the clock as 12:00.
                    frac = (48 + 5 * max(0, st["period"] - 4)) / 48
                elif name == "STATUS_HALFTIME":
                    frac = 0.5
                else:
                    frac = elapsed_minutes(st["period"], st["displayClock"]) / 48
                label = st["type"]["shortDetail"]
            elif name == "STATUS_HALFTIME" and g["id"] not in done:
                frac, label = 0.5, "HALFTIME"
            else:
                if g["id"] not in done and st["type"]["state"] != "post" and \
                        not (st["type"]["state"] == "in" and st["period"] >= 3):
                    pending += 1
                continue
            alerts, margin = check_game(g, proj, args, frac)
            report(g, alerts, margin, label)
            log_alerts(log_path, alerts, label)
            done.add(g["id"])
        if args.check_now:
            return
        if not pending:
            print("\nNo games left to reach halftime. Done.")
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
