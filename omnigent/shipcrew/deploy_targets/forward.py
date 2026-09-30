"""A tiny TCP forwarder: a stable local port in front of a swappable container.

``python -m omnigent.shipcrew.deploy_targets.forward --listen 127.0.0.1:41000
--upstream-file <path>`` accepts on ``--listen`` and pipes each connection to the
``host:port`` written in ``--upstream-file``, read again for every new
connection. The docker target writes the new container's port there once it
is healthy, so the quick tunnel (which points at the stable port) keeps its URL
while the container behind it is replaced. Runs detached from the server, so a
server restart does not cut the site off. Standard library only.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from pathlib import Path

_CHUNK = 65536


def read_upstream(path: Path) -> tuple[str, int]:
    """``host:port`` from the upstream file."""
    host, _, port = path.read_text().strip().rpartition(":")
    return host or "127.0.0.1", int(port)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(_CHUNK):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass


async def _handle(
    upstream_file: Path, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        host, port = read_upstream(upstream_file)
        up_reader, up_writer = await asyncio.open_connection(host, port)
    except (OSError, ValueError):
        writer.close()
        return
    try:
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
    finally:
        for w in (writer, up_writer):
            with contextlib.suppress(Exception):
                w.close()


async def serve(listen: str, upstream_file: Path) -> None:
    host, _, port = listen.rpartition(":")
    server = await asyncio.start_server(
        lambda r, w: _handle(upstream_file, r, w), host or "127.0.0.1", int(port)
    )
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", required=True, help="host:port to accept on")
    parser.add_argument("--upstream-file", required=True, type=Path)
    args = parser.parse_args(argv)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(args.listen, args.upstream_file))


if __name__ == "__main__":
    main()
