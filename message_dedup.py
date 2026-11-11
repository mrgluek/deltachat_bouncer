"""Account-scoped deduplication of in-flight and successfully processed messages."""
from collections import OrderedDict
from contextlib import contextmanager
import threading


class MessageDedup:
    def __init__(self, limit=2000):
        self.limit = limit
        self._done = OrderedDict()
        self._inflight = set()
        self._lock = threading.Lock()

    @contextmanager
    def processing(self, accid, msgid):
        key = (accid, msgid)
        with self._lock:
            claimed = key not in self._inflight and key not in self._done
            if claimed:
                self._inflight.add(key)
        if not claimed:
            yield False
            return
        try:
            yield True
        except BaseException:
            with self._lock:
                self._inflight.discard(key)
            raise
        else:
            with self._lock:
                self._inflight.discard(key)
                self._done[key] = True
                while len(self._done) > self.limit:
                    self._done.popitem(last=False)
