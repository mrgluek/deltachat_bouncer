"""Background audio for voice meetings: internet radio streams and played files.

A BackgroundSource decodes one input (an http(s) stream or a local file the
bot downloaded) in its own thread into 20 ms frames of 48 kHz mono int16 -
the meeting mixer's format - and hands them over through a small queue. The
queue is the clock: the mixer takes one frame per tick, and the decoder
blocks while the queue is full, so a file plays in real time and a live
stream simply fills its socket buffer. Decoding never runs on the call loop.

Inputs are opened with ffmpeg protocol/format whitelists: streams may only
use http(s), files only the local file protocol, and only audio/video
container formats are accepted, so a crafted file or playlist cannot make
ffmpeg read other local files or fetch other URLs.

A radio stream pauses (its connection is closed) while nobody is connected
to the meeting and reconnects with back-off after errors. A file pauses
while nobody listens and ends at EOF.
"""
import os
import queue
import re
import threading
import time
from typing import Callable, Optional
from urllib.parse import urlparse

import numpy as np

import config

SAMPLE_RATE = 48000
FRAME_SAMPLES = 960
QUEUE_FRAMES = 25            # 0.5 s between decoder and mixer
OPEN_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 15.0
IDLE_CLOSE_S = 10.0          # radio: drop the connection after this long without listeners
RECONNECT_MAX_S = 30.0

STREAM_PROTOCOLS = "http,https,tcp,tls,crypto,httpproxy"
STREAM_FORMATS = "mp3,aac,ogg,flac,wav,hls,mpegts,mov,matroska"
FILE_PROTOCOLS = "file"
FILE_FORMATS = "mp3,aac,ogg,flac,wav,mov,matroska,mpegts,amr,aiff,caf,m4a,webm"

URL_RE = re.compile(r"^https?://[^\s/?#]+[^\s]*$", re.IGNORECASE)


def valid_stream_url(url: str) -> bool:
    if not url or len(url) > 500 or not URL_RE.match(url):
        return False
    host = urlparse(url).hostname or ""
    return bool(host)


def _open(spec: str, kind: str):
    import av

    if kind == "radio":
        options = {
            "protocol_whitelist": STREAM_PROTOCOLS,
            "format_whitelist": STREAM_FORMATS,
            "icy": "1",
            "user_agent": f"DeltaChatBouncerBot/{config.VERSION}",
        }
        return av.open(spec, timeout=(OPEN_TIMEOUT_S, READ_TIMEOUT_S), options=options)
    options = {"protocol_whitelist": FILE_PROTOCOLS, "format_whitelist": FILE_FORMATS}
    return av.open("file:" + os.path.abspath(spec), options=options)


def _audio_stream(container):
    if not container.streams.audio:
        raise ValueError("no audio track")
    return container.streams.audio[0]


def describe(container, stream) -> dict:
    meta = {k.lower(): v for k, v in (container.metadata or {}).items()}
    ctx = stream.codec_context
    duration = None
    if container.duration and container.duration > 0:
        duration = container.duration / 1_000_000
    elif stream.duration and stream.time_base:
        duration = float(stream.duration * stream.time_base)
    return {
        "name": (meta.get("icy-name") or meta.get("title") or "").strip(),
        "codec": ctx.name,
        "rate": ctx.sample_rate,
        "channels": getattr(ctx, "channels", None) or len(ctx.layout.channels),
        "duration": duration,
    }


def probe(spec: str, kind: str = "radio", timeout: float = 15.0) -> dict:
    """Open an input, decode its first audio and return describe() info.

    Raises on anything that would not play (unreachable, not audio, ...)."""
    started = time.time()
    container = _open(spec, kind)
    try:
        stream = _audio_stream(container)
        info = describe(container, stream)
        for packet in container.demux(stream):
            for frame in packet.decode():
                if frame.samples:
                    return info
            if time.time() - started > timeout:
                break
        raise ValueError("no audio could be decoded")
    finally:
        container.close()


def format_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return ""
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class BackgroundSource:
    """Decodes ``spec`` (``kind`` "radio" or "file") into mixer frames.

    ``listening()`` tells whether anyone is in the meeting; ``on_end(source,
    error)`` is called once when a file finished (error None) or the source
    gave up (error text). ``stop()`` never calls it.
    """

    def __init__(self, spec: str, kind: str, title: str,
                 listening: Callable[[], bool] = lambda: True,
                 on_end: Optional[Callable[["BackgroundSource", Optional[str]], None]] = None,
                 radio_id: Optional[int] = None):
        self.spec, self.kind, self.title = spec, kind, title
        self.radio_id = radio_id
        self.listening = listening
        self.on_end = on_end
        self.frames: queue.Queue = queue.Queue(maxsize=QUEUE_FRAMES)
        self.started = threading.Event()      # first frame decoded (or failed: see error)
        self.error: Optional[str] = None
        self.info: dict = {}
        self.frames_out = 0
        self._retries = 0
        self._idle_since: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- control -----------------------------------------------------------

    def start(self) -> "BackgroundSource":
        self._thread = threading.Thread(target=self._run, name=f"meet-{self.kind}", daemon=True)
        self._thread.start()
        return self

    def wait_started(self, timeout: float = OPEN_TIMEOUT_S + 5) -> bool:
        """True once audio flows; False on error or timeout (see .error)."""
        self.started.wait(timeout)
        return self.started.is_set() and self.error is None

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def read(self) -> Optional[np.ndarray]:
        """One 960-sample int16 frame, or None (nothing decoded yet / underrun)."""
        try:
            pcm = self.frames.get_nowait()
        except queue.Empty:
            return None
        self.frames_out += 1
        return pcm

    # -- decoder thread ----------------------------------------------------

    def _check_idle(self) -> None:
        """Radio: give up the connection after IDLE_CLOSE_S without listeners."""
        if self.kind != "radio":
            return
        if self.listening():
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = time.time()
        elif time.time() - self._idle_since > IDLE_CLOSE_S:
            self._idle_since = None
            raise _Idle()

    def _put(self, pcm: np.ndarray) -> bool:
        """Queue a frame, waiting while the mixer is behind (or nobody listens)."""
        while not self._stop.is_set():
            try:
                self.frames.put(pcm, timeout=0.5)
                return True
            except queue.Full:
                self._check_idle()
        return False

    def _decode(self, container) -> None:
        """Feed frames until EOF, stop, or (radio) a long time without listeners."""
        import av

        stream = _audio_stream(container)
        self.info = describe(container, stream)
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE,
                                      frame_size=FRAME_SAMPLES)
        self._idle_since = None
        for packet in container.demux(stream):
            if self._stop.is_set():
                return
            for frame in packet.decode():
                for out in resampler.resample(frame):
                    pcm = out.to_ndarray().reshape(-1).copy()
                    if pcm.size != FRAME_SAMPLES:
                        continue
                    if not self.started.is_set():
                        self.started.set()
                    self._retries = 0  # audio flows again: reset the back-off
                    self._check_idle()
                    if not self._put(pcm):
                        return
        for out in resampler.resample(None):
            pcm = out.to_ndarray().reshape(-1)
            if pcm.size == FRAME_SAMPLES and not self._put(pcm.copy()):
                return

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.kind == "radio" and self.started.is_set() and not self.listening():
                time.sleep(0.5)  # paused: nobody in the meeting
                continue
            try:
                container = _open(self.spec, self.kind)
            except Exception as e:
                if not self._failed(f"cannot open: {_short(e)}"):
                    continue
                return
            try:
                self._decode(container)
            except _Idle:
                config.logger.info(f"Meet {self.kind} '{self.title}': no listeners, pausing")
                continue
            except Exception as e:
                if self._stop.is_set():
                    return
                if not self._failed(f"stream error: {_short(e)}"):
                    continue
                return
            finally:
                try:
                    container.close()
                except Exception:
                    pass
            if self._stop.is_set():
                return
            if self.kind == "file":
                # let the mixer play what is still queued before reporting the end
                while not self.frames.empty() and not self._stop.is_set():
                    time.sleep(0.02)
                if not self._stop.is_set():
                    self._end(None)
                return
            # a live stream ended: reconnect
            if not self._failed("stream ended"):
                continue
            return

    def _failed(self, why: str) -> bool:
        """Handle an error. Returns False to retry (radio after back-off), True when done."""
        if not self.started.is_set():
            self.error = why
            self.started.set()
            self._end(why)
            return True
        if self.kind == "file":
            self._end(why)
            return True
        self._retries += 1
        delay = min(RECONNECT_MAX_S, 2.0 * 2 ** (self._retries - 1))
        config.logger.info(f"Meet radio '{self.title}': {why}, reconnecting in {delay:.0f}s")
        if self._retries > 20:
            self._end(why)
            return True
        self._stop.wait(delay)
        return self._stop.is_set()

    def _end(self, error: Optional[str]) -> None:
        if self.on_end is not None and not self._stop.is_set():
            try:
                self.on_end(self, error)
            except Exception as e:
                config.logger.warning(f"Meet {self.kind} on_end failed: {e}")


class _Idle(Exception):
    pass


def _short(e: Exception) -> str:
    text = str(e) or type(e).__name__
    return text if len(text) < 160 else text[:157] + "..."
