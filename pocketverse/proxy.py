"""Host-side domain-allowlist forward proxy.

Ownership: Lane 2 (@fixer). Implement to spec. Do not edit models.py.

Runs ON THE HOST, listening on a unix domain socket inside the session
sock dir. The sandbox (bwrap --unshare-net) has no other egress: the
socket is bind-mounted in, and an in-sandbox relay (shim.py) exposes it
as a local TCP port so ordinary HTTP_PROXY/HTTPS_PROXY work.

Supported protocol (both on the same connection-oriented socket):
  1. CONNECT host:port HTTP/1.1   -> tunnel. Peek at the first line to
     decide. On allow: open TCP to host:port FROM THE HOST, reply
     'HTTP/1.1 200 Connection Established' then bidirectionally relay.
     On deny: reply 'HTTP/1.1 403 Forbidden' and close.
  2. Plain HTTP proxy requests (absolute-URI form, e.g.
     'GET http://example.com/x HTTP/1.1'): parse request line + Host
     header, enforce allowlist, forward the request bytes to the upstream
     (rewriting to origin-form is NOT required; forward as-is), then relay
     the response back. One request per connection is acceptable if
     'Connection: close' is sent upstream; keep-alive is a nice-to-have.

Allowlist semantics (see models.NetworkConfig):
  - rule 'example.com'        -> host == 'example.com' (case-insensitive)
  - rule '*.example.com'      -> host is a strict subdomain of example.com
  - rule 'example.com:8443'   -> as above but only that port
  - rules without a port      -> port must be in allow_ports
  - host may be a literal IPv4/IPv6 address; those match only exact rules.

Enforcement notes:
  - Compare hostnames lowercased, strip trailing dot.
  - NEVER resolve-then-compare; matching is purely lexical on the CONNECT
    target (DNS happens on the host via the upstream connect).

CLI (module is executed as `python3 -m pocketverse.proxy`):
    --sock PATH          unix socket to listen on (required)
    --allow RULE         repeatable allowlist rule
    --allow-port PORT    repeatable upstream port (default 443, 80)
    --log PATH           append decision log lines:
                         '<iso8601> ALLOW|DENY <host>:<port> (<rule or "no-match">)'
                         plus one line per accepted upstream connection.

Implementation requirements:
  - asyncio only (stdlib), no third-party deps.
  - Robust against malformed requests, early disconnects, huge header
    blocks (cap at 64 KiB).
  - Concurrent connections welcome; no global locks needed.
  - Log DENY with reason; never crash the listener on a bad connection.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

_log = logging.getLogger(__name__)


def match_rule(host: str, port: int, rules: list[str], allow_ports: list[int]) -> str | None:
    """Return the matching rule string, or None if (host, port) is not allowed."""
    host = host.lower().rstrip(".")

    for rule in rules:
        rule_lower = rule.lower().rstrip(".")
        rule_host = rule_lower
        rule_port: int | None = None

        # --- extract optional :port from the rule ---
        # Rules with a single colon are "host:port".
        # Rules with multiple colons may be IPv6; detect "]:port" suffix.
        if ":" in rule_lower:
            if rule_lower.count(":") == 1 or (rule_lower.count(":") > 1 and "]:" in rule_lower):
                candidate_host, _, port_str = rule_lower.rpartition(":")
                if port_str.isdigit():
                    rule_port = int(port_str)
                    rule_host = candidate_host

        # Strip IPv6 brackets for comparison.
        if rule_host.startswith("[") and rule_host.endswith("]"):
            rule_host = rule_host[1:-1]

        # --- port check ---
        if rule_port is not None:
            if port != rule_port:
                continue
        elif port not in allow_ports:
            continue

        # --- host matching ---
        if rule_host.startswith("*."):
            suffix = rule_host[2:]
            # Strict subdomain: host must end with .suffix and not equal suffix.
            if host.endswith("." + suffix) and host != suffix:
                return rule
        elif host == rule_host:
            return rule

    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_MAX_HEADER = 64 * 1024  # 64 KiB


def _parse_host_port(target: str) -> tuple[str, int] | None:
    """Parse 'host:port' or '[ipv6]:port' from a CONNECT target string.

    Returns (host, port) on success, or None on failure.
    """
    target = target.strip()
    if not target:
        return None
    if target.startswith("["):
        # [::1]:port
        if "]" not in target:
            return None
        host_raw, _, rest = target.partition("]")
        host = host_raw[1:]  # strip leading [
        rest = rest.lstrip(":")
        if not rest.isdigit():
            return None
        return host, int(rest)
    else:
        if ":" not in target:
            return None
        host, _, port_str = target.rpartition(":")
        if not port_str.isdigit():
            return None
        return host, int(port_str)


def _parse_absolute_uri(uri: str) -> tuple[str, int, str] | None:
    """Parse an http:// or https:// absolute URI.

    Returns (host, port, path) on success, or None on failure.
    """
    uri = uri.strip()
    if uri.lower().startswith("http://"):
        default_port = 80
    elif uri.lower().startswith("https://"):
        default_port = 443
    else:
        return None

    scheme_rest = uri.split("://", 1)[1]
    if "/" not in scheme_rest:
        host_part = scheme_rest
        path = "/"
    else:
        host_part, _, path = scheme_rest.partition("/")
        path = "/" + path

    # Extract host and optional port from the authority part.
    host = host_part
    port = default_port
    if host_part.startswith("["):
        # IPv6 [::1]:port
        if "]" not in host_part:
            return None
        close = host_part.index("]")
        host = host_part[1:close]
        rest = host_part[close + 1 :]
        if rest.startswith(":"):
            try:
                port = int(rest[1:])
            except ValueError:
                return None
    elif ":" in host_part:
        h, _, p = host_part.rpartition(":")
        try:
            port = int(p)
        except ValueError:
            return None
        host = h

    return host, port, path


async def _read_full_headers(reader: asyncio.StreamReader) -> bytes | None:
    """Read up to _MAX_HEADER bytes until the end of HTTP headers (\\r\\n\\r\\n).

    Returns the header block (including the terminator) on success,
    or None if the connection was closed before headers completed.
    """
    data = b""
    while len(data) < _MAX_HEADER:
        chunk = await reader.read(_MAX_HEADER - len(data))
        if not chunk:
            return None if not data else data
        data += chunk
        if b"\r\n\r\n" in data or data.endswith(b"\n\n"):
            break
    return data


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


# ---------------------------------------------------------------------------
# Connection handlers
# ---------------------------------------------------------------------------

_DENY_RESPONSE = (
    b"HTTP/1.1 403 Forbidden\r\n"
    b"Content-Length: 9\r\n"
    b"Connection: close\r\n"
    b"\r\n"
    b"Forbidden"
)


async def _handle_connect(
    target: str,
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    rules: list[str],
    allow_ports: list[int],
    log_writer: "_LogWriter | None",
) -> None:
    hp = _parse_host_port(target)
    if hp is None:
        client_writer.write(_DENY_RESPONSE)
        await client_writer.drain()
        return

    host, port = hp
    rule = match_rule(host, port, rules, allow_ports)

    if rule is None:
        _log_decision(log_writer, host, port, "DENY", "no-match")
        client_writer.write(_DENY_RESPONSE)
        await client_writer.drain()
        return

    _log_decision(log_writer, host, port, "ALLOW", rule)

    try:
        remote_reader, remote_writer = await asyncio.open_connection(host, port)
    except OSError as exc:
        _log_decision(log_writer, host, port, "DENY", f"connect-error:{exc}")
        client_writer.write(_DENY_RESPONSE)
        await client_writer.drain()
        return

    # Inform the client the tunnel is open.
    client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await client_writer.drain()

    await _bidirectional_relay(client_reader, client_writer, remote_reader, remote_writer)


async def _handle_http_proxy(
    data: bytes,
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    rules: list[str],
    allow_ports: list[int],
    log_writer: "_LogWriter | None",
) -> None:
    """Handle an absolute-URI plain HTTP proxy request.

    *data* is the initial bytes already read (must contain at least the
    request line).  Any remaining request data is relayed after the headers.
    """
    # Extract the request line (first line).
    first_line_end = data.find(b"\n")
    if first_line_end == -1:
        client_writer.write(_DENY_RESPONSE)
        await client_writer.drain()
        return

    request_line = data[:first_line_end].decode("utf-8", errors="replace").rstrip("\r\n")

    parts = request_line.split(None, 2)
    if len(parts) < 3:
        client_writer.write(_DENY_RESPONSE)
        await client_writer.drain()
        return

    _method, uri, _version = parts

    parsed = _parse_absolute_uri(uri)
    if parsed is None:
        client_writer.write(_DENY_RESPONSE)
        await client_writer.drain()
        return

    host, port, _path = parsed

    rule = match_rule(host, port, rules, allow_ports)

    if rule is None:
        _log_decision(log_writer, host, port, "DENY", "no-match")
        client_writer.write(_DENY_RESPONSE)
        await client_writer.drain()
        return

    _log_decision(log_writer, host, port, "ALLOW", rule)

    try:
        remote_reader, remote_writer = await asyncio.open_connection(host, port)
    except OSError as exc:
        _log_decision(log_writer, host, port, "DENY", f"connect-error:{exc}")
        client_writer.write(_DENY_RESPONSE)
        await client_writer.drain()
        return

    # Forward the request bytes (as-is, including absolute URI).
    remote_writer.write(data)
    await remote_writer.drain()

    # Relay the response back.
    await _bidirectional_relay(client_reader, client_writer, remote_reader, remote_writer)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log_decision(
    log_writer: "_LogWriter | None",
    host: str,
    port: int,
    decision: str,
    rule: str,
) -> None:
    if log_writer is not None:
        log_writer.write(host, port, decision, rule)


class _LogWriter:
    """Append-only decision log writer."""

    def __init__(self, path: str) -> None:
        self._path = path

    def write(self, host: str, port: int, decision: str, rule: str) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        line = f"{ts} {decision} {host}:{port} ({rule})\n"
        try:
            with open(self._path, "a") as f:
                f.write(line)
        except OSError as exc:
            _log.warning("failed to write log: %s", exc)


# ---------------------------------------------------------------------------
# Client handler (one per connection)
# ---------------------------------------------------------------------------


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    rules: list[str],
    allow_ports: list[int],
    log_writer: _LogWriter | None,
) -> None:
    """Handle one client connection on the proxy unix socket."""
    try:
        peername = writer.get_extra_info("peername")
    except OSError:
        peername = None

    try:
        data = await reader.read(_MAX_HEADER)
    except (OSError, asyncio.IncompleteReadError) as exc:
        _log.debug("read error from %s: %s", peername, exc)
        writer.close()
        return

    if not data:
        writer.close()
        return

    # First line ends at the first \n.
    first_line_end = data.find(b"\n")
    if first_line_end == -1:
        # No complete line received — malformed or too large.
        writer.write(_DENY_RESPONSE)
        await writer.drain()
        writer.close()
        return

    first_line = data[:first_line_end].decode("utf-8", errors="replace").rstrip("\r\n")

    # If headers are incomplete try to read the rest (up to _MAX_HEADER total).
    if not (data.endswith(b"\r\n\r\n") or data.endswith(b"\n\n")):
        try:
            more = await reader.read(_MAX_HEADER - len(data))
            data += more
        except (OSError, asyncio.IncompleteReadError):
            pass

    # Dispatch based on the request method.
    if first_line.startswith("CONNECT "):
        # CONNECT host:port HTTP/1.1
        parts = first_line.split()
        target = parts[1] if len(parts) >= 2 else ""
        await _handle_connect(
            target, reader, writer, rules, allow_ports, log_writer
        )
    elif first_line.upper().startswith("GET ") or first_line.upper().startswith("POST ") or \
         first_line.upper().startswith("PUT ") or first_line.upper().startswith("DELETE ") or \
         first_line.upper().startswith("PATCH ") or first_line.upper().startswith("HEAD ") or \
         first_line.upper().startswith("OPTIONS "):
        await _handle_http_proxy(
            data, reader, writer, rules, allow_ports, log_writer
        )
    else:
        # Unknown / malformed.
        writer.write(_DENY_RESPONSE)
        await writer.drain()
        writer.close()


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


async def _serve(
    sock_path: str,
    rules: list[str],
    allow_ports: list[int],
    log_path: str | None,
) -> None:
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass

    log_writer = _LogWriter(log_path) if log_path else None

    server = await asyncio.start_unix_server(
        lambda r, w: _handle_client(r, w, rules, allow_ports, log_writer),
        path=sock_path,
    )

    os.chmod(sock_path, 0o777)

    _log.info("proxy listening on %s", sock_path)

    async with server:
        await server.serve_forever()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PocketVerse host-side forward proxy")
    p.add_argument("--sock", required=True, help="UNIX socket path to listen on")
    p.add_argument(
        "--allow",
        action="append",
        default=[],
        dest="rules",
        help="Allowlist rule (repeatable)",
    )
    p.add_argument(
        "--allow-port",
        type=int,
        action="append",
        default=[],
        dest="allow_ports",
        help="Allowed upstream port (repeatable, default 443,80)",
    )
    p.add_argument("--log", default=None, help="Path to decision log file")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    rules = args.rules
    allow_ports = args.allow_ports or [443, 80]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    asyncio.run(_serve(args.sock, rules, allow_ports, args.log))


if __name__ == "__main__":
    main()
