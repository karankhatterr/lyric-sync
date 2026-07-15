#!/usr/bin/env python3
"""LyricSync — live synced lyrics for whatever Spotify is playing.

Usage:
  python3 main.py                       # AppleScript source, terminal display
  python3 main.py --serve 8765          # also expose JSON at http://<mac-ip>:8765/now
  python3 main.py --source spotify      # Spotify Web API source (Premium, later)

The /now endpoint is the microcontroller contract: an ESP32 just polls it
and draws the JSON it gets back. Nothing else in the app is device-specific.
"""
import argparse
import bisect
import json
import threading
import time

import cover_art
from lyrics import fetch_synced_lyrics
from now_playing import make_source

POLL_INTERVAL = 1.0     # seconds between source polls
RESYNC_THRESHOLD = 1.5  # seconds of drift before we trust the source over our clock


class SyncEngine:
    """Tracks current song + interpolated position + current lyric line."""

    def __init__(self, source):
        self.source = source
        self.track = None          # NowPlaying of current song
        self.lyrics = None         # list of (seconds, text) or None
        self.times = []            # just the timestamps, for bisect
        self.position = 0.0
        self.last_tick = time.monotonic()
        self.last_nudge = 0.0
        self.art_url = ""
        self.lock = threading.Lock()

    def poll(self):
        np = self.source.poll()
        now = time.monotonic()
        with self.lock:
            if np is None:
                self.track = None
                self.lyrics = None
                self.times = []
                return
            if self.track is None or np.track_id != self.track.track_id:
                # New song: fetch lyrics once
                self.lyrics = fetch_synced_lyrics(
                    np.title, np.artist, np.album, np.duration)
                self.times = [t for t, _ in self.lyrics] if self.lyrics else []
                self.position = np.position
                # Cover art: use the source's URL, else free iTunes lookup
                self.art_url = (np.art_url
                                or cover_art.itunes_lookup(np.title, np.artist))
            elif abs(np.position - self._estimate(now)) > RESYNC_THRESHOLD:
                self.position = np.position  # user seeked, or drift
            else:
                self.position = self._estimate(now)
            self.track = np
            self.last_tick = now
            # Song seems over (position past the last lyric line)? Ask the
            # source to re-check now instead of waiting for its next cycle.
            if (self.times and self.position > self.times[-1] + 3
                    and now - self.last_nudge > 10):
                recheck = getattr(self.source, "request_recheck", None)
                if recheck:
                    self.last_nudge = now
                    recheck()

    def _estimate(self, now):
        if self.track and self.track.playing:
            return self.position + (now - self.last_tick)
        return self.position

    def snapshot(self):
        """Thread-safe view of current state (used by display AND http server)."""
        now = time.monotonic()
        with self.lock:
            if self.track is None:
                return {"state": "idle",
                        "source": getattr(self.source, "name", "?")}
            pos = self._estimate(now)
            idx = bisect.bisect_right(self.times, pos) - 1 if self.times else -1
            line = self.lyrics[idx][1] if (self.lyrics and idx >= 0) else ""
            next_line = (self.lyrics[idx + 1][1]
                         if self.lyrics and 0 <= idx + 1 < len(self.lyrics) else "")
            return {
                "state": "playing" if self.track.playing else "paused",
                "title": self.track.title,
                "artist": self.track.artist,
                "album": self.track.album,
                "duration": round(self.track.duration, 1),
                "position": round(pos, 1),
                "has_lyrics": self.lyrics is not None,
                "index": idx,
                "line": line,
                "next_line": next_line,
                "art_url": self.art_url or self.track.art_url,
            }

    def window(self, before=2, after=3):
        """Lyric lines around the current one, for the terminal display."""
        snap = self.snapshot()
        idx = snap.get("index", -1)
        with self.lock:
            if not self.lyrics or idx < 0 or idx >= len(self.lyrics):
                return snap, []
            lo = max(0, idx - before)
            hi = min(len(self.lyrics), idx + after + 1)
            rows = [(i == idx, self.lyrics[i][1]) for i in range(lo, hi)]
        return snap, rows


# ------------------------------------------------------------------ display

BOLD, DIM, RESET, CLEAR = "\x1b[1;96m", "\x1b[2m", "\x1b[0m", "\x1b[2J\x1b[H"


def render_esp(engine):
    """esp display mode: 128x64 OLED cover preview on the left,
    the normal live lyrics view on the right."""
    snap, rows = engine.window()
    out = [CLEAR]
    if snap["state"] == "idle":
        out.append("  (idle — esp mode shows the screens when music plays)\n")
        print("".join(out), end="", flush=True)
        return
    if not cover_art.available():
        out.append("  (esp mode needs Pillow: pip3 install pillow)\n")
        print("".join(out), end="", flush=True)
        return
    cover_img = cover_art.oled_image(snap.get("art_url")) or cover_art.blank_oled()
    left = cover_art.braille_lines(cover_img)

    # right column: exactly the normal terminal lyrics view
    mins, secs = divmod(int(snap["position"]), 60)
    right = [f"{snap['title']} — {snap['artist']}   "
             f"[{mins}:{secs:02d}] {snap['state']}", ""]
    if not snap["has_lyrics"]:
        right.append(f"{DIM}(no synced lyrics found on LRCLIB){RESET}")
    else:
        for is_current, text in rows:
            text = text or "♪"
            if is_current:
                right.append(f"{BOLD}▶ {text}{RESET}")
            else:
                right.append(f"{DIM}  {text}{RESET}")
    # vertically center the lyrics block next to the 16-row disc
    pad_top = max(0, (len(left) - len(right)) // 2)
    right = [""] * pad_top + right
    right += [""] * (len(left) - len(right))

    out.append(f"  {DIM}screen 0: cover (128x64){RESET}\n")
    for l, r in zip(left, right):
        out.append(f" {l}  {DIM}|{RESET}  {r}\n")
    print("".join(out), end="", flush=True)


def render(engine):
    snap, rows = engine.window()
    out = [CLEAR]
    if snap["state"] == "idle":
        src = snap.get("source", "")
        if src == "mic":
            out.append("  (listening... play some music near the mic)\n")
        elif src == "apple-music":
            out.append("  (nothing playing — start something in Apple Music)\n")
        else:
            out.append("  (nothing playing — start something in Spotify)\n")
    else:
        mins, secs = divmod(int(snap["position"]), 60)
        out.append(f"  {snap['title']} — {snap['artist']}   "
                   f"[{mins}:{secs:02d}] {snap['state']}\n\n")
        # Spinning pixelated cover when there are no lyrics to show
        # (song has no synced lyrics, or we're in the intro before line 1)
        show_art = (not snap["has_lyrics"]) or snap.get("index", -1) < 0
        art = cover_art.spinner_frame(snap.get("art_url")) if show_art else None
        if art:
            out.append("\n" + art)
            if not snap["has_lyrics"]:
                out.append(f"\n  {DIM}(no synced lyrics found on LRCLIB){RESET}\n")
        elif not snap["has_lyrics"]:
            out.append(f"  {DIM}(no synced lyrics found on LRCLIB){RESET}\n")
        else:
            for is_current, text in rows:
                text = text or "♪"
                if is_current:
                    out.append(f"  {BOLD}▶ {text}{RESET}\n")
                else:
                    out.append(f"  {DIM}  {text}{RESET}\n")
    print("".join(out), end="", flush=True)


# ------------------------------------------------------------------- server

def start_server(engine, port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            p = self.path.rstrip("/")
            if p in ("/frame", "/frame/0", "/frame/1"):
                # raw 1024-byte 128x64 1-bit framebuffers for the ESP32:
                # /frame or /frame/0 = cover disc, /frame/1 = lyrics screen
                snap = engine.snapshot()
                if p == "/frame/1":
                    img = cover_art.oled_lyrics_image(
                        snap.get("title", ""), snap.get("artist", ""),
                        snap.get("line", ""), snap.get("next_line", ""),
                        has_lyrics=snap.get("has_lyrics", False))
                    buf = img.tobytes() if img else None
                else:
                    buf = cover_art.oled_packed(snap.get("art_url"))
                if buf is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(buf)))
                self.end_headers()
                self.wfile.write(buf)
                return
            if self.path.rstrip("/") in ("", "/now", "/api/now"):
                body = json.dumps(engine.snapshot()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Live synced lyrics")
    ap.add_argument("--source",
                    choices=["applescript", "applemusic", "spotify", "mic"],
                    default="applescript")
    ap.add_argument("--serve", type=int, metavar="PORT",
                    help="expose JSON at /now for microcontrollers")
    ap.add_argument("--fps", type=float, default=5.0,
                    help="display refresh rate (default 5)")
    ap.add_argument("--display", choices=["normal", "esp"], default="normal",
                    help="esp = 128x64 OLED preview, spinning cover only")
    args = ap.parse_args()

    engine = SyncEngine(make_source(args.source))
    if args.serve:
        start_server(engine, args.serve)
        print(f"JSON endpoint: http://0.0.0.0:{args.serve}/now")
        time.sleep(1)

    def poller():
        while True:
            engine.poll()
            time.sleep(POLL_INTERVAL)

    threading.Thread(target=poller, daemon=True).start()

    draw = render_esp if args.display == "esp" else render
    try:
        while True:
            draw(engine)
            time.sleep(1.0 / args.fps)
    except KeyboardInterrupt:
        print(RESET + "\nbye")
        # Stop any in-flight mic recording before teardown, then exit
        # immediately so PortAudio's audio thread can't emit noisy errors.
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass
        import os
        os._exit(0)


if __name__ == "__main__":
    main()
