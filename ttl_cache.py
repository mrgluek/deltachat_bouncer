"""Small thread-safe caches for responses with explicit expiry timestamps."""
from collections.abc import MutableMapping
import threading
import time


class TTLCache(MutableMapping):
    """Bound entries of the form (expires_at, ...), evicting the earliest expiry."""
    def __init__(self, max_entries=256):
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._entries = {}
        self._lock = threading.Lock()

    def _sweep(self, now):
        for key in list(self._entries):
            if self._entries[key][0] <= now:
                del self._entries[key]

    def __getitem__(self, key):
        with self._lock:
            value = self._entries[key]
            if value[0] <= time.time():
                del self._entries[key]
                raise KeyError(key)
            return value

    def __setitem__(self, key, value):
        with self._lock:
            now = time.time()
            self._sweep(now)
            if value[0] <= now:
                self._entries.pop(key, None)
                return
            if key not in self._entries and len(self._entries) >= self.max_entries:
                oldest = min(self._entries, key=lambda k: self._entries[k][0])
                del self._entries[oldest]
            self._entries[key] = value

    def __delitem__(self, key):
        with self._lock:
            del self._entries[key]

    def __iter__(self):
        with self._lock:
            self._sweep(time.time())
            return iter(list(self._entries))

    def __len__(self):
        with self._lock:
            self._sweep(time.time())
            return len(self._entries)

    def clear(self):
        with self._lock:
            self._entries.clear()

    def remove_prefix(self, prefix):
        """Invalidate every variant of a channel while holding the cache lock."""
        with self._lock:
            for key in list(self._entries):
                if key.startswith(prefix):
                    del self._entries[key]
