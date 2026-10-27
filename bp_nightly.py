#!/usr/bin/env python3
"""Run a full NBA night: pull projected minutes, then alert at each halftime.

For the day's slate (US Eastern) it:
  1. pulls fresh BettingPros projected minutes 5 min before each tip-off
     wave (needs the one-time `python3 bp_projected_minutes.py --login`),
  2. sends a Mac notification whenever a game reaches halftime with players
     way over or under half their projected minutes (rules in bp_halftime_alerts.py),
  3. exits once every game is past halftime.

Usage:
    python3 bp_nightly.py              # run tonight now (sleeps until it is needed)
    python3 bp_nightly.py --install    # start it automatically every day at noon
    python3 bp_nightly.py --uninstall  # stop the daily start
"""

import argparse
import plistlib
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import bp_halftime_alerts as ha

HERE = Path(__file__).resolve().parent
LABEL = "local.nba-minutes-alerts"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
PULL_LEAD = timedelta(minutes=5)


def tip(game):
    return datetime.fromisoformat(game["date"].replace("Z", "+00:00"))


def pull(date, csv_path):
    """Refresh the projections CSV; keep the previous file if the pull fails."""
    # Imported here so the alerts still run if Playwright is missing.
    try:
        import bp_projected_minutes as pm
        rows = pm.scrape(date)
    except (SystemExit, Exception) as e:
        msg = str(e) or "pull failed"
        print(f"{datetime.now():%H:%M} projections pull failed: {msg}")
        fallback = "using the earlier pull" if csv_path.exists() else "no alerts until it works"
        ha.notify("Projected minutes pull failed",
                  f"Sign-in may have expired: run bp_projected_minutes.py --login ({fallback})")
        return False
    pm.write_csv(rows, csv_path)
    return True


def run(date, args):
    csv_path = HERE / f"nba_projected_minutes_{date}.csv"
    log_path = HERE / f"minutes_alerts_{date}.csv"
    games = ha.get(f"{ha.ESPN}/scoreboard?dates={date.replace('-', '')}")["events"]
    if not games:
        print(f"No NBA games on {date}.")
        return
    waves = sorted({tip(g) for g in games})
    print(f"{date}: {len(games)} games, tip-offs "
          + ", ".join(t.astimezone(ZoneInfo('America/New_York')).strftime('%-I:%M') for t in waves) + " ET")

    pulled, done = set(), set()
    deadline = datetime.now(timezone.utc) + timedelta(minutes=args.max_minutes) if args.max_minutes else None
    while True:
        now = datetime.now(timezone.utc)
        if deadline and now >= deadline:
            # Stop between halftimes so the next run can pick up without repeating alerts.
            live_half = [g for g in ha.get(f"{ha.ESPN}/scoreboard?dates={date.replace('-', '')}")["events"]
                         if g["status"]["type"]["name"] == "STATUS_HALFTIME"]
            if not live_half:
                print("Time limit reached; stopping (a later run continues).")
                return
        due = [t for t in waves if t - PULL_LEAD <= now and t not in pulled]
        if due:
            pull(date, csv_path)
            pulled.update(due)

        games = ha.get(f"{ha.ESPN}/scoreboard?dates={date.replace('-', '')}")["events"]
        remaining = []
        for g in games:
            st = g["status"]
            if g["id"] in done:
                continue
            if st["type"]["name"] == "STATUS_HALFTIME":
                if not csv_path.exists():
                    print(f"{g['shortName']} at halftime but no projections file; skipped.")
                else:
                    # Reload each time so later pulls (injury news) are used.
                    proj = ha.load_projections(csv_path)
                    alerts, margin = ha.check_game(g, proj, args, 0.5)
                    ha.report(g, alerts, margin, "HALFTIME")
                    ha.log_alerts(log_path, alerts, "HALFTIME")
                done.add(g["id"])
            elif st["type"]["state"] == "post" or (st["type"]["state"] == "in" and st["period"] >= 3):
                done.add(g["id"])  # halftime already passed
            else:
                remaining.append(g)
        if not remaining:
            print("Every game is past halftime. Done for tonight.")
            return

        # Before any game is live, sleep until the next pull or tip-off instead of polling.
        if all(g["status"]["type"]["state"] == "pre" for g in remaining):
            wake = min([t - PULL_LEAD for t in waves if t not in pulled] +
                       [min(tip(g) for g in remaining) + timedelta(minutes=45)])
            time.sleep(max(args.interval, min((wake - now).total_seconds(), 3600)))
        else:
            time.sleep(args.interval)


def install(hour, minute):
    log = HERE / "nightly.log"
    plist = {
        "Label": LABEL,
        # caffeinate keeps the Mac from idle-sleeping while games are on.
        "ProgramArguments": ["/usr/bin/caffeinate", "-i", sys.executable, str(HERE / "bp_nightly.py")],
        "WorkingDirectory": str(HERE),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    with PLIST.open("wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["launchctl", "load", str(PLIST)], check=True)
    print(f"Installed: runs daily at {hour}:{minute:02d} from {HERE}. Log: {log}")


def uninstall():
    subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
    PLIST.unlink(missing_ok=True)
    print("Daily start removed.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", help="slate date YYYY-MM-DD (default: today, US Eastern)")
    ap.add_argument("--install", action="store_true", help="start automatically every day")
    ap.add_argument("--at", default="12:00", help="daily start time, local, HH:MM (default 12:00)")
    ap.add_argument("--uninstall", action="store_true", help="remove the daily start")
    ap.add_argument("--min-diff", type=float, default=4.0)
    ap.add_argument("--min-pct", type=float, default=0.25)
    ap.add_argument("--min-proj", type=float, default=12.0)
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--max-minutes", type=int, default=0,
                    help="stop after this long, at a point with no game at halftime (for CI time limits)")
    args = ap.parse_args()

    if args.install:
        h, m = map(int, args.at.split(":"))
        install(h, m)
    elif args.uninstall:
        uninstall()
    else:
        # Before 6 AM ET still counts as the previous night's slate.
        et = datetime.now(ZoneInfo("America/New_York")) - timedelta(hours=6)
        run(args.date or et.strftime("%Y-%m-%d"), args)


if __name__ == "__main__":
    main()
