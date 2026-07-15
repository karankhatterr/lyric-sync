"""Spinning vinyl-disc album cover, rendered as ANSI half-block pixel art.

- Circular crop with a small center hole (vinyl look); corners transparent.
- Truecolor output when the terminal supports it (COLORTERM), else 256-color
  so it still works in stock macOS Terminal.app.
- itunes_lookup(): free, keyless iTunes Search API fallback for sources that
  don't provide art (e.g. Apple Music's AppleScript).

Requires Pillow (pip3 install pillow). Degrades gracefully without it.
"""
import io
import json
import math
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None

SIZE = 30          # disc width in "pixels" (chars wide = SIZE, rows = SIZE/2)
SPIN_DEG_PER_S = 60
_SRC = 120         # working resolution before downscale
_UA = {"User-Agent": "LyricSync/0.1"}

_DEBUG = bool(os.environ.get("LYRICSYNC_DEBUG"))
TRUECOLOR = os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")

if _DEBUG and Image is None:
    print("[art] Pillow not installed -> cover spinner disabled "
          "(pip3 install pillow --break-system-packages)", file=sys.stderr)

_cache_url = None
_cache_img = None
_fetching = False
_lock = threading.Lock()


def available():
    return Image is not None


# ------------------------------------------------------------- iTunes lookup

def itunes_lookup(title, artist):
    """Free cover-art lookup by song+artist (no API key). Returns URL or ''."""
    if not title:
        return ""
    try:
        q = urllib.parse.urlencode({"term": f"{artist} {title}",
                                    "media": "music", "entity": "song",
                                    "limit": 1})
        req = urllib.request.Request("https://itunes.apple.com/search?" + q,
                                     headers=_UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            results = json.loads(r.read().decode()).get("results") or []
        url = results[0].get("artworkUrl100", "") if results else ""
        return url.replace("100x100", "600x600")
    except Exception as e:
        if _DEBUG:
            print(f"[art] itunes lookup failed: {e}", file=sys.stderr)
        return ""


# ------------------------------------------------------------ fetch + shape

def _make_disc(img):
    """Square cover -> RGBA vinyl disc (circular crop + center hole) on a
    padded transparent canvas so rotation never clips."""
    mask = Image.new("L", (_SRC, _SRC), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse((0, 0, _SRC - 1, _SRC - 1), fill=255)
    hole = int(_SRC * 0.07)
    c = _SRC // 2
    d.ellipse((c - hole, c - hole, c + hole, c + hole), fill=0)
    pad = int(_SRC * (math.sqrt(2) - 1) / 2) + 1
    canvas = Image.new("RGBA", (_SRC + 2 * pad, _SRC + 2 * pad), (0, 0, 0, 0))
    canvas.paste(img, (pad, pad), mask)
    return canvas


def _fetch(url):
    global _cache_url, _cache_img, _fetching
    try:
        req = urllib.request.Request(url, headers=_UA)
        data = urllib.request.urlopen(req, timeout=10).read()
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img = img.resize((_SRC, _SRC), Image.LANCZOS)
        disc = _make_disc(img)
        with _lock:
            _cache_url, _cache_img = url, disc
        if _DEBUG:
            print(f"[art] cover loaded: {url[:60]}", file=sys.stderr)
    except Exception as e:
        if _DEBUG:
            print(f"[art] cover fetch failed: {e} ({url[:60]})", file=sys.stderr)
        with _lock:
            _cache_url, _cache_img = url, None  # remember the failure
    finally:
        _fetching = False


def _get_disc(url):
    global _fetching
    if not url or Image is None:
        return None
    with _lock:
        if url == _cache_url:
            return _cache_img
        busy = _fetching
    if not busy:
        _fetching = True
        threading.Thread(target=_fetch, args=(url,), daemon=True).start()
    return None


# ---------------------------------------------------------------- rendering

def _fg(p):
    if TRUECOLOR:
        return f"\x1b[38;2;{p[0]};{p[1]};{p[2]}m"
    n = 16 + 36 * round(p[0] / 255 * 5) + 6 * round(p[1] / 255 * 5) + round(p[2] / 255 * 5)
    return f"\x1b[38;5;{n}m"


def _bg(p):
    if TRUECOLOR:
        return f"\x1b[48;2;{p[0]};{p[1]};{p[2]}m"
    n = 16 + 36 * round(p[0] / 255 * 5) + 6 * round(p[1] / 255 * 5) + round(p[2] / 255 * 5)
    return f"\x1b[48;5;{n}m"


# ------------------------------------------------- 128x64 OLED (esp mode)

OLED_W, OLED_H = 128, 64
_DISC_PX = 60  # disc diameter on the OLED


def oled_image(url):
    """1-bit dithered 128x64 frame with the spinning disc centered, or None."""
    disc = _get_disc(url)
    if disc is None:
        return None
    angle = (time.monotonic() * SPIN_DEG_PER_S) % 360
    frame = disc.rotate(-angle, resample=Image.BICUBIC)
    target = int(_DISC_PX * frame.width / _SRC)
    frame = frame.resize((target, target), Image.LANCZOS)
    left = (target - OLED_H) // 2
    frame = frame.crop((left, left, left + OLED_H, left + OLED_H))
    rgb = Image.new("RGB", (OLED_H, OLED_H), (0, 0, 0))
    rgb.paste(frame, (0, 0), frame)
    mono = rgb.convert("L").convert("1")  # Floyd-Steinberg dither
    out = Image.new("1", (OLED_W, OLED_H), 0)
    out.paste(mono, ((OLED_W - OLED_H) // 2, 0))
    return out


def oled_packed(url):
    """Raw 1024-byte framebuffer (row-major, MSB first) for the ESP32 to
    blit straight to an SSD1306 via drawBitmap(). None if no art."""
    img = oled_image(url)
    return None if img is None else img.tobytes()


def _fit(draw, text, font, width):
    while text and draw.textlength(text, font=font) > width:
        text = text[:-1]
    return text


def _wrap(draw, text, font, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if draw.textlength(cand, font=font) <= width:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = _fit(draw, w, font, width)
    if cur:
        lines.append(cur)
    return lines or [""]


def oled_lyrics_image(title, artist, line, next_line, has_lyrics=True):
    """1-bit 128x64 lyrics screen, same layout as the ESP32 sketch:
    header / separator / wrapped current line / next-line preview."""
    if Image is None:
        return None
    from PIL import ImageFont
    img = Image.new("1", (OLED_W, OLED_H), 0)
    d = ImageDraw.Draw(img)
    f = ImageFont.load_default()
    d.text((0, 0), _fit(d, f"{title} - {artist}", f, OLED_W), font=f, fill=1)
    d.line((0, 11, OLED_W - 1, 11), fill=1)
    body = line if has_lyrics else "(no synced lyrics)"
    y = 15
    for chunk in _wrap(d, body or "~", f, OLED_W)[:3]:
        d.text((0, y), chunk, font=f, fill=1)
        y += 11
    if next_line and has_lyrics:
        d.text((0, 52), _fit(d, "> " + next_line, f, OLED_W), font=f, fill=1)
    return img


_BRAILLE_BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))


def braille_lines(img):
    """1-bit 128x64 image -> list of 16 braille strings (64 chars each)."""
    px = img.load()
    out = []
    for cy in range(0, OLED_H, 4):
        row = []
        for cx in range(0, OLED_W, 2):
            bits = 0
            for dy in range(4):
                for dx in range(2):
                    if px[cx + dx, cy + dy]:
                        bits |= _BRAILLE_BITS[dy][dx]
            row.append(chr(0x2800 + bits))
        out.append("".join(row))
    return out


def blank_oled():
    return Image.new("1", (OLED_W, OLED_H), 0) if Image else None


def oled_braille(url, indent=" "):
    """Terminal preview of the cover OLED frame using braille (2x4 px/char)."""
    img = oled_image(url)
    if img is None:
        return None
    return "".join(indent + ln + "\n" for ln in braille_lines(img))


def spinner_frame(url, indent="  "):
    """ANSI art of the disc at the current rotation angle, or None."""
    disc = _get_disc(url)
    if disc is None:
        return None
    angle = (time.monotonic() * SPIN_DEG_PER_S) % 360
    frame = disc.rotate(-angle, resample=Image.BICUBIC)
    small = frame.resize((SIZE, SIZE), Image.LANCZOS)
    px = small.load()
    out = []
    for y in range(0, SIZE - 1, 2):
        row = [indent]
        for x in range(SIZE):
            t, b = px[x, y], px[x, y + 1]
            top, bot = t[3] >= 128, b[3] >= 128
            if top and bot:
                row.append(f"{_fg(t)}{_bg(b)}▀")       # ▀ fg=top bg=bottom
            elif top:
                row.append(f"\x1b[0m{_fg(t)}▀")        # ▀ on default bg
            elif bot:
                row.append(f"\x1b[0m{_fg(b)}▄")        # ▄ on default bg
            else:
                row.append("\x1b[0m ")                      # fully transparent
        row.append("\x1b[0m\n")
        out.append("".join(row))
    return "".join(out)
