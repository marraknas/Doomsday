# Doomsday Watch

Pings Telegram the moment **Avengers: Doomsday** showtimes (any format, any cinema) appear on
**Golden Village** or **Shaw Theatres**. Runs on GitHub Actions every ~5 minutes.

## What it checks

| Site | Signal |
|------|--------|
| Shaw | `get_selectors` bookable-movie list contains a "Doomsday" title (catches IMAX / Infinity Vision / Premiere variants, which Shaw lists separately), and `get_date_selectors?movieId=1112` returns any dates |
| GV   | `nowshowing` / `advancesales` lists contain a "Doomsday" title (GV lists IV / Atmos / Gold Class variants separately), and `sessionforfilm` for film 1395 or any matched variant returns showtimes |

Each finding is alerted once (tracked in `state.json`). New formats/cinemas opening later trigger a follow-up alert.

## Messages you'll get

- 🚨 **Tickets are out**: the one you're waiting for, with booking links
- 🆕 **More showtimes/formats opened**: follow-ups (e.g. IMAX opens after standard)
- 👀 **Daily heartbeat** after 9am SGT, so you know it's alive
- ⚠️ **Check failing**: after 3 consecutive failed runs on a site; ✅ when it recovers

## Setup

1. Repo secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (optional: `HEALTHCHECK_URL`)
2. Actions tab → **Doomsday Watch** → **Run workflow** with **test_mode** ticked. You should get a
   🧪 TEST RUN message listing Endgame Encore showtimes. That proves the whole chain works.
3. The schedule then runs by itself. Disable the workflow once you've bought tickets.
