"""Move lights saved in the old `<night>/<target>/light_*` layout into sessions.

A dry run by default: it prints what would move where and what it would
skip, and touches nothing.

    uv run python scripts/migrate_legacy.py                 # dry run
    uv run python scripts/migrate_legacy.py --apply         # move, log, make sessions
    uv run python scripts/migrate_legacy.py --undo <log>    # put it all back

`--filter` names the filter the old lights were shot through (they did not
record one); it goes in the new names and the sessions. See
`astropi.storage.legacy` for the layout either side.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import Counter
from pathlib import Path

from astropi.config import load_settings
from astropi.storage.imaging import ImagingStore
from astropi.storage.legacy import apply, plan, undo


def main() -> int:
    settings = load_settings()
    default_root = settings.frames_dir or (
        Path("/mnt/ssd/astropi") if Path("/mnt/ssd/astropi").exists() else settings.data_dir / "frames"
    )
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, default=default_root, help=f"frames root ({default_root})")
    parser.add_argument("--data-dir", type=Path, default=settings.data_dir, help="where sessions are kept")
    parser.add_argument("--filter", default="None", help='the old lights\' filter, e.g. "L-eXtreme"')
    parser.add_argument("--apply", action="store_true", help="really move the files")
    parser.add_argument("--undo", type=Path, metavar="LOG", help="reverse a migration from its log")
    args = parser.parse_args()
    store = ImagingStore(args.data_dir / "imaging")

    if args.undo:
        restored, problems = undo(args.undo, store)
        print(f"put back {restored} frames")
        for problem in problems:
            print(f"  ! {problem}")
        return 1 if problems else 0

    result = plan(args.root, args.filter)
    for group in result.groups:
        print(f"{group.night}  {group.target}: {len(group.moves)} lights -> {group.folder}")
        for move in group.moves:
            print(f"    {move.old.relative_to(args.root)}\n      -> {move.new.relative_to(args.root)}")
    if result.skipped:
        reasons = Counter(reason for _, reason in result.skipped)
        print(f"\nskipping {len(result.skipped)} files:")
        for path, reason in result.skipped:
            print(f"    {path.relative_to(args.root)}: {reason}")
        for reason, count in reasons.items():
            print(f"  {count} x {reason}")
    total = len(result.moves)
    if not args.apply:
        print(f"\ndry run: {total} lights would move. Add --apply to move them.")
        return 0
    if not total:
        print("nothing to move")
        return 0

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log = args.root / f"migration_log_{stamp}.csv"
    sessions = apply(result, store, log, args.filter)
    print(f"\nmoved {total} lights into {len(sessions)} sessions")
    print(f"log: {log}\nundo: uv run python scripts/migrate_legacy.py --undo {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
