"""LRCLIB client + LRC parser. Zero dependencies (stdlib only)."""
import json
import re
import urllib.parse
import urllib.request

USER_AGENT = "LyricSync/0.1 (personal hobby project)"
_TS = re.compile(r"\[(\d+):(\d{1,2}(?:\.\d+)?)\]")


def parse_lrc(text):
    """Parse LRC text into a sorted list of (seconds, line_text)."""
    lines = []
    for raw in text.splitlines():
        stamps = _TS.findall(raw)
        if not stamps:
            continue
        lyric = _TS.sub("", raw).strip()
        for minutes, seconds in stamps:
            lines.append((int(minutes) * 60 + float(seconds), lyric))
    lines.sort(key=lambda x: x[0])
    return lines


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_synced_lyrics(track, artist, album=None, duration=None):
    """Fetch synced lyrics from LRCLIB.

    Returns a list of (seconds, text), or None if nothing synced was found.
    Tries the exact /api/get match first, then falls back to /api/search.
    """
    params = {"track_name": track, "artist_name": artist}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = int(round(duration))
    try:
        data = _get_json("https://lrclib.net/api/get?" + urllib.parse.urlencode(params))
        if data.get("syncedLyrics"):
            return parse_lrc(data["syncedLyrics"])
    except Exception:
        pass

    # Fallback: fuzzy search, take the first hit with synced lyrics
    try:
        q = urllib.parse.urlencode({"track_name": track, "artist_name": artist})
        for hit in _get_json("https://lrclib.net/api/search?" + q)[:5]:
            if hit.get("syncedLyrics"):
                return parse_lrc(hit["syncedLyrics"])
    except Exception:
        pass
    return None
