"""Ephemeral, session-bound playback provenance, never a learning evidence source."""
from collections import OrderedDict
import re
import time
from threading import RLock


def normalized_speech(text: str) -> str:
    return re.sub(r"\s+", "", text).strip("。！？!?，,；;：:、…\"'“”‘’")


class VoicePlaybackGuard:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.entries = OrderedDict()
        self.revoked_playbacks = set()
        self._lock = RLock()

    def _expire(self):
        now = self.clock()
        for key, entry in list(self.entries.items()):
            if now - entry["updated_at"] > 180:
                del self.entries[key]

    def record(self, session_id, playback_id, text, event, *, child_id=None):
        with self._lock:
            return self._record(session_id, playback_id, text, event, child_id=child_id)

    def _record(self, session_id, playback_id, text, event, *, child_id=None):
        self._expire()
        key = (session_id, playback_id)
        if key in self.revoked_playbacks:
            return {"playback_id": playback_id, "registered": False}
        existing = self.entries.get(key)
        if existing and existing["text"] != text:
            raise ValueError("Playback identifier was reused with different text")
        if event == "ended" and not existing:
            return {"playback_id": playback_id, "registered": False}
        if not existing:
            self.entries[key] = {"text": text, "updated_at": self.clock(), "ended": False, "child_id": child_id}
        self.entries[key]["updated_at"] = self.clock()
        self.entries[key]["ended"] = event == "ended" or self.entries[key]["ended"]
        self.entries.move_to_end(key)
        session_keys = [item for item in self.entries if item[0] == session_id]
        for old in session_keys[:-32]:
            del self.entries[old]
        while len(self.entries) > 128:
            self.entries.popitem(last=False)
        return {"playback_id": playback_id, "registered": True}

    def clear_child(self, child_id):
        with self._lock:
            keys = [key for key, entry in self.entries.items() if entry.get("child_id") == child_id]
            self.revoked_playbacks.update(keys)
            for key in keys:
                self.entries.pop(key, None)
            return len(keys)

    def clear_all(self):
        with self._lock:
            self.revoked_playbacks.update(self.entries)
            count = len(self.entries)
            self.entries.clear()
            return count

    def matches(self, session_id, playback_id, transcript):
        with self._lock:
            self._expire()
            entry = self.entries.get((session_id, playback_id))
            if not entry:
                return False
            heard = normalized_speech(transcript)
            return len(heard) >= 16 and heard in normalized_speech(entry["text"])
