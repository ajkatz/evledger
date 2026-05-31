"""Machine-id resolution for the ledger partition key.

A *machine id* identifies which machine produced an event; it is the
partition key under which a machine appends its events (see Decision 2 of the
whim). It must be stable across processes and reboots on a given machine.

Resolution order (:func:`resolve_machine_id`):

1. The ``CLAUDE_MACHINE_ID`` environment variable, if set and non-blank.
2. An id persisted in ``id_file``; if the file is absent, a fresh uuid4 hex
   is generated, written there, and returned (so subsequent calls are
   stable).
3. The host name, as a last-resort fallback (also used if the id file cannot
   be created/read).

Every input is a parameter with a generic default, so an adopter can point
the resolver at their own environment variable name (via ``env``) or id-file
location. The default ``id_file`` is ``~/.claude/machine-id`` purely as a
convenience for this repo's first consumer; nothing in the logic assumes that
path exists or is writable.

Stdlib-only; imports nothing from sibling ``claude_kg`` modules.
"""

from __future__ import annotations

import os
import socket
import uuid
from pathlib import Path

#: Environment variable consulted first, by default.
DEFAULT_ENV_VAR = "CLAUDE_MACHINE_ID"


def default_id_file() -> Path:
    """Return the default machine-id file location (``~/.claude/machine-id``).

    Resolved lazily so that importing this module never touches the home
    directory, and so adopters can override it by passing ``id_file`` to
    :func:`resolve_machine_id`.
    """
    return Path.home() / ".claude" / "machine-id"


def resolve_machine_id(
    env: dict[str, str] | None = None,
    id_file: Path | None = None,
    hostname: str | None = None,
    env_var: str = DEFAULT_ENV_VAR,
) -> str:
    """Resolve the machine id, creating and persisting one if needed.

    Args:
        env: Environment mapping to read ``env_var`` from. Defaults to
            ``os.environ``.
        id_file: Where a generated uuid4 id is persisted. Defaults to
            :func:`default_id_file`.
        hostname: Final fallback. Defaults to ``socket.gethostname()``.
        env_var: Name of the environment variable to consult first.

    Returns:
        A non-empty machine-id string.
    """
    if env is None:
        env = dict(os.environ)
    id_file = default_id_file() if id_file is None else id_file
    hostname = socket.gethostname() if hostname is None else hostname

    # 1. Environment variable wins.
    raw = env.get(env_var)
    if raw is not None and raw.strip():
        return raw.strip()

    # 2. Persisted id file (read existing, else generate + persist).
    try:
        if id_file.exists():
            existing = id_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        generated = uuid.uuid4().hex
        id_file.parent.mkdir(parents=True, exist_ok=True)
        id_file.write_text(generated + "\n", encoding="utf-8")
        return generated
    except OSError:
        # Unwritable/unreadable id file -> fall through to hostname.
        pass

    # 3. Hostname fallback.
    return hostname
