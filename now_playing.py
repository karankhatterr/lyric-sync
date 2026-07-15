"""Now-playing sources. Each source returns a NowPlaying snapshot or None.

Swap sources freely: the rest of the app only knows this interface.
- AppleScriptSource: talks to the Spotify desktop app on macOS (free account OK)
- SpotifyApiSource:  Spotify Web API (needs Premium for dev mode since Feb 2026)
"""
import os
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class NowPlaying:
    title: str
    artist: str
    album: str
    duration: float   # seconds
    position: float   # seconds
    playing: bool

    @property
    def track_id(self):
        return f"{self.artist} - {self.title}"


# ---------------------------------------------------------------- AppleScript
# Simple one-line commands only (multiline scripts fail on some setups).
# The "is running" guard stops osascript from auto-launching the app.
# Works for both Spotify and Apple Music (app "Music") — same dictionary,
# except Spotify reports duration in ms and Music in seconds.

_SEP = "|~|"
_DEBUG = bool(os.environ.get("LYRICSYNC_DEBUG"))


def _num(s):
    """Parse a number that may use a comma decimal separator (locale quirk)."""
    return float(s.replace(",", "."))


def _osascript(cmd):
    res = subprocess.run(["osascript", "-e", cmd],
                         capture_output=True, text=True, timeout=5)
    if _DEBUG and res.stderr.strip():
        print(f"osascript stderr: {res.stderr.strip()}", file=sys.stderr)
    return res.stdout.strip()


class AppleScriptSource:
    """Reads the Spotify or Apple Music desktop app on macOS.

    No account restrictions. app="Spotify" (duration in ms) or
    app="Music" (duration in seconds).
    """

    def __init__(self, app="Spotify"):
        self.app = app
        self.name = "spotify-app" if app == "Spotify" else "apple-music"
        self._dur_scale = 1000.0 if app == "Spotify" else 1.0
        guard = (f'if application "{app}" is running then '
                 f'tell application "{app}" to ')
        self._cmd_state = guard + "get player state"
        self._cmd_meta = (guard + 'name of current track & "|~|" & '
                          'artist of current track & "|~|" & '
                          'album of current track & "|~|" & '
                          'duration of current track')
        self._cmd_pos = guard + "get player position"

    def poll(self):
        try:
            state = _osascript(self._cmd_state)
            if state not in ("playing", "paused"):
                if _DEBUG:
                    print(f"player state = {state!r} (not running/stopped)",
                          file=sys.stderr)
                return None
            meta = _osascript(self._cmd_meta).split(_SEP)
            position = _osascript(self._cmd_pos)
            if len(meta) < 4 or not position:
                if _DEBUG:
                    print(f"bad meta={meta!r} pos={position!r}", file=sys.stderr)
                return None
        except Exception as e:
            if _DEBUG:
                print(f"osascript failed: {e}", file=sys.stderr)
            return None
        return NowPlaying(
            title=meta[0],
            artist=meta[1],
            album=meta[2],
            duration=_num(meta[3]) / self._dur_scale,
            position=_num(position),
            playing=(state == "playing"),
        )


# ------------------------------------------------------------ Spotify Web API

class SpotifyApiSource:
    """Spotify Web API source (add later — requires Premium for dev mode).

    Setup:
      1. Create an app at https://developer.spotify.com/dashboard
         with redirect URI http://127.0.0.1:8888/callback
      2. export SPOTIFY_CLIENT_ID=...
      3. Run with --source spotify ; a browser opens once to authorize.
         The refresh token is cached in ~/.lyric_sync_spotify.json
    """

    name = "spotify"
    TOKEN_URL = "https://accounts.spotify.com/api/token"
    AUTH_URL = "https://accounts.spotify.com/authorize"
    API_URL = "https://api.spotify.com/v1/me/player/currently-playing"
    REDIRECT = "http://127.0.0.1:8888/callback"
    SCOPE = "user-read-currently-playing user-read-playback-state"

    def __init__(self):
        self.client_id = os.environ.get("SPOTIFY_CLIENT_ID")
        if not self.client_id:
            raise SystemExit("Set SPOTIFY_CLIENT_ID env var (see SpotifyApiSource docstring).")
        self.token_file = os.path.expanduser("~/.lyric_sync_spotify.json")
        self.access_token = None
        self.expires_at = 0
        self._ensure_token()

    # --- auth (PKCE flow, no client secret needed) ---

    def _ensure_token(self):
        import json, time
        if self.access_token and time.time() < self.expires_at - 30:
            return
        refresh = None
        if os.path.exists(self.token_file):
            with open(self.token_file) as f:
                refresh = json.load(f).get("refresh_token")
        if refresh:
            tok = self._token_request({
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": self.client_id,
            })
        else:
            tok = self._interactive_auth()
        self.access_token = tok["access_token"]
        self.expires_at = time.time() + tok.get("expires_in", 3600)
        if tok.get("refresh_token"):
            with open(self.token_file, "w") as f:
                json.dump({"refresh_token": tok["refresh_token"]}, f)

    def _interactive_auth(self):
        import base64, hashlib, http.server, secrets, urllib.parse, webbrowser
        verifier = secrets.token_urlsafe(64)[:128]
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        url = self.AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.REDIRECT,
            "scope": self.SCOPE,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        })
        code_holder = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                code_holder["code"] = q.get("code", [None])[0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Authorized! You can close this tab.")

            def log_message(self, *a):
                pass

        print("Opening browser for Spotify authorization...")
        webbrowser.open(url)
        with http.server.HTTPServer(("127.0.0.1", 8888), Handler) as srv:
            while "code" not in code_holder:
                srv.handle_request()
        return self._token_request({
            "grant_type": "authorization_code",
            "code": code_holder["code"],
            "redirect_uri": self.REDIRECT,
            "client_id": self.client_id,
            "code_verifier": verifier,
        })

    def _token_request(self, data):
        import json, urllib.parse, urllib.request
        req = urllib.request.Request(
            self.TOKEN_URL,
            data=urllib.parse.urlencode(data).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    # --- polling ---

    def poll(self):
        import json, urllib.request
        self._ensure_token()
        req = urllib.request.Request(
            self.API_URL, headers={"Authorization": f"Bearer {self.access_token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 204:
                    return None
                data = json.loads(resp.read().decode())
        except Exception:
            return None
        item = data.get("item")
        if not item:
            return None
        return NowPlaying(
            title=item["name"],
            artist=", ".join(a["name"] for a in item["artists"]),
            album=item["album"]["name"],
            duration=item["duration_ms"] / 1000.0,
            position=(data.get("progress_ms") or 0) / 1000.0,
            playing=bool(data.get("is_playing")),
        )


def make_source(name):
    if name == "spotify":
        return SpotifyApiSource()
    if name == "mic":
        from mic_source import MicSource
        return MicSource()
    if name == "applemusic":
        return AppleScriptSource(app="Music")
    return AppleScriptSource(app="Spotify")
