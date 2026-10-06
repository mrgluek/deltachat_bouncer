"""CPU / memory benchmark of a voice meeting (meet.MeetManager).

The bot side runs in its own process (as in production) with a fake Delta
Chat RPC; N clients in this process call into one room over localhost
WebRTC and send audio. Reports the bot process's CPU (in % of one core) and
memory with everyone talking (worst case) and with one speaker.

Run inside the bot's environment, e.g. in the container:
    python3 tests/bench_meet.py            # 8 participants, 20 s per phase
    python3 tests/bench_meet.py 4 30
Clients need CPU too: on a small machine they compete with the bot, so the
numbers are a lower bound there.
"""
import multiprocessing as mp
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------
# bot side (child process)
# --------------------------------------------------------------------------


class _Ev(dict):
    __getattr__ = dict.__getitem__


def bot_side(conn, db_dir):
    os.environ["DB_PATH"] = os.path.join(db_dir, "bench.db")
    sys.path.insert(0, HERE)
    from unittest.mock import MagicMock

    import database
    import meet

    database.DB_PATH = os.environ["DB_PATH"]
    database.init_db()
    database.set_config("meets_enabled", "1")
    bot = MagicMock()
    bot.rpc.ice_servers.return_value = "[]"
    bot.rpc.accept_incoming_call.side_effect = lambda accid, msg_id, sdp: conn.send(("answer", msg_id, sdp))
    m = meet.MeetManager(bot)
    room = m.create_room(owner=1)
    while True:
        cmd = conn.recv()
        if cmd[0] == "offer":
            _, cid, msg_id, sdp = cmd
            m.contact_room[cid] = room.id
            ev = _Ev(kind="IncomingCall", msg_id=msg_id, chat_id=100 + cid, place_call_info=sdp)
            threading.Thread(target=m.on_incoming_call, args=(1, ev, cid), daemon=True).start()
        elif cmd[0] == "connected":
            conn.send(("connected", sum(1 for p in room.participants.values() if p.connected)))
        elif cmd[0] == "measure":
            secs = cmd[1]
            t0, c0 = time.time(), time.process_time()
            time.sleep(secs)
            cpu = (time.process_time() - c0) / (time.time() - t0) * 100
            rss = 0
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss = int(line.split()[1]) // 1024
            conn.send(("measure", cpu, rss))
        elif cmd[0] == "stop":
            room.closed = True
            conn.send(("stopped",))
            return


# --------------------------------------------------------------------------
# clients (this process)
# --------------------------------------------------------------------------


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    phase = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
    sys.path.insert(0, HERE)
    import numpy as np
    from cmcall import rtc

    class Voice(rtc.PacedAudioTrack):
        """Speech-ish: a warbling tone, loud enough to pass the noise gate."""

        def __init__(self, base):
            super().__init__()
            self.base, self.talking, self.t = base, False, 0

        async def recv(self):
            await self.pace()
            if not self.talking:
                return self.stamp(rtc.silence_frame())
            t = (np.arange(rtc.FRAME_SAMPLES) + self.t) / rtc.SAMPLE_RATE
            self.t += rtc.FRAME_SAMPLES
            f = self.base * (1 + 0.1 * np.sin(2 * np.pi * 3 * t))
            pcm = np.sin(2 * np.pi * f * t) * 6000
            return self.stamp(rtc.frame_from_pcm(pcm))

    heard = [0] * n

    def ear(i):
        def on_audio(frame):
            if rtc.rms(rtc.frame_mono(frame)) > 500:
                heard[i] += 1
        return on_audio

    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe()
    db_dir = tempfile.mkdtemp(prefix="bench-meet-")
    proc = ctx.Process(target=bot_side, args=(child, db_dir), daemon=True)
    proc.start()
    loop = rtc.CallLoop("bench-clients")
    voices = [Voice(200 + 60 * i) for i in range(n)]
    peers = [rtc.EchoPeer([], greeting=False, out_track=voices[i], on_audio=ear(i)) for i in range(n)]
    try:
        for i, peer in enumerate(peers):
            offer = loop.run(peer.offer(), timeout=30)
            parent.send(("offer", 10 + i, 1000 + i, offer))
            kind, msg_id, answer = parent.recv()
            loop.run(peer.accept_answer(answer), timeout=10)
            assert loop.run(peer.wait_connected(20), timeout=25), f"client {i} did not connect"
        time.sleep(2)
        parent.send(("connected",))
        print(f"{parent.recv()[1]}/{n} participants connected (bot pid {proc.pid}); {os.cpu_count()} CPUs; "
              f"{phase:.0f} s per phase")

        def measure(label):
            c0, t0 = time.process_time(), time.time()
            parent.send(("measure", phase))
            _, cpu, rss = parent.recv()
            clients = (time.process_time() - c0) / (time.time() - t0) * 100
            print(f"{label:<22} bot CPU {cpu:5.1f}% of one core, RSS {rss} MB   (clients {clients:.0f}%)")

        measure("silence")
        voices[0].talking = True
        time.sleep(1)
        before = list(heard)
        measure("1 speaker")
        got = [h - b for h, b in zip(heard, before)]
        print(f"{'':<22} listeners heard {min(got[1:]) if n > 1 else 0}+ voice frames, speaker {got[0]}")
        for v in voices:
            v.talking = True
        time.sleep(1)
        measure(f"all {n} speaking")
    finally:
        parent.send(("stop",))
        for peer in peers:
            loop.run(peer.close(), timeout=10)
        loop.stop()
        proc.join(5)


if __name__ == "__main__":
    main()
