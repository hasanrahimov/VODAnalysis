"""
Killfeed deduplication.

A kill entry lingers on screen for several seconds, so the same (killer,
victim) pair appears across multiple ~5s-apart frames. This emits each unique
kill only once within a time window.
"""

from typing import List

import config
from killfeed_reader import KillEvent


class KillfeedDeduper:
    def __init__(self, window_sec: int = None):
        self.window = window_sec if window_sec is not None else config.DEDUP_WINDOW_SEC
        # key -> last time (sec) we emitted it
        self._seen: dict = {}

    @staticmethod
    def _key(ev: KillEvent) -> tuple:
        # Normalize for stable comparison
        return (ev.killer.lower().strip(), ev.victim.lower().strip())

    def filter(self, events: List[KillEvent], frame_time_sec: float) -> List[KillEvent]:
        """Return only events not seen within the dedup window."""
        out: List[KillEvent] = []
        for ev in events:
            if not ev.killer and not ev.victim:
                continue
            key = self._key(ev)
            last = self._seen.get(key)
            if last is None or (frame_time_sec - last) > self.window:
                out.append(ev)
            # Refresh timestamp so a long-lingering entry keeps suppressing
            self._seen[key] = frame_time_sec
        return out

    def reset(self):
        """Clear memory — call at the start of each new round."""
        self._seen.clear()
