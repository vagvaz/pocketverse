"""In-sandbox unix-socket -> TCP relay (the reverse of shim.py).

Ownership: Lane 1 (@fixer). Implement to spec.

Runs INSIDE the bwrap sandbox. Listens on a unix socket path and forwards
every accepted connection byte-for-byte to 127.0.0.1:TARGET inside the
sandbox (where the agent's dev server listens). The unix socket is
bind-mounted from the host, where a host-side shim listens on
127.0.0.1:HOSTPORT and forwards into this socket — so
``curl http://127.0.0.1:HOSTPORT`` on the host reaches the agent's server.

Because unix sockets are not network-namespace scoped, this pair works in
EVERY network mode (full/allowlist/none) — same trick as the proxy.

CLI (executed as ``python3 -m pocketverse.unishim``):
    --sock PATH    unix socket path to listen on (required)
    --target INT   sandbox TCP port to forward to (required)
    --host ADDR    target address, default 127.0.0.1

Requirements: stdlib asyncio only; bidirectional copy; half-close the
upstream when the client half-closes; mkdir -p the socket parent and unlink
any stale socket before binding.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

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
    unix_reader: asyncio.StreamReader,
    unix_writer: asyncio.StreamWriter,
    host: str,
    target: int,
) -> None:
    """Forward one unix-socket connection to the TCP target inside."""
    try:
        tcp_reader, tcp_writer = await asyncio.open_connection(host, target)
    except OSError as exc:
        _log.warning("cannot connect to %s:%d: %s", host, target, exc)
        unix_writer.close()
        return

    await _bidirectional_relay(
        unix_reader, unix_writer,
        tcp_reader, tcp_writer,
    )


async def _serve(sock_path: str, target: int, host: str) -> None:
    # Ensure the parent dir exists (the bind target /run/pocketverse is
    # created by bwrap; other paths may need the full tree).
    parent = os.path.dirname(sock_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # Unlink a stale socket left by a crashed previous session.  A unix
    # socket path that already exists makes bind() fail with EADDRINUSE.
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass

    server = await asyncio.start_unix_server(
        lambda r, w: _handle_client(r, w, host, target),
        path=sock_path,
    )

    _log.info("unishim listening on %s -> %s:%d", sock_path, host, target)

    async with server:
        await server.serve_forever()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PocketVerse in-sandbox unix->TCP relay (reverse of shim)",
    )
    p.add_argument("--sock", required=True, help="UNIX socket path to listen on")
    p.add_argument("--target", type=int, required=True, help="TCP target port")
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Target address (default 127.0.0.1)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    asyncio.run(_serve(args.sock, args.target, args.host))


if __name__ == "__main__":
    main()
