"""Background audio for meetings (meet_media.py): streams, files, whitelists.

A tiny Icecast-like HTTP server on localhost serves an endless ADTS/AAC
stream with ICY headers; files are generated with PyAV.

Run: python3 -m unittest tests.test_meet_media -v
"""
import http.server
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

TEST_DB = "test_meet_media.db"
os.environ["DB_PATH"] = TEST_DB

from tests import test_calls  # noqa: E402,F401  (installs the deltachat2/deltabot_cli mocks if needed)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import av
    import numpy as np

    import meet_media
except ImportError:  # no call support installed
    av = None


def make_aac(seconds=2.0, freq=440.0, rate=44100) -> bytes:
    buf = io.BytesIO()
    out = av.open(buf, "w", format="adts")
    st = out.add_stream("aac", rate=rate, layout="stereo")
    t = np.arange(int(rate * seconds)) / rate
    pcm = (np.sin(2 * np.pi * freq * t) * 0.4 * 32767).astype(np.int16)
    frame = av.AudioFrame.from_ndarray(np.stack([pcm, pcm]).reshape(1, -1, order="F"),
                                       format="s16", layout="stereo")
    frame.sample_rate = rate
    for p in st.encode(frame):
        out.mux(p)
    for p in st.encode(None):
        out.mux(p)
    out.close()
    return buf.getvalue()


def make_video(path, seconds=2.0, freq=660.0) -> str:
    """An mp4 with a black video track and a sine audio track."""
    out = av.open(path, "w")
    vs = out.add_stream("mpeg4", rate=10)
    vs.width, vs.height, vs.pix_fmt = 64, 48, "yuv420p"
    aud = out.add_stream("aac", rate=48000, layout="mono")
    for _ in range(int(seconds * 10)):
        img = av.VideoFrame.from_ndarray(np.zeros((48, 64, 3), np.uint8), format="rgb24")
        for p in vs.encode(img):
            out.mux(p)
    t = np.arange(int(48000 * seconds)) / 48000
    a = av.AudioFrame.from_ndarray((np.sin(2 * np.pi * freq * t) * 12000).astype(np.int16).reshape(1, -1),
                                   format="s16", layout="mono")
    a.sample_rate = 48000
    for p in aud.encode(a):
        out.mux(p)
    for s in (vs, aud):
        for p in s.encode(None):
            out.mux(p)
    out.close()
    return path


class StreamServer:
    """Endless AAC 'radio' with ICY headers. ``drop_after`` chunks closes the
    connection (to test reconnects); ``connections`` counts GETs."""

    def __init__(self, drop_after=None, freq=440.0):
        self.chunk = make_aac(freq=freq)
        self.connections = 0
        self.open_connections = 0
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                server.connections += 1
                server.open_connections += 1
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/aacp")
                    self.send_header("icy-name", "Test FM: Deep Space")
                    self.end_headers()
                    sent = 0
                    while drop_after is None or sent < drop_after:
                        self.wfile.write(server.chunk)
                        self.wfile.flush()
                        sent += 1
                        time.sleep(1.9)
                except Exception:
                    pass
                finally:
                    server.open_connections -= 1

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/stream"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def drain(src, seconds) -> int:
    """Consume frames like the mixer would (50/s) for ``seconds``; return how many came."""
    n, end = 0, time.time() + seconds
    while time.time() < end:
        if src.read() is not None:
            n += 1
        time.sleep(0.02)
    return n


@unittest.skipIf(av is None, "PyAV not installed")
class TestMeetMedia(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meet-media-")
        self.sources = []

    def tearDown(self):
        for s in self.sources:
            s.stop()

    def source(self, *a, **kw):
        s = meet_media.BackgroundSource(*a, **kw)
        self.sources.append(s)
        return s.start()

    def test_url_validation(self):
        ok = meet_media.valid_stream_url
        self.assertTrue(ok("https://ice2.somafm.com/synphaera-128-aac"))
        self.assertTrue(ok("http://radio.example.org:8000/live.mp3"))
        for bad in ("", "ftp://x/y", "file:///etc/passwd", "https://", "javascript:alert(1)",
                    "https://a b/c", "https://x/" + "a" * 600):
            self.assertFalse(ok(bad), bad)

    def test_probe_stream_and_file(self):
        srv = StreamServer()
        try:
            info = meet_media.probe(srv.url, "radio")
        finally:
            srv.close()
        self.assertEqual(info["name"], "Test FM: Deep Space")
        self.assertEqual((info["codec"], info["rate"], info["channels"]), ("aac", 44100, 2))
        video = make_video(os.path.join(self.tmp, "clip.mp4"))
        info = meet_media.probe(video, "file")
        self.assertAlmostEqual(info["duration"], 2.0, delta=0.2)

    def test_whitelists(self):
        text = os.path.join(self.tmp, "note.txt")
        with open(text, "w") as f:
            f.write("hello")
        for spec, kind in ((text, "file"),                       # not audio
                           ("file:///etc/hostname", "radio"),    # streams: http(s) only
                           ("http://127.0.0.1:9/x", "radio")):   # nothing listening
            with self.assertRaises(Exception, msg=spec):
                meet_media.probe(spec, kind)
        # a playlist file must not make ffmpeg fetch URLs or read other files
        playlist = os.path.join(self.tmp, "list.m3u8")
        with open(playlist, "w") as f:
            f.write("#EXTM3U\n#EXTINF:10,\nfile:///etc/hostname\nhttp://127.0.0.1:9/x\n")
        with self.assertRaises(Exception):
            meet_media.probe(playlist, "file")

    def test_radio_frames_and_format(self):
        srv = StreamServer()
        try:
            src = self.source(srv.url, "radio", "test")
            self.assertTrue(src.wait_started())
            n = drain(src, 2.0)
            self.assertGreater(n, 80)  # ~50 frames per second
            pcm = src.frames.get(timeout=2)
            self.assertEqual((pcm.dtype, pcm.size), (np.int16, meet_media.FRAME_SAMPLES))
        finally:
            srv.close()

    def test_radio_pauses_without_listeners_and_reconnects(self):
        srv = StreamServer()
        listeners = {"on": True}
        try:
            with patch.object(meet_media, "IDLE_CLOSE_S", 0.5):
                src = self.source(srv.url, "radio", "test", listening=lambda: listeners["on"])
                self.assertTrue(src.wait_started())
                drain(src, 1.0)
                listeners["on"] = False           # everybody left: the mixer stops reading
                deadline = time.time() + 10
                while srv.open_connections and time.time() < deadline:
                    time.sleep(0.1)
                self.assertEqual(srv.open_connections, 0, "radio kept the connection open")
                self.assertEqual(srv.connections, 1)
                listeners["on"] = True            # someone is back
                while src.read() is not None:     # old buffered frames
                    pass
                self.assertGreater(drain(src, 3.0), 30)
                self.assertEqual(srv.connections, 2)
        finally:
            srv.close()

    def test_radio_reconnects_after_drop(self):
        srv = StreamServer(drop_after=1)
        try:
            src = self.source(srv.url, "radio", "test")
            self.assertTrue(src.wait_started())
            n = drain(src, 6.0)
            self.assertGreaterEqual(srv.connections, 2)
            self.assertGreater(n, 100)
            self.assertIsNone(src.error)
        finally:
            srv.close()

    def test_radio_unreachable(self):
        ended = []
        src = self.source("http://127.0.0.1:9/x", "radio", "dead", on_end=lambda s, e: ended.append(e))
        self.assertFalse(src.wait_started())
        self.assertIn("cannot open", src.error)
        self.assertEqual(len(ended), 1)

    def test_file_plays_in_real_time_and_ends(self):
        video = make_video(os.path.join(self.tmp, "clip.mp4"), seconds=2.0)
        ended = []
        src = self.source(video, "file", "clip.mp4", on_end=lambda s, e: ended.append(e))
        self.assertTrue(src.wait_started())
        time.sleep(0.5)
        self.assertLessEqual(src.frames.qsize(), meet_media.QUEUE_FRAMES)  # waits for the mixer
        n = drain(src, 3.0)
        self.assertAlmostEqual(n, 100, delta=3)  # 2 s = 100 frames
        self.assertEqual(ended, [None])

    def test_stop_ends_quietly(self):
        video = make_video(os.path.join(self.tmp, "clip.mp4"))
        ended = []
        src = self.source(video, "file", "clip.mp4", on_end=lambda s, e: ended.append(e))
        self.assertTrue(src.wait_started())
        src.stop()
        time.sleep(1.0)
        self.assertEqual(ended, [])  # stop() never calls on_end

    def test_format_duration(self):
        self.assertEqual(meet_media.format_duration(125.4), "2:05")
        self.assertEqual(meet_media.format_duration(3725), "1:02:05")
        self.assertEqual(meet_media.format_duration(None), "")


if __name__ == "__main__":
    unittest.main()
