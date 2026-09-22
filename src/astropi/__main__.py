"""Entry point: `astropi` or `python -m astropi`."""

from __future__ import annotations

import logging
import socket

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


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    settings = load_settings()
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
