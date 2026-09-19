"""Entry point: `astropi` or `python -m astropi`."""

from __future__ import annotations

import logging

import uvicorn

from astropi.config import load_settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    settings = load_settings()
    uvicorn.run(
        "astropi.api.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
