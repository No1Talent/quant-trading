"""
Pack the repository into a zip for an external reviewer / consultant.

Two modes:
  archive  (default, safest)
      Exactly what's committed on HEAD. Uses `git archive`, so anything
      in .gitignore — credentials, logs, data, caches — is excluded by
      construction.

  curated
      archive + selected research artifacts that are not in git:
        - research/figures/**          (plots / PDFs)
        - research/*_summary.csv       (factor / sensitivity summaries)
        - research/*_panel*.csv        (panel union CSVs)
      Still excludes research/*.log (multi-MB run logs).

Usage:
  python pack_for_review.py                      # archive mode, default name
  python pack_for_review.py --mode curated
  python pack_for_review.py --list               # dry run, print contents
  python pack_for_review.py --out C:\\tmp\\x.zip
  python pack_for_review.py --include-context-md # also pack quant_codebase_context.md

Safety:
  - Refuses to write if the resulting zip would contain a real
    connect_ctp.json or notify_config.json (non-template).
  - Warns if working tree is dirty (HEAD won't reflect uncommitted edits).
    Use --force to suppress the warning.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PREFIX = "Quant/"  # top-level folder inside the zip

# Files that would leak credentials if shipped. Templates are fine.
FORBIDDEN_BASENAMES = {"connect_ctp.json", "notify_config.json"}

# Curated-mode extras: glob patterns relative to repo root.
CURATED_GLOBS = [
    "research/figures/**/*",
    "research/*_summary.csv",
    "research/*_panel*.csv",
]


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


def working_tree_dirty() -> bool:
    return bool(run_git("status", "--porcelain").strip())


def git_archive_to(zip_path: Path) -> None:
    """Write `git archive HEAD` straight into zip_path."""
    run_git(
        "archive",
        "--format=zip",
        f"--prefix={PREFIX}",
        "-o",
        str(zip_path),
        "HEAD",
    )


def collect_curated_extras() -> list[Path]:
    extras: set[Path] = set()
    for pattern in CURATED_GLOBS:
        for p in ROOT.glob(pattern):
            if p.is_file():
                extras.add(p)
    return sorted(extras)


def append_to_zip(zip_path: Path, files: list[Path]) -> None:
    with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            arcname = PREFIX + str(f.relative_to(ROOT)).replace(os.sep, "/")
            zf.write(f, arcname)


def scan_for_credentials(zip_path: Path) -> list[str]:
    """Return list of member paths that look like leaked credentials."""
    leaks = []
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            base = name.rsplit("/", 1)[-1]
            if base in FORBIDDEN_BASENAMES:
                leaks.append(name)
    return leaks


def list_members(zip_path: Path) -> list[tuple[str, int]]:
    with zipfile.ZipFile(zip_path) as zf:
        return [(i.filename, i.file_size) for i in zf.infolist()]


def default_output_path(mode: str) -> Path:
    # dist/ is gitignored so output never accidentally gets committed.
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return ROOT / "dist" / f"Quant-{mode}-{stamp}.zip"


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--mode", choices=["archive", "curated"], default="archive")
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output zip path (default: ../Quant-{mode}-{timestamp}.zip)",
    )
    ap.add_argument(
        "--list", action="store_true", help="Print zip contents and exit without keeping the file"
    )
    ap.add_argument("--force", action="store_true", help="Don't abort on dirty working tree")
    ap.add_argument(
        "--include-context-md",
        action="store_true",
        help="Also include quant_codebase_context.md if present",
    )
    args = ap.parse_args()

    # 1. Working-tree sanity check
    if working_tree_dirty():
        msg = "Working tree is dirty — uncommitted changes will NOT be in the zip (git archive packs HEAD)."
        if args.force or args.list:
            print(f"warn: {msg}", file=sys.stderr)
        else:
            print(f"error: {msg}\nCommit or stash first, or pass --force.", file=sys.stderr)
            return 2

    out = args.out or default_output_path(args.mode)
    out.parent.mkdir(parents=True, exist_ok=True)

    # 2. Build zip (use temp file so we never leave a half-written zip at `out`)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False, dir=out.parent) as tmp:
        tmp_path = Path(tmp.name)

    try:
        git_archive_to(tmp_path)

        extras: list[Path] = []
        if args.mode == "curated":
            extras.extend(collect_curated_extras())
        if args.include_context_md:
            ctx = ROOT / "quant_codebase_context.md"
            if ctx.exists():
                extras.append(ctx)
            else:
                print(
                    "warn: --include-context-md set but quant_codebase_context.md not found",
                    file=sys.stderr,
                )
        if extras:
            append_to_zip(tmp_path, extras)

        # 3. Safety scan
        leaks = scan_for_credentials(tmp_path)
        if leaks:
            print("error: would-be zip contains forbidden credential files:", file=sys.stderr)
            for path in leaks:
                print(f"  {path}", file=sys.stderr)
            print("Refusing to write. Check .gitignore and curated globs.", file=sys.stderr)
            tmp_path.unlink(missing_ok=True)
            return 3

        # 4. List-only mode
        members = list_members(tmp_path)
        if args.list:
            for name, size in members:
                print(f"{human_size(size):>10}  {name}")
            print(f"\n{len(members)} files, zip = {human_size(tmp_path.stat().st_size)}")
            tmp_path.unlink(missing_ok=True)
            return 0

        # 5. Promote temp file to final location
        if out.exists():
            out.unlink()
        tmp_path.replace(out)

        print(f"done: {out}")
        print(f"  mode    : {args.mode}")
        print(f"  files   : {len(members)}")
        print(f"  size    : {human_size(out.stat().st_size)}")
        head = run_git("rev-parse", "--short", "HEAD").strip()
        print(f"  HEAD    : {head}")
        return 0
    finally:
        # Defensive: if we crashed between archive and promote, clean up.
        if tmp_path.exists() and tmp_path != out:
            tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
