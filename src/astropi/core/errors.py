"""Exception hierarchy.

Every failure the API can surface maps to one of these, so route handlers
translate a known domain error into an HTTP status without each router
inventing its own error shape.
"""

from __future__ import annotations


class AstropiError(Exception):
    """Base for every error this application raises deliberately."""


class DeviceError(AstropiError):
    """A device could not carry out a request."""


class DeviceNotFoundError(DeviceError):
    """No device is registered under the requested role or id."""


class NotConnectedError(DeviceError):
    """The device is registered but not connected."""


class DeviceBusyError(DeviceError):
    """The device is mid-operation and cannot accept this request."""


class CapabilityError(DeviceError):
    """The device does not support the requested capability.

    Raised rather than silently no-op'ing: a rig without a focuser should
    fail loudly when something tries to autofocus, not appear to succeed.
    """


class SolveFailedError(AstropiError):
    """Plate solving did not converge on a solution for a frame."""


class TaskCancelledError(AstropiError):
    """A long-running task was cancelled by the operator."""


class SafetyError(AstropiError):
    """A requested move would violate a configured safety limit."""
