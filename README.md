# LyricSync

Live synced lyrics in your terminal for whatever Spotify is playing. Zero dependencies — Python 3 stdlib only.

## Run it

```bash
python3 main.py
```

Play something in the Spotify desktop app and the current lyric line highlights in real time.
First run: macOS will ask permission for Terminal to control Spotify — click OK.

## Architecture (why it's portable)

```
[source]  →  SyncEngine  →  terminal display
                         →  HTTP JSON endpoint (/now)   ← ESP32 polls this
```

- **Sources are pluggable** (`now_playing.py`). Today: AppleScript → Spotify desktop app (works on a free account). Later: `--source spotify` uses the Web API — nothing else changes.
- **Lyrics** come from [LRCLIB](https://lrclib.net) (`lyrics.py`): free, no API key, no rate limits.
- **Sync**: source polled every 1s, position interpolated locally between polls, current line found by binary search over LRC timestamps.

## ESP32 contract

Run with the server enabled:

```bash
python3 main.py --serve 8765
```

Any device on your WiFi can then `GET http://<your-mac-ip>:8765/now`:

```json
{"state": "playing", "title": "...", "artist": "...", "album": "...",
 "duration": 180.0, "position": 42.3, "has_lyrics": true,
 "index": 7, "line": "current lyric line", "next_line": "upcoming line"}
```

The ESP32 sketch only needs: WiFi connect → HTTP GET every ~1s → draw `line` (and `next_line`) on the display. No HTTPS, no lyric storage, no Spotify auth on the microcontroller.

Find your Mac's IP: `ipconfig getifaddr en0`

## Apple Music

Same precise sync as the Spotify app source — the macOS Music app has the
identical AppleScript interface:

```bash
python3 main.py --source applemusic
```

Only works when playing through the Mac's Music app (Apple's cloud API does
not expose live playback position, so phone playback needs mic mode).

## Mic mode (works with ANY audio source)

Identifies whatever is audible via Shazam and syncs from the match offset —
Spotify free, YouTube, vinyl, anything. Accuracy ~±1s.

```bash
pip3 install shazamio sounddevice numpy --break-system-packages
python3 main.py --source mic
```

First run: macOS asks for microphone permission for Terminal — allow it.
It records 5s, identifies, then re-checks every 30s. Add `LYRICSYNC_DEBUG=1`
to see match logs. Note: shazamio is an unofficial API and could break someday.

## ESP32 client

`esp32_lyrics/esp32_lyrics.ino` — polls `/now` and renders on a 128x64 SSD1306 OLED.

Wiring: VCC→3V3, GND→GND, SDA→GPIO21, SCL→GPIO22.
Arduino IDE: install the `esp32` board package plus libraries `ArduinoJson`,
`Adafruit SSD1306`, `Adafruit GFX Library`. Set your WiFi credentials and
Mac IP at the top of the sketch, upload, and run `python3 main.py --serve 8765`
(any source: applescript, mic, or spotify — the ESP32 doesn't care).

## Adding the Spotify Web API later

Requires Spotify Premium (Development Mode rule since Feb 2026).

1. Create an app at https://developer.spotify.com/dashboard — redirect URI `http://127.0.0.1:8888/callback`
2. `export SPOTIFY_CLIENT_ID=<your client id>`
3. `python3 main.py --source spotify` — browser opens once to authorize; the refresh token is cached in `~/.lyric_sync_spotify.json`

Once that works, the ESP32 could even go fully standalone (calling Spotify + LRCLIB itself over HTTPS) — the sync logic in `main.py` is the blueprint.

## Files

- `main.py` — sync engine, terminal display, HTTP server, CLI
- `now_playing.py` — AppleScript + Spotify API sources behind one interface
- `lyrics.py` — LRCLIB client and LRC parser
