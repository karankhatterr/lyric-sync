"""MicSource — identify whatever is audible via Shazam and sync from the offset.

Works with ANY audio source: Spotify free, YouTube, vinyl, a speaker nearby.

Requires:  pip3 install shazamio sounddevice numpy
Accuracy:  ~±1s (vs near-perfect from the Spotify sources)

How it works:
  1. Record SAMPLE_SECONDS of mic audio
  2. Send to Shazam (unofficial free API via shazamio) -> track + match offset
  3. position = offset + time elapsed since the recording started
  4. Stays responsive via three triggers:
     - silence detection: a few quiet seconds -> idle immediately
     - request_recheck(): called by the sync engine when lyrics run out
       (song ended) -> re-identify right away
     - periodic recheck every RECHECK_SECONDS as fallback for mid-song skips
"""
import asyncio
import io
import os
import sys
import threading
import time
import wave

from now_playing import NowPlaying

SAMPLE_RATE = 16000
SAMPLE_SECONDS = 4      # length of the identification recording
GAP_COOLDOWN = 8        # min seconds between gap-triggered identifications
RECHECK_SECONDS = 30    # periodic re-identify while a track is known
RETRY_SECONDS = 4       # identify pace while nothing is known (but audible)
CHUNK_SECONDS = 0.3     # short volume-check recording
CHECK_EVERY = 1.5       # seconds between volume checks
SILENT_CHUNKS = 3       # consecutive quiet checks (~4.5s) -> idle
# Below this RMS the mic is considered "suspiciously quiet". Volume alone
# never declares idle — it only triggers a Shazam check, and Shazam's
# match/no-match is the final verdict. So this number is not critical.
SILENCE_RMS = float(os.environ.get("LYRICSYNC_SILENCE_RMS", 100))
CONFIRM_COOLDOWN = 12   # min seconds between quiet-triggered confirmations

_DEBUG = bool(os.environ.get("LYRICSYNC_DEBUG"))


def _log(msg):
    if _DEBUG:
        print(f"[mic] {msg}", file=sys.stderr)


class MicSource:
    name = "mic"

    def __init__(self):
        try:
            import sounddevice  # noqa: F401
            from shazamio import Shazam  # noqa: F401
        except ImportError as e:
            raise SystemExit(
                f"Missing dependency ({e.name}). Install with:\n"
                "  pip3 install shazamio sounddevice numpy --break-system-packages"
            )
        self.lock = threading.Lock()
        self.current = None      # dict: title/artist/album/offset/t0
        self.wake = threading.Event()
        self.quiet_streak = 0
        threading.Thread(target=self._worker, daemon=True).start()

    def request_recheck(self):
        """Sync engine calls this when the song seems over -> identify now."""
        _log("recheck requested (song likely ended)")
        self.wake.set()

    # ------------------------------------------------------------- recording

    def _record(self, seconds):
        import numpy as np
        import sounddevice as sd
        t0 = time.monotonic()
        audio = sd.rec(int(SAMPLE_RATE * seconds), samplerate=SAMPLE_RATE,
                       channels=1, dtype="int16")
        sd.wait()
        return np.asarray(audio), t0

    def _chunk_rms(self):
        import numpy as np
        audio, _ = self._record(CHUNK_SECONDS)
        return float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))

    # --------------------------------------------------------- identification

    def _identify_once(self):
        from shazamio import Shazam
        audio, t0 = self._record(SAMPLE_SECONDS)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(audio.tobytes())
        result = asyncio.run(Shazam().recognize(buf.getvalue()))
        track = result.get("track")
        matches = result.get("matches") or []
        if not track or not matches:
            return None
        offset = matches[0].get("offset", 0.0) or 0.0
        album = ""
        for section in track.get("sections", []):
            for meta in section.get("metadata", []):
                if meta.get("title") == "Album":
                    album = meta.get("text", "")
        return {
            "title": track.get("title", ""),
            "artist": track.get("subtitle", ""),
            "album": album,
            "offset": float(offset),
            "t0": t0,
        }

    def _worker(self):
        last_identify = 0.0
        while True:
            forced = self.wake.wait(timeout=CHECK_EVERY)
            self.wake.clear()
            now = time.monotonic()
            with self.lock:
                known = self.current is not None

            # 1. cheap volume check
            try:
                rms = self._chunk_rms()
            except Exception as e:
                _log(f"mic read failed: {e}")
                time.sleep(2)
                continue
            if rms < SILENCE_RMS:
                self.quiet_streak += 1
                _log(f"quiet (rms={rms:.0f}, streak={self.quiet_streak})")
                if (known and self.quiet_streak >= SILENT_CHUNKS
                        and now - last_identify >= CONFIRM_COOLDOWN):
                    # Sounds like silence, but volume is unreliable (user may
                    # have just turned it down). Let Shazam make the call.
                    _log("quiet streak -> confirming with Shazam")
                    last_identify = now
                    try:
                        hit = self._identify_once()
                    except Exception as e:
                        _log(f"confirm failed: {e}")
                        continue
                    with self.lock:
                        if hit:
                            _log("still playing (quietly) — resynced")
                            self.current = hit
                            self.quiet_streak = 0
                        else:
                            _log("no match -> idle")
                            self.current = None
                continue  # idle + silent room: don't waste lookups

            # quiet gap followed by sound again = classic song-change
            # signature -> identify right away
            if (self.quiet_streak > 0 and known
                    and now - last_identify >= GAP_COOLDOWN):
                _log("audio gap detected -> checking for song change")
                forced = True
            self.quiet_streak = 0

            # 2. identify when: forced, or retry pace (idle), or recheck pace
            due = RECHECK_SECONDS if known else RETRY_SECONDS
            if not forced and now - last_identify < due:
                continue
            last_identify = now
            try:
                hit = self._identify_once()
            except Exception as e:
                _log(f"identify failed: {e}")
                continue
            with self.lock:
                if hit:
                    _log(f"matched: {hit['artist']} - {hit['title']} "
                         f"@ {hit['offset']:.1f}s (rms={rms:.0f})")
                    self.current = hit
                elif not known:
                    _log("no match (audible but unrecognized)")
                else:
                    _log("no match -> assuming song ended")
                    self.current = None

    # ---------------------------------------------------------------- source

    def poll(self):
        with self.lock:
            cur = self.current
        if cur is None:
            return None
        return NowPlaying(
            title=cur["title"],
            artist=cur["artist"],
            album=cur["album"],
            duration=0.0,  # Shazam doesn't give duration; LRCLIB search copes
            position=cur["offset"] + (time.monotonic() - cur["t0"]),
            playing=True,
        )
