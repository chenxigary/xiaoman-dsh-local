#!/usr/bin/env python3
"""Safely clear generated output for the two managed DSH overlay packages.

This is intentionally narrower than a package-manager clean command.  It
only removes the exact ``lib`` directories owned by the ui-voice and Host
overlay targets, or (with ``--post-build``) validated Finder duplicate files
and directories inside those directories.  Every path is checked without following symlinks
before anything is deleted, so a malformed checkout fails closed instead of
deleting outside the harness.
"""

from __future__ import annotations

import argparse
import filecmp
import os
import re
import stat
import sys
from pathlib import Path


TARGETS = (
    Path("packages/client/ui-voice/lib"),
    Path("packages/host/codex/lib"),
)

# macOS Finder can preserve a second or later copy created during a
# provider/build race by inserting `` 2``/`` 3``/... before the final
# extension (or as a directory suffix). Every non-empty duplicate must be
# byte-identical to its canonical counterpart; differing output fails closed.
FINDER_CONFLICT = re.compile(r"^(?P<stem>.+) (?P<number>[2-9][0-9]*)(?P<extension>(?:\.[^.]+)*)$")
FINDER_MARKER = re.compile(r" [0-9]+(?:\.|$)")


class CleanError(RuntimeError):
    pass


def fail(message: str) -> None:
    raise CleanError(message)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", type=Path, required=True)
    parser.add_argument(
        "--post-build",
        action="store_true",
        help="remove only validated macOS Finder duplicate outputs after a build",
    )
    return parser.parse_args(argv)


def exact_harness(value: Path) -> Path:
    if value.is_symlink():
        fail(f"harness must not be a symlink: {value}")
    try:
        harness = value.absolute()
    except OSError as exc:
        fail(f"cannot resolve harness: {exc}")
    if harness == Path(harness.anchor) or not harness.is_dir():
        fail(f"harness must be an existing directory: {harness}")
    if harness.is_symlink():
        fail(f"harness must not be a symlink: {harness}")
    return harness


def checked_target(harness: Path, relative: Path) -> Path:
    target = harness.joinpath(*relative.parts)
    # Validate every component from the exact harness down to the generated
    # directory.  A symlinked parent could otherwise make a lexical path look
    # safe while resolving outside the checkout.
    current = harness
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            fail(f"managed generated path must not contain a symlink: {current}")
    if target.exists() and not target.is_dir():
        fail(f"managed generated path is not a directory: {target}")
    return target


def validate_tree(path: Path) -> None:
    """Preflight the complete tree before the first deletion."""

    if not path.exists():
        return
    if path.is_symlink():
        fail(f"managed generated path must not be a symlink: {path}")
    if not path.is_dir():
        fail(f"managed generated path is not a directory: {path}")
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            fail(f"cannot inspect generated path {current}: {exc}")
        for entry in entries:
            child = Path(entry.path)
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISLNK(mode):
                fail(f"generated output contains a symlink: {child}")
            if stat.S_ISDIR(mode):
                stack.append(child)
            elif not stat.S_ISREG(mode):
                fail(f"generated output contains an unsupported file: {child}")


def conflict_canonical(path: Path) -> Path | None:
    """Return the canonical path for a recognized Finder duplicate name."""

    match = FINDER_CONFLICT.fullmatch(path.name)
    if match is None:
        return None
    return path.with_name(f"{match.group('stem')}{match.group('extension')}")


def generated_trees_identical(left: Path, right: Path) -> bool:
    """Compare two already-symlink-validated generated trees recursively."""

    if left.is_file() and right.is_file():
        return filecmp.cmp(left, right, shallow=False)
    if not left.is_dir() or not right.is_dir():
        return False
    left_entries = {entry.name: entry for entry in left.iterdir()}
    right_entries = {entry.name: entry for entry in right.iterdir()}
    if left_entries.keys() != right_entries.keys():
        return False
    return all(generated_trees_identical(left_entry, right_entries[name])
               for name, left_entry in left_entries.items())


def validate_conflict_pair(path: Path, canonical: Path) -> None:
    if path.is_file():
        if not canonical.is_file() or not filecmp.cmp(path, canonical, shallow=False):
            fail(f"generated conflict is not byte-identical: {path}")
        return
    if not path.is_dir() or not canonical.is_dir():
        fail(f"generated conflict has an invalid canonical counterpart: {path}")
    # Finder may leave empty directory copies when a package build races with
    # a file-provider sync. Empty copies are safe to remove; populated copies
    # require an exact recursive proof before deletion.
    if any(path.iterdir()):
        if not generated_trees_identical(path, canonical):
            fail(f"generated directory conflict is not byte-identical: {path}")


def find_post_build_conflicts(target: Path) -> list[tuple[Path, Path]]:
    """Validate and collect safe Finder duplicates without deleting anything."""

    conflicts: list[tuple[Path, Path]] = []
    for path in sorted(target.rglob("*"), key=lambda item: len(item.parts)):
        # A populated conflict directory owns its descendants; validate and
        # remove it as one unit rather than independently deleting nested
        # entries. Nested conflict files in ordinary directories are still
        # handled by this same scan.
        if any(parent in {entry[0] for entry in conflicts} for parent in path.parents):
            continue
        if not FINDER_MARKER.search(path.name):
            continue
        canonical = conflict_canonical(path)
        if canonical is None or not canonical.exists():
            fail(f"unrecognized generated conflict (canonical file missing): {path}")
        validate_conflict_pair(path, canonical)
        conflicts.append((path, canonical))
    return conflicts


def remove_post_build_conflicts(conflicts: list[tuple[Path, Path]]) -> int:
    """Remove only prevalidated duplicate files, preserving canonical output."""

    for duplicate, _canonical in conflicts:
        if duplicate.is_symlink():
            fail(f"generated conflict changed during cleanup: {duplicate}")
        if duplicate.is_file():
            duplicate.unlink()
        elif duplicate.is_dir():
            if any(duplicate.iterdir()):
                remove_tree(duplicate)
            else:
                duplicate.rmdir()
        else:
            fail(f"generated conflict changed during cleanup: {duplicate}")
    return len(conflicts)


def remove_tree(path: Path) -> int:
    removed = 0
    if not path.exists():
        return removed
    stack: list[tuple[Path, bool]] = [(path, False)]
    while stack:
        current, visited = stack.pop()
        if visited:
            current.rmdir()
            continue
        stack.append((current, True))
        for entry in os.scandir(current):
            child = Path(entry.path)
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                stack.append((child, False))
            elif stat.S_ISREG(mode):
                child.unlink()
                removed += 1
            else:
                # validate_tree() ran for every target before this function;
                # this is a race/TOCTOU guard rather than a deletion fallback.
                fail(f"generated output changed during cleanup: {child}")
    return removed


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        harness = exact_harness(args.harness)
        targets = [checked_target(harness, relative) for relative in TARGETS]
        # Preflight both targets before deleting either one.  A bad Host path
        # must not leave Client output half-cleaned.
        for target in targets:
            validate_tree(target)
        if args.post_build:
            conflicts = [
                conflict
                for target in targets
                for conflict in find_post_build_conflicts(target)
            ]
            removed = remove_post_build_conflicts(conflicts)
            print(f"[dsh-clean] removed {removed} recognized post-build conflict file(s)")
            return 0
        removed = sum(remove_tree(target) for target in targets)
        print(f"[dsh-clean] cleared {removed} generated file(s) from {len(targets)} managed lib path(s)")
        return 0
    except CleanError as exc:
        print(f"[dsh-clean] ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"[dsh-clean] ERROR: generated output changed during cleanup: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
