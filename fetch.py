"""Pull everything the roundup needs from the FPL Draft API.

Usage:
    python fetch.py            # current gameweek
    python fetch.py --gw 2     # a specific gameweek

Writes raw JSON to data/raw/. Nothing here computes anything; keeping the
fetch dumb means a bad build can never cost you a re-pull.
"""

import argparse
import json
import pathlib
import sys
import time

import requests

import config

RAW = pathlib.Path(__file__).parent / "data" / "raw"


def get(url, session, tries=3):
    for attempt in range(tries):
        r = session.get(url, headers=config.HEADERS, timeout=20)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (403, 429, 503):
            wait = 2 ** attempt
            print(f"  {r.status_code} on {url}, retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        r.raise_for_status()
    raise RuntimeError(
        f"Gave up on {url}. A persistent 403 means Cloudflare has blocked the "
        f"host. Run from your own machine rather than a cloud runner."
    )


def save(name, payload):
    RAW.mkdir(parents=True, exist_ok=True)
    path = RAW / f"{name}.json"
    path.write_text(json.dumps(payload, indent=1))
    print(f"  wrote {path.relative_to(RAW.parent.parent)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gw", type=int, help="gameweek to pull squads for")
    args = ap.parse_args()

    s = requests.Session()
    d, lid = config.DRAFT_API, config.LEAGUE_ID

    print("Game state")
    game = get(f"{d}/game", s)
    save("game", game)
    gw = args.gw or game.get("current_event") or 1
    print(f"  gameweek {gw}")

    print("Players and league")
    save("bootstrap_static", get(f"{d}/bootstrap-static", s))
    details = get(f"{d}/league/{lid}/details", s)
    save("league_details", details)
    save("element_status", get(f"{d}/league/{lid}/element-status", s))

    print(f"Live scores, gameweek {gw}")
    save(f"live_gw{gw}", get(f"{d}/event/{gw}/live", s))

    entries = [e["entry_id"] for e in details["league_entries"]]
    print(f"Squads and transactions for {len(entries)} managers")
    squads, transactions = {}, {}
    for entry in entries:
        squads[str(entry)] = get(f"{d}/entry/{entry}/event/{gw}", s)
        transactions[str(entry)] = get(f"{d}/entry/{entry}/transactions", s)
        time.sleep(0.4)  # be a good citizen
    save(f"squads_gw{gw}", squads)
    save("transactions", transactions)

    # Draft picks, so the season ledger can separate drafters from operators.
    for draft in details["league"].get("drafts", []):
        if draft.get("draft_started"):
            save(f"draft_choices_{draft['id']}", get(f"{d}/draft/{lid}/choices", s))
            break

    save("meta", {"gameweek": gw, "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ")})
    print(f"\nDone. Now run: python kpis.py --gw {gw} && python build.py")


if __name__ == "__main__":
    main()
