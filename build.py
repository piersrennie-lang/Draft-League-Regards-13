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
    pages = {"leaders": "leaders", "results": "results", "index": "standings", "transfers": "transfers",
              "releases": "releases", "totw": "totw", "manager-of-week": "manager-of-week",
              "manager-of-month": "manager-of-month"}
    for filename, template_name in pages.items():
        html = env.get_template(f"{template_name}.html").render(active=template_name, **render_kwargs)
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

    # One live-squad page per manager for this gameweek -- starting XI and
    # bench, with projected autosubs -- linked only from the Results page
    # (its manager name/icon and score), not from the nav or manager profile.
    squad_template = env.get_template("squad.html")
    for le, m in data["managers"].items():
        name = m["manager"]
        html = squad_template.render(profile_name=name, squad=data["squads"].get(le), **render_kwargs)
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
