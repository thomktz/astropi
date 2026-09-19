"""Shared dependencies for route handlers."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from astropi.runtime import Observatory


def get_observatory(request: Request) -> Observatory:
    return request.app.state.observatory


ObservatoryDep = Annotated[Observatory, Depends(get_observatory)]
