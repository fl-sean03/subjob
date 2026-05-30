"""Race-test the atomic claim primitive.

Spawn N processes contending for M tasks. Verify:
  - every task is claimed by exactly one worker
  - no claims are lost
  - no double-claims

If this ever flakes, the rename invariant is broken — investigate immediately.
"""

from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path

from subjob.lib.lock import atomic_move


def _race_worker(pending_dir: str, claimed_dir: str, my_id: int, queue):
    pending = Path(pending_dir)
    claimed = Path(claimed_dir)
    wins: list[str] = []
    for path in sorted(pending.iterdir()):
        target = claimed / path.name
        if atomic_move(path, target):
            wins.append(path.name)
    queue.put((my_id, wins))


def test_no_double_claim_under_contention(tmp_path):
    pending = tmp_path / "pending"
    claimed = tmp_path / "claimed"
    pending.mkdir()
    claimed.mkdir()

    n_tasks = 50
    for i in range(n_tasks):
        (pending / f"t{i:03d}.yaml").write_text("placeholder\n")

    n_workers = 4
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    procs = [
        ctx.Process(target=_race_worker, args=(str(pending), str(claimed), i, queue))
        for i in range(n_workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=10)
        assert p.exitcode == 0

    all_wins: dict[str, int] = {}  # task_name -> winning worker id
    for _ in range(n_workers):
        wid, wins = queue.get()
        for name in wins:
            assert name not in all_wins, f"task {name} double-claimed"
            all_wins[name] = wid

    # Every task accounted for, claimed/ is full, pending/ is empty.
    assert len(all_wins) == n_tasks
    assert sorted(os.listdir(claimed)) == sorted(f"t{i:03d}.yaml" for i in range(n_tasks))
    assert os.listdir(pending) == []


def test_lost_race_returns_false(tmp_path):
    src = tmp_path / "a.yaml"
    dst1 = tmp_path / "first.yaml"
    dst2 = tmp_path / "second.yaml"
    src.write_text("x")

    assert atomic_move(src, dst1) is True
    # Now src is gone — a second mover sees FileNotFoundError → returns False.
    assert atomic_move(src, dst2) is False
    assert dst1.exists()
    assert not dst2.exists()
