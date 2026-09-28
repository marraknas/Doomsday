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

- 🚨 **Tickets are out**: a dashboard card (formats, cinemas, show counts, dates, first show per
  format, for both chains) with **Book at GV / Book at Shaw** buttons
- 🆕 **More showtimes/formats opened**: the same card, with new formats badged **NEW**
- 🎯 **IMAX (Shaw)**: a separate IMAX timetable card (every IMAX showtime by cinema and date,
  with available / selling fast / sold out) when IMAX opens, and again whenever Shaw adds new IMAX dates
- If the card can't be rendered for any reason, the alert still goes out as plain text
- 👀 **Daily heartbeat** after 9am SGT, so you know it's alive
- ⚠️ **Check failing**: after 3 consecutive failed runs on a site; ✅ when it recovers

## Files

- `monitor.py`: the checks, state, and Telegram sending
- `card.py`: renders the alert card (Pillow); fonts are in `assets/fonts` (Poppins, OFL)

## Setup

1. Repo secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (optional: `HEALTHCHECK_URL`)
2. Actions tab → **Doomsday Watch** → **Run workflow** with **test_mode** ticked. You should get a
   🧪 TEST RUN message listing Endgame Encore showtimes. That proves the whole chain works.
3. The schedule then runs by itself. Disable the workflow once you've bought tickets.
