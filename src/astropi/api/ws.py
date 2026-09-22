"""WebSocket telemetry.

One socket carries everything: mount position, camera state, guide samples,
solve results and task progress. A single multiplexed stream rather than a
socket per concern, because the client needs them correlated in time and
because a phone on a weak link should open one connection, not six.

On connect the buffered recent events are replayed, so a dashboard opened
mid-session renders immediately instead of waiting for the next event.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from astropi.runtime import Observatory

logger = logging.getLogger(__name__)

router = APIRouter()

#: Interval for the position poll that supplements event-driven updates.
POSITION_INTERVAL_S = 1.0


@router.websocket("/ws")
async def telemetry(socket: WebSocket) -> None:
    await socket.accept()
    observatory: Observatory = socket.app.state.observatory

    await socket.send_json({"topic": "hello", "payload": observatory.describe()})
    for event in observatory.events.recent():
        await socket.send_json(event.as_json())

    # Current state last, after the history, so a stale event replayed from
    # the buffer cannot overwrite it.
    #
    # These are state rather than events, and the history is a ring buffer:
    # during guiding the samples push older `guiding.state` events out of
    # it within a minute, so a dashboard opened mid-session would otherwise
    # claim guiding was stopped while the loop was running.
    target = observatory.active_target
    await socket.send_json(
        {
            "topic": "target.active",
            "payload": {
                "target": None if target is None else observatory.describe_target(target)
            },
        }
    )
    if observatory.guider is not None:
        guiding = await observatory.guider.status()
        await socket.send_json({"topic": "guiding.state", "payload": {"state": str(guiding.state)}})

    poller = asyncio.create_task(_poll_position(socket, observatory))
    try:
        async with observatory.events.subscribe() as stream:
            async for event in stream:
                await socket.send_json(event.as_json())
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("telemetry socket failed")
    finally:
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller


async def _poll_position(socket: WebSocket, observatory: Observatory) -> None:
    """Push mount position on a timer.

    The mount publishes on state changes, but while tracking it changes
    continuously without any discrete event to hang a publish on - so the
    position readout would sit frozen without this.
    """
    from astropi.core.timekeeping import hour_angle_deg
    from astropi.devices import DeviceRole, Mount

    while True:
        await asyncio.sleep(POSITION_INTERVAL_S)
        try:
            mount = observatory.registry.require_connected(DeviceRole.MOUNT, Mount)
            status = await mount.status()
        except Exception:
            continue
        try:
            await socket.send_json(
                {
                    "topic": "mount.position",
                    "payload": {
                        "state": str(status.state),
                        "ra_deg": status.position.ra_deg,
                        "dec_deg": status.position.dec_deg,
                        "alt_deg": status.horizontal.alt_deg if status.horizontal else None,
                        "az_deg": status.horizontal.az_deg if status.horizontal else None,
                        "tracking": status.tracking,
                        "hour_angle_deg": hour_angle_deg(
                            status.position.ra_deg, observatory.site.longitude_deg
                        ),
                    },
                }
            )
        except WebSocketDisconnect:
            # The browser tab closed. Normal, and it was arriving as a
            # stack trace in the log every single time - the poller died
            # with it, and the handler's `await poller` re-raised it past
            # the disconnect handling that was already there.
            return
