"""
Doomsday Watch: alerts on Telegram when Avengers: Doomsday showtimes appear
on Shaw Theatres or Golden Village (any format, any cinema).

Runs once per invocation (GitHub Actions calls it every ~5 minutes).
State is kept in state.json so each finding is only alerted once.

Alerts are sent as a rendered dashboard card (card.py) with booking buttons.
If rendering or sending the photo fails, a plain-text alert goes out instead.

Env vars:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (required)
  TEST_MODE=true                          watch Endgame Encore instead (has showtimes now)
  HEALTHCHECK_URL                         optional dead-man's-switch ping (e.g. healthchecks.io)
"""

import html
import json
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests

SGT = timezone(timedelta(hours=8))
TEST_MODE = os.environ.get("TEST_MODE", "").lower() == "true"

if TEST_MODE:
    TITLE = "Avengers: Endgame Encore"
    TITLE_RE = re.compile(r"endgame", re.I)
    SHAW_MOVIE_IDS = ["1261"]          # Endgame Encore (standard) on Shaw
    GV_FILM_CODES = ["1465"]           # Endgame Encore (standard) on GV
    STATE_FILE = None                  # test runs never persist state
else:
    TITLE = "Avengers: Doomsday"
    TITLE_RE = re.compile(r"doomsday", re.I)
    SHAW_MOVIE_IDS = ["1112"]          # Avengers: Doomsday movieId on Shaw (release page 1633)
    GV_FILM_CODES = ["1395"]           # Avengers: Doomsday filmCd on GV
    STATE_FILE = "state.json"

GV_URL = "https://www.gv.com.sg/GVMovieDetails#/movie/1395"
SHAW_URL = "https://shaw.sg/movie-details/1633"

SHAW_BASE = "https://shaw.sg/internal"
SHAW_HEADERS = {"x-api-forward-to": "internal", "x-app": "PWSM"}
GV_BASE = "https://www.gv.com.sg/.gv-api"
GV_HEADERS = {"Content-Type": "application/json; charset=utf-8", "X_Developer": "ENOVAX",
              # what GV's own website sends; requests without these look more like a bot to GV's firewall
              "Origin": "https://www.gv.com.sg", "Referer": "https://www.gv.com.sg/",
              "Accept-Language": "en-SG,en;q=0.9"}
GV_BLOCK_RETRY_WAIT = 20      # seconds to wait before one fresh retry when GV answers 403 (blocked)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

FAIL_ALERT_AFTER = 3          # consecutive failed runs before an error alert (~15 min)
FAIL_ALERT_AFTER_SITE = {"GV": 6,   # other GV errors (e.g. GV changed its site): ~30 min
                         "DBS Sailing": 6}
# Since mid-Nov GV's firewall blocks GitHub's servers (403) for up to ~30 min at a time, in about half of all
# checks, while the checks in between still get through. Blocks alone only alert when GV has not been
# reachable at all for ~3 hours, which would mean GV is blocking GitHub for good.
GV_BLOCK_ALERT_AFTER = 36


def gv_blocked(e):
    """True when the site answered 403 Forbidden (a firewall block), not a timeout or other error."""
    return "403 Client Error" in str(e)


SITES = ("Shaw", "GV", "DBS Sailing")


def alert_after(site):
    return FAIL_ALERT_AFTER_SITE.get(site, FAIL_ALERT_AFTER)
HEARTBEAT_HOUR_SGT = 9        # daily "still watching" message after 9am SGT
SHAW_MAX_DATES = 21           # cap on per-date showtime lookups when building the card
# New dates and new cinemas for a listing the bot already knows also alert (Shaw opened 17–23 Dec under its
# existing Doomsday listing on 27 Nov and nothing fired). Dates only up to this day: after the opening run
# cinemas add a date every day, which would be a daily alert.
NEW_DATE_ALERTS_UNTIL = "2026-12-31"
SHAW_CINEMA_SAMPLE = (4, 2)   # showtime lookups per run for spotting new Shaw cinemas: first 4 + last 2 dates
DETAIL_KINDS = ("date", "cinema")


def key_code(k):
    """Film/listing code of a finding key: shaw:dates:1395 -> 1395, shaw:date:1395:2026-12-17 -> 1395."""
    parts = k.split(":")
    return parts[2] if len(parts) > 3 or parts[1] in DETAIL_KINDS + ("imaxdate",) else parts[-1]


class SiteError(Exception):
    pass


def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*"})
    return s


def get_json(session, url, **kw):
    last = None
    for _ in range(2):
        try:
            r = session.request(timeout=20, url=url, **kw)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(2)
    raise SiteError(f"{url.split('?')[0]} → {type(last).__name__}: {str(last)[:150]}")


def clean_title(t):
    return re.sub(r"\s+", " ", (t or "").replace("*", "")).strip()


def time_key(t):
    """'6:50PM' / '6:50 PM' / '1850' → minutes since midnight, for sorting."""
    t = (t or "").strip().upper().replace(" ", "")
    m = re.match(r"^(\d{1,2}):(\d{2})(AM|PM)$", t)
    if m:
        h, mi = int(m.group(1)) % 12, int(m.group(2))
        return (h + (12 if m.group(3) == "PM" else 0)) * 60 + mi
    if t.isdigit() and len(t) == 4:
        return int(t[:2]) * 60 + int(t[2:])
    return 0


# ---------------------------------------------------------------- Shaw

IMAX_RE = re.compile(r"\bIMAX\b", re.I)
IMAX_MAX_DAYS = 7             # dates shown on the IMAX timetable card
CINEMA_ORDER = ["Jewel", "Lido", "PLQ", "Jem", "Waterway Pt"]
CINEMA_SHORT = {"Waterway Point": "Waterway Pt", "Paya Lebar Quarter": "PLQ"}

SHAW_FORMATS = {"IMAX LASER": "IMAX Laser", "INFINITY VISION": "Infinity Vision",
                "DREAMERS": "Dreamers", "PREMIERE": "Premiere", "LUMIERE": "Lumiere"}


def shaw_format(name):
    m = re.search(r"\(([^()]*)\)\s*$", name or "")
    if not m:
        return "Standard"
    raw = re.sub(r"^\s*2D\s*\|\s*", "", m.group(1)).strip().upper()
    return SHAW_FORMATS.get(raw, raw.title().replace("Imax", "IMAX"))


def check_shaw(session):
    """Cheap check. Returns (findings: key -> text, ctx for building the card)."""
    findings = {}
    selectors = get_json(session, f"{SHAW_BASE}/get_selectors?cache=no-cache",
                         method="GET", headers=SHAW_HEADERS)
    if not isinstance(selectors, list):
        raise SiteError("Shaw get_selectors returned unexpected shape")

    locations = {str(s["code"]): s["name"].replace("Shaw Theatres ", "")
                 for s in selectors if s.get("type") == 2}
    # type 1 = movies that currently have bookable showtimes
    listed = [m for m in selectors if m.get("type") == 1 and TITLE_RE.search(m.get("name", ""))]
    names = {str(m["code"]): m["name"] for m in listed}
    for m in listed:
        findings[f"shaw:listed:{m['code']}"] = f"Shaw lists <b>{m['name']}</b> as bookable"

    dates_by_movie = {}
    for mid in dict.fromkeys(SHAW_MOVIE_IDS + list(names)):
        dates = get_json(session, f"{SHAW_BASE}/get_date_selectors?movieId={mid}",
                         method="GET", headers=SHAW_HEADERS)
        if not isinstance(dates, list):
            raise SiteError(f"Shaw get_date_selectors({mid}) returned unexpected shape")
        codes = sorted(d.get("code") for d in dates if d.get("code"))
        if codes:
            dates_by_movie[mid] = codes
            label = names.get(mid, TITLE)
            findings[f"shaw:dates:{mid}"] = (
                f"Shaw <b>{label}</b>: showtimes on {len(codes)} date(s), {codes[0]} → {codes[-1]}")
            if IMAX_RE.search(label):
                for dt in codes:          # every IMAX date is its own finding → new dates re-alert
                    findings[f"shaw:imaxdate:{mid}:{dt}"] = f"Shaw IMAX showtimes on {dt}"
            else:
                for dt in codes:          # new dates under a listing we already know (opening run only)
                    if dt <= NEW_DATE_ALERTS_UNTIL:
                        findings[f"shaw:date:{mid}:{dt}"] = f"Shaw {shaw_format(label)}: new date {dt}"
                first_n, last_n = SHAW_CINEMA_SAMPLE
                for dt in dict.fromkeys(codes[:first_n] + codes[-last_n:]):   # cinemas showing it
                    for movie in get_json(session, f"{SHAW_BASE}/get_show_times?date={dt}&movieId={mid}",
                                          method="GET", headers=SHAW_HEADERS) or []:
                        for st in movie.get("showTimes") or []:
                            loc = str(st.get("locationId"))
                            findings[f"shaw:cinema:{mid}:{loc}"] = (
                                f"Shaw {shaw_format(label)}: now at {locations.get(loc, loc)}")
    imax_codes = [mid for mid, n in names.items() if IMAX_RE.search(n)]
    return findings, {"names": names, "dates": dates_by_movie, "locations": locations,
                      "imax_codes": imax_codes}


def shaw_rows(session, ctx):
    rows = []
    for mid, dates in ctx["dates"].items():
        shows = []
        for dt in dates[:SHAW_MAX_DATES]:
            for movie in get_json(session, f"{SHAW_BASE}/get_show_times?date={dt}&movieId={mid}",
                                  method="GET", headers=SHAW_HEADERS) or []:
                shows.extend(movie.get("showTimes") or [])
        name = ctx["names"].get(mid) or (shows[0].get("primaryTitle") if shows else TITLE)
        cinemas = sorted({ctx["locations"].get(str(s.get("locationId")), str(s.get("locationId")))
                          for s in shows})
        venues = " ".join(s.get("locationVenueName") or "" for s in shows).lower()
        note = None
        extras = [n for n in ("Lumiere", "Premiere", "Dreamers") if n.lower() in venues]
        if shaw_format(name) == "Standard" and extras:
            note = "incl. " + " & ".join(extras)
        first = min(shows, key=lambda s: (s.get("displayDate", ""), time_key(s.get("displayTime"))),
                    default=None)
        rows.append({
            "chain": "Shaw", "code": mid, "label": shaw_format(name), "note": note,
            "cinemas": cinemas, "shows": len(shows), "dates": dates,
            "first": first and {"date": first["displayDate"], "time": first["displayTime"],
                                "cinema": first.get("locationVenueName") or "",
                                "sort": first["displayDate"] + f"{time_key(first['displayTime']):04d}"},
        })
    return rows


# ---------------------------------------------------------------- GV

GV_FORMATS = {"IV": "Infinity Vision", "ATMOS IV": "Dolby Atmos · Infinity Vision",
              "GVMAX": "GVmax", "ATMOS": "Dolby Atmos", "GOLD CLASS": "Gold Class",
              "3D": "3D", "D-BOX": "D-Box", "DREAMERS": "Dreamers"}


def gv_prefix(title):
    m = re.match(r"^\s*\(([^()]*)\)", title or "")
    return m.group(1).strip() if m else None


def gv_post(session, path, body=None):
    t = f"{random.randint(1, 1000)}_{int(time.time() * 1000)}"
    return get_json(session, f"{GV_BASE}/{path}?t={t}", method="POST", headers=GV_HEADERS,
                    data=json.dumps(body) if body is not None else None)


def check_gv(session):
    """Returns (findings, rows). GV's session data already has everything the card needs."""
    findings, rows = {}, []
    try:  # pick up any cookies the site sets; harmless if it fails
        session.get("https://www.gv.com.sg/", timeout=20)
    except requests.RequestException:
        pass

    films = {}
    for listing in ("nowshowing", "advancesales"):
        res = gv_post(session, listing)
        data = res.get("data") if isinstance(res, dict) else None
        if not isinstance(data, list):
            raise SiteError(f"GV {listing} returned unexpected shape")
        for m in data:
            title = m.get("filmTitle", "")
            if TITLE_RE.search(title):
                films[str(m.get("filmCd"))] = title
                findings[f"gv:listed:{m.get('filmCd')}"] = (
                    f"GV lists <b>{clean_title(title)}</b> under "
                    f"{'Advance Sales' if listing == 'advancesales' else 'Now Showing'}")

    for code in dict.fromkeys(GV_FILM_CODES + list(films)):
        res = gv_post(session, "sessionforfilm", {"filmCode": code})
        if not isinstance(res, dict) or not res.get("success", False):
            raise SiteError(f"GV sessionforfilm({code}) failed: {str(res)[:150]}")
        data = res.get("data")
        if not data:          # "No record found." → no showtimes yet
            continue
        title = data.get("filmTitle") or films.get(code) or code
        prefix = gv_prefix(title)

        groups = {}           # label -> list of (cinema, date, time24, time12)
        for loc in data.get("locations", []) or []:
            cinema = loc.get("name", "?")
            if prefix:
                label = GV_FORMATS.get(prefix.upper(), prefix)
            elif cinema.startswith("Gold Class"):
                label = "Gold Class"
            elif cinema.upper().startswith("GVMAX"):
                label = "GVmax"
            else:
                label = "Standard"
            for d in loc.get("dates", []) or []:
                day = datetime.fromtimestamp(d["date"] / 1000, SGT).strftime("%Y-%m-%d")
                for t in d.get("times", []) or []:
                    groups.setdefault(label, []).append(
                        (cinema, day, t.get("time24") or "", t.get("time12") or ""))

        if not groups:
            continue
        all_dates = sorted({s[1] for g in groups.values() for s in g})
        findings[f"gv:sessions:{code}"] = (
            f"GV <b>{clean_title(title)}</b>: showtimes at "
            f"{len({s[0] for g in groups.values() for s in g})} cinema(s), "
            f"{all_dates[0]} → {all_dates[-1]}")
        for label, shows in groups.items():
            for cinema, day, _, _ in shows:
                findings[f"gv:cinema:{code}:{cinema}"] = f"GV {label}: now at {cinema}"
                if day <= NEW_DATE_ALERTS_UNTIL:
                    findings[f"gv:date:{code}:{day}"] = f"GV {label}: new date {day}"
            first = min(shows, key=lambda s: (s[1], s[2]))
            rows.append({
                "chain": "GV", "code": code, "label": label, "note": None,
                "cinemas": sorted({s[0] for s in shows}), "shows": len(shows),
                "dates": sorted({s[1] for s in shows}),
                "first": {"date": first[1], "time": first[3], "cinema": first[0],
                          "sort": first[1] + f"{time_key(first[2] or first[3]):04d}"},
            })
    return findings, rows


# ---------------------------------------------------------------- DBS Sailing At The Bay
#
# www.dbs.com/sailing/index.html is a plain static page. Each month's sign-up is one red button in the
# "regBtn" block, e.g.  <a href="https://cvent.me/5eZQML" class="redBtn ripple">November Registration</a>
# ("Session bookings open one week prior to the start of each month"). We read every registration link
# on the page (text + link) and alert the Jailer chats when one we have never seen appears: a new
# "December Registration" button, the old button renamed, or the same text pointing at a new link.

DBS_URL = "https://www.dbs.com/sailing/index.html"
DBS_REG_RE = re.compile(r"regist|sign[\s-]*up|\bbook|cvent", re.I)
DBS_MONTH_RE = re.compile(r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
                          r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b", re.I)
DBS_WANTED_RE = re.compile(r"\bdec(?:ember)?\b", re.I)   # the month we're waiting for (also alerted on first run)


def dbs_links(page):
    """Registration links on the page as [(text, href)], in page order. Ignores commented-out HTML,
    the FAQ anchor, e-mail links and share buttons."""
    page = re.sub(r"<!--.*?-->", "", page, flags=re.S)
    out = []
    for attrs, inner in re.findall(r"<a\b([^>]*)>(.*?)</a>", page, flags=re.S | re.I):
        m = re.search(r"""href\s*=\s*["']([^"']*)["']""", attrs, re.I)
        href = html.unescape(m.group(1).strip()) if m else ""
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", inner))).strip()
        if href.startswith(("#", "mailto:", "tel:")) or "sharer" in href or "share?" in href:
            continue
        if DBS_REG_RE.search(text) or DBS_REG_RE.search(href) or "redBtn" in attrs:
            if (text, href) not in out:
                out.append((text, href))
    return out


def check_dbs(session):
    """-> [(text, href)] registration links on the DBS sailing page right now."""
    last = None
    for url, params in ((DBS_URL, {"nocache": int(time.time())}), (DBS_URL, None)):   # fresh copy first
        try:
            r = session.get(url, params=params, timeout=20,
                            headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                                     "Accept-Language": "en-SG,en;q=0.9", "Cache-Control": "no-cache"})
            r.raise_for_status()
            break
        except requests.RequestException as e:
            last = e
            time.sleep(2)
    else:
        raise SiteError(f"{DBS_URL} → {type(last).__name__}: {str(last)[:150]}")
    if not re.search(r"<title>[^<]*sailing", r.text, re.I):
        raise SiteError("DBS sailing page didn't look right (bot check or redesign?): "
                        + re.sub(r"\s+", " ", r.text[:150]))
    return dbs_links(r.text)


def dbs_sig(link):
    return f"{link[0]} | {link[1]}"


def dbs_chat_ids():
    """CARNIVAL_CHAT_ID (comma-separated) = you + the Jailer group. Unset → you only, never the Doomsday chats."""
    ids = [c.strip() for c in os.environ.get("CARNIVAL_CHAT_ID", "").split(",") if c.strip()]
    return ids or chat_ids()[:1]


def dbs_markup(links):
    rows = [[{"text": f"⛵ {t or 'Register'}"[:60], "url": h}] for t, h in links[:3] if h.startswith("http")]
    return {"inline_keyboard": rows + [[{"text": "DBS Sailing page", "url": DBS_URL}]]}


MONTHS = {m[:3]: m for m in ("January", "February", "March", "April", "May", "June", "July", "August",
                              "September", "October", "November", "December")}


def dbs_month(links):
    """Full month name mentioned in the first link text that has one ('December Registration' -> 'December')."""
    for t, h in links:
        m = DBS_MONTH_RE.search(t)
        if m:
            return MONTHS.get(m.group(0)[:3].title())
    return None


def send_dbs_alert(new_links, all_links, test=False):
    """Sailing card (picture) + emoji caption + Register button; plain text where the picture fails.
    Real alerts AND the test-run card go to the DBS chats (you + the Jailer group)."""
    wanted = [l for l in new_links if DBS_WANTED_RE.search(l[0])]
    month = "December" if test else dbs_month(wanted or new_links)
    link_lines = "\n".join(f"• “{t or 'button'}” → {h}" for t, h in (all_links if test else new_links))
    if test:
        caption = ("🧪 <b>TEST RUN — DBS Sailing</b>\nPage OK. On the page now:\n"
                   + (link_lines or "• no registration button right now")
                   + "\n\nThis is a test: it's what the alert will look like when December opens ⛵")
    elif month:
        caption = (f"⛵🌊 <b>You can book {month} slots now!</b>\nGet ready to sail! ☀️⚓\n\n{link_lines}"
                   f"\n\nTap Register below 👇")
    else:
        caption = (f"⛵🌊 <b>New DBS sailing registration is up!</b>\nGet ready to sail! ☀️⚓\n\n{link_lines}"
                   f"\n\nTap Register below 👇")
    markup = dbs_markup(all_links if test else new_links)
    try:
        import card
        png = card.render_sailing(month, test=test, checked_at=None if test else datetime.now(SGT))
    except Exception:  # noqa: BLE001 — the alert still goes out as text
        traceback.print_exc()
        png = None
    # the DBS test card goes to the same chats as a real DBS alert (you + the Jailer group)
    deliver(png, caption, caption, markup, chats=dbs_chat_ids(), test_ok=test)


# ---------------------------------------------------------------- Telegram

def tg(method, **kw):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", timeout=30, **kw)
    if not r.ok:
        print(f"Telegram {method} error {r.status_code}: {r.text[:200]}", file=sys.stderr)
        r.raise_for_status()
    return r


def buttons():
    return {"inline_keyboard": [[{"text": "🎟 Book at GV", "url": GV_URL},
                                 {"text": "🎟 Book at Shaw", "url": SHAW_URL}]]}


def chat_ids():
    """TELEGRAM_CHAT_ID may hold several comma-separated IDs. The FIRST is the owner:
    heartbeats and error messages go only there; ticket alerts go to everyone."""
    ids = [c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()]
    if not ids:
        raise RuntimeError("TELEGRAM_CHAT_ID is empty")
    return ids


def send_telegram(text, with_buttons=False, everyone=False, silent=False, chats=None, markup=None):
    targets = chats or (chat_ids() if everyone else chat_ids()[:1])
    errors = []
    for chat in targets:
        payload = {"chat_id": chat, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True, "disable_notification": silent}
        if with_buttons:
            payload["reply_markup"] = buttons()
        if markup:
            payload["reply_markup"] = markup
        try:
            tg("sendMessage", json=payload)
        except Exception as e:  # noqa: BLE001 — one bad chat must not block the others
            errors.append((chat, e))
    if errors and len(errors) == len(targets):
        raise errors[0][1]


def send_card(chat, png, caption):
    tg("sendPhoto", data={"chat_id": chat, "caption": caption,
                          "parse_mode": "HTML", "reply_markup": json.dumps(buttons())},
       files={"photo": ("doomsday.png", png, "image/png")})


def caption_for(mode, rows, new_rows, extra=()):
    by = lambda c: sorted({r["label"] for r in rows if r["chain"] == c})  # noqa: E731
    firsts = [r["first"] for r in rows if r.get("first")]
    first = min(firsts, key=lambda f: f["sort"]) if firsts else None
    first_line = (f"\nEarliest: {datetime.strptime(first['date'], '%Y-%m-%d'):%a %-d %b}, "
                  f"{first['time']} · {first['cinema']}") if first else ""
    chains = " · ".join(f"{c}: {len(by(c))} format{'s' if len(by(c)) != 1 else ''}"
                        for c in ("GV", "Shaw") if by(c))
    if mode == "test":
        return f"🧪 <b>TEST RUN</b> — {TITLE}\n{chains}{first_line}"
    if mode == "live":
        return f"🚨 <b>AVENGERS: DOOMSDAY TICKETS ARE LIVE</b>\n{chains}{first_line}"
    news = "\n".join([f"• {r['chain']} {r['label']}" for r in new_rows] + [f"• {x}" for x in extra]) \
        or "• more showtimes"
    return f"🆕 <b>New Doomsday showtimes</b>\n{news}"


def _day(d):
    return f"{datetime.strptime(d, '%Y-%m-%d'):%a %-d %b}"


def detail_lines(keys, findings, shaw_ctx=None):
    """One line per listing for new dates / new cinemas, e.g.
       'Shaw Infinity Vision: new dates Thu 17 Dec – Wed 23 Dec (7 days)'  /  'GV Standard: now at GV Jurong'."""
    out, groups = [], {}
    names = (shaw_ctx or {}).get("names", {})
    for k in keys:
        chain, kind = k.split(":")[0], k.split(":")[1]
        if kind not in DETAIL_KINDS:
            out.append(findings.get(k, k))
            continue
        label = findings.get(k, "").split(":")[0]           # "Shaw Infinity Vision" / "GV Gold Class"
        g = groups.setdefault((label or chain, key_code(k)), {"date": [], "cinema": []})
        g[kind].append(k.split(":", 3)[3])
    for (label, code), g in groups.items():
        if g["date"]:
            ds = sorted(g["date"])
            span = _day(ds[0]) if len(ds) == 1 else f"{_day(ds[0])} – {_day(ds[-1])} ({len(ds)} days)"
            out.append(f"{label}: new date{'s' if len(ds) > 1 else ''} {span}")
        if g["cinema"]:
            where = sorted({findings.get(f"{'gv' if label.startswith('GV') else 'shaw'}:cinema:{code}:{c}", c)
                            .split("now at ")[-1] for c in g["cinema"]})
            out.append(f"{label}: now at {', '.join(where)}")
    return out


def send_alert(mode, findings, new_keys, shaw_ctx, gv_rows):
    """Ticket alerts go to every chat. Card first; plain text for any chat where the card fails."""
    head = {"test": f"🧪 <b>TEST RUN</b> ({TITLE})",
            "live": "🚨🚨 <b>AVENGERS: DOOMSDAY TICKETS ARE OUT!</b>",
            "update": "🆕 <b>More Doomsday showtimes/formats just opened</b>"}[mode]
    items = findings if mode == "test" else {k: findings[k] for k in new_keys}
    # dates/cinemas of a listing that is itself new are already covered by that listing's line
    fresh = {(k.split(":")[0], key_code(k)) for k in new_keys if k.split(":")[1] not in DETAIL_KINDS}
    details = [k for k in new_keys if k.split(":")[1] in DETAIL_KINDS and (k.split(":")[0], key_code(k)) not in fresh]
    if mode == "update":
        lines = detail_lines([k for k in new_keys if k.split(":")[1] not in DETAIL_KINDS] + details,
                             findings, shaw_ctx)
        text = f"{head}\n" + "\n".join(f"• {v}" for v in lines)
    else:
        text = f"{head}\n" + ("\n".join(f"• {v}" for k, v in items.items()
                                       if k.split(":")[1] not in DETAIL_KINDS) or "• nothing found")

    png = caption = None
    try:
        import card
        rows = list(gv_rows)
        if shaw_ctx and shaw_ctx["dates"]:
            rows += shaw_rows(make_session(), shaw_ctx)
        for r in rows:
            prefix = "gv" if r["chain"] == "GV" else "shaw"
            r["new"] = mode != "test" and any(k.startswith(f"{prefix}:") and key_code(k) == str(r["code"])
                                              for k in new_keys)
        order = {"Standard": 0, "Gold Class": 1}
        rows.sort(key=lambda r: (order.get(r["label"], 2), -r["shows"]))
        png = card.render({"title": TITLE, "mode": mode, "checked_at": datetime.now(SGT),
                           "rows": rows})
        # new listings with no showtimes yet have no row on the card, so name them in the caption
        drawn = {(("gv" if r["chain"] == "GV" else "shaw"), str(r["code"])) for r in rows}
        extra = ([f"{findings[k]} (no showtimes yet)" for k in new_keys
                  if k.split(":")[1] == "listed" and (k.split(":")[0], key_code(k)) not in drawn]
                 + detail_lines(details, findings, shaw_ctx)
                 ) if mode == "update" else []
        new_rows = [r for r in rows if r.get("new")
                    and any(k.split(":")[1] not in DETAIL_KINDS and key_code(k) == str(r["code"]) for k in new_keys)]
        caption = caption_for(mode, rows, new_rows, extra)
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    deliver(png, caption, text, buttons())


def imax_snapshot(session, ctx, mode, new_dates, title=None):
    per_day = {}
    total = 0
    release_id = None
    for mid in ctx.get("imax_codes", []):
        for dt in ctx["dates"].get(mid, [])[:SHAW_MAX_DATES]:
            for movie in get_json(session, f"{SHAW_BASE}/get_show_times?date={dt}&movieId={mid}",
                                  method="GET", headers=SHAW_HEADERS) or []:
                for sh in movie.get("showTimes") or []:
                    release_id = release_id or sh.get("movieReleaseId")
                    venue = (sh.get("locationVenueName")
                             or ctx["locations"].get(str(sh.get("locationId")), "?"))
                    cin = re.sub(r"\s*IMAX\s*", " ", venue).strip()
                    cin = CINEMA_SHORT.get(cin, cin)
                    per_day.setdefault(sh.get("displayDate") or dt, {}).setdefault(cin, []).append(
                        {"time": sh.get("displayTime", "?"), "status": sh.get("seatingStatus")})
                    total += 1
    for day in per_day.values():
        for lst in day.values():
            lst.sort(key=lambda x: time_key(x["time"]))
    dates = sorted(per_day)
    if mode == "update" and new_dates:
        start = min(new_dates)
        dates_to_show = [x for x in dates if x >= start][:IMAX_MAX_DAYS]
    else:
        dates_to_show = dates[:IMAX_MAX_DAYS]
    seen_cins = {c for day in per_day.values() for c in day}
    cinemas = [c for c in CINEMA_ORDER if c in seen_cins] + sorted(seen_cins - set(CINEMA_ORDER))
    return {"title": title or TITLE, "mode": mode, "checked_at": datetime.now(SGT), "cinemas": cinemas,
            "days": [{"date": x, "new": x in new_dates, "shows": per_day[x]} for x in dates_to_show],
            "more_days": len(dates) - len(dates_to_show), "total_shows": total, "release_id": release_id}


def imax_text(snap):
    lines = []
    for day in snap["days"]:
        dt = datetime.strptime(day["date"], "%Y-%m-%d")
        parts = [f"{c} " + ", ".join(x["time"] + (" (sold out)" if (x["status"] or "").upper() == "SO" else "")
                                     for x in day["shows"][c])
                 for c in snap["cinemas"] if day["shows"].get(c)]
        lines.append(f"<b>{dt:%a %-d %b}</b>{' 🆕' if day['new'] and snap['mode'] == 'update' else ''}: "
                     + " · ".join(parts))
    if snap["more_days"]:
        lines.append(f"+{snap['more_days']} more date(s) on shaw.sg")
    return "\n".join(lines)


def deliver(png, caption, text, markup, chats=None, test_ok=False):
    """Send to every chat: photo+caption, or text if the photo fails for that chat.
    Test runs only go to the owner (first TELEGRAM_CHAT_ID), never to groups,
    unless the caller says groups may see this test (test_ok: the DBS sailing test card)."""
    delivered = 0
    if TEST_MODE and not test_ok:
        chats = chat_ids()[:1]
    for chat in chats or chat_ids():
        try:
            if png is not None:
                try:
                    tg("sendPhoto", data={"chat_id": chat, "caption": caption, "parse_mode": "HTML",
                                          "reply_markup": json.dumps(markup)},
                       files={"photo": ("doomsday.png", png, "image/png")})
                    delivered += 1
                    continue
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
            tg("sendMessage", json={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                                    "disable_web_page_preview": True, "reply_markup": markup})
            delivered += 1
        except Exception:  # noqa: BLE001 — keep going so other chats still get the alert
            traceback.print_exc()
    if not delivered:
        raise RuntimeError("Alert could not be delivered to any chat")


def send_imax_alert(mode, shaw_ctx, new_dates, title=None):
    title = title or TITLE
    snap = imax_snapshot(make_session(), shaw_ctx, mode, set(new_dates), title=title)
    markup = {"inline_keyboard": [[{"text": "🎟 Book IMAX at Shaw", "url": SHAW_URL}]]}
    snap["brand"] = "DOOMSDAY WATCH"
    head = {"live": f"🎯 <b>IMAX IS OPEN — {title}</b>",
            "update": f"🎯 <b>New IMAX dates — {title}</b>",
            "test": f"🧪 <b>TEST RUN — IMAX</b> ({title}"
                    + (", stand-in: the test film has no IMAX shows now)" if title != TITLE else ")")}[mode]
    if mode == "update" and new_dates:
        sub = "Just added: " + ", ".join(datetime.strptime(x, "%Y-%m-%d").strftime("%a %-d %b")
                                         for x in sorted(new_dates))
    else:
        sub = f"{snap['total_shows']} IMAX showtimes at {len(snap['cinemas'])} cinemas"
    png = None
    try:
        import card
        png = card.render_imax(snap)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    deliver(png, f"{head}\n{sub}", f"{head}\n{imax_text(snap)}", markup)


def imax_test_stand_in(session):
    """Test runs only: when the test film has no IMAX shows on Shaw, borrow whichever IMAX film Shaw
    has bookable right now, so the IMAX card still gets tested. Returns (ctx, title) or (None, None)."""
    selectors = get_json(session, f"{SHAW_BASE}/get_selectors?cache=no-cache", method="GET", headers=SHAW_HEADERS)
    locations = {str(x["code"]): x["name"].replace("Shaw Theatres ", "")
                 for x in selectors if x.get("type") == 2}
    for m in (x for x in selectors if x.get("type") == 1 and IMAX_RE.search(x.get("name", ""))):
        mid = str(m["code"])
        dates = get_json(session, f"{SHAW_BASE}/get_date_selectors?movieId={mid}", method="GET", headers=SHAW_HEADERS)
        codes = sorted(d.get("code") for d in dates or [] if d.get("code"))
        if codes:
            title = re.sub(r"\s*\([^()]*\)", "", m["name"]).strip() or m["name"]
            return {"names": {mid: m["name"]}, "dates": {mid: codes}, "locations": locations,
                    "imax_codes": [mid]}, title
    return None, None


# ---------------------------------------------------------------- state / main

def load_state():
    if STATE_FILE and os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    if STATE_FILE:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write("\n")


def run():
    state = load_state()
    seen = set(state.get("seen", []))
    first_ever = not seen     # nothing alerted yet → the next ticket alert is the big 'tickets are live' one
    failures = state.get("failures", {})
    # sites we've sent a ⚠️ for, so the ✅ goes out exactly once when they recover
    # (first run on this version: the old code had alerted for anything at/over its old thresholds)
    alerted = set(state.get("alerted", [k for k, v in failures.items() if v >= alert_after(k)]))
    soft_fails = state.get("other_failures", {})   # failures in the current streak that were NOT GV blocks
    now = datetime.now(SGT)
    findings, ok_sites = {}, []
    shaw_ctx, gv_rows = None, []
    dbs_now = None            # registration links on the DBS sailing page, if that check worked

    for site in SITES:
        try:
            if site == "Shaw":
                f, shaw_ctx = check_shaw(make_session())
            elif site == "DBS Sailing":
                f, dbs_now = {}, check_dbs(make_session())
            else:
                try:
                    f, gv_rows = check_gv(make_session())
                except SiteError as e:
                    if not gv_blocked(e):
                        raise
                    # blocked: wait a little, then try once more with a fresh session (new cookies)
                    print(f"[GV] blocked ({e}); retrying in {GV_BLOCK_RETRY_WAIT}s", file=sys.stderr)
                    time.sleep(GV_BLOCK_RETRY_WAIT)
                    f, gv_rows = check_gv(make_session())
            findings.update(f)
            ok_sites.append(site)
            if site in alerted and not TEST_MODE:
                send_telegram(f"✅ {site} checks are working again.")
            alerted.discard(site)
            failures[site] = soft_fails[site] = 0
        except Exception as e:  # noqa: BLE001 — any failure counts, keep checking the other site
            failures[site] = failures.get(site, 0) + 1
            print(f"[{site}] {e}", file=sys.stderr)
            blocked = site == "GV" and gv_blocked(e)
            if not blocked:
                soft_fails[site] = soft_fails.get(site, 0) + 1
            due = (failures[site] >= GV_BLOCK_ALERT_AFTER) if blocked else (soft_fails[site] >= alert_after(site))
            if TEST_MODE or (due and site not in alerted):
                mins = failures[site] * 5
                if blocked:
                    head = (f"⚠️ {site} has blocked every check for ~{mins // 60}h {mins % 60:02d}m. "
                            f"Short blocks are normal now, but this long may mean {site} is blocking "
                            f"GitHub for good. Other checks keep running."
                            if mins >= 60 else f"⚠️ {site} is blocking the watcher (403) for ~{mins} min.")
                else:
                    head = f"⚠️ {site} check failing ({failures[site]}x in a row):"
                send_telegram(f"{head}\n<code>{str(e)[:300]}</code>")
                if not TEST_MODE:
                    alerted.add(site)

    if not TEST_MODE:
        baselined = set(state.get("detail_baselined", []))
        for site, prefix in (("Shaw", "shaw:"), ("GV", "gv:")):
            if site in ok_sites and site not in baselined:
                seen |= {k for k in findings if k.startswith(prefix) and k.split(":")[1] in DETAIL_KINDS}
                baselined.add(site)
        state["detail_baselined"] = sorted(baselined)
    new_keys = [k for k in findings if k not in seen]
    print(f"{now:%Y-%m-%d %H:%M} SGT | ok={ok_sites} | findings={len(findings)} | new={len(new_keys)}")

    imax_codes = set((shaw_ctx or {}).get("imax_codes", []))

    def is_imax(k):
        return k.startswith("shaw:imaxdate:") or (k.startswith("shaw:") and key_code(k) in imax_codes)

    if TEST_MODE:
        send_alert("test", findings, new_keys, shaw_ctx, gv_rows)
        if imax_codes:
            send_imax_alert("test", shaw_ctx, [])
        else:
            try:
                ctx, title = imax_test_stand_in(make_session())
                if ctx:
                    send_imax_alert("test", ctx, [], title=title)
                else:
                    send_telegram("🧪 <b>TEST RUN — IMAX</b>: Shaw has no IMAX showtimes on sale right now, "
                                  "so there was nothing to draw the IMAX card with.")
            except Exception as e:  # noqa: BLE001
                send_telegram(f"🧪 <b>TEST RUN — IMAX</b> card failed:\n<code>{str(e)[:300]}</code>")
        if dbs_now is not None:
            send_dbs_alert([], dbs_now, test=True)
        return

    new_imax_dates = sorted({k.split(":")[-1] for k in new_keys if k.startswith("shaw:imaxdate:")})
    if new_imax_dates:
        first_imax = not any(k.startswith("shaw:imaxdate:") for k in seen)
        try:
            send_imax_alert("live" if first_imax else "update", shaw_ctx, new_imax_dates)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    other = [k for k in new_keys if not is_imax(k)]
    if other:
        send_alert("live" if first_ever else "update", findings, new_keys, shaw_ctx, gv_rows)
    seen |= set(new_keys)

    if dbs_now is not None:
        dbs_seen = state.get("dbs_seen")
        if dbs_seen is None:   # first run: remember what's there now; only alert if December is already up
            new_dbs = [l for l in dbs_now if DBS_WANTED_RE.search(l[0])]
        else:
            new_dbs = [l for l in dbs_now if dbs_sig(l) not in dbs_seen]
        try:
            if new_dbs:
                send_dbs_alert(new_dbs, dbs_now)
            # remember every link ever shown, so a flickering page/CDN can't alert twice for the same one
            state["dbs_seen"] = sorted(set(dbs_seen or []) | {dbs_sig(l) for l in dbs_now})
        except Exception:  # noqa: BLE001 — not marked as seen, so the next run tries again
            traceback.print_exc()
        state["dbs_now"] = [t for t, h in dbs_now]

    today = now.strftime("%Y-%m-%d")
    if now.hour >= HEARTBEAT_HOUR_SGT and state.get("heartbeat") != today:
        status = "tickets are OUT (see earlier alerts)" if seen else "no showtimes yet"
        dbs_line = (f"DBS Sailing: page shows {', '.join(f'“{t}”' for t, h in dbs_now) or 'no registration button'}"
                    if dbs_now is not None else "DBS Sailing: check failed this run")
        send_telegram(f"👀 Watch still running.\nDoomsday: {status}\n{dbs_line}\n"
                      f"Sites OK: {', '.join(ok_sites) or 'none!'}")
        state["heartbeat"] = today

    state["seen"] = sorted(seen)
    for old in ("carnival_seen", "jailer_imax_seen", "mycinemas_seen"):   # Jailer 2 tracking was removed
        state.pop(old, None)
    state["failures"] = {k: v for k, v in failures.items() if k in SITES}
    state["alerted"] = sorted(alerted)
    state["other_failures"] = {k: v for k, v in soft_fails.items() if k in SITES}
    save_state(state)

    hc = os.environ.get("HEALTHCHECK_URL")
    if hc and ok_sites:
        try:
            requests.get(hc, timeout=10)
        except requests.RequestException:
            pass


if __name__ == "__main__":
    run()
