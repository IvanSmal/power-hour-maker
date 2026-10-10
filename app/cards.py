"""On-screen graphics drawn with Pillow: title cards, drink counter, lower thirds, countdowns.

Every overlay renderer returns (RGBA image, x, y) so ffmpeg can overlay a small PNG at a fixed
position, and the UI frame previews can composite exactly the same pixels.
"""
import io
import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont, ImageOps

FONT_FILES = [
    ("Impact", "impact.ttf"), ("Arial Black", "ariblk.ttf"), ("Bahnschrift", "bahnschrift.ttf"),
    ("Segoe UI Black", "seguibl.ttf"), ("Segoe UI Bold", "segoeuib.ttf"), ("Arial Bold", "arialbd.ttf"),
    ("Verdana Bold", "verdanab.ttf"), ("Trebuchet Bold", "trebucbd.ttf"), ("Georgia Bold", "georgiab.ttf"),
    ("Comic Sans Bold", "comicbd.ttf"), ("Consolas Bold", "consolab.ttf"), ("Ink Free", "Inkfree.ttf"),
    ("Segoe Print Bold", "segoeprb.ttf"), ("Showcard Gothic", "SHOWG.TTF"), ("Cooper Black", "COOPBL.TTF"),
    ("Stencil", "STENCIL.TTF"), ("Broadway", "BROADW.TTF"), ("Jokerman", "JOKERMAN.TTF"),
    ("Chiller", "CHILLER.TTF"), ("Snap ITC", "SNAP____.TTF"),
]
_FONT_DIRS = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
              Path(os.environ.get("LOCALAPPDATA", ".")) / "Microsoft" / "Windows" / "Fonts"]
_probe = ImageDraw.Draw(Image.new("L", (1, 1)))


@lru_cache(None)
def font_map():
    out = {}
    for name, fname in FONT_FILES:
        for d in _FONT_DIRS:
            if (d / fname).exists():
                out[name] = str(d / fname)
                break
    return out


# Fallbacks for scripts the decorative fonts don't cover (they'd render as empty boxes).
_SCRIPT_FONTS = [   # first match wins: kana before Hangul before Han
    (((0x3040, 0x30FF), (0x31F0, 0x31FF)), ("YuGothB.ttc", "meiryob.ttc", "msgothic.ttc")),        # Kana
    (((0x1100, 0x11FF), (0x3130, 0x318F), (0xAC00, 0xD7AF)), ("malgunbd.ttf", "malgun.ttf")),     # Hangul
    (((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF)), ("msyhbd.ttc", "msyh.ttc", "simhei.ttf")),  # Han
    (((0x0E00, 0x0E7F),), ("LeelaUIb.ttf", "leelawdb.ttf")),                                     # Thai
    (((0x0900, 0x0DFF),), ("NirmalaB.ttf", "Nirmala.ttf")),                                      # Indic
    (((0x0370, 0x03FF), (0x0400, 0x052F), (0x0590, 0x06FF), (0x0750, 0x077F)), ("segoeuib.ttf",)),  # Greek/Cyrillic/Hebrew/Arabic
]
_FONTS_WITH_CYRILLIC = {"Impact", "Arial Black", "Arial Bold", "Segoe UI Black", "Segoe UI Bold", "Verdana Bold",
                        "Trebuchet Bold", "Georgia Bold", "Comic Sans Bold", "Consolas Bold", "Bahnschrift"}


def _find_font_file(*names):
    for n in names:
        for d in _FONT_DIRS:
            if (d / n).exists():
                return str(d / n)
    return None


def _fallback_for(name, text):
    for ranges, files in _SCRIPT_FONTS:
        if any(lo <= ord(ch) <= hi for ch in text for lo, hi in ranges):
            if files == ("segoeuib.ttf",) and name in _FONTS_WITH_CYRILLIC and not any(
                    0x0590 <= ord(ch) <= 0x077F for ch in text):
                return None       # Greek/Cyrillic are fine in the common Windows fonts
            return _find_font_file(*files)
    return None


@lru_cache(512)
def _load(path, size):
    return ImageFont.truetype(path, size) if path else ImageFont.load_default(size)


def font(name, size, text=""):
    fm = font_map()
    path = (_fallback_for(name, text) if text else None) or fm.get(name) or fm.get("Arial Bold") \
        or next(iter(fm.values()), None)
    return _load(path, max(6, int(size)))


def rgb(c, default=(255, 255, 255)):
    try:
        return ImageColor.getrgb(c)[:3]
    except (ValueError, AttributeError, TypeError):
        return default


def rgba(c, a=255):
    return (*rgb(c), int(a))


def ink(text, f, stroke=0):
    """Ink bounding box of text drawn at the origin with the default 'la' anchor."""
    return _probe.textbbox((0, 0), text, font=f, stroke_width=stroke)


def text_w(text, f, stroke=0):
    l, _, r, _ = ink(text, f, stroke)
    return r - l


def wrap(text, f, max_w, stroke=0):
    lines = []
    for para in str(text).split("\n"):
        words = para.split()
        if not words:
            continue
        cur = words[0]
        for w in words[1:]:
            if text_w(cur + " " + w, f, stroke) <= max_w:
                cur += " " + w
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def fit(text, font_name, size, max_w, max_lines=2, min_size=10, stroke_ratio=0.0):
    """Largest font (<= size) whose word-wrapped text fits max_w in max_lines."""
    size = int(size)
    while True:
        f = font(font_name, size, text)
        stroke = int(round(size * stroke_ratio))
        lines = wrap(text, f, max_w, stroke)
        if (len(lines) <= max_lines and all(text_w(x, f, stroke) <= max_w for x in lines)) or size <= min_size:
            return f, lines[:max_lines], stroke
        size = max(min_size, int(size * 0.92))


def _shadow(img, radius, alpha=150, offset=(0, 0)):
    """Soft drop shadow underneath img's opaque pixels."""
    a = img.getchannel("A").point(lambda v: v * alpha // 255)
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sh.putalpha(a)
    sh = sh.filter(ImageFilter.GaussianBlur(radius))
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.alpha_composite(sh, (max(0, offset[0]), max(0, offset[1])))
    out.alpha_composite(img)
    return out


def _crop(img, pad=0):
    box = img.getbbox()
    if not box:
        return Image.new("RGBA", (2, 2), (0, 0, 0, 0)), 0, 0
    l, t, r, b = box
    l, t = max(0, l - pad), max(0, t - pad)
    r, b = min(img.width, r + pad), min(img.height, b + pad)
    return img.crop((l, t, r, b)), l, t


def _place(position, W, H, w, h, margin, bottom_extra=0):
    x = {"l": margin, "c": (W - w) // 2, "r": W - margin - w}[position[1] if len(position) > 1 else "c"]
    y = {"t": margin, "m": (H - h) // 2, "b": H - margin - h - bottom_extra}[position[0]]
    return int(x), int(y)


def _even(v):
    return int(v) // 2 * 2


# --- overlay renderers -----------------------------------------------------------------------

def counter(W, H, p):
    s = H / 1080 * float(p.get("scale") or 1)
    n, total = p["n"], p["total"]
    try:
        num = str(p.get("format") or "{n}").format(n=n, total=total, left=total - n)
    except (KeyError, IndexError, ValueError):
        num = str(n)
    label = (p.get("label") or "").strip()
    fn = font(p["font"], 92 * s, num)
    fl = font(p["font"], 30 * s, label)
    pad, gap, radius = 16 * s, 6 * s, 18 * s
    nb = ink(num, fn)
    lb = ink(label, fl) if label else (0, 0, 0, 0)
    inner_w = max(nb[2] - nb[0], lb[2] - lb[0])
    bw = int(max(inner_w + 2 * pad + 12 * s, 120 * s))
    bh = int(pad + (lb[3] - lb[1] + gap if label else 0) + (nb[3] - nb[1]) + pad)
    m = int(14 * s)
    img = Image.new("RGBA", (bw + 2 * m, bh + 2 * m), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((m, m, m + bw, m + bh), radius=radius, fill=rgba(p["accent"], 240))
    tc = rgba(p.get("badge_text") or "#111111")
    y = m + pad
    if label:
        d.text((m + bw / 2 - (lb[0] + lb[2]) / 2, y - lb[1]), label, font=fl, fill=tc[:3] + (210,))
        y += lb[3] - lb[1] + gap
    d.text((m + bw / 2 - (nb[0] + nb[2]) / 2, y - nb[1]), num, font=fn, fill=tc)
    img = _shadow(img, max(2, int(8 * s)), 140, (0, int(3 * s)))
    x, y = _place(p.get("position") or "tr", W, H, img.width, img.height, int(40 * s) - m)
    return img, x, y


def lower_third(W, H, p):
    s = H / 1080
    title, artist = p.get("title") or "", p.get("artist") or ""
    max_w = W * 0.62
    ft, lt, _ = fit(title, p["font"], 58 * s, max_w, 1)
    lt = lt or [""]
    tb = ink(lt[0], ft)
    if artist:
        fa, la, _ = fit(artist, p["font"], 36 * s, max_w, 1)
        ab = ink(la[0], fa)
    else:
        la, ab = [], (0, 0, 0, 0)
    pad_x, pad_y, bar, gap = 26 * s, 18 * s, 10 * s, 10 * s
    inner_w = max(tb[2] - tb[0], ab[2] - ab[0])
    w = int(bar + 2 * pad_x + inner_w)
    h = int(2 * pad_y + (tb[3] - tb[1]) + ((ab[3] - ab[1]) + gap if la else 0))
    m = int(14 * s)
    img = Image.new("RGBA", (w + 2 * m, h + 2 * m), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((m, m, m + w, m + h), radius=int(10 * s), fill=(12, 10, 18, 200))
    d.rounded_rectangle((m, m, m + bar, m + h), radius=int(4 * s), fill=rgba(p["accent"]))
    x0 = m + bar + pad_x
    y = m + pad_y
    d.text((x0 - tb[0], y - tb[1]), lt[0], font=ft, fill=rgba(p.get("text_color") or "#ffffff"))
    if la:
        y += tb[3] - tb[1] + gap
        d.text((x0 - ab[0], y - ab[1]), la[0], font=fa, fill=rgba(p["accent"]))
    img = _shadow(img, max(2, int(10 * s)), 120, (0, int(4 * s)))
    x, y = _place(p.get("position") or "bl", W, H, img.width, img.height, int(44 * s) - m,
                  int(p.get("bottom_extra") or 0))
    return img, x, y


def big_number(W, H, p):
    s = H / 1080
    size = (420 if p.get("position") == "mc" else 170) * s * float(p.get("scale") or 1)
    f = font(p["font"], size, str(p["text"]))
    text = str(p["text"])
    stroke = max(2, int(size * 0.05))
    b = ink(text, f, stroke)
    m = int(size * 0.15)
    img = Image.new("RGBA", (b[2] - b[0] + 2 * m, b[3] - b[1] + 2 * m), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.text((m - b[0], m - b[1]), text, font=f, fill=rgba(p["accent"]), stroke_width=stroke,
           stroke_fill=(15, 12, 20, 255))
    img = _shadow(img, max(3, int(size * 0.04)), 160, (0, int(size * 0.02)))
    x, y = _place(p.get("position") or "br", W, H, img.width, img.height, int(40 * s) - m,
                  int(p.get("bottom_extra") or 0))
    return img, x, y


def card_text(W, H, p):
    s = H / 1080
    title, sub = (p.get("title") or "").strip(), (p.get("subtitle") or "").strip()
    scale = float(p.get("scale") or 1)
    max_w = W * 0.86
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    blocks = []
    if title:
        ft, lt, st = fit(title, p["font"], 190 * s * scale, max_w, 2, stroke_ratio=0.035)
        blocks.append((ft, lt, st, rgba(p.get("color") or "#ffffff")))
    if sub:
        fs, ls, ss = fit(sub, p["font"], 62 * s * scale * float(p.get("sub_scale") or 1), max_w, 3, stroke_ratio=0.04)
        blocks.append((fs, ls, ss, rgba(p.get("sub_color") or p.get("color") or "#ffffff")))
    if not blocks:
        return Image.new("RGBA", (2, 2), (0, 0, 0, 0)), 0, 0
    gap = 34 * s
    heights = []
    for f, lines, st, _ in blocks:
        asc, desc = f.getmetrics()
        heights.append((asc + desc) * 1.0 * len(lines))
    total = sum(heights) + gap * (len(blocks) - 1)
    y = (H - total) / 2 + float(p.get("y_offset") or 0) * H
    for (f, lines, st, color), bh in zip(blocks, heights):
        asc, desc = f.getmetrics()
        for i, line in enumerate(lines):
            d.text((W / 2, y + i * (asc + desc)), line, font=f, fill=color, anchor="ma",
                   stroke_width=st, stroke_fill=(15, 12, 20, 255))
        y += bh + gap
    img = _shadow(img, max(3, int(12 * s)), 170, (0, int(5 * s)))
    return _crop(img, 2)


RENDERERS = {"counter": counter, "lower_third": lower_third, "big_number": big_number, "card_text": card_text}


def render(ov, W, H):
    return RENDERERS[ov["type"]](W, H, ov)


# --- backgrounds (frame previews) ---------------------------------------------------------------

def gradient(W, H, c1, c2, style="linear"):
    a = Image.new("RGB", (W, H), rgb(c1))
    b = Image.new("RGB", (W, H), rgb(c2))
    if style == "linear":
        side = W + H        # big enough that the rotated square still covers the whole frame
        mask = Image.linear_gradient("L").resize((side, side)).rotate(45, resample=Image.BILINEAR)
        mask = mask.crop(((side - W) // 2, (side - H) // 2, (side + W) // 2, (side + H) // 2))
    else:
        mask = Image.radial_gradient("L").resize((W, H))
    return Image.composite(b, a, mask)


def cover(img, W, H):
    return ImageOps.fit(img.convert("RGB"), (W, H), Image.LANCZOS)


def static_background(W, H, bg, image_path=None):
    t = bg.get("type")
    if t == "image" and image_path and Path(image_path).exists():
        return cover(Image.open(image_path), W, H)
    if t == "gradient":
        return gradient(W, H, bg.get("c1") or "#000000", bg.get("c2") or "#333333", bg.get("style") or "linear")
    return Image.new("RGB", (W, H), rgb(bg.get("c1") or "#000000", (0, 0, 0)))


def dim(img, amount):
    if amount <= 0:
        return img
    return Image.blend(img, Image.new("RGB", img.size, (0, 0, 0)), min(0.9, amount))


def compose(base, layers):
    out = base.convert("RGBA")
    for img, x, y in layers:
        if x < 0 or y < 0:
            img = img.crop((max(0, -x), max(0, -y), img.width, img.height))
            x, y = max(0, x), max(0, y)
        out.alpha_composite(img, (int(x), int(y)))
    return out.convert("RGB")


def jpeg(img, quality=85):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()
