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

## DBS Sailing At The Bay (registration watch)

The same run also reads www.dbs.com/sailing/index.html. That page is plain HTML: each month's sign-up is one
red button (e.g. "November Registration" linking to a cvent.me page), put up about a week before the month.

- ⛵🌊 A sailing card ("You can book December slots now! Get ready to sail!") the moment a registration
  button appears that the bot hasn't seen before: a new "December Registration" button, the old button
  renamed, or the same text pointing to a new link. It has a Register button straight to the sign-up page.
  Later months alert too. The card is drawn by `render_sailing` in `card.py`.
- Goes **only** to the chats in the `CARNIVAL_CHAT_ID` secret (you + the Jailer group). If that secret
  is empty, only you get it. Doomsday chats never get DBS alerts. Test runs go to you only.
- Every link ever shown is remembered (`state.json` → `dbs_seen`), so a flickering page can't alert twice.
- ⚠️ to you if the DBS check fails 6 runs in a row (~30 min); ✅ when it recovers.

## Files

- `monitor.py`: the checks, state, and Telegram sending
- `card.py`: renders the alert cards (Pillow); fonts are in `assets/fonts` (Poppins, OFL)

## Setup

1. Repo secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (optional: `HEALTHCHECK_URL`)
2. Actions tab → **Doomsday Watch** → **Run workflow** with **test_mode** ticked. You should get a
   🧪 TEST RUN message listing Endgame Encore showtimes. That proves the whole chain works.
3. The schedule then runs by itself. Disable the workflow once you've bought tickets.
