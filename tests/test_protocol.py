"""Tests for the myftp wire-protocol helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import protocol
from p2p import Message


def test_message_builders() -> None:
    assert protocol.offer("t1", "a.txt", 5, "BOB") == Message(
        "offer", meta={"tid": "t1", "filename": "a.txt", "size": 5, "name": "BOB"}
    )
    assert protocol.accept("t1") == Message("accept", meta={"tid": "t1"})
    assert protocol.decline("t1", "no thanks") == Message(
        "decline", meta={"tid": "t1", "reason": "no thanks"}
    )
    assert protocol.file_header("t1", "a.txt", 5) == Message(
        "file", meta={"tid": "t1", "filename": "a.txt", "size": 5}
    )
    assert protocol.chunk("t1", b"xy") == Message("chunk", b"xy", {"tid": "t1"})
    assert protocol.done("t1") == Message("done", meta={"tid": "t1"})
    assert protocol.saved("t1") == Message("saved", meta={"tid": "t1"})
    assert protocol.cancel("t1", "changed my mind") == Message(
        "cancel", meta={"tid": "t1", "reason": "changed my mind"}
    )


def test_offer_parse_roundtrip() -> None:
    message = protocol.offer("t9", "holiday.jpg", 1234, "ALICE")
    offer = protocol.Offer.parse(message)
    assert offer == protocol.Offer(
        tid="t9", filename="holiday.jpg", size=1234, name="ALICE"
    )


@pytest.mark.parametrize(
    "message",
    [
        Message("offer", meta={}),
        Message("offer", meta={"tid": "t", "filename": "f", "size": "big"}),
        Message("offer", meta={"tid": "t", "filename": "f", "size": -1}),
        Message("offer", meta={"tid": "", "filename": "f", "size": 1}),
    ],
)
def test_malformed_offers_are_rejected(message: Message) -> None:
    assert protocol.Offer.parse(message) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("photo.jpg", "photo.jpg"),
        ("../../evil.txt", "evil.txt"),
        ("..\\..\\evil.txt", "evil.txt"),
        ("/etc/passwd", "passwd"),
        ("C:\\Users\\me\\holiday.jpg", "holiday.jpg"),
        ("nul\x00byte.txt", "nul_byte.txt"),
        ('a:b*c?.txt', "a_b_c_.txt"),
        ("....", "received.bin"),
        ("", "received.bin"),
        (".", "received.bin"),
        ("..", "received.bin"),
        ("/", "received.bin"),
    ],
)
def test_safe_filename(raw: str, expected: str) -> None:
    assert protocol.safe_filename(raw) == expected


def test_safe_filename_is_length_capped() -> None:
    assert len(protocol.safe_filename("x" * 500 + ".txt")) == 200


def test_unique_path_never_overwrites(tmp_path: Path) -> None:
    (tmp_path / "photo.jpg").write_bytes(b"one")
    assert protocol.unique_path(tmp_path, "photo.jpg") == tmp_path / "photo (1).jpg"
    (tmp_path / "photo (1).jpg").write_bytes(b"two")
    assert protocol.unique_path(tmp_path, "photo.jpg") == tmp_path / "photo (2).jpg"
    (tmp_path / "archive.tar.gz").write_bytes(b"three")
    assert (
        protocol.unique_path(tmp_path, "archive.tar.gz")
        == tmp_path / "archive.tar (1).gz"
    )


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (999, "999 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (5 * 1024 * 1024, "5.0 MB"),
        (3 * 1024**3, "3.0 GB"),
        (int(2.5 * 1024**4), "2.5 TB"),
    ],
)
def test_human_size(size: float, expected: str) -> None:
    assert protocol.human_size(size) == expected


def test_human_speed() -> None:
    assert protocol.human_speed(2_300_000) == "2.2 MB/s"


@pytest.mark.parametrize(
    ("done", "total", "expected"),
    [(0, 100, 0.0), (50, 100, 0.5), (100, 100, 1.0), (150, 100, 1.0), (0, 0, 1.0)],
)
def test_fraction(done: int, total: int, expected: float) -> None:
    assert protocol.fraction(done, total) == expected
