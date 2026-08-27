# Draft League Regards, matchweek roundup

Automates the data side of the weekly roundup for FPL Draft league **39805**
and publishes it as a website in the same house style as the PDF.

## Run order

```bash
pip install -r requirements.txt

python fetch.py --gw 2     # pull raw JSON from the Draft API
python kpis.py  --gw 2     # compute every metric the roundup uses
python build.py --gw 2     # render dist/index.html
```

Open `dist/index.html`, or send round `dist/gw2-standalone.html`, which is a
single file with the CSS inlined.

## Two layers, deliberately separate

| Layer | Lives in | Written by |
|---|---|---|
| Metrics | `data/derived/gw{n}.json` | `kpis.py`, never by hand |
| Copy | `data/prose/gw{n}.json` | you, or a generation step |

Every number on the site comes from `kpis.py`, so there is one place to check
when a figure looks wrong. Every judgement lives in the prose file. If the
prose file is missing the page still builds and renders the numbers, and says
so, rather than quietly looking finished.

`entertainment_overrides` in the prose file exists because some ratings are not
in the numbers. The engine rates fixtures relative to the week on closeness and
combined total; a benched 17-point defender is a story the formula cannot see,
so editorial can bump that rating and the page reorders itself.

## Metrics computed

- Scoreline, margin, combined total, entertainment rating, week ranking
- Active XI and bench split, points per player, blanks, wasted bench points
- Compulsory release under the Highest Scorer Rule, next in line, ties flagged
- Release cost as a share of score, which drives the release ledger
- Rule-breach detection: last week's mandated player still on the books, with
  the fine and running pot
- Standings recomputed from finished matches, with shared ranks and movement
- Waiver and free agent moves per manager
- Next gameweek's fixtures

Two things `kpis.py` does not trust the API on. The standings block reports
`matches_played: 38` for everyone before a ball is kicked, so the table is
rebuilt from finished matches. And `league_entries` carries both `id` (used by
matches and standings) and `entry_id` (used by squad and transaction
endpoints); mixing them up is the fastest way to get plausible nonsense.

## Endpoints used

All unofficial and undocumented, all on `https://draft.premierleague.com/api`:

| Path | For |
|---|---|
| `game` | current gameweek |
| `bootstrap-static` | player names, clubs, positions |
| `league/39805/details` | entries, standings, all 190 fixtures |
| `league/39805/element-status` | ownership and the free agent pool |
| `event/{gw}/live` | points per player |
| `entry/{id}/event/{gw}` | that manager's XI and bench |
| `entry/{id}/transactions` | waivers and free agents |
| `draft/39805/choices` | the original draft |

## The failure mode to expect

These endpoints sit behind Cloudflare. A persistent 403 means the host has been
blocked, not that the code is wrong. `fetch.py` sends a browser User-Agent and
backs off, which is usually enough from a home machine and often not enough
from a cloud runner. If GitHub Actions starts failing, run `fetch.py` locally
and commit `data/raw/`; the build itself needs no network.

Nothing here needs your login. The league is readable without a session, so no
credentials are stored anywhere in this repo. Do not paste `Copy as cURL`
output into it, since that carries your session cookies.

## Weekly workflow

1. Tuesday, after bonus points settle: `python fetch.py`
2. `python kpis.py` and read the derived JSON, or the built page
3. Write `data/prose/gw{n}.json` against the numbers
4. `python build.py` and publish `dist/`

## Manual fallback

For a week where you have the releases but not the picks, drop a list into
`data/manual/gw{n}_releases.json` with manager, release, release_points, next,
next_points and score. Percentages and win/loss are still computed. `gw1` is
seeded this way as a worked example.
