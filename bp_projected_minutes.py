#!/usr/bin/env python3
"""Export BettingPros NBA projected minutes for a day's slate to CSV.

Projected minutes are Premium-only on bettingpros.com and are not served to
consumer API keys, so this drives a real Chromium with its own saved login.

First run (opens a browser window; sign in to BettingPros, then press Enter):
    python3 bp_projected_minutes.py --login

Daily use (headless):
    python3 bp_projected_minutes.py                 # today's slate (ET)
    python3 bp_projected_minutes.py --date 2026-10-27 -o minutes.csv

Requires: pip install playwright && python3 -m playwright install chromium
"""

import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

PAGE_URL = "https://www.bettingpros.com/nba/projected-minutes/?date={date}"
PROFILE_DIR = Path.home() / ".bettingpros_browser_profile"
# Saved sign-in (cookies + storage), written by --login. Session-only cookies
# survive here even though the browser profile drops them; also used in CI.
STATE_FILE = Path(os.environ.get("BP_STATE_FILE", Path.home() / ".bettingpros_state.json"))
VIEWPORT = {"width": 390, "height": 900}

COLUMNS = [
    "date", "event_id", "team", "opp", "home_away", "player", "player_id", "pos",
    "site_starter_flag", "status", "proj_min", "range_low", "range_high",
    "delta_vs_season", "season_mpg", "l5_mpg", "why_primary", "why_other",
    "blowout", "spread",
]

# Runs in the page. The public island JSON has ids/status/averages for every
# player; the rendered cards add the Premium fields (projection, range, reasons).
# The island JSON is removed from the DOM on hydration, so re-read it from the HTML.
EXTRACT_JS = r"""
async () => {
  const html = await fetch(location.href, {credentials: 'include'}).then(r => r.text());
  const raw = [...html.matchAll(/<script type="application\/json" class="island-props">([\s\S]*?)<\/script>/g)]
    .map(m => m[1]).find(t => t.includes('"board"'));
  const pub = raw ? JSON.parse(raw) : {board: {players: []}};
  const byLink = {};
  pub.board.players.forEach(p => { byLink[p.participant.link] = p; });
  const txt = (root, sel) => (root.querySelector(sel)?.innerText || '').trim();
  return [...document.querySelectorAll('.pm__m-row')].map(r => {
    const a = r.querySelector('.pm__name');
    const p = byLink[a.getAttribute('href')] || {};
    const range = txt(r, '.pm__m-proj .pm__meta').match(/([\d.]+)\s*[–-]\s*([\d.]+)/);
    const out = r.classList.contains('pm__row--out');
    // Card meta reads "G · MIN @ CLE" (away) or "G · CLE vs MIN" (home); used
    // when a player is missing from the public JSON.
    const meta = txt(r, '.pm__m-who .pm__meta');
    const mm = meta.match(/·\s*(\w+)\s+(@|vs)\s+(\w+)/) || [];
    return {
      date: pub.date || '',
      event_id: p.event_id ?? '',
      team: p.team ?? mm[1] ?? '',
      opp: p.opp ?? mm[3] ?? '',
      home_away: p.is_home !== undefined ? (p.is_home ? 'home' : 'away')
        : (mm[2] === '@' ? 'away' : mm[2] === 'vs' ? 'home' : ''),
      player: a.innerText.trim(),
      player_id: p.participant?.id ?? '',
      pos: meta.split('·')[0].trim(),
      site_starter_flag: p.starter ? 'starter' : 'bench',
      status: out ? 'out' : (p.status || ''),
      proj_min: txt(r, '.pm__proj-num'),
      range_low: range ? range[1] : '',
      range_high: range ? range[2] : '',
      delta_vs_season: txt(r, '.pm__m-delta').replace('+', ''),
      season_mpg: p.season_mpg ?? '',
      l5_mpg: p.l5_mpg ?? '',
      why_primary: txt(r, '.pm__m-driver').replace(/\s+/g, ' '),
      why_other: txt(r, '.pm__m-rest'),
      blowout: txt(r, '.pm__risk'),
      spread: (r.querySelector('.pm__m-risk .pm__meta:last-child')?.innerText || '')
        .replace('·', '').trim(),
    };
  });
}
"""


def launch(pw, headless):
    # A phone-sized viewport makes the page render the card layout parsed above.
    return pw.chromium.launch_persistent_context(
        str(PROFILE_DIR), headless=headless, viewport=VIEWPORT,
    )


def login():
    with sync_playwright() as pw:
        ctx = launch(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.bettingpros.com/nba/projected-minutes/",
                  wait_until="domcontentloaded")
        input("Sign in to BettingPros in the browser window, then press Enter here... ")
        ctx.storage_state(path=str(STATE_FILE))
        STATE_FILE.chmod(0o600)
        ctx.close()
    print(f"Login saved to {STATE_FILE}")


def scrape(date):
    with sync_playwright() as pw:
        if STATE_FILE.exists():
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(storage_state=str(STATE_FILE), viewport=VIEWPORT)
        else:
            ctx = launch(pw, headless=True)
        page = ctx.new_page()
        page.goto(PAGE_URL.format(date=date), wait_until="domcontentloaded")
        try:
            page.wait_for_selector(".pm__m-row", timeout=30_000)
            # Premium projections load after the page hydrates with the session.
            page.wait_for_function(
                "!document.body.innerText.includes('Create a free account')"
                " && document.querySelectorAll('.pm__proj-num').length > 8",
                timeout=30_000,
            )
        except PWTimeout:
            ctx.close()
            sys.exit("Board did not unlock: not signed in (run with --login) "
                     "or no NBA games on that date.")
        rows = page.evaluate(EXTRACT_JS)
        ctx.close()
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--login", action="store_true", help="open a browser to sign in once")
    ap.add_argument("--date", help="slate date YYYY-MM-DD (default: today, US Eastern)")
    ap.add_argument("-o", "--out", help="output CSV path")
    args = ap.parse_args()

    if args.login:
        login()
        return

    date = args.date or datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    rows = scrape(date)
    write_csv(rows, Path(args.out or f"nba_projected_minutes_{date}.csv"))


def write_csv(rows, out):
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    projected = sum(1 for r in rows if r["proj_min"])
    ruled_out = sum(1 for r in rows if r["status"] == "out")
    print(f"{out}: {len(rows)} players, {projected} projected, {ruled_out} out")


if __name__ == "__main__":
    main()
