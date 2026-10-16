"""The myftp wire protocol: message kinds and small helpers on top of p2p.

A transfer happens over **one** p2p connection. The sender opens it and sends::

    offer  ->  (the receiver's human decides)  ->  accept | decline
    file   ->  chunk * N  ->  done              (only after accept)
    ... receiver answers: saved   (the file is on disk, under its final name)

Closing the connection in the middle cancels the transfer - no message needed.
``cancel`` exists only so the *receiver* can politely abort a stream mid-flow
with a reason the sender can show.

Nothing here touches the disk or any UI: this module only turns transfer state
into :class:`p2p.Message` objects and back, so the GUI, both terminal UIs and
the tests all share exactly one wire format.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from pathlib import Path

from p2p import Message

# --------------------------------------------------------------------------
# message kinds
# --------------------------------------------------------------------------

KIND_OFFER = "offer"
KIND_ACCEPT = "accept"
KIND_DECLINE = "decline"
KIND_FILE = "file"
KIND_CHUNK = "chunk"
KIND_DONE = "done"
KIND_SAVED = "saved"
KIND_CANCEL = "cancel"


def offer(tid: str, filename: str, size: int, name: str) -> Message:
    """Sender -> receiver: "may I send you this file?"."""
    return Message(
        KIND_OFFER,
        meta={"tid": tid, "filename": filename, "size": size, "name": name},
    )


def accept(tid: str) -> Message:
    """Receiver -> sender: "yes, stream it"."""
    return Message(KIND_ACCEPT, meta={"tid": tid})


def decline(tid: str, reason: str = "") -> Message:
    """Receiver -> sender: "no" (a reason is optional and for humans only)."""
    return Message(KIND_DECLINE, meta={"tid": tid, "reason": reason})


def file_header(tid: str, filename: str, size: int) -> Message:
    """Sender -> receiver: the data stream for ``tid`` starts now."""
    return Message(
        KIND_FILE, meta={"tid": tid, "filename": filename, "size": size}
    )


def chunk(tid: str, data: bytes) -> Message:
    """One slice of the file; ``len(data)`` is capped by the p2p frame limit."""
    return Message(KIND_CHUNK, data=data, meta={"tid": tid})


def done(tid: str) -> Message:
    """Sender -> receiver: every byte was sent; the receiver verifies the size."""
    return Message(KIND_DONE, meta={"tid": tid})


def saved(tid: str) -> Message:
    """Receiver -> sender: the file is on disk under its final name."""
    return Message(KIND_SAVED, meta={"tid": tid})


def cancel(tid: str, reason: str = "") -> Message:
    """Receiver -> sender: abort the stream (the connection closes right after)."""
    return Message(KIND_CANCEL, meta={"tid": tid, "reason": reason})


@dataclass(frozen=True, slots=True)
class Offer:
    """A parsed ``offer`` message, with the sender's claims in typed fields."""

    tid: str
    filename: str
    size: int
    name: str = ""

    @property
    def safe_name(self) -> str:
        """``filename`` after :func:`safe_filename` - always use this one."""
        return safe_filename(self.filename)

    @classmethod
    def parse(cls, message: Message) -> Offer | None:
        """Return the offer carried by ``message``, or ``None`` if malformed."""
        try:
            tid = str(message.meta["tid"])
            filename = str(message.meta["filename"])
            size = int(message.meta["size"])
        except (KeyError, TypeError, ValueError):
            return None
        if not tid or not filename or size < 0:
            return None
        return cls(
            tid=tid,
            filename=filename,
            size=size,
            name=str(message.meta.get("name") or ""),
        )


# --------------------------------------------------------------------------
# files that arrive from strangers
# --------------------------------------------------------------------------

#: Characters that are special on some platform, plus ASCII control codes.
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

_MAX_FILENAME_LENGTH = 200


def safe_filename(raw: str, *, fallback: str = "received.bin") -> str:
    """Turn a peer-supplied name into a safe, *bare* file name.

    Guarantees: no directory part (``../../evil`` cannot escape the save
    folder), no path separators or control characters, not empty, never ``.``
    or ``..`` - while keeping the name recognisable to the human.
    """
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]  # drop any directory part
    name = _UNSAFE_FILENAME_CHARS.sub("_", name).strip(" .")
    if not name:
        return fallback
    return name[:_MAX_FILENAME_LENGTH]


def unique_path(directory: Path, filename: str) -> Path:
    """``photo.jpg`` -> the first free name: photo.jpg, photo (1).jpg, ...

    Existing files are never overwritten - receiving a second ``photo.jpg``
    stores it as ``photo (1).jpg`` next to the first one.
    """
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for number in itertools.count(1):
        candidate = directory / f"{stem} ({number}){suffix}"
        if not candidate.exists():
            return candidate
    raise AssertionError("unreachable")  # pragma: no cover - itertools is infinite


# --------------------------------------------------------------------------
# formatting shared by every front-end
# --------------------------------------------------------------------------


def human_size(size: float) -> str:
    """Format a byte count for humans: ``1536 -> "1.5 KB"``."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def human_speed(bytes_per_second: float) -> str:
    """``2_300_000 -> "2.2 MB/s"``."""
    return f"{human_size(bytes_per_second)}/s"


def fraction(done: int, total: int) -> float:
    """``done / total`` as 0..1; an empty transfer counts as complete."""
    if total <= 0:
        return 1.0
    return min(1.0, done / total)
