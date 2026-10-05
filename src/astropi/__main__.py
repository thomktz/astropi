"""Entry point: `astropi` or `python -m astropi`."""

from __future__ import annotations

import logging
import socket
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from astropi.config import load_settings

logger = logging.getLogger(__name__)

#: Hosts that mean "anything that can reach me", rather than one address.
WILDCARDS = {"0.0.0.0", "::", "*"}


def listening_sockets(host: str, port: int) -> list[socket.socket] | None:
    """Sockets to serve on, or None to let uvicorn bind its own.

    A wildcard host means both address families, which uvicorn cannot do
    from one `--host`. It matters on the Pi: macOS resolves `astropi.local`
    to its IPv6 address, so a dashboard bound to 0.0.0.0 is reachable by
    ssh and by IP and not by the name anyone would type - while `::` is
    IPv6-only on this kernel, so binding that instead just moves which
    half of the house cannot connect.
    """
    if host not in WILDCARDS:
        return None

    sockets: list[socket.socket] = []
    for family, address in ((socket.AF_INET6, "::"), (socket.AF_INET, "0.0.0.0")):
        try:
            listener = socket.socket(family, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family is socket.AF_INET6:
                # Refuse v4-mapped connections on the v6 socket, so the
                # dedicated v4 socket below can bind the same port without
                # the two fighting over it.
                listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            listener.bind((address, port))
            listener.listen(2048)
            listener.set_inheritable(True)
            sockets.append(listener)
        except OSError as error:
            logger.warning("not listening on %s: %s", address, error)

    if not sockets:
        return None
    return sockets


def _trace_mount(data_dir: Path) -> None:
    """Every command sent to the mount and every reply, to a file of its own.

    Kept out of the main log, which it would drown - a few commands a
    second, all night - and kept at all because when an axis does what it
    was not told to, this is the only record of what it *was* told.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(data_dir / "mount-trace.log", maxBytes=5_000_000, backupCount=4)
    handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(message)s", datefmt="%H:%M:%S"))
    trace = logging.getLogger("astropi.synta.trace")
    trace.addHandler(handler)
    trace.setLevel(logging.DEBUG)
    trace.propagate = False


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    settings = load_settings()
    _trace_mount(settings.data_dir)
    config = uvicorn.Config(
        "astropi.api.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )
    server = uvicorn.Server(config)
    sockets = listening_sockets(settings.host, settings.port)
    if sockets:
        logger.info(
            "listening on port %d (%s)",
            settings.port,
            ", ".join(sorted({s.getsockname()[0] for s in sockets})),
        )
    server.run(sockets=sockets)


if __name__ == "__main__":
    main()
