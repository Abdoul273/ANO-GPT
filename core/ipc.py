"""
Control socket for ANO-GPT.

Wayland compositors (Hyprland) deliberately give no client global access to the
keyboard, so X11-style global hotkey libraries (pynput, keyboard) cannot work.
The portable answer is to let the compositor own the hotkey and have it poke the
running app: Hyprland binds a key to `anogpt-ctl <command>`, which writes one
line to this Unix domain socket.

Protocol: one UTF-8 line per connection, `<command>[ <argument>]`, answered with
a single line. Unknown commands answer `ERR unknown command: ...` so the CLI can
report the problem instead of failing silently.

The socket lives in $XDG_RUNTIME_DIR (per-user, 0700, cleared on logout), so it
is not reachable by other users on the machine.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("anogpt.ipc")


def socket_path() -> Path:
    """Per-user control socket path (honours XDG_RUNTIME_DIR when available)."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        return Path(runtime) / "anogpt.sock"
    return Path(f"/tmp/anogpt-{os.getuid()}.sock")


class ControlServer:
    """
    Asyncio Unix-socket server exposing assistant control commands.

    Handlers are registered as `name -> callable`. A handler may be sync or
    async and may return a string, which is sent back as the reply.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[str], object]] = {}
        self._server: Optional[asyncio.AbstractServer] = None
        self._path = socket_path()

    def register(self, name: str, handler: Callable[[str], object]) -> None:
        self._handlers[name] = handler

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                return

            cmd, _, arg = line.partition(" ")
            cmd = cmd.strip().lower()
            arg = arg.strip()

            handler = self._handlers.get(cmd)
            if handler is None:
                known = ", ".join(sorted(self._handlers)) or "none"
                reply = f"ERR unknown command: {cmd} (known: {known})"
            else:
                try:
                    result = handler(arg)
                    if asyncio.iscoroutine(result):
                        result = await result
                    reply = f"OK {result}" if result else "OK"
                except Exception as e:                       # handler bug
                    logger.exception("IPC handler %r failed", cmd)
                    reply = f"ERR {type(e).__name__}: {e}"

            writer.write((reply + "\n").encode("utf-8"))
            await writer.drain()
        except asyncio.TimeoutError:
            pass
        except Exception:
            logger.exception("IPC client error")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def start(self) -> None:
        # A stale socket file survives a crash/SIGKILL and would block bind().
        # Only unlink it if nothing is actually listening, so we never kill a
        # second, genuinely-running instance's socket.
        if self._path.exists():
            if await self._is_live():
                raise RuntimeError(
                    f"another ANO-GPT instance is already listening on {self._path}"
                )
            self._path.unlink(missing_ok=True)

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._path)
        )
        os.chmod(self._path, 0o600)
        logger.info("Control socket listening on %s", self._path)
        print(f"[ANO-GPT] 🎛️  Control socket: {self._path}")

    async def _is_live(self) -> bool:
        """True if some process is actually accepting connections on the socket."""
        try:
            _, w = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self._path)), timeout=0.5
            )
            w.close()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        self._path.unlink(missing_ok=True)


def send_command(line: str, timeout: float = 3.0) -> str:
    """
    Blocking client used by the `anogpt-ctl` CLI. Returns the reply line,
    or raises ConnectionError if the assistant is not running.
    """
    import socket

    path = socket_path()
    if not path.exists():
        raise ConnectionError(f"ANO-GPT is not running (no socket at {path})")

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect(str(path))
        except (ConnectionRefusedError, FileNotFoundError) as e:
            raise ConnectionError(f"ANO-GPT is not running ({e})") from e
        s.sendall((line.strip() + "\n").encode("utf-8"))
        chunks = []
        while True:
            try:
                buf = s.recv(4096)
            except socket.timeout:
                break
            if not buf:
                break
            chunks.append(buf)
            if b"\n" in buf:
                break
    return b"".join(chunks).decode("utf-8", errors="replace").strip()
