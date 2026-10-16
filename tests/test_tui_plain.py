"""End-to-end: two real ``--text --plain`` processes transfer a file.

No Flet, no fakes, no monkeypatching: two subprocesses drive the actual app
over loopback TCP and the real wire protocol, exactly like two peers would.
The subprocess plumbing lives in ``plain_peer.py`` (shared with the GUI test
that has a terminal peer talk to the Flet app).
"""

from __future__ import annotations

import os
from pathlib import Path

from plain_peer import spawn_plain_peer


async def test_plain_to_plain_transfer(tmp_path: Path) -> None:
    alice_dir = tmp_path / "alice"
    bob_dir = tmp_path / "bob"
    alice_dir.mkdir()
    bob_dir.mkdir()
    payload = os.urandom(1_200_000)  # several 512 KB chunks
    source = alice_dir / "report.bin"
    source.write_bytes(payload)

    alice = await spawn_plain_peer("ALICE", tmp_path, alice_dir)
    bob = await spawn_plain_peer("BOB", tmp_path, bob_dir)
    try:
        # Alice learns Bob's address the same way a user would.
        await alice.send(f"connect 127.0.0.1:{bob.port}")
        await alice.read_until(lambda line: line.startswith("added "))

        await alice.send(f"send 1 {source}")
        await bob.read_until(lambda line: "[offer]" in line and "report.bin" in line)
        await bob.send("a")

        alice_finished = await alice.read_until(
            lambda line: line.startswith("[finished] ok"), timeout=20
        )
        assert "Delivered" in alice_finished
        bob_finished = await bob.read_until(
            lambda line: line.startswith("[finished] ok"), timeout=20
        )
        assert "Saved to" in bob_finished
    finally:
        await alice.quit()
        await bob.quit()

    assert (bob_dir / "report.bin").read_bytes() == payload
    assert not list(bob_dir.glob("*.part"))


async def test_plain_decline_leaves_nothing_behind(tmp_path: Path) -> None:
    alice_dir = tmp_path / "alice"
    bob_dir = tmp_path / "bob"
    alice_dir.mkdir()
    bob_dir.mkdir()
    source = alice_dir / "junk.bin"
    source.write_bytes(b"just junk")

    alice = await spawn_plain_peer("ALICE", tmp_path, alice_dir)
    bob = await spawn_plain_peer("BOB", tmp_path, bob_dir)
    try:
        await alice.send(f"connect 127.0.0.1:{bob.port}")
        await alice.read_until(lambda line: line.startswith("added "))
        await alice.send(f"send 1 {source}")
        await bob.read_until(lambda line: "[offer]" in line)
        await bob.send("d")
        await alice.read_until(lambda line: "[reply] declined", timeout=20)
        await alice.read_until(lambda line: "[finished] failed", timeout=20)
    finally:
        await alice.quit()
        await bob.quit()

    assert list(bob_dir.iterdir()) == []
    assert alice.proc.returncode in (None, 0)


async def test_plain_peer_renames_itself(tmp_path: Path) -> None:
    """The `name` command changes what other peers see - offer included."""
    alice_dir = tmp_path / "alice"
    bob_dir = tmp_path / "bob"
    alice_dir.mkdir()
    bob_dir.mkdir()
    source = alice_dir / "note.txt"
    source.write_bytes(b"hello")

    alice = await spawn_plain_peer("ALICE", tmp_path, alice_dir)
    bob = await spawn_plain_peer("BOB", tmp_path, bob_dir)
    try:
        # bare `name` is a question, not a change
        await alice.send("name")
        usage = await alice.read_until(lambda line: "your name is" in line)
        assert "'ALICE'" in usage and "usage: name" in usage

        await alice.send("name   Ada   Lovelace  ")  # tidied on the way in
        changed = await alice.read_until(lambda line: line.startswith("name changed"))
        assert "'Ada Lovelace'" in changed

        # the next offer introduces Alice under the new name
        await alice.send(f"connect 127.0.0.1:{bob.port}")
        await alice.read_until(lambda line: line.startswith("added "))
        await alice.send(f"send 1 {source}")
        offer = await bob.read_until(lambda line: "[offer]" in line)
        assert offer.startswith("[offer] Ada Lovelace wants to send")
        await bob.send("d")
        await alice.read_until(lambda line: "[finished] failed", timeout=20)
    finally:
        await alice.quit()
        await bob.quit()
