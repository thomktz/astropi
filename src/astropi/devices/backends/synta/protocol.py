"""Sky-Watcher / Synta motor controller protocol.

The wire format every Sky-Watcher mount speaks over its serial port, the
Star Adventurer GTi included: an ASCII request `:<command><axis><data>\\r`
and a reply that is either `=<data>\\r` or `!<code>\\r`.

Kept apart from the mount itself because it is pure encoding with one
quirk worth isolating: **24-bit values travel low byte first**. The mount's
counts per revolution come back as `005F37`, which is 0x375F00 - 3,628,800
- and reading those six characters in the order they arrive gives a number
23,000 times too small. Every field in this protocol is like that.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from astropi.core.errors import DeviceError

logger = logging.getLogger(__name__)

#: The controller's origin. Counts are unsigned and the axes have to turn
#: both ways, so the middle of the range is zero.
HOME_COUNTS = 0x800000

#: Seconds to wait for a reply before giving up on a command.
REPLY_TIMEOUT_S = 1.5

#: What the mount says went wrong. Its own numbering.
ERRORS = {
    "0": "unknown command",
    "1": "command length error",
    "2": "motor not stopped",
    "3": "invalid character",
    "4": "not initialised",
    "5": "driver sleeping",
    "7": "mount not initialised",
    "8": "motor still running",
}


class SyntaError(DeviceError):
    """The mount answered, and the answer was a refusal."""


def encode24(value: int) -> str:
    """Six hex characters, low byte first, as the controller expects."""
    value &= 0xFFFFFF
    return f"{value & 0xFF:02X}{(value >> 8) & 0xFF:02X}{(value >> 16) & 0xFF:02X}"


def decode24(data: str) -> int:
    """Undo `encode24`, for the 2-, 4- and 6-character replies alike."""
    text = data.strip()
    if len(text) not in (2, 4, 6):
        raise SyntaError(f"cannot read {text!r} as a value")
    value = 0
    for index in range(0, len(text), 2):
        value |= int(text[index : index + 2], 16) << (4 * index)
    return value


@dataclass(frozen=True, slots=True)
class AxisStatus:
    """What `:f` says an axis is doing.

    Three hex digits, one bit field each. `initialised` is the one that
    bites: a freshly powered mount reports false and refuses every motion
    command with "not initialised" until `:F` has been sent.
    """

    slewing: bool
    """Constant-rate motion (tracking, guiding) rather than a goto."""
    backward: bool
    fast: bool
    running: bool
    blocked: bool
    initialised: bool
    level_switch: bool

    @classmethod
    def parse(cls, data: str) -> AxisStatus:
        text = data.strip()
        if len(text) != 3:
            raise SyntaError(f"cannot read {text!r} as an axis status")
        first, second, third = (int(char, 16) for char in text)
        return cls(
            slewing=bool(first & 0x1),
            backward=bool(first & 0x2),
            fast=bool(first & 0x4),
            running=bool(second & 0x1),
            blocked=bool(second & 0x2),
            initialised=bool(third & 0x1),
            level_switch=bool(third & 0x2),
        )


class Transport:
    """Somewhere to send bytes. A serial port, or a fake in a test."""

    def write(self, data: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def read_until(self, terminator: bytes, timeout_s: float) -> bytes:  # pragma: no cover
        raise NotImplementedError

    def reset(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class SerialTransport(Transport):
    """A real serial port, opened through pyserial."""

    def __init__(self, port: str, baud: int = 9600) -> None:
        import serial  # imported here so the simulator needs no serial stack

        self._serial = serial.Serial(port=port, baudrate=baud, timeout=0.05)

    def write(self, data: bytes) -> None:
        self._serial.write(data)

    def read_until(self, terminator: bytes, timeout_s: float) -> bytes:
        deadline = time.monotonic() + timeout_s
        buffer = bytearray()
        while time.monotonic() < deadline:
            chunk = self._serial.read(64)
            if chunk:
                buffer += chunk
                if terminator in buffer:
                    break
        return bytes(buffer)

    def reset(self) -> None:
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()

    def close(self) -> None:
        self._serial.close()


class SyntaLink:
    """One conversation with the controller, one command at a time.

    Synchronous and locked, because the protocol is: a request is followed
    by exactly one reply, and two overlapping requests on one serial line
    read each other's answers. The mount backend keeps this off the event
    loop with `asyncio.to_thread`.
    """

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._lock = threading.Lock()

    def close(self) -> None:
        self._transport.close()

    def command(self, command: str, axis: int, data: str = "") -> str:
        """Send one command and return the reply's payload.

        Retried once on a garbled reply. The mount is on a USB serial line
        that occasionally hands back a partial frame, and a retry is far
        better than an aborted slew.
        """
        message = f":{command}{axis}{data}\r".encode("ascii")
        with self._lock:
            for attempt in (1, 2):
                self._transport.reset()
                self._transport.write(message)
                raw = self._transport.read_until(b"\r", REPLY_TIMEOUT_S)
                reply = raw.decode("ascii", errors="replace").strip()
                if reply.startswith("="):
                    return reply[1:]
                if reply.startswith("!"):
                    code = reply[1:2]
                    raise SyntaError(
                        f"{command}{axis} refused: {ERRORS.get(code, f'error {code}')}"
                    )
                logger.warning("garbled reply to %s%s: %r (attempt %d)", command, axis, reply, attempt)
        raise SyntaError(f"no reply to {command}{axis}")

    # Inquiries -----------------------------------------------------------

    def version(self, axis: int = 1) -> int:
        return decode24(self.command("e", axis))

    def counts_per_revolution(self, axis: int) -> int:
        return decode24(self.command("a", axis))

    def timer_frequency(self, axis: int) -> int:
        return decode24(self.command("b", axis))

    def sidereal_period(self, axis: int) -> int:
        """Timer ticks per step at sidereal rate, straight from the mount.

        Computable from the counts per revolution and the sidereal rate,
        and worth asking for anyway: the two agreeing to the tick is a
        strong sign the gearing has been read correctly.
        """
        return decode24(self.command("D", axis))

    def high_speed_ratio(self, axis: int) -> int:
        return decode24(self.command("g", axis))

    def position(self, axis: int) -> int:
        """Axis position in counts, zeroed on the controller's origin."""
        return decode24(self.command("j", axis)) - HOME_COUNTS

    def status(self, axis: int) -> AxisStatus:
        return AxisStatus.parse(self.command("f", axis))

    # Commands ------------------------------------------------------------

    def initialise(self, axis: int) -> None:
        self.command("F", axis)

    def set_position(self, axis: int, counts: int) -> None:
        self.command("E", axis, encode24(counts + HOME_COUNTS))

    def set_motion_mode(self, axis: int, *, goto: bool, fast: bool, backward: bool) -> None:
        """Choose what the next `start` will do.

        The first character is not two independent bits: for a goto, 0 is
        fast and 2 is slow; for constant-rate motion, 3 is fast and 1 is
        slow. Writing that as "bit 1 means slow" gets tracking and slewing
        backwards from each other, which is a mistake that ends with the
        mount running away at 800x sidereal.
        """
        # The table above, in one line: goto is 0/2, constant rate is
        # 3/1, and "fast" is not the same digit in each.
        mode = ("0" if fast else "2") if goto else ("3" if fast else "1")
        self.command("G", axis, f"{mode}{'1' if backward else '0'}")

    def set_goto_target(self, axis: int, increment: int) -> None:
        """How far the next goto travels, in counts, as a magnitude."""
        self.command("H", axis, encode24(abs(increment)))

    def set_step_period(self, axis: int, period: int) -> None:
        """Timer ticks per step - the speed of constant-rate motion."""
        self.command("I", axis, encode24(period))

    def set_brake_increment(self, axis: int, counts: int) -> None:
        self.command("M", axis, encode24(counts))

    def start(self, axis: int) -> None:
        self.command("J", axis)

    def stop(self, axis: int) -> None:
        """Decelerate to a halt. The polite stop, and the usual one."""
        self.command("K", axis)

    def stop_now(self, axis: int) -> None:
        """Stop without decelerating. For an abort, not for routine use."""
        self.command("L", axis)
