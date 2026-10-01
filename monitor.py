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

import json
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests

import carnival

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
GV_HEADERS = {"Content-Type": "application/json; charset=utf-8", "X_Developer": "ENOVAX"}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

FAIL_ALERT_AFTER = 3          # consecutive failed runs before an error alert (~15 min)
HEARTBEAT_HOUR_SGT = 9        # daily "still watching" message after 9am SGT
SHAW_MAX_DATES = 21           # cap on per-date showtime lookups when building the card


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
            first = min(shows, key=lambda s: (s[1], s[2]))
            rows.append({
                "chain": "GV", "code": code, "label": label, "note": None,
                "cinemas": sorted({s[0] for s in shows}), "shows": len(shows),
                "dates": sorted({s[1] for s in shows}),
                "first": {"date": first[1], "time": first[3], "cinema": first[0],
                          "sort": first[1] + f"{time_key(first[2] or first[3]):04d}"},
            })
    return findings, rows


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


def carnival_chat_ids():
    """Optional CARNIVAL_CHAT_ID secret sends Jailer 2 alerts to different chats; default = everyone."""
    ids = [c.strip() for c in os.environ.get("CARNIVAL_CHAT_ID", "").split(",") if c.strip()]
    return ids or chat_ids()


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


def caption_for(mode, rows, new_rows):
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
    news = "\n".join(f"• {r['chain']} {r['label']}" for r in new_rows) or "• more showtimes"
    return f"🆕 <b>New Doomsday showtimes</b>\n{news}"


def send_alert(mode, findings, new_keys, shaw_ctx, gv_rows):
    """Ticket alerts go to every chat. Card first; plain text for any chat where the card fails."""
    head = {"test": f"🧪 <b>TEST RUN</b> ({TITLE})",
            "live": "🚨🚨 <b>AVENGERS: DOOMSDAY TICKETS ARE OUT!</b>",
            "update": "🆕 <b>More Doomsday showtimes/formats just opened</b>"}[mode]
    items = findings if mode == "test" else {k: findings[k] for k in new_keys}
    text = f"{head}\n" + ("\n".join(f"• {v}" for v in items.values()) or "• nothing found")

    png = caption = None
    try:
        import card
        rows = list(gv_rows)
        if shaw_ctx and shaw_ctx["dates"]:
            rows += shaw_rows(make_session(), shaw_ctx)
        for r in rows:
            prefix = "gv" if r["chain"] == "GV" else "shaw"
            r["new"] = mode != "test" and any(k.startswith(f"{prefix}:") and k.endswith(f":{r['code']}")
                                              for k in new_keys)
        order = {"Standard": 0, "Gold Class": 1}
        rows.sort(key=lambda r: (order.get(r["label"], 2), -r["shows"]))
        png = card.render({"title": TITLE, "mode": mode, "checked_at": datetime.now(SGT),
                           "rows": rows})
        caption = caption_for(mode, rows, [r for r in rows if r.get("new")])
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    deliver(png, caption, text, buttons())


def imax_snapshot(session, ctx, mode, new_dates):
    per_day = {}
    total = 0
    for mid in ctx.get("imax_codes", []):
        for dt in ctx["dates"].get(mid, [])[:SHAW_MAX_DATES]:
            for movie in get_json(session, f"{SHAW_BASE}/get_show_times?date={dt}&movieId={mid}",
                                  method="GET", headers=SHAW_HEADERS) or []:
                for sh in movie.get("showTimes") or []:
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
    return {"title": TITLE, "mode": mode, "checked_at": datetime.now(SGT), "cinemas": cinemas,
            "days": [{"date": x, "new": x in new_dates, "shows": per_day[x]} for x in dates_to_show],
            "more_days": len(dates) - len(dates_to_show), "total_shows": total}


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


def deliver(png, caption, text, markup, chats=None):
    """Send to every chat: photo+caption, or text if the photo fails for that chat.
    Test runs only ever go to the owner (first TELEGRAM_CHAT_ID), never to groups / CARNIVAL_CHAT_ID."""
    delivered = 0
    if TEST_MODE:
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


def send_imax_alert(mode, shaw_ctx, new_dates):
    markup = {"inline_keyboard": [[{"text": "🎟 Book IMAX at Shaw", "url": SHAW_URL}]]}
    snap = imax_snapshot(make_session(), shaw_ctx, mode, set(new_dates))
    head = {"live": f"🎯 <b>IMAX IS OPEN — {TITLE}</b>",
            "update": f"🎯 <b>New IMAX dates — {TITLE}</b>",
            "test": f"🧪 <b>TEST RUN — IMAX</b> ({TITLE})"}[mode]
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


def send_carnival_alert(mode, ctx, new_keys):
    snap = carnival.snapshot(make_session(), ctx, mode, new_keys, datetime.now(SGT))
    markup = {"inline_keyboard": [[{"text": "🎟 Book on Carnival", "url": carnival.book_url(ctx)}]]}
    png = None
    try:
        import card
        png = card.render_jailer(snap)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    deliver(png, carnival.caption(snap)[:1000], carnival.text(snap), markup, chats=carnival_chat_ids())


def handle_carnival(cf, ctx, car_seen):
    """Alert on any new / newly bookable Jailer 2 show; quiet note when it's announced. Returns updated seen."""
    new = [k for k in cf if k not in car_seen]
    if TEST_MODE:
        send_carnival_alert("test", ctx, [])
        return car_seen
    show_new = [k for k in new if k.startswith(("carnival:show:", "carnival:open:"))]
    if show_new:
        first = not any(k.startswith("carnival:show:") for k in car_seen)
        send_carnival_alert("live" if first else "update", ctx, show_new)
    if "carnival:soon" in new:
        link = {"inline_keyboard": [[{"text": "🎟 Carnival Cinemas", "url": carnival.book_url(ctx)}]]}
        send_telegram(f"👀 {carnival.TITLE} is now listed as Coming Soon on Carnival. "
                      f"You'll get an alert the moment any showtime drops.",
                      silent=True, chats=carnival_chat_ids(), markup=link)
    return car_seen | set(new)


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
    failures = state.get("failures", {})
    now = datetime.now(SGT)
    findings, ok_sites = {}, []
    shaw_ctx, gv_rows = None, []

    for site in ("Shaw", "GV"):
        try:
            if site == "Shaw":
                f, shaw_ctx = check_shaw(make_session())
            else:
                f, gv_rows = check_gv(make_session())
            findings.update(f)
            ok_sites.append(site)
            if failures.get(site, 0) >= FAIL_ALERT_AFTER and not TEST_MODE:
                send_telegram(f"✅ {site} checks are working again.")
            failures[site] = 0
        except Exception as e:  # noqa: BLE001 — any failure counts, keep checking the other site
            failures[site] = failures.get(site, 0) + 1
            print(f"[{site}] {e}", file=sys.stderr)
            if TEST_MODE or failures[site] == FAIL_ALERT_AFTER:
                send_telegram(f"⚠️ {site} check failing ({failures[site]}x in a row):\n"
                              f"<code>{str(e)[:300]}</code>")

    car_seen = set(state.get("carnival_seen", []))
    car_findings, car_ctx = None, None
    try:
        car_findings, car_ctx = carnival.check(make_session(), test=TEST_MODE)
        ok_sites.append("Carnival")
        if failures.get("Carnival", 0) >= FAIL_ALERT_AFTER and not TEST_MODE:
            send_telegram("✅ Carnival checks are working again.")
        failures["Carnival"] = 0
    except Exception as e:  # noqa: BLE001
        failures["Carnival"] = failures.get("Carnival", 0) + 1
        print(f"[Carnival] {e}", file=sys.stderr)
        if TEST_MODE or failures["Carnival"] == FAIL_ALERT_AFTER:
            send_telegram(f"⚠️ Carnival check failing ({failures['Carnival']}x in a row):\n"
                          f"<code>{str(e)[:300]}</code>")
    if car_findings is not None:
        try:
            car_seen = handle_carnival(car_findings, car_ctx, car_seen)
        except Exception:  # noqa: BLE001 — never let Jailer alerts break the Doomsday watch
            traceback.print_exc()

    new_keys = [k for k in findings if k not in seen]
    print(f"{now:%Y-%m-%d %H:%M} SGT | ok={ok_sites} | findings={len(findings)} | new={len(new_keys)}"
          f" | carnival={len(car_findings or {})}")

    imax_codes = set((shaw_ctx or {}).get("imax_codes", []))

    def is_imax(k):
        return k.startswith("shaw:imaxdate:") or (k.startswith("shaw:") and k.split(":")[-1] in imax_codes)

    if TEST_MODE:
        send_alert("test", findings, new_keys, shaw_ctx, gv_rows)
        if imax_codes:
            send_imax_alert("test", shaw_ctx, [])
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
        send_alert("live" if not seen else "update", findings, new_keys, shaw_ctx, gv_rows)
    seen |= set(new_keys)

    today = now.strftime("%Y-%m-%d")
    if now.hour >= HEARTBEAT_HOUR_SGT and state.get("heartbeat") != today:
        status = "tickets are OUT (see earlier alerts)" if seen else "no showtimes yet"
        pri = sum(1 for k in car_seen if k.startswith("carnival:show:"))
        jailer = (f"showtimes OUT ({pri} show(s) seen)" if pri else
                  "announced, no showtimes yet" if "carnival:soon" in car_seen else "not on Carnival yet")
        send_telegram(f"👀 Watch still running.\nDoomsday: {status}\nJailer 2: {jailer}\n"
                      f"Sites OK: {', '.join(ok_sites) or 'none!'}")
        state["heartbeat"] = today

    state["seen"] = sorted(seen)
    state["carnival_seen"] = sorted(car_seen)
    state["failures"] = failures
    save_state(state)

    hc = os.environ.get("HEALTHCHECK_URL")
    if hc and ok_sites:
        try:
            requests.get(hc, timeout=10)
        except requests.RequestException:
            pass


if __name__ == "__main__":
    run()
