"""App-level flow control: the bounded queue above the transport.

NDJSON-stdio runtimes have no HTTP/2 credit windows, so the host needs an
explicit bounded buffer with a declared overflow policy (design doc,
flow-control section 6). Two policies:

- drop_oldest: evict oldest, count the drops (lossy, log the fact)
- pause: stop accepting until the consumer resumes (lossless)
"""

from collections import deque


class BoundedEventBuffer:
    def __init__(self, capacity, policy="drop_oldest"):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if policy not in ("drop_oldest", "pause"):
            raise ValueError(f"unknown policy: {policy!r}")
        self.capacity = capacity
        self.policy = policy
        self._queue = deque()
        self._paused = False
        self._dropped = 0

    def push(self, item):
        """Returns ("ok", 0), ("dropped", n) or ("paused", 0)."""
        if self.policy == "pause":
            if self._paused or len(self._queue) >= self.capacity:
                self._paused = True
                return "paused", 0
            self._queue.append(item)
            return "ok", 0
        if len(self._queue) >= self.capacity:
            evicted = self._queue.popleft()
            self._dropped += 1
            if evicted != item:
                self._queue.append(item)
            return "dropped", self._dropped
        self._queue.append(item)
        return "ok", 0

    def resume(self):
        """Consumer drains; explicit-resume releases a paused buffer."""
        if self._paused:
            self._paused = False
            while self._queue:
                self._queue.popleft()
            return "resumed"
        return "idle"

    def drain(self):
        items = list(self._queue)
        self._queue.clear()
        return items

    @property
    def dropped(self):
        return self._dropped

    def __len__(self):
        return len(self._queue)
