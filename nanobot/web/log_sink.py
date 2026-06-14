"""Log sink with ring buffer for real-time console streaming."""

import threading
import time
from collections import deque
from typing import Any


class LogBuffer:
    """Thread-safe ring buffer for log entries with sequential IDs."""

    def __init__(self, maxlen: int = 500):
        self._buffer: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._counter = 0
        self._lock = threading.Lock()

    def append(self, entry: dict[str, Any]) -> None:
        """Add a log entry with auto-incrementing ID."""
        with self._lock:
            self._counter += 1
            entry["id"] = self._counter
            self._buffer.append(entry)

    def get_since(self, last_id: int = 0) -> list[dict[str, Any]]:
        """Return entries newer than last_id."""
        with self._lock:
            return [e for e in self._buffer if e["id"] > last_id]

    def get_all(self) -> list[dict[str, Any]]:
        """Return all buffered entries."""
        with self._lock:
            return list(self._buffer)

    @property
    def latest_id(self) -> int:
        """Return the latest entry ID."""
        with self._lock:
            return self._counter


# Global singleton buffer
log_buffer = LogBuffer(maxlen=500)

# Exec output buffer (separate from system logs)
exec_buffer = LogBuffer(maxlen=200)


def buffer_sink(message: Any) -> None:
    """Loguru custom sink — parse message record and append to buffer."""
    record = message.record
    log_buffer.append({
        "timestamp": record["time"].strftime("%H:%M:%S"),
        "level": record["level"].name,
        "module": record["name"] or "",
        "message": record["message"],
        "ts": time.time(),
    })


def exec_sink(command: str, stdout: str, stderr: str, exit_code: int, duration: float) -> None:
    """Capture exec tool output into the exec buffer."""
    exec_buffer.append({
        "timestamp": time.strftime("%H:%M:%S"),
        "command": command,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "duration": round(duration, 2),
        "ts": time.time(),
    })
