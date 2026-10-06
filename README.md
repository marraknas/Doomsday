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

## Jailer 2 at Carnival Cinemas (Golden Mile Tower)

`carnival.py` watches Carnival's JSON API (`service.carnivalcinemas.sg`) for any title matching
"Jailer 2" (incl. `(FDFS)`, language or `Jailer II` variants).

- 🎬 Gold "JAILER 2" card the moment **any** showtime drops; 🆕 again whenever a new show is listed
  or a listed show becomes bookable. Every show is a tile with booking state and seat classes /
  prices / availability; early-morning shows (3–9 AM) get an **EARLY** badge.
- A silent (no-sound) note when it's first announced as Coming Soon.
- Optional secret `CARNIVAL_CHAT_ID` (comma-separated) to send Jailer alerts to different chats;
  defaults to the same chats as `TELEGRAM_CHAT_ID`.

## Jailer 2 IMAX at Shaw

`check_shaw_jailer` in `monitor.py` watches Shaw for an IMAX listing matching "Jailer 2"
(Shaw names Indian IMAX releases like `TITLE (IMAX) (Tam)`). Alerts go to the **Jailer chats**
(`CARNIVAL_CHAT_ID`), never the Doomsday group:

- 👀 silent note when it appears under Shaw's IMAX **Coming Soon**; ⏰ note if Shaw publishes a sales start time
- 🎯 gold **IMAX timetable card** (every IMAX showtime by cinema and date) the moment showtimes open,
  and again whenever Shaw adds new IMAX dates. State is kept separately in `state.json` → `jailer_imax_seen`.

## Files

- `monitor.py`: the checks, state, and Telegram sending
- `card.py`: renders the alert cards (Pillow); fonts are in `assets/fonts` (Poppins, OFL)
- `carnival.py`: Carnival Cinemas / Jailer 2 checks

## Setup

1. Repo secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (optional: `HEALTHCHECK_URL`)
2. Actions tab → **Doomsday Watch** → **Run workflow** with **test_mode** ticked. You should get a
   🧪 TEST RUN message listing Endgame Encore showtimes. That proves the whole chain works.
3. The schedule then runs by itself. Disable the workflow once you've bought tickets.
