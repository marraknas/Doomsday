"""
MY Cinemas (Causeway Point, Woodlands) watcher for Jailer 2.

mycinemas.sg is server-rendered PHP (no JSON API for showtimes):

  /                     homepage: "Now Showing" cards link to /movie/<slug>;
                        "WHAT'S COMING" cards show title + "Release Date : DD-MM-YYYY"
  /movie/<slug>         movie page: one tab per date, one <button id="show<ID>"> per session with
                          onclick="handleRedirection('https://mycinemas.sg/desktop-seat/...
                                   %26shid%3D3060%26mvid%3D91%26mvname%3DBaththa%26timstr%3D2026-10-12+16%3A15%3A00 ...')"
                          data-online="0"  (0 = bookable online)   data-cnt="192" (seats in hall)
  /ajax/book-status-ajax.php  POST action=getColor&mov=<mvid>  -> {"now": epoch, "stat": {"<showId>": booked}}
                          (the site's own Available / Filling Fast (>40%) / Sold Out labels come from this)

The Jailer 2 page already exists as /movie/jailer-2-2 (no sessions yet), so it is polled directly;
any other /movie/ link whose slug matches Jailer 2 on the homepage is checked too.
"""

import html as htmlmod
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote_plus

import requests

import carnival   # shared title matching + time helpers

SITE = "https://mycinemas.sg"
TITLE = "Jailer 2"
KNOWN_SLUGS = ["jailer-2-2"]          # pre-published Jailer 2 page found on 12 Oct 2026
SGT = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
MAX_PAGES = 6                         # movie pages fetched per run (Jailer variants)


class MyCinemasError(Exception):
    pass


def _req(session, method, url, **kw):
    last = None
    for _ in range(2):
        try:
            r = session.request(method, url, timeout=20,
                                headers={"User-Agent": UA, "Accept-Language": "en-SG,en;q=0.9",
                                         **kw.pop("headers", {})}, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(2)
    raise MyCinemasError(f"{url.replace(SITE, '')} → {type(last).__name__}: {str(last)[:150]}")


def _attr(tag, name):
    m = re.search(rf'{name}\s*=\s*"([^"]*)"', tag)
    return htmlmod.unescape(m.group(1)) if m else ""


def parse_home(page):
    """-> (movie slugs linked from the page, coming-soon [(title, 'YYYY-MM-DD')])"""
    slugs = list(dict.fromkeys(re.findall(r'href="https?://(?:www\.)?mycinemas\.sg/movie/([^"#?]+)"', page)))
    coming = []
    i = re.search(r"WHAT.{0,3}S COMING", page, re.I)
    if i:
        for alt, rel in re.findall(r'<img[^>]+alt="([^"]*)"[^>]*>[\s\S]{0,1500}?Release Date\s*:\s*([0-9]{2}-[0-9]{2}-[0-9]{4})',
                                   page[i.start():]):
            try:
                coming.append((htmlmod.unescape(alt).strip(), datetime.strptime(rel, "%d-%m-%Y").strftime("%Y-%m-%d")))
            except ValueError:
                pass
    return slugs, coming


def parse_movie(page, slug):
    """-> (title or '', [session dicts])"""
    heads = [htmlmod.unescape(h).strip() for h in
             re.findall(r"<h1[^>]*banner-title[^>]*>\s*([^<]{2,120}?)\s*</h1>", page)]   # the film's own title
    sessions = []
    for tag in re.findall(r'<button[^>]*\bid="show\d+"[^>]*>', page):
        sid = re.search(r'id="show(\d+)"', tag).group(1)
        q = unquote_plus(_attr(tag, "onclick"))
        params = dict(re.findall(r"[&?](\w+)=([^&']*)", q))
        when = params.get("timstr", "")
        try:
            dt = datetime.strptime(when.strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        mins = dt.hour * 60 + dt.minute
        cls = _attr(tag, "class")
        disabled = "disabled" in cls.split() or re.search(r"\sdisabled(\s|>|=)", tag) is not None
        sessions.append({
            "date": dt.strftime("%Y-%m-%d"), "time": dt.strftime("%-I:%M %p"), "mins": mins,
            "early": carnival.is_early(mins), "dt": dt,
            "bookable": _attr(tag, "data-online") in ("0", "") and not disabled,
            "sid": sid, "mvid": params.get("mvid", ""), "movie": params.get("mvname") or (heads[0] if heads else slug),
            "seats": int(_attr(tag, "data-cnt") or 0), "dimen": params.get("dimen", ""), "slug": slug})
    return (heads[0] if heads else ""), sessions


def check(session, test=False):
    """Returns (findings, ctx). Keys (kept in state["mycinemas_seen"]):
         myc:soon             Jailer 2 shown under WHAT'S COMING
         myc:any              Jailer 2 has any sessions
         myc:show:<showId>    a session is listed
         myc:open:<showId>    a session is bookable online
    In test mode the now-showing film with the most sessions stands in for Jailer 2."""
    home = _req(session, "GET", f"{SITE}/").text
    slugs, coming = parse_home(home)
    if not slugs and "movie" not in home.lower():
        raise MyCinemasError("homepage returned no movie listings (blocked or changed?)")
    match = lambda s: bool(carnival.TARGET_RE.search((s or "").replace("-", " ")))  # noqa: E731

    findings = {}
    soon = [(t, d) for t, d in coming if match(t)]
    if soon and not test:
        findings["myc:soon"] = f"{soon[0][0]} coming {soon[0][1]}"

    if test:
        candidates = slugs[:MAX_PAGES + 4]
    else:
        candidates = list(dict.fromkeys(KNOWN_SLUGS + [s for s in slugs if match(s)]))[:MAX_PAGES]

    now = datetime.now(SGT).replace(tzinfo=None)
    by_slug = {}
    for slug in candidates:
        title, sess = parse_movie(_req(session, "GET", f"{SITE}/movie/{slug}").text, slug)
        if not test and not (match(title) or match(slug)):
            continue          # a generic/empty page or a different film
        sess = [x for x in sess if x["dt"] >= now - timedelta(minutes=30)]
        if test or match(title) or any(match(x["movie"]) for x in sess):
            by_slug[slug] = sess

    if test:
        best = max(by_slug, key=lambda k: (len(by_slug[k]), k), default=None)
        by_slug = {best: by_slug[best]} if best else {}
    sessions = sorted((x for v in by_slug.values() for x in v), key=carnival.sort_key)
    if sessions:
        findings["myc:any"] = f"{len(sessions)} session(s) on MY Cinemas"
    for x in sessions:
        findings[f"myc:show:{x['sid']}"] = f"{x['date']} {x['time']} listed"
        if x["bookable"]:
            findings[f"myc:open:{x['sid']}"] = f"{x['date']} {x['time']} bookable"
    stand_in = sessions[0]["movie"] if (test and sessions) else ""
    return findings, {"sessions": sessions, "soon": soon, "test": test,
                      "slugs": [s for s in by_slug if by_slug[s]] or list(by_slug) or KNOWN_SLUGS[:1],
                      "listings": sorted({x["movie"] for x in sessions}),
                      "base_re": re.compile(re.escape(stand_in), re.I) if stand_in else carnival.TARGET_RE}


# ------------------------------------------------------------------ alert content

def seat_status(session, mvids):
    """{showId: booked} from the site's own seat-count endpoint (best effort)."""
    out = {}
    for mv in sorted(set(mvids)) or [""]:
        try:
            r = _req(session, "POST", f"{SITE}/ajax/book-status-ajax.php", data={"action": "getColor", "mov": mv},
                     headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{SITE}/"})
            stat = (json.loads(r.text) or {}).get("stat") or {}
            out.update({str(k): v for k, v in stat.items()})
        except (MyCinemasError, ValueError, AttributeError):
            pass
    return out


def book_url(ctx):
    return f"{SITE}/movie/{ctx['slugs'][0]}" if ctx.get("slugs") else f"{SITE}/"


def snapshot(session, ctx, mode, new_keys, now):
    new_sids = {k.split(":", 2)[2] for k in new_keys if k.startswith(("myc:show:", "myc:open:"))}
    sessions = ctx["sessions"]
    booked = seat_status(session, [x["mvid"] for x in sessions]) if sessions else {}
    for x in sessions:
        x["new"] = mode != "test" and x["sid"] in new_sids
        tags = [carnival.variant(x["movie"], ctx.get("base_re", carnival.TARGET_RE))]
        if x.get("dimen"):
            tags.append(x["dimen"])
        x["tag"] = " · ".join(t for t in tags if t)
        x["classes"] = []
        if x["bookable"] and x["seats"]:
            b = booked.get(x["sid"])
            b = int(b) if str(b).isdigit() else None
            if b is not None:
                pct = 100 * b / x["seats"]
                label, state = ("Sold Out", "sold") if pct >= 100 else ("Filling Fast", "fast") if pct > 40 \
                    else ("Available", "open")
                x["classes"] = [{"name": f"{max(0, x['seats'] - b)} / {x['seats']} left", "price": None,
                                 "status": label, "state": state}]
    all_days = sorted({x["date"] for x in sessions})
    focus = [d for d in all_days if any(x["new"] for x in sessions if x["date"] == d)] if mode == "update" else []
    card_days = (focus or all_days)[:carnival.CARD_DAYS]
    days = [{"date": d, "first": d == all_days[0], "shows": [x for x in sessions if x["date"] == d]}
            for d in card_days]
    title = TITLE if not ctx.get("test") else (re.sub(r"\s*\([^()]*\)\s*$", "", sessions[0]["movie"])
                                               if sessions else "MY Cinemas test")
    return {"title": title, "mode": mode, "checked_at": now, "days": days,
            "total": len(sessions), "bookable": sum(1 for x in sessions if x["bookable"]),
            "early": sum(1 for x in sessions if x["early"]), "all_days": all_days,
            "more_days": len(all_days) - len(card_days), "listings": ctx.get("listings", []),
            "new": [x for x in sessions if x["new"]],
            "venue": "MY CINEMAS  ·  CAUSEWAY POINT", "site": "mycinemas.sg",
            "footer": "Book:  mycinemas.sg  ·  Causeway Point, Woodlands"}


def caption(snap):
    cap = carnival.caption(snap)
    head = {"live": f"🎬 <b>{TITLE.upper()} SHOWTIMES ARE OUT — MY Cinemas</b>",
            "update": f"🆕 <b>{TITLE} @ MY Cinemas: new / newly bookable shows</b>",
            "test": f"🧪 <b>TEST RUN — MY Cinemas</b> ({snap['title']})"}[snap["mode"]]
    return "\n".join([head] + cap.split("\n")[1:]).replace("more day(s) on Carnival", "more day(s) on MY Cinemas")


def text(snap):
    body = carnival.text(snap).split("\n")[1:]
    return "\n".join([caption(snap).split("\n")[0]] + body).replace("carnivalcinemas.sg", "mycinemas.sg")
