"""Which shared session a room is bound to, and who bound it.

A room reaches a session only because a person who can reach that session said so, with their own
token (the bridge checks that before it writes here). The binding is not authority: every sender
in the room is still checked by the front door as themselves, so a room in a binding grants no one
access to the session.

Invariants: the file is replaced atomically, so a crash leaves the old bindings or the new, never
half; only the person who bound a room unbinds it.
"""

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Binding:
    """A room's session, and the Entra object id of the person who bound it."""

    session_id: str
    bound_by: str


class BindingStore:
    """Room -> session bindings kept in one JSON file on a mounted volume.

    One process holds it: the bridge is a single replica, because Bot Framework delivers an
    activity once and the bridge keeps no state a second replica would share.
    """

    def __init__(self, path: str) -> None:
        """Load the bindings at `path`, or start empty when the file does not exist yet."""
        self._path = Path(path)
        self._lock = threading.Lock()
        self._rooms: dict[str, Binding] = {}
        if self._path.exists():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._rooms = {room: Binding(**entry) for room, entry in raw.items()}

    def get(self, room: str) -> Binding | None:
        """The room's binding, or `None` when it is unbound."""
        return self._rooms.get(room)

    def bind(self, room: str, binding: Binding) -> None:
        """Bind `room`, replacing any earlier binding, and persist."""
        with self._lock:
            self._rooms[room] = binding
            self._write()

    def unbind(self, room: str, *, by: str) -> bool:
        """Remove the room's binding when `by` is who bound it; whether it was removed."""
        with self._lock:
            held = self._rooms.get(room)
            if held is None or held.bound_by != by:
                return False
            del self._rooms[room]
            self._write()
            return True

    def _write(self) -> None:
        """Replace the file with the current bindings, atomically."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({room: asdict(b) for room, b in self._rooms.items()}, indent=2)
        handle, temporary = tempfile.mkstemp(dir=self._path.parent, prefix=".bindings-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(payload)
            os.replace(temporary, self._path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
