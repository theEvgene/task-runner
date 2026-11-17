"""Error taxonomy of the signed LAN bridge (stdlib only).

Kept in its own module so the envelope layer and the immutable file queue can
each raise and catch the same classes without importing one another: every
refusal a caller sees is one of these types, and the CLI exits 2 for all of
them.
"""

from __future__ import annotations


class BridgeError(Exception):
    """Base error for this bridge."""


class ProtocolError(BridgeError):
    pass


class FileQueueError(BridgeError):
    """Base error for the file queue."""


class InvalidJobIdError(FileQueueError):
    pass


class JobExistsError(FileQueueError):
    """Job already exists and is immutable (no duplicate execution)."""


class JobNotFoundError(FileQueueError):
    pass


class SizeLimitError(FileQueueError):
    pass


class UnsafePathError(FileQueueError):
    pass


class AtomicPublishNotSupportedError(FileQueueError):
    pass


class FetchTimeout(BridgeError):
    pass
