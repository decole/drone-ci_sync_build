"""Real-time event broadcasting service for Server-Sent Events (SSE)."""
import asyncio
import logging
from typing import Optional, Set, Dict, Any
from sqlalchemy import event

from app.models import PollLog

logger = logging.getLogger("drone_sync.broadcaster")


class LogBroadcaster:
    """Thread-safe in-memory pub-sub broadcaster for streaming PollLog events via SSE."""

    def __init__(self):
        self._subscribers: Set[asyncio.Queue] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=150)
        self._subscribers.add(q)
        logger.debug("SSE subscriber joined. Total active subscribers: %d", len(self._subscribers))
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers.discard(q)
        logger.debug("SSE subscriber left. Remaining active subscribers: %d", len(self._subscribers))

    def _dispatch_to_queue(self, q: asyncio.Queue, payload: Dict[str, Any]):
        try:
            if q.full():
                try:
                    q.get_nowait()
                except Exception:
                    pass
            q.put_nowait(payload)
        except Exception:
            self._subscribers.discard(q)

    def broadcast(self, event_type: str, data: Dict[str, Any]):
        """Deliver an event to all connected subscriber queues safely."""
        if not self._subscribers:
            return

        payload = {"event": event_type, "data": data}

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if self._loop and self._loop.is_running() and current_loop != self._loop:
            for q in list(self._subscribers):
                self._loop.call_soon_threadsafe(self._dispatch_to_queue, q, payload)
        else:
            for q in list(self._subscribers):
                self._dispatch_to_queue(q, payload)

    def publish(self, event_type: str, data: Dict[str, Any]):
        """Public alias for broadcasting an event."""
        self.broadcast(event_type, data)


log_broadcaster = LogBroadcaster()

_listeners_registered = False


def register_broadcaster_listeners():
    """Register SQLAlchemy event hooks to automatically stream created PollLogs."""
    global _listeners_registered
    if _listeners_registered:
        return
    _listeners_registered = True

    @event.listens_for(PollLog, "after_insert")
    def _on_poll_log_insert(mapper, connection, target: PollLog):
        try:
            created_str = (
                target.created_at.strftime("%Y-%m-%d %H:%M:%S")
                if target.created_at and hasattr(target.created_at, "strftime")
                else str(target.created_at or "")
            )
            payload = {
                "id": target.id,
                "repo_id": target.repo_id,
                "repo_name": target.repo_name,
                "branch_name": target.branch_name,
                "commit_sha": target.commit_sha,
                "build_number": target.build_number,
                "level": target.level,
                "message": target.message,
                "created_at": created_str
            }
            log_broadcaster.publish("log", payload)
        except Exception as e:
            logger.debug("Failed to dispatch log broadcast: %s", e)


# Auto-register on import
register_broadcaster_listeners()
