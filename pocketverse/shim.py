"""In-sandbox TCP -> unix-socket relay.

Ownership: Lane 2 (@fixer). Implement to spec.

Runs INSIDE the bwrap sandbox (which has --unshare-net, i.e. loopback
only). Listens on 127.0.0.1:PORT and forwards every accepted connection
byte-for-byte to the unix socket bind-mounted from the host (where
proxy.py enforces the domain allowlist).

This exists because HTTP clients generally cannot speak to proxies over
unix sockets; with this relay, plain `HTTPS_PROXY=http://127.0.0.1:PORT`
works inside the sandbox.

CLI (executed as `python3 -m pocketverse.shim`):
    --port INT    local listen port (required)
    --sock PATH   unix socket path to forward to (required)
    --bind ADDR   listen address, default 127.0.0.1

Requirements: stdlib asyncio only; bidirectional copy; half-close the
upstream when the client half-closes; die cleanly (exit code 2) if the
socket path does not exist at startup (retry for up to 5 s first, the
proxy may still be starting).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

_log = logging.getLogger(__name__)


async def _relay_one_way(
    src_reader: asyncio.StreamReader, dst_writer: asyncio.StreamWriter
) -> None:
    """Read from *src_reader* and write to *dst_writer* until EOF.

    On EOF (or error) attempt to half-close *dst_writer* via *write_eof*.
    """
    try:
        while True:
            data = await src_reader.read(65536)
            if not data:
                break
            dst_writer.write(data)
            await dst_writer.drain()
    finally:
        try:
            dst_writer.write_eof()
        except (AttributeError, OSError):
            pass


async def _bidirectional_relay(
    reader_a: asyncio.StreamReader,
    writer_a: asyncio.StreamWriter,
    reader_b: asyncio.StreamReader,
    writer_b: asyncio.StreamWriter,
) -> None:
    """Relay bytes in both directions until both sides close.

    Half-close signals are propagated: when one side sends EOF the
    other side receives a write_eof (SHUT_WR).
    """
    t1 = asyncio.create_task(_relay_one_way(reader_a, writer_b))
    t2 = asyncio.create_task(_relay_one_way(reader_b, writer_a))

    await asyncio.gather(t1, t2, return_exceptions=True)

    for w in (writer_a, writer_b):
        try:
            if not w.is_closing():
                w.close()
        except OSError:
            pass


async def _handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    sock_path: str,
) -> None:
    """Forward one TCP connection to the unix socket."""
    try:
        unix_reader, unix_writer = await asyncio.open_unix_connection(sock_path)
    except OSError as exc:
        _log.warning("cannot connect to proxy socket %s: %s", sock_path, exc)
        client_writer.close()
        return

    await _bidirectional_relay(
        client_reader, client_writer,
        unix_reader, unix_writer,
    )


async def _wait_for_socket(sock_path: str, timeout: float = 5.0) -> bool:
    """Return True if *sock_path* exists and is connectable within timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(sock_path):
            try:
                r, w = await asyncio.open_unix_connection(sock_path)
                w.close()
                return True
            except OSError:
                pass
        await asyncio.sleep(0.2)
    return False


async def _serve(port: int, sock_path: str, bind: str) -> None:
    if not await _wait_for_socket(sock_path):
        _log.error("proxy socket %s not available after 5 s; exiting", sock_path)
        sys.exit(2)

    server = await asyncio.start_server(
        lambda r, w: _handle_client(r, w, sock_path),
        host=bind,
        port=port,
    )

    _log.info("shim listening on %s:%d -> %s", bind, port, sock_path)

    async with server:
        await server.serve_forever()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PocketVerse in-sandbox TCP->unix relay")
    p.add_argument("--port", type=int, required=True, help="Local TCP port")
    p.add_argument("--sock", required=True, help="UNIX socket path to forward to")
    p.add_argument(
        "--bind",
        default="127.0.0.1",
        help="Listen address (default 127.0.0.1)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    asyncio.run(_serve(args.port, args.sock, args.bind))


if __name__ == "__main__":
    main()
