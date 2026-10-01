"""
Renders the Doomsday Watch alert card (PNG) from a showtime snapshot.

snapshot = {
  "title": "Avengers: Doomsday",
  "mode": "live" | "update" | "test",
  "checked_at": datetime (SGT),
  "rows": [ {chain, label, note, cinemas[], shows, dates[], first{date,time,cinema}, new}, ... ],
}
"""

import io
import os
from datetime import datetime

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "assets", "fonts")

W = 1200
PAD = 48
GAP = 24

C = {
    "bg": (10, 11, 16), "panel": (20, 22, 30), "panel2": (26, 29, 39), "line": (38, 42, 56),
    "text": (242, 243, 245), "muted": (139, 144, 160), "dim": (92, 97, 112),
    "live": (52, 211, 153), "update": (96, 165, 250), "test": (245, 184, 61),
    "GV": (245, 184, 61), "Shaw": (229, 72, 77), "crimson": (190, 30, 45),
}

_font_cache = {}


def font(weight, size):
    key = (weight, size)
    if key in _font_cache:
        return _font_cache[key]
    candidates = {
        "bold": ["Poppins-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
        "medium": ["Poppins-Medium.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
        "regular": ["Poppins-Regular.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    }[weight]
    f = None
    for c in candidates:
        path = c if os.path.isabs(c) else os.path.join(FONT_DIR, c)
        if os.path.exists(path):
            f = ImageFont.truetype(path, size)
            break
    if f is None:
        f = ImageFont.load_default(size=size)
    _font_cache[key] = f
    return f


def fit(draw, text, fnt, max_w):
    """Truncate text with an ellipsis so it fits max_w pixels."""
    if draw.textlength(text, font=fnt) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=fnt) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def fmt_date(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%a %-d %b")


def short_date(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%-d %b")


def date_span(dates):
    if not dates:
        return "—"
    ds = sorted(dates)
    if len(ds) == 1:
        return fmt_date(ds[0])
    return f"{short_date(ds[0])} – {short_date(ds[-1])}"


def pill(draw, x, y, text, fg, bg, fnt, pad_x=14, h=34):
    w = int(draw.textlength(text, font=fnt)) + pad_x * 2
    draw.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=bg)
    draw.text((x + pad_x, y + h / 2), text, font=fnt, fill=fg, anchor="lm")
    return w


def blend(c, bg, a):
    return tuple(int(bg[i] + (c[i] - bg[i]) * a) for i in range(3))


# ------------------------------------------------------------------ layout

ROW_H = 132
PANEL_HEAD = 78


def render(snapshot):
    rows = snapshot["rows"]
    mode = snapshot.get("mode", "live")
    by_chain = {"GV": [r for r in rows if r["chain"] == "GV"],
                "Shaw": [r for r in rows if r["chain"] == "Shaw"]}
    n_rows = max(1, len(by_chain["GV"]), len(by_chain["Shaw"]))

    header_h = 250
    kpi_h = 128
    panel_h = PANEL_HEAD + n_rows * ROW_H + 16
    footer_h = 76
    H = header_h + kpi_h + GAP + panel_h + footer_h

    img = Image.new("RGB", (W, H), C["bg"])

    # --- background glow
    glow = Image.new("RGB", (W, H), C["bg"])
    gd = ImageDraw.Draw(glow)
    accent = C[mode] if mode != "live" else C["crimson"]
    gd.ellipse((-200, -420, W * 0.75, 380), fill=blend(accent, C["bg"], 0.55))
    gd.ellipse((W * 0.55, -300, W + 250, 260), fill=blend(C["Shaw"], C["bg"], 0.25))
    glow = glow.filter(ImageFilter.GaussianBlur(120))
    img.paste(glow)
    d = ImageDraw.Draw(img)

    # --- header
    y = PAD
    d.text((PAD, y), "DOOMSDAY WATCH", font=font("bold", 20), fill=C["muted"])
    status = {"live": ("TICKETS LIVE", C["live"]),
              "update": ("NEW SHOWTIMES", C["update"]),
              "test": ("TEST RUN", C["test"])}[mode]
    sf = font("bold", 18)
    sw = d.textlength(status[0], font=sf) + 58
    sx = W - PAD - sw
    d.rounded_rectangle((sx, y - 6, sx + sw, y + 32), radius=19, fill=status[1])
    d.ellipse((sx + 16, y + 7, sx + 28, y + 19), fill=C["bg"])
    d.text((sx + 38, y + 13), status[0], font=sf, fill=C["bg"], anchor="lm")

    y += 44
    title = snapshot.get("title", "Avengers: Doomsday").upper()
    d.text((PAD, y), fit(d, title, font("bold", 76), W - 2 * PAD), font=font("bold", 76), fill=C["text"])
    y += 100
    checked = snapshot["checked_at"]
    sub = {"live": "Showtimes just went live in Singapore",
           "update": "More showtimes just opened",
           "test": "Test run — pipeline check"}[mode]
    d.text((PAD, y), f"{sub}  ·  detected {checked:%a %-d %b, %-I:%M %p} SGT",
           font=font("regular", 24), fill=C["muted"])

    # --- KPI tiles
    y = header_h
    all_cinemas = {c for r in rows for c in r["cinemas"]}
    all_dates = sorted({dt for r in rows for dt in r["dates"]})
    first = min((r["first"] for r in rows if r.get("first")),
                key=lambda f: f["sort"], default=None)
    kpis = [
        (f"{sum(r['shows'] for r in rows):,}", "SHOWTIMES"),
        (str(len(all_cinemas)), "CINEMAS"),
        (str(len(rows)), "FORMATS"),
        (short_date(all_dates[0]) if all_dates else "—",
         "OPENING DAY" + (f" · {datetime.strptime(all_dates[0], '%Y-%m-%d'):%a}".upper() if all_dates else "")),
    ]
    tile_w = (W - 2 * PAD - 3 * GAP) / 4
    for i, (val, lab) in enumerate(kpis):
        x0 = PAD + i * (tile_w + GAP)
        d.rounded_rectangle((x0, y, x0 + tile_w, y + kpi_h - 12), radius=20,
                            fill=C["panel"], outline=C["line"], width=2)
        d.text((x0 + 24, y + 22), lab, font=font("medium", 16), fill=C["muted"])
        size = 40
        while size > 24 and d.textlength(val, font=font("bold", size)) > tile_w - 48:
            size -= 2
        d.text((x0 + 24, y + 50 + (40 - size) / 2), fit(d, val, font("bold", size), tile_w - 48),
               font=font("bold", size), fill=C["text"])

    # --- chain panels
    y = header_h + kpi_h + GAP - 12
    panel_w = (W - 2 * PAD - GAP) / 2
    names = {"GV": "GOLDEN VILLAGE", "Shaw": "SHAW THEATRES"}
    for i, chain in enumerate(("GV", "Shaw")):
        x0 = PAD + i * (panel_w + GAP)
        x1 = x0 + panel_w
        d.rounded_rectangle((x0, y, x1, y + panel_h), radius=24, fill=C["panel"],
                            outline=C["line"], width=2)
        d.ellipse((x0 + 28, y + 36, x0 + 42, y + 50), fill=C[chain])
        d.text((x0 + 54, y + 30), names[chain], font=font("bold", 24), fill=C["text"])
        chain_rows = by_chain[chain]
        count = f"{len(chain_rows)} format{'s' if len(chain_rows) != 1 else ''}" if chain_rows else ""
        if count:
            d.text((x1 - 28, y + 32), count, font=font("medium", 18), fill=C["muted"], anchor="ra")

        ry = y + PANEL_HEAD
        if not chain_rows:
            d.rounded_rectangle((x0 + 20, ry, x1 - 20, ry + ROW_H - 16), radius=16, fill=C["panel2"])
            d.text(((x0 + x1) / 2, ry + (ROW_H - 16) / 2), "No showtimes yet",
                   font=font("medium", 24), fill=C["dim"], anchor="mm")
        for r in chain_rows:
            _row(d, x0 + 20, ry, x1 - 20, r, C[chain])
            ry += ROW_H

    # --- footer
    fy = H - footer_h + 18
    d.text((PAD, fy), "Book now:  gv.com.sg   ·   shaw.sg", font=font("medium", 22), fill=C["text"])
    if first:
        txt = f"Earliest: {fmt_date(first['date'])} {first['time']} · {first['cinema']}"
        d.text((W - PAD, fy), fit(d, txt, font("regular", 20), 560), font=font("regular", 20),
               fill=C["muted"], anchor="ra")

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _row(d, x0, y, x1, r, accent):
    h = ROW_H - 16
    d.rounded_rectangle((x0, y, x1, y + h), radius=16, fill=C["panel2"])
    d.rounded_rectangle((x0, y + 18, x0 + 6, y + h - 18), radius=3, fill=accent)

    tx = x0 + 26
    right = x1 - 22
    badge_w = 0
    if r.get("new"):
        bf = font("bold", 15)
        badge_w = d.textlength("NEW", font=bf) + 24 + 12
        pill(d, right - badge_w + 12, y + 18, "NEW", C["bg"], C["live"], bf, pad_x=12, h=28)

    label_font = font("bold", 26)
    label = fit(d, r["label"], label_font, right - tx - badge_w)
    d.text((tx, y + 14), label, font=label_font, fill=C["text"])
    if r.get("note"):
        nx = tx + d.textlength(label, font=label_font) + 12
        room = right - badge_w - nx
        if room > 60:
            d.text((nx, y + 24), fit(d, r["note"], font("regular", 17), room),
                   font=font("regular", 17), fill=C["dim"])

    n_c = len(r["cinemas"])
    stats = (f"{n_c} cinema{'s' if n_c != 1 else ''}  ·  {r['shows']:,} show{'s' if r['shows'] != 1 else ''}"
             f"  ·  {date_span(r['dates'])}")
    d.text((tx, y + 52), fit(d, stats, font("medium", 19), right - tx), font=font("medium", 19),
           fill=blend(accent, C["text"], 0.35))

    if r.get("first"):
        f = r["first"]
        pre = "First show  "
        d.text((tx, y + 82), pre, font=font("regular", 18), fill=C["dim"])
        px = tx + d.textlength(pre, font=font("regular", 18))
        txt = f"{short_date(f['date'])}, {f['time']}  ·  {f['cinema']}"
        d.text((px, y + 82), fit(d, txt, font("regular", 18), right - px), font=font("regular", 18),
               fill=C["muted"])


# ================================================================== IMAX timetable card
#
# imax_snapshot = {
#   "title": "Avengers: Doomsday",
#   "mode": "live" | "update" | "test",
#   "checked_at": datetime (SGT),
#   "cinemas": ["Jewel", "Lido", ...],                    # column order
#   "days": [ {"date": "2026-12-17", "new": bool,
#              "shows": {"Jewel": [{"time": "12:10 PM", "status": "AV"}, ...], ...}}, ... ],
#   "more_days": 3,                                       # dates not shown on the card
#   "total_shows": 42,
# }

IMAX = {"accent": (56, 189, 248), "accent2": (99, 102, 241), "chip": (22, 36, 52),
        "chip_line": (44, 86, 120), "fast": (245, 158, 11), "sold": (112, 116, 130)}

DATE_W = 150
CHIP_H = 38
CHIP_GAP = 8


def _status(s):
    s = (s or "AV").upper()
    if s in ("SO", "FULL", "SOLDOUT"):
        return "sold"
    if s in ("AV", "", "A"):
        return "open"
    return "fast"          # any other code (limited / filling fast)


def render_imax(snap):
    mode = snap.get("mode", "live")
    cinemas = snap["cinemas"] or ["—"]
    days = snap["days"]
    n = len(cinemas)
    grid_x0 = PAD + DATE_W
    col_w = (W - PAD - grid_x0) / n
    chip_font = font("medium", 18 if col_w >= 170 else 16)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    chip_w = int(probe.textlength("12:10 PM", font=chip_font)) + 22
    per_row = max(1, int((col_w - 20 + CHIP_GAP) // (chip_w + CHIP_GAP)))

    def rows_needed(day):
        return max([1] + [-(-len(day["shows"].get(c, [])) // per_row) for c in cinemas])

    row_heights = [max(108, rows_needed(d) * (CHIP_H + CHIP_GAP) - CHIP_GAP + 46) for d in days] or [108]

    header_h = 300
    colhead_h = 64
    footer_h = 96
    H = header_h + colhead_h + sum(row_heights) + footer_h + 20

    img = Image.new("RGB", (W, H), C["bg"])
    glow = Image.new("RGB", (W, H), C["bg"])
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-260, -460, W * 0.7, 360), fill=blend(IMAX["accent2"], C["bg"], 0.55))
    gd.ellipse((W * 0.45, -340, W + 260, 300), fill=blend(IMAX["accent"], C["bg"], 0.45))
    img.paste(glow.filter(ImageFilter.GaussianBlur(130)))
    d = ImageDraw.Draw(img)

    # --- header
    y = PAD
    d.text((PAD, y), "DOOMSDAY WATCH  ·  SHAW THEATRES", font=font("bold", 20), fill=C["muted"])
    label, col = {"live": ("IMAX IS OPEN", IMAX["accent"]),
                  "update": ("NEW IMAX DATES", IMAX["accent"]),
                  "test": ("TEST RUN", C["test"])}[mode]
    sf = font("bold", 18)
    sw = d.textlength(label, font=sf) + 58
    sx = W - PAD - sw
    d.rounded_rectangle((sx, y - 6, sx + sw, y + 32), radius=19, fill=col)
    d.ellipse((sx + 16, y + 7, sx + 28, y + 19), fill=C["bg"])
    d.text((sx + 38, y + 13), label, font=sf, fill=C["bg"], anchor="lm")

    y += 42
    d.text((PAD, y), "IMAX LASER", font=font("bold", 34), fill=IMAX["accent"])
    y += 44
    title = snap.get("title", "Avengers: Doomsday").upper()
    d.text((PAD, y), fit(d, title, font("bold", 70), W - 2 * PAD), font=font("bold", 70), fill=C["text"])
    y += 94
    all_dates = [x["date"] for x in days]
    span = date_span(all_dates) if all_dates else "—"
    sub = (f"{snap.get('total_shows', 0)} IMAX showtimes  ·  {len(snap['cinemas'])} cinemas  ·  {span}"
           f"  ·  checked {snap['checked_at']:%-I:%M %p}")
    d.text((PAD, y), fit(d, sub, font("regular", 23), W - 2 * PAD), font=font("regular", 23), fill=C["muted"])

    # --- column headers
    y = header_h
    d.rounded_rectangle((PAD, y, W - PAD, y + colhead_h - 10), radius=14, fill=C["panel"],
                        outline=C["line"], width=2)
    d.text((PAD + 20, y + (colhead_h - 10) / 2), "DATE", font=font("bold", 16), fill=C["muted"], anchor="lm")
    for i, c in enumerate(cinemas):
        cx = grid_x0 + i * col_w + col_w / 2
        d.text((cx, y + (colhead_h - 10) / 2), fit(d, c.upper(), font("bold", 19), col_w - 16),
               font=font("bold", 19), fill=C["text"], anchor="mm")

    # --- day rows
    y = header_h + colhead_h
    for day, rh in zip(days, row_heights):
        is_new = day.get("new") and mode == "update"
        fill = blend(IMAX["accent"], C["panel"], 0.10) if is_new else C["panel"]
        d.rounded_rectangle((PAD, y, W - PAD, y + rh - 10), radius=16, fill=fill,
                            outline=IMAX["chip_line"] if is_new else C["line"], width=2)
        dt = datetime.strptime(day["date"], "%Y-%m-%d")
        ly = y + (rh - 10) / 2 - (44 if is_new else 30)
        d.text((PAD + 20, ly), dt.strftime("%a").upper(), font=font("bold", 16), fill=C["muted"])
        d.text((PAD + 20, ly + 20), dt.strftime("%-d %b"), font=font("bold", 26), fill=C["text"])
        if is_new:
            pill(d, PAD + 20, ly + 60, "NEW", C["bg"], C["live"], font("bold", 13), pad_x=10, h=24)

        box_h = rh - 10
        for i, c in enumerate(cinemas):
            col_mid = grid_x0 + i * col_w + col_w / 2
            shows = day["shows"].get(c, [])
            if not shows:
                d.text((col_mid, y + box_h / 2), "—", font=font("medium", 20), fill=C["dim"], anchor="mm")
                continue
            n_rows = -(-len(shows) // per_row)
            group_h = n_rows * (CHIP_H + CHIP_GAP) - CHIP_GAP
            top = y + (box_h - group_h) / 2
            for j, s in enumerate(shows):
                r, k = divmod(j, per_row)
                in_row = min(per_row, len(shows) - r * per_row)
                row_w = in_row * chip_w + (in_row - 1) * CHIP_GAP
                x0 = col_mid - row_w / 2 + k * (chip_w + CHIP_GAP)
                y0 = top + r * (CHIP_H + CHIP_GAP)
                st = _status(s.get("status"))
                bg = {"open": IMAX["chip"], "fast": blend(IMAX["fast"], C["panel"], 0.18),
                      "sold": C["panel2"]}[st]
                ol = {"open": IMAX["chip_line"], "fast": IMAX["fast"], "sold": C["line"]}[st]
                fg = {"open": C["text"], "fast": (253, 230, 138), "sold": IMAX["sold"]}[st]
                d.rounded_rectangle((x0, y0, x0 + chip_w, y0 + CHIP_H), radius=10, fill=bg, outline=ol, width=2)
                tx = x0 + chip_w / 2
                d.text((tx, y0 + CHIP_H / 2), s["time"], font=chip_font, fill=fg, anchor="mm")
                if st == "sold":
                    tw = d.textlength(s["time"], font=chip_font)
                    d.line((tx - tw / 2 - 2, y0 + CHIP_H / 2, tx + tw / 2 + 2, y0 + CHIP_H / 2),
                           fill=IMAX["sold"], width=2)
        y += rh

    # --- footer / legend
    fy = H - footer_h + 10
    lx = PAD
    for lab, bg, ol in (("Available", IMAX["chip"], IMAX["chip_line"]),
                        ("Selling fast", blend(IMAX["fast"], C["panel"], 0.18), IMAX["fast"]),
                        ("Sold out", C["panel2"], C["line"])):
        d.rounded_rectangle((lx, fy + 4, lx + 26, fy + 24), radius=6, fill=bg, outline=ol, width=2)
        d.text((lx + 36, fy + 14), lab, font=font("regular", 18), fill=C["muted"], anchor="lm")
        lx += 36 + d.textlength(lab, font=font("regular", 18)) + 32
    more = snap.get("more_days", 0)
    if more:
        d.text((W - PAD, fy + 14), f"+{more} more date{'s' if more != 1 else ''} on shaw.sg",
               font=font("medium", 18), fill=IMAX["accent"], anchor="rm")
    d.text((PAD, fy + 48), "Book now:  shaw.sg  ·  Shaw Theatres app", font=font("medium", 22), fill=C["text"])

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ================================================================== Jailer 2 / Carnival card
#
# jailer_snapshot = {
#   "title": "Jailer 2", "mode": "live" | "update" | "test", "checked_at": datetime,
#   "days": [ {"date": "2027-01-14", "first": bool, "shows": [show, ...]} ],
#   "total": int, "bookable": int, "early": int, "more_days": int,
#   show = {"time": "7:00 AM", "early": bool, "bookable": bool, "tag": "FDFS", "new": bool,
#           "classes": [{"name": "Platinum", "price": 12, "status": "Available", "state": "open"|"fast"|"sold"}]}
# }

import math

J = {"bg": (13, 10, 8), "gold": (247, 183, 51), "saffron": (255, 122, 26), "cream": (250, 244, 230),
     "muted": (176, 164, 142), "dim": (112, 102, 88), "panel": (28, 23, 18), "panel2": (38, 31, 24),
     "line": (66, 54, 40), "open": (52, 211, 153), "wait": (245, 158, 11), "sold": (239, 68, 68)}

TILE_W = (W - 2 * PAD - 2 * GAP) / 3


def _sun(d, x, y, r, col):
    d.ellipse((x - r, y - r, x + r, y + r), fill=col)
    for k in range(8):
        a = k * math.pi / 4
        d.line((x + math.cos(a) * (r + 3), y + math.sin(a) * (r + 3),
                x + math.cos(a) * (r + 7), y + math.sin(a) * (r + 7)), fill=col, width=2)


def _tile_h(show):
    wrap = 32 if (show.get("early") and show.get("tag")) else 0
    return 128 + wrap + 30 * max(1, min(3, len(show.get("classes") or [])))


def render_jailer(snap):
    mode = snap.get("mode", "live")
    days = snap["days"]

    def day_h(day):
        rows = [day["shows"][i:i + 3] for i in range(0, len(day["shows"]), 3)]
        return 78 + sum(max(_tile_h(x) for x in r) + GAP for r in rows) + 18

    header_h = 330
    footer_h = 92
    H = header_h + sum(day_h(dd) for dd in days) + (160 if not days else 0) + footer_h

    img = Image.new("RGB", (W, H), J["bg"])
    glow = Image.new("RGB", (W, H), J["bg"])
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-300, -520, W * 0.8, 380), fill=blend(J["saffron"], J["bg"], 0.42))
    gd.ellipse((W * 0.5, -260, W + 300, 320), fill=blend(J["gold"], J["bg"], 0.30))
    img.paste(glow.filter(ImageFilter.GaussianBlur(140)))
    d = ImageDraw.Draw(img)

    # --- header
    y = PAD
    d.text((PAD, y), "CARNIVAL CINEMAS  ·  GOLDEN MILE TOWER", font=font("bold", 20), fill=J["muted"])
    label, col = {"live": ("SHOWTIMES OUT", J["gold"]), "update": ("NEW SHOWS", J["gold"]),
                  "test": ("TEST RUN", C["test"])}[mode]
    sf = font("bold", 18)
    sw = d.textlength(label, font=sf) + 58
    sx = W - PAD - sw
    d.rounded_rectangle((sx, y - 6, sx + sw, y + 32), radius=19, fill=col)
    d.ellipse((sx + 16, y + 7, sx + 28, y + 19), fill=J["bg"])
    d.text((sx + 38, y + 13), label, font=sf, fill=J["bg"], anchor="lm")

    y += 46
    title = snap.get("title", "Jailer 2").upper()
    tf = font("bold", 104)
    while tf.size > 54 and d.textlength(title, font=tf) > W - 2 * PAD:
        tf = font("bold", tf.size - 6)
    d.text((PAD, y), title, font=tf, fill=J["gold"])
    y += tf.size + 30
    parts = [f"{snap['total']} show{'s' if snap['total'] != 1 else ''}", f"{snap['bookable']} bookable now"]
    if snap.get("early"):
        parts.append(f"{snap['early']} early")
    parts.append(f"checked {snap['checked_at']:%a %-d %b, %-I:%M %p}")
    d.text((PAD, y), fit(d, "  ·  ".join(parts), font("regular", 24), W - 2 * PAD), font=font("regular", 24),
           fill=J["muted"])

    # --- days
    y = header_h
    if not days:
        d.rounded_rectangle((PAD, y, W - PAD, y + 130), radius=20, fill=J["panel"], outline=J["line"], width=2)
        d.text((W / 2, y + 65), "No showtimes yet", font=font("medium", 28), fill=J["dim"], anchor="mm")
    for day in days:
        dt = datetime.strptime(day["date"], "%Y-%m-%d")
        d.text((PAD, y), dt.strftime("%A").upper(), font=font("bold", 18), fill=J["saffron"])
        dl = dt.strftime("%-d %B %Y")
        d.text((PAD, y + 22), dl, font=font("bold", 34), fill=J["cream"])
        px = PAD + d.textlength(dl, font=font("bold", 34)) + 16
        if day.get("first"):
            px += pill(d, px, y + 28, "FIRST DAY", J["bg"], J["gold"], font("bold", 14), pad_x=12, h=28) + 10
        n = len(day["shows"])
        d.text((W - PAD, y + 42), f"{n} show{'s' if n != 1 else ''}", font=font("medium", 20),
               fill=J["muted"], anchor="rm")
        y += 78
        for r0 in range(0, n, 3):
            row = day["shows"][r0:r0 + 3]
            rh = max(_tile_h(x) for x in row)
            for i, sh in enumerate(row):
                _tile(d, PAD + i * (TILE_W + GAP), y, sh, rh)
            y += rh + GAP
        y += 18

    # --- footer
    fy = H - footer_h + 14
    lx = PAD
    for lab, c in (("Booking open", J["open"]), ("Listed, not open yet", J["wait"]), ("Sold out", J["sold"])):
        d.ellipse((lx, fy + 6, lx + 14, fy + 20), fill=c)
        d.text((lx + 22, fy + 13), lab, font=font("regular", 18), fill=J["muted"], anchor="lm")
        lx += 22 + d.textlength(lab, font=font("regular", 18)) + 30
    if snap.get("more_days"):
        m = snap["more_days"]
        d.text((W - PAD, fy + 13), f"+{m} more day{'s' if m != 1 else ''} on carnivalcinemas.sg",
               font=font("medium", 18), fill=J["gold"], anchor="rm")
    d.text((PAD, fy + 44), "Book:  carnivalcinemas.sg  ·  Golden Mile Tower, Beach Road",
           font=font("medium", 22), fill=J["cream"])

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _tile(d, x, y, s, h):
    bookable = s["bookable"]
    classes = s.get("classes") or []
    all_sold = bookable and classes and all(c["state"] == "sold" for c in classes)
    accent = J["sold"] if all_sold else J["open"] if bookable else J["wait"]
    new = s.get("new")
    d.rounded_rectangle((x, y, x + TILE_W, y + h), radius=18, fill=J["panel2"] if new else J["panel"],
                        outline=J["gold"] if new else J["line"], width=2)
    d.rounded_rectangle((x, y + 20, x + 6, y + h - 20), radius=3, fill=accent)
    tx, right = x + 24, x + TILE_W - 20
    d.text((tx, y + 16), s["time"], font=font("bold", 42), fill=J["cream"])
    if new:
        pill(d, right - 58, y + 22, "NEW", J["bg"], J["gold"], font("bold", 14), pad_x=12, h=26)

    # badges row: EARLY (sun) + variant tag, then status
    yy = y + 72
    bx = tx
    bf = font("bold", 15)
    if s.get("early"):
        w = d.textlength("EARLY", font=bf) + 50
        d.rounded_rectangle((bx, yy, bx + w, yy + 24), radius=12, fill=J["gold"])
        _sun(d, bx + 17, yy + 12, 4, J["bg"])
        d.text((bx + 34, yy + 12), "EARLY", font=bf, fill=J["bg"], anchor="lm")
        bx += w + 8
    if s.get("tag"):
        tag = fit(d, s["tag"].upper(), bf, max(40, right - bx - 150))
        bx += pill(d, bx, yy, tag, J["bg"], J["saffron"], bf, pad_x=10, h=24) + 10
    status = "Sold out" if all_sold else "Booking open" if bookable else "Not open yet"
    if s.get("early") and s.get("tag"):   # two badges: status gets its own line
        yy += 32
        bx = tx
    d.ellipse((bx, yy + 6, bx + 12, yy + 18), fill=accent)
    d.text((bx + 20, yy + 12), fit(d, status, font("medium", 18), right - bx - 20), font=font("medium", 18),
           fill=accent, anchor="lm")
    yy += 40
    if not classes:
        msg = "Seats show once booking opens" if not bookable else "Seat info unavailable"
        d.text((tx, yy), fit(d, msg, font("regular", 16), right - tx), font=font("regular", 16), fill=J["dim"])
        return
    for c in classes[:3]:
        col = {"open": J["muted"], "fast": J["wait"], "sold": J["sold"]}[c["state"]]
        price = f"S${c['price']:g}" if isinstance(c.get("price"), (int, float)) else ""
        d.text((tx, yy), fit(d, c["name"], font("medium", 17), 150), font=font("medium", 17), fill=J["cream"])
        d.text((tx + 158, yy), price, font=font("medium", 17), fill=J["muted"])
        d.text((right, yy), fit(d, c["status"], font("regular", 16), 96), font=font("regular", 16),
               fill=col, anchor="ra")
        yy += 30
