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
