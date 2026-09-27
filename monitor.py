"""
Doomsday Watch — alerts on Telegram when Avengers: Doomsday showtimes appear
on Shaw Theatres or Golden Village (any format, any cinema).

Runs once per invocation (GitHub Actions calls it every ~5 minutes).
State is kept in state.json so each finding is only alerted once.

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
from datetime import datetime, timedelta, timezone

import requests

SGT = timezone(timedelta(hours=8))
TEST_MODE = os.environ.get("TEST_MODE", "").lower() == "true"

if TEST_MODE:
    TITLE_RE = re.compile(r"endgame", re.I)
    SHAW_MOVIE_IDS = ["1261"]          # Endgame Encore (standard) on Shaw
    GV_FILM_CODES = ["1465"]           # Endgame Encore (standard) on GV
    STATE_FILE = None                  # test runs never persist state
else:
    TITLE_RE = re.compile(r"doomsday", re.I)
    SHAW_MOVIE_IDS = ["1112"]          # Avengers: Doomsday movieId on Shaw (release page 1633)
    GV_FILM_CODES = ["1395"]           # Avengers: Doomsday filmCd on GV
    STATE_FILE = "state.json"

SHAW_BASE = "https://shaw.sg/internal"
SHAW_HEADERS = {"x-api-forward-to": "internal", "x-app": "PWSM"}
GV_BASE = "https://www.gv.com.sg/.gv-api"
GV_HEADERS = {"Content-Type": "application/json; charset=utf-8", "X_Developer": "ENOVAX"}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

FAIL_ALERT_AFTER = 3          # consecutive failed runs before an error alert (~15 min)
HEARTBEAT_HOUR_SGT = 9        # daily "still watching" message after 9am SGT


class SiteError(Exception):
    pass


def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*"})
    return s


def get_json(session, url, **kw):
    for attempt in range(2):
        try:
            r = session.request(timeout=20, url=url, **kw)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(2)
    raise SiteError(f"{url.split('?')[0]} → {type(last).__name__}: {str(last)[:150]}")


# ---------------------------------------------------------------- Shaw

def check_shaw(session):
    """Returns (findings: dict key->description, summary lines)."""
    findings = {}
    selectors = get_json(session, f"{SHAW_BASE}/get_selectors?cache=no-cache",
                         method="GET", headers=SHAW_HEADERS)
    if not isinstance(selectors, list):
        raise SiteError("Shaw get_selectors returned unexpected shape")

    # type 1 = movies that currently have bookable showtimes
    listed = [m for m in selectors if m.get("type") == 1 and TITLE_RE.search(m.get("name", ""))]
    movie_ids = list(dict.fromkeys(SHAW_MOVIE_IDS + [str(m["code"]) for m in listed]))
    names = {str(m["code"]): m["name"] for m in listed}

    for m in listed:
        findings[f"shaw:listed:{m['code']}"] = f"Shaw lists <b>{m['name']}</b> as bookable"

    for mid in movie_ids:
        dates = get_json(session, f"{SHAW_BASE}/get_date_selectors?movieId={mid}",
                         method="GET", headers=SHAW_HEADERS)
        if not isinstance(dates, list):
            raise SiteError(f"Shaw get_date_selectors({mid}) returned unexpected shape")
        codes = sorted(d.get("code") for d in dates if d.get("code"))
        if codes:
            label = names.get(mid, "Avengers: Doomsday" if not TEST_MODE else "Endgame Encore")
            findings[f"shaw:dates:{mid}"] = (
                f"Shaw <b>{label}</b>: showtimes on {len(codes)} date(s), {codes[0]} → {codes[-1]}")
    return findings


# ---------------------------------------------------------------- GV

def gv_post(session, path, body=None):
    t = f"{random.randint(1, 1000)}_{int(time.time() * 1000)}"
    return get_json(session, f"{GV_BASE}/{path}?t={t}", method="POST", headers=GV_HEADERS,
                    data=json.dumps(body) if body is not None else None)


def check_gv(session):
    findings = {}
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
                    f"GV lists <b>{title.strip(' *')}</b> under "
                    f"{'Advance Sales' if listing == 'advancesales' else 'Now Showing'}")

    for code in dict.fromkeys(GV_FILM_CODES + list(films)):
        res = gv_post(session, "sessionforfilm", {"filmCode": code})
        if not isinstance(res, dict) or not res.get("success", False):
            raise SiteError(f"GV sessionforfilm({code}) failed: {str(res)[:150]}")
        data = res.get("data")
        if not data:          # "No record found." → no showtimes yet
            continue
        cinemas, dates = set(), set()
        for loc in data.get("locations", []) or []:
            for d in loc.get("dates", []) or []:
                if d.get("times"):
                    cinemas.add(loc.get("name", "?"))
                    dates.add(datetime.fromtimestamp(d["date"] / 1000, SGT).strftime("%Y-%m-%d"))
        if cinemas:
            title = (data.get("filmTitle") or films.get(code) or code).strip(" *")
            ds = sorted(dates)
            findings[f"gv:sessions:{code}"] = (
                f"GV <b>{title}</b>: showtimes at {len(cinemas)} cinema(s), {ds[0]} → {ds[-1]}")
    return findings


# ---------------------------------------------------------------- Telegram / state

def send_telegram(text):
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20, json={
        "chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
    if not r.ok:
        print(f"Telegram error {r.status_code}: {r.text[:200]}", file=sys.stderr)
        r.raise_for_status()


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


LINKS = ("\n\n🎟 <a href=\"https://www.gv.com.sg/GVMovieDetails#/movie/1395\">GV</a> · "
         "<a href=\"https://shaw.sg/movie-details/1633\">Shaw</a>")


def run():
    state = load_state()
    seen = set(state.get("seen", []))
    failures = state.get("failures", {})
    now = datetime.now(SGT)
    all_findings, ok_sites = {}, []

    for site, fn in (("Shaw", check_shaw), ("GV", check_gv)):
        try:
            all_findings.update(fn(make_session()))
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

    new = {k: v for k, v in all_findings.items() if k not in seen}
    print(f"{now:%Y-%m-%d %H:%M} SGT | ok={ok_sites} | findings={len(all_findings)} | new={len(new)}")

    if TEST_MODE:
        body = "\n".join(f"• {v}" for v in all_findings.values()) or "• nothing found"
        send_telegram(f"🧪 <b>TEST RUN</b> (watching Endgame Encore)\n{body}{LINKS}")
        return

    if new:
        first = not seen
        head = ("🚨🚨 <b>AVENGERS: DOOMSDAY TICKETS ARE OUT!</b>" if first
                else "🆕 <b>More Doomsday showtimes/formats just opened</b>")
        send_telegram(head + "\n" + "\n".join(f"• {v}" for v in new.values()) + LINKS)
        seen |= set(new)

    today = now.strftime("%Y-%m-%d")
    if now.hour >= HEARTBEAT_HOUR_SGT and state.get("heartbeat") != today:
        status = "tickets are OUT (see earlier alerts)" if seen else "no showtimes yet"
        send_telegram(f"👀 Doomsday Watch still running — {status}. "
                      f"Sites OK: {', '.join(ok_sites) or 'none!'}")
        state["heartbeat"] = today

    state["seen"] = sorted(seen)
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
