"""One running panel per user: later launches hand their command to it and exit.

The instance holds an abstract Unix socket. Binding it is the lock: only one process can, and the
kernel releases it when that process ends, so a crash leaves nothing stale behind. Connections are
queued from the moment of the bind, so a command sent while the instance is still loading Qt is
handled once its event loop runs.
"""

import errno
import logging
import os
import socket
import struct

logger = logging.getLogger(__name__)

# The leading NUL puts the name in the abstract namespace, outside the file system.
ADDRESS = f"\0screenguide-{os.getuid()}".encode()
COMMANDS = ("show", "toggle", "next")
MAX_COMMAND_BYTES = 64
TIMEOUT_SECONDS = 2.0
# The instance can end between a failed bind and the connect; then the bind is tried again.
CLAIM_ATTEMPTS = 3
_UCRED = struct.Struct("3i")


class InstanceError(RuntimeError):
    """The running instance could not be reached or claimed. The message is written for the user."""


def claim_or_send(command: str) -> socket.socket | None:
    """Become the instance and return its listening socket, or send `command` to the one running."""
    if command not in COMMANDS:
        raise ValueError(f"unknown command {command!r}")
    for _ in range(CLAIM_ATTEMPTS):
        listener = _claim()
        if listener is not None:
            return listener
        try:
            _send(command)
            logger.info("instance command sent command=%s", command)
            return None
        except ConnectionRefusedError:
            continue
        except OSError as exc:
            raise InstanceError(f"cannot reach the running screenguide: {exc}") from exc
    raise InstanceError("cannot start screenguide or reach the running one; try again")


def forward(command: str) -> bool:
    """Send `command` to the running instance, if one answers; `claim_or_send` handles the rest."""
    try:
        _send(command)
    except (OSError, InstanceError):
        return False
    logger.info("instance command sent command=%s", command)
    return True


def receive(listener: socket.socket) -> list[str]:
    """Commands from every connection waiting on `listener`, which must be non-blocking."""
    commands = []
    while True:
        try:
            connection, _ = listener.accept()
        except BlockingIOError:
            return commands
        with connection:
            command = _read_command(connection)
        if command is not None:
            commands.append(command)


def _claim() -> socket.socket | None:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(ADDRESS)
    except OSError as exc:
        listener.close()
        if exc.errno == errno.EADDRINUSE:
            return None
        raise InstanceError(f"cannot start the screenguide instance: {exc}") from exc
    listener.listen()
    listener.setblocking(False)
    logger.info("instance claimed pid=%s", os.getpid())
    return listener


def _send(command: str) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(TIMEOUT_SECONDS)
        client.connect(ADDRESS)
        # Abstract names have no file permissions, so the peer's user is what keeps others out.
        if _peer_uid(client) != os.getuid():
            raise InstanceError("the screenguide socket belongs to another user")
        client.sendall(command.encode() + b"\n")


def _read_command(connection: socket.socket) -> str | None:
    if _peer_uid(connection) != os.getuid():
        logger.warning("instance command refused: another user")
        return None
    # A peer that connects and sends nothing must not hold up the panel for long.
    connection.settimeout(TIMEOUT_SECONDS)
    data = b""
    try:
        while b"\n" not in data and len(data) <= MAX_COMMAND_BYTES:
            chunk = connection.recv(MAX_COMMAND_BYTES)
            if not chunk:
                break
            data += chunk
    except OSError as exc:
        logger.warning("instance command lost: %s", exc)
        return None
    command = data.split(b"\n", 1)[0].decode(errors="replace")
    if command not in COMMANDS:
        logger.warning("instance command unknown: %r", command[:MAX_COMMAND_BYTES])
        return None
    return command


def _peer_uid(connection: socket.socket) -> int:
    _, uid, _ = _UCRED.unpack(
        connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
    )
    return uid
