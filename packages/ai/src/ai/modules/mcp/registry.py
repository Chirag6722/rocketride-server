# Copyright 2026 Aparavi Software AG. MIT License.
"""Server-owned task registry.

The RocketRide SDK has no client-side task registry: ``use()`` returns a
bare task token, and enumerate/terminate/monitor across separate tool calls
need somewhere to keep ``{token -> metadata}``. This is a plain in-memory
dict, scoped to a single asyncio event loop (one process, one persistent
``RocketRideClient``) — it is NOT thread-safe and must not be shared across
event loops or accessed concurrently from multiple threads.
"""

from typing import Any, Dict, List, Optional


class TaskRegistry:
    """In-memory ``{token -> metadata}`` registry.

    Single-event-loop use only; not thread-safe.
    """

    def __init__(self) -> None:
        self._tasks: Dict[str, Dict[str, Any]] = {}

    def add(self, token: str, **metadata: Any) -> None:
        """Register ``token`` with the given metadata, replacing any prior entry."""
        self._tasks[token] = dict(metadata)

    def remove(self, token: str) -> None:
        """Drop ``token`` from the registry. A no-op if it is not present."""
        self._tasks.pop(token, None)

    def get(self, token: str) -> Optional[Dict[str, Any]]:
        """Return ``{'token': token, **metadata}`` for ``token``, or ``None``."""
        metadata = self._tasks.get(token)
        if metadata is None:
            return None
        return {'token': token, **metadata}

    def list(self) -> List[Dict[str, Any]]:
        """Return ``[{'token': token, **metadata}, ...]`` for every registered task."""
        return [{'token': token, **metadata} for token, metadata in self._tasks.items()]
