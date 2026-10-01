"""
Carnival Cinemas (Golden Mile Tower, Beach Road) watcher for Jailer 2.

Carnival's site is an AngularJS app on top of a JSON API at service.carnivalcinemas.sg.
The calls used here are the same ones the site makes:

  GetShowDatesByCinema         -> every date the cinema has sessions for
  GetMoviesAndShowTimeByCinema -> every movie + session on one date
                                  showTime looks like " 07:00 AMT, 07:30 PMF":
                                  trailing T = bookable, F = listed but greyed out
  GetCommingSoonMovies         -> early heads-up when the film is announced
  GetPriceList                 -> seat classes, price and availability per session

EVERY Jailer 2 showtime alerts: when it is first listed, and again when it becomes
bookable. Early-morning shows (FDFS territory) are badged EARLY on the card.
"""

import re
import time
from datetime import datetime
from urllib.parse import quote

import requests

BASE = "https://service.carnivalcinemas.sg/api/QuickSearch"
LOCATION = "Mumbai"            # the site always sends this, even for Singapore
CINEMA = "BCSG"                # Carnival Cinema Golden Mile Tower (their only SG cinema)
SITE = "https://carnivalcinemas.sg/#/"
TITLE = "Jailer 2"
TARGET_RE = re.compile(r"\bjailer[\s\-_:]*(2|ii)\b", re.I)    # Jailer 2 / JAILER-2 / Jailer II (FDFS) ...
RATING_RE = re.compile(r"^(G|PG|PG13|NC16|M18|R21)$", re.I)

EARLY = (3 * 60, 9 * 60)       # shows from 3:00 to 9:00 AM get the EARLY badge (visual only)
LATE_UNTIL = 3 * 60            # after-midnight shows (before 3 AM) sort to the end of their day
MAX_DATES = 21                 # dates scanned per run
CARD_DAYS = 4                  # days drawn on one card (the rest are summarised)
MAX_PRICE_LOOKUPS = 24         # seat-class lookups per alert

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")


class CarnivalError(Exception):
    pass


def _get(session, path, key):
    last = None
    for _ in range(2):
        try:
            r = session.get(f"{BASE}/{path}", timeout=20,
                            headers={"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                                     "Origin": "https://carnivalcinemas.sg",
                                     "Referer": "https://carnivalcinemas.sg/"})
            r.raise_for_status()
            j = r.json()
            code = str((j.get("responseError") or {}).get("ErrorCode", "200"))
            if code not in ("200", "0"):
                # the API answers "no data" with an error code instead of an empty list
                return []
            data = j.get(key)
            if data is None:
                return []
            if not isinstance(data, list):
                raise CarnivalError(f"{path.split('?')[0]}: unexpected shape")
            return data
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(2)
    raise CarnivalError(f"{path.split('?')[0]} → {type(last).__name__}: {str(last)[:150]}")


def minutes(t):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s*$", t or "", re.I)
    if not m:
        return None
    h, mi = int(m.group(1)) % 12, int(m.group(2))
    return (h + (12 if m.group(3).upper() == "PM" else 0)) * 60 + mi


def is_early(mins):
    return mins is not None and EARLY[0] <= mins <= EARLY[1]


def sort_key(s):
    m = s["mins"] if s["mins"] is not None else 0
    return (s["date"], m + (1440 if m < LATE_UNTIL else 0))


def variant(name, base_re=TARGET_RE):
    """'Jailer 2 (FDFS) (NC16)' -> 'FDFS'; '' for the plain title. Age ratings are dropped."""
    rest = base_re.sub("", name or "")
    tags = [t.strip() for t in re.findall(r"\(([^()]*)\)", rest)
            if t.strip() and not RATING_RE.match(t.strip())]
    loose = re.sub(r"\([^()]*\)", "", rest).strip(" -:·")
    if loose:
        tags.insert(0, loose)
    return " · ".join(tags)


def parse_sessions(movie, date):
    times = [x.strip() for x in (movie.get("showTime") or "").split(",") if x.strip()]
    sids = [x.strip() for x in (movie.get("longSessionID") or "").split(",")]
    out = []
    for i, raw in enumerate(times):
        has_flag = raw[-1:].upper() in ("T", "F")
        t = raw[:-1].strip() if has_flag else raw
        mins = minutes(t)
        out.append({"date": date, "time": re.sub(r"^0", "", t), "mins": mins, "early": is_early(mins),
                    "bookable": (raw[-1].upper() == "T") if has_flag else True,
                    "sid": sids[i] if i < len(sids) and sids[i] else f"{date}-{t}",
                    "movie": (movie.get("movieName") or "").strip()})
    return out


def check(session, test=False):
    """Returns (findings: key -> text, ctx). Keys:
         carnival:soon         Jailer 2 announced under Coming Soon
         carnival:any          Jailer 2 has any sessions at all
         carnival:show:<sid>   a session is listed
         carnival:open:<sid>   a session is bookable
    """
    findings, sessions = {}, []
    base_re = TARGET_RE

    soon = [m for m in _get(session, f"GetCommingSoonMovies?location={LOCATION}", "responseMovies")
            if TARGET_RE.search(m.get("name") or "")]
    if soon and not test:
        findings["carnival:soon"] = f"{soon[0].get('name')} listed as Coming Soon"

    dates = _get(session, f"GetShowDatesByCinema?location={LOCATION}&CinemaCode={CINEMA}",
                 "responseShowDates")
    everything = []
    for d in sorted({(x.get("showDateValue") or "")[:10] for x in dates if x.get("showDateValue")})[:MAX_DATES]:
        movies = _get(session, f"GetMoviesAndShowTimeByCinema?location={LOCATION}&cinemaCode={CINEMA}&date={d}",
                      "responseMoviesWithShowTime")
        for m in movies:
            everything.extend(parse_sessions(m, d))

    if test:
        # Jailer 2 isn't on sale yet: stand in with ONE real film (the one with the most sessions),
        # so the test card looks exactly like a real single-film Jailer 2 card.
        counts = {}
        for x in everything:
            counts[x["movie"]] = counts.get(x["movie"], 0) + 1
        stand_in = max(counts, key=lambda k: (counts[k], k)) if counts else ""
        base_re = re.compile(re.escape(stand_in), re.I) if stand_in else TARGET_RE
        sessions = [x for x in everything if x["movie"] == stand_in]
    else:
        sessions = [x for x in everything if TARGET_RE.search(x["movie"])]

    sessions.sort(key=sort_key)
    if sessions:
        findings["carnival:any"] = f"{TITLE} has {len(sessions)} session(s) on Carnival"
    for s in sessions:
        findings[f"carnival:show:{s['sid']}"] = f"{s['date']} {s['time']} listed"
        if s["bookable"]:
            findings[f"carnival:open:{s['sid']}"] = f"{s['date']} {s['time']} bookable"
    listings = sorted({x["movie"] for x in sessions})
    return findings, {"sessions": sessions, "soon": soon, "test": test, "base_re": base_re,
                      "listings": listings}


# ------------------------------------------------------------------ alert content

def class_name(raw):
    """'SOFA+1REPOPCORN' -> 'Sofa + Popcorn', 'PLATINUM' -> 'Platinum'."""
    n = re.sub(r"\s*\+\s*\d*\s*(RE)?\s*POPCORN", " + POPCORN", (raw or "").upper())
    return n.title().strip() or "Seats"


def seat_classes(session, sid):
    try:
        rows = _get(session, f"GetPriceList?cinemaCode={CINEMA}&lngSessionId={sid}", "responsePriceList")
    except CarnivalError:
        return []
    out = []
    for r in rows:
        status = (r.get("AvailableStatus") or "").strip()
        s = status.lower()
        state = "sold" if ("sold" in s or "full" in s) else "fast" if ("fast" in s or "filling" in s or "few" in s) \
            else "open"
        out.append({"name": class_name(r.get("ClassType")), "price": r.get("ClassPrice"),
                    "status": status or "—", "state": state})
    return out


def book_url(ctx):
    names = [s["movie"] for s in ctx["sessions"]]
    if not names:
        return SITE
    n = quote(names[0])
    return f"https://carnivalcinemas.sg/#/{n}/{n}"


def snapshot(session, ctx, mode, new_keys, now):
    new_sids = {k.split(":", 2)[2] for k in new_keys if k.startswith(("carnival:show:", "carnival:open:"))}
    sessions = ctx["sessions"]
    for s in sessions:
        s["new"] = mode != "test" and s["sid"] in new_sids
        s["tag"] = variant(s["movie"], ctx.get("base_re", TARGET_RE))
    all_days = sorted({s["date"] for s in sessions})
    # updates show the days where something changed; first alert shows the opening days
    focus = [d for d in all_days if any(s["new"] for s in sessions if s["date"] == d)] if mode == "update" else []
    card_days = (focus or all_days)[:CARD_DAYS]
    lookups = 0
    days = []
    for d in card_days:
        shows = [s for s in sessions if s["date"] == d]
        for s in shows:
            s["classes"] = []
            if s["bookable"] and lookups < MAX_PRICE_LOOKUPS:
                s["classes"] = seat_classes(session, s["sid"])
                lookups += 1
        days.append({"date": d, "first": bool(all_days) and d == all_days[0], "shows": shows})
    title = TITLE if not ctx.get("test") else (re.sub(r"\s*\([^()]*\)\s*$", "", sessions[0]["movie"])
                                               if sessions else "Carnival test")
    return {"title": title, "mode": mode, "checked_at": now, "days": days,
            "total": len(sessions), "bookable": sum(1 for s in sessions if s["bookable"]),
            "early": sum(1 for s in sessions if s["early"]),
            "all_days": all_days, "more_days": len(all_days) - len(card_days),
            "listings": ctx.get("listings", []),
            "new": [s for s in sessions if s["new"]]}


def _mark(s):
    return f"{s['time']}{' 🌅' if s['early'] else ''}{' ✅' if s['bookable'] else ' ⏳'}"


def caption(snap):
    head = {"live": f"🎬 <b>{TITLE.upper()} SHOWTIMES ARE OUT</b>",
            "update": f"🆕 <b>{TITLE}: new / newly bookable shows</b>",
            "test": f"🧪 <b>TEST RUN — Carnival</b> ({snap['title']})"}[snap["mode"]]
    lines = [head]
    if snap["mode"] == "update" and snap["new"]:
        lines.append("Just changed: " + ", ".join(
            f"{datetime.strptime(s['date'], '%Y-%m-%d'):%a %-d %b} {_mark(s)}" for s in snap["new"][:8])
            + (" …" if len(snap["new"]) > 8 else ""))
    else:
        by_day = {}
        for d in snap["days"]:
            by_day[d["date"]] = d["shows"]
        for d, shows in list(by_day.items())[:CARD_DAYS]:
            lines.append(f"<b>{datetime.strptime(d, '%Y-%m-%d'):%a %-d %b}</b>: " + ", ".join(_mark(s) for s in shows))
        if snap["more_days"]:
            lines.append(f"+{snap['more_days']} more day(s) on Carnival")
    early = f" · {snap['early']} early 🌅" if snap["early"] else ""
    lines.append(f"{snap['total']} shows · {snap['bookable']} bookable{early}  (✅ open · ⏳ not open yet)")
    return "\n".join(lines)


def text(snap):
    out = [caption(snap).split("\n")[0]]
    for d in snap["days"]:
        dt = datetime.strptime(d["date"], "%Y-%m-%d")
        out.append(f"\n<b>{dt:%a %-d %b}</b>{' (first day)' if d['first'] else ''}")
        out.append(", ".join(f"{_mark(s)}{' (' + s['tag'] + ')' if s['tag'] else ''}{' 🆕' if s.get('new') else ''}"
                             for s in d["shows"]))
    if snap["more_days"]:
        out.append(f"\n+{snap['more_days']} more day(s) on carnivalcinemas.sg")
    return "\n".join(out)
