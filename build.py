"""Render the site: transfers and standings, straight from the numbers.

Reads data/derived/gw{n}.json, which is the only input. kpis.py computes
everything on the page; nothing here is hand-edited.

Usage:
    python build.py            # latest derived gameweek
    python build.py --gw 2
"""

import argparse
import hashlib
import json
import pathlib
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = pathlib.Path(__file__).parent
DERIVED = ROOT / "data" / "derived"
DIST = ROOT / "dist"


def latest_gw():
    weeks = [int(p.stem[2:]) for p in DERIVED.glob("gw*.json")
             if p.stem[2:].isdigit()]
    if not weeks:
        raise SystemExit("Nothing in data/derived. Run kpis.py first.")
    return max(weeks)


def friendly_time(iso):
    if not iso:
        return ""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LONDON)
    return dt.strftime("%d %b, %H:%M %Z")


def manager_slug(name):
    return name.lower().replace("'", "").replace(".", "").strip().replace(" ", "-")


# First-name nicknames that differ from the registered FPL name -- only
# needs an entry when the nickname itself isn't just name.split()[0].
NICKNAMES = {
    "Michael Lavarack": "Mike",
    "Mattato Alcock": "Matt",
    "Matthew Xenakis": "Matt",
    "Matthew Lees": "Matt",
}


def build_display_names(manager_names):
    """Full name -> short display name: first name (or nickname) alone,
    unless that collides with another manager's, in which case both get
    "<first> <last initial>" instead. Used for display only -- avatars,
    result matching etc. all still key off the full registered name.
    """
    manager_names = list(manager_names)
    firsts = {name: NICKNAMES.get(name, name.split()[0]) for name in manager_names}
    counts = {}
    for first in firsts.values():
        counts[first] = counts.get(first, 0) + 1
    return {
        name: f"{first} {name.split()[-1][0]}" if counts[first] > 1 else first
        for name, first in firsts.items()
    }


def build_avatars(manager_names):
    """Manager name -> static path, for whichever managers have a photo
    dropped in static/managers/{slug}.{jpg,jpeg,png,webp}. No mapping to
    maintain in code -- add a correctly-named file and it just appears.
    """
    avatars = {}
    for name in manager_names:
        slug = manager_slug(name)
        for ext in ("jpg", "jpeg", "png", "webp"):
            if (ROOT / "static" / "managers" / f"{slug}.{ext}").exists():
                avatars[name] = f"static/managers/{slug}.{ext}"
                break
    return avatars


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gw", type=int)
    args = ap.parse_args()
    gw = args.gw or latest_gw()

    data = json.loads((DERIVED / f"gw{gw}.json").read_text())
    next_gw = gw + 1

    transfers_current = []
    for le, m in data["managers"].items():
        cur = data["transfers"].get(le, {"in": [], "out": [], "count": 0, "source": "none"})
        transfers_current.append({"manager": m["manager"], "team": m["team"], **cur})
    transfers_current.sort(key=lambda t: t["manager"])

    # Grouped by row for the pitch layout, goalkeeper at the top down to
    # strikers, matching how the official FPL app lays out a pitch.
    totw_by_pos = {"FWD": [], "MID": [], "DEF": [], "GKP": []}
    for p in data["team_of_week"]["players"]:
        if p["pos"] in totw_by_pos:
            totw_by_pos[p["pos"]].append(p)

    env = Environment(
        loader=FileSystemLoader(ROOT / "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["signed"] = lambda n: "" if not n else f"{'+' if n > 0 else ''}{n}"
    env.filters["friendly_time"] = friendly_time
    display_names = build_display_names(m["manager"] for m in data["managers"].values())
    env.filters["dname"] = lambda name: display_names.get(name, name)
    env.filters["slug"] = manager_slug

    # Cache-bust the stylesheet link so a style change is visible on the
    # next load instead of waiting out whatever the browser/CDN cached.
    css_bytes = (ROOT / "static" / "style.css").read_bytes()
    css_version = hashlib.md5(css_bytes).hexdigest()[:8]

    avatars = build_avatars(m["manager"] for m in data["managers"].values())

    render_kwargs = dict(d=data, gw=gw, next_gw=next_gw, transfers_current=transfers_current,
                          totw_by_pos=totw_by_pos, css_version=css_version, avatars=avatars)

    DIST.mkdir(exist_ok=True)
    shutil.copytree(ROOT / "static", DIST / "static", dirs_exist_ok=True)

    # Three real pages, each its own file, so navigating between them is a
    # normal page load rather than jumping to an anchor on one big page.
    pages = {"leaders": "leaders", "index": "standings"}
    for filename, template_name in pages.items():
        html = env.get_template(f"{template_name}.html").render(active=template_name, **render_kwargs)
        (DIST / f"{filename}.html").write_text(html)

    available_gws = sorted(int(p.stem[2:]) for p in DERIVED.glob("gw*.json") if p.stem[2:].isdigit())

    # Manager Competitions, plus one page per gameweek for the Manager of
    # the Week section specifically -- same picker as Results. Manager of
    # the Month/Season aren't scoped to a single gameweek, so those (and
    # the masthead, and gw itself) stay pinned to the site's current state
    # throughout; only the Top 5/Bottom 5 shown changes with the dropdown.
    mow_template = env.get_template("manager-of-week.html")
    for n in available_gws:
        if n == gw:
            n_mow = data["manager_of_week"]
        else:
            n_data = json.loads((DERIVED / f"gw{n}.json").read_text())
            n_mow = n_data.get("manager_of_week")
        filename = "manager-of-week" if n == gw else f"manager-of-week-gw{n}"
        html = mow_template.render(active="manager-of-week", mow=n_mow, mow_gw=n,
                                    mow_live=(n == gw and not data["league"]["gw_fully_over"]),
                                    current_gw=gw, available_gws=available_gws, **render_kwargs)
        (DIST / f"{filename}.html").write_text(html)

    # Results page, plus one per gameweek that's already happened -- a
    # dropdown lets you jump to any of them, but results.html itself (the
    # one the nav links to) always tracks gw, whichever week is current.
    results_template = env.get_template("results.html")
    for g in available_gws:
        g_data = data if g == gw else json.loads((DERIVED / f"gw{g}.json").read_text())
        filename = "results" if g == gw else f"results-gw{g}"
        html = results_template.render(active="results", d=g_data, gw=g, current_gw=gw,
                                        available_gws=available_gws, next_gw=next_gw,
                                        transfers_current=transfers_current, totw_by_pos=totw_by_pos,
                                        css_version=css_version, avatars=avatars)
        (DIST / f"{filename}.html").write_text(html)

    # Transfers page, plus one per gameweek that's already happened -- same
    # picker as Results; transfers.html itself always tracks gw, whichever
    # week is current.
    transfers_template = env.get_template("transfers.html")
    for g in available_gws:
        g_data = data if g == gw else json.loads((DERIVED / f"gw{g}.json").read_text())
        g_transfers_current = []
        for le, m in g_data["managers"].items():
            cur = g_data["transfers"].get(le, {"in": [], "out": [], "count": 0, "source": "none"})
            g_transfers_current.append({"manager": m["manager"], "team": m["team"], **cur})
        g_transfers_current.sort(key=lambda t: t["manager"])
        filename = "transfers" if g == gw else f"transfers-gw{g}"
        html = transfers_template.render(active="transfers", d=g_data, gw=g, current_gw=gw,
                                          available_gws=available_gws, next_gw=g + 1,
                                          transfers_current=g_transfers_current, totw_by_pos=totw_by_pos,
                                          css_version=css_version, avatars=avatars)
        (DIST / f"{filename}.html").write_text(html)

    # Releases page, plus one per gameweek that's already happened -- same
    # picker as Results; releases.html itself always tracks gw, whichever
    # week is current.
    releases_template = env.get_template("releases.html")
    for g in available_gws:
        g_data = data if g == gw else json.loads((DERIVED / f"gw{g}.json").read_text())
        filename = "releases" if g == gw else f"releases-gw{g}"
        html = releases_template.render(active="releases", d=g_data, gw=g, current_gw=gw,
                                         available_gws=available_gws, next_gw=g + 1,
                                         totw_by_pos=totw_by_pos, css_version=css_version, avatars=avatars)
        (DIST / f"{filename}.html").write_text(html)

    # Team of the Week, plus one page per gameweek whose Team of the Week
    # has already finalised -- same picker as Results. Unlike Results
    # though, only the pitch shown changes with the dropdown: the
    # masthead (and gw itself) stays pinned to the site's actual current
    # gameweek throughout, same as totw.html always has, since totw_gw
    # deliberately lags gw until a week is fully settled.
    current_totw_gw = data["team_of_week"]["gameweek"]
    available_totw_gws = list(range(1, current_totw_gw + 1))
    totw_template = env.get_template("totw.html")
    for n in available_totw_gws:
        if n == current_totw_gw:
            n_totw = data["team_of_week"]
        else:
            n_data = json.loads((DERIVED / f"gw{n}.json").read_text())
            n_totw = n_data.get("team_of_week") or {}
        n_totw_by_pos = {"FWD": [], "MID": [], "DEF": [], "GKP": []}
        for p in n_totw.get("players", []):
            if p["pos"] in n_totw_by_pos:
                n_totw_by_pos[p["pos"]].append(p)
        filename = "totw" if n == current_totw_gw else f"totw-gw{n}"
        html = totw_template.render(active="totw", totw=n_totw, totw_by_pos=n_totw_by_pos,
                                     current_totw_gw=current_totw_gw, available_totw_gws=available_totw_gws,
                                     d=data, gw=gw, next_gw=next_gw, transfers_current=transfers_current,
                                     css_version=css_version, avatars=avatars)
        (DIST / f"{filename}.html").write_text(html)

    # One page per manager -- fixtures, head-to-head, biggest win, best
    # player performance, personal best/worst transfers -- linked from
    # every avatar+name the manager() macro renders anywhere on the site.
    profiles = data.get("manager_profiles", {})
    manager_template = env.get_template("manager.html")
    for m in data["managers"].values():
        name = m["manager"]
        html = manager_template.render(profile_name=name, profile=profiles.get(name), **render_kwargs)
        (DIST / f"manager-{manager_slug(name)}.html").write_text(html)

    # Per-manager transfer-history pages -- season (every block), the
    # current gameweek alone, and every Manager of the Month block that's
    # either live or already finalised -- so a Transfer pts number anywhere
    # on the site links to a page scoped to exactly the swaps behind that
    # number, not the manager's whole season.
    transfers_template = env.get_template("manager-transfers.html")
    block_ranges = set()
    mom_current = data.get("manager_of_month", {}).get("current")
    if mom_current:
        block_ranges.add((mom_current["block_start"], mom_current["block_end"]))
    for h in data.get("manager_of_month", {}).get("history", []):
        block_ranges.add((h["block_start"], h["block_end"]))

    for m in data["managers"].values():
        name = m["manager"]
        slug = manager_slug(name)
        blocks = profiles.get(name, {}).get("transfer_blocks", [])
        raw_transfers = profiles.get(name, {}).get("raw_transfers", [])

        # The season page shows every transfer, but a leg that's already
        # qualified for scoring (both players played, fixtures finished)
        # gets its points swing shown alongside it rather than sitting in
        # the plain in/out list as if nothing were known about it yet.
        qualified_by_gw = {}
        for block in blocks:
            for s in block["swaps"]:
                qualified_by_gw.setdefault(s["gameweek"], []).append(s)
        season_weeks = []
        for week in raw_transfers:
            qualified = qualified_by_gw.get(week["gameweek"], [])
            qualified_in_names = {s["in_name"] for s in qualified}
            qualified_out_names = {s["out_name"] for s in qualified}
            season_weeks.append({
                "gameweek": week["gameweek"],
                "swaps": qualified,
                "pending_in": [p for p in week["in"] if p["name"] not in qualified_in_names],
                "pending_out": [p for p in week["out"] if p["name"] not in qualified_out_names],
            })

        html = transfers_template.render(profile_name=name, scope="season", raw_transfers=season_weeks, **render_kwargs)
        (DIST / f"manager-{slug}-transfers.html").write_text(html)

        for n in available_gws:
            n_swaps = [s for block in blocks for s in block["swaps"] if s["gameweek"] == n]
            n_pending = [t for t in raw_transfers
                         if t["gameweek"] == n and t["gameweek"] not in {s["gameweek"] for s in n_swaps}]
            html = transfers_template.render(profile_name=name, scope="week", week=n,
                                              swaps=n_swaps, pending=n_pending, **render_kwargs)
            (DIST / f"manager-{slug}-transfers-gw{n}.html").write_text(html)

        for start, end in block_ranges:
            block_swaps = next((b["swaps"] for b in blocks
                                 if b["block_start"] == start and b["block_end"] == end), [])
            qualifying_gws = {s["gameweek"] for s in block_swaps}
            block_pending = [t for t in raw_transfers
                             if start <= t["gameweek"] <= end and t["gameweek"] not in qualifying_gws]
            html = transfers_template.render(profile_name=name, scope="block", block_start=start, block_end=end,
                                              swaps=block_swaps, pending=block_pending, **render_kwargs)
            (DIST / f"manager-{slug}-transfers-gw{start}-{end}.html").write_text(html)

    # One live-squad page per manager for this gameweek -- starting XI and
    # bench, with projected autosubs -- linked only from the Results page
    # (its manager name/icon and score), not from the nav or manager profile.
    squad_template = env.get_template("squad.html")
    for le, m in data["managers"].items():
        name = m["manager"]
        squad = data["squads"].get(le)
        squad_by_pos = {"GKP": [], "DEF": [], "MID": [], "FWD": []}
        formation = None
        if squad:
            for p in squad["effective_xi"]:
                if p["pos"] in squad_by_pos:
                    squad_by_pos[p["pos"]].append(p)
            formation = "-".join(str(len(squad_by_pos[pos])) for pos in ("DEF", "MID", "FWD"))
        transfers = data["transfers"].get(str(le), {"in": [], "out": [], "count": 0, "source": "none"})
        html = squad_template.render(profile_name=name, squad=squad, squad_by_pos=squad_by_pos,
                                      formation=formation, transfers=transfers, **render_kwargs)
        (DIST / f"squad-{manager_slug(name)}.html").write_text(html)

    # Single file combining everything, CSS inlined, for sending round the
    # league the way the PDF used to go round -- not part of the site nav.
    combined = env.get_template("roundup.html").render(**render_kwargs)
    css = css_bytes.decode()
    standalone = combined.replace(
        '<link rel="stylesheet" href="static/style.css">',
        f"<style>\n{css}\n</style>",
    )
    (DIST / f"gw{gw}-standalone.html").write_text(standalone)

    print(f"Built dist/index.html, transfers.html, releases.html for gameweek {gw}")
    print(f"       dist/gw{gw}-standalone.html (single file, CSS inlined)")


if __name__ == "__main__":
    main()
