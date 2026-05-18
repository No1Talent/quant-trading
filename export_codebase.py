"""
Export codebase to a single Markdown file for Gemini architect review.
Usage: python export_codebase.py
Output: quant_codebase_context.md (in the same directory)
"""

import os
import subprocess
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(ROOT_DIR, "quant_codebase_context.md")

# Directories that add zero signal for an architect review
IGNORE_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "venv",
    ".venv",
    "data",
    "logs",
    "temp",
    "quant_vnpy.egg-info",
    ".github",
}

# File extensions worth reading, mapped to their markdown fence language tag
EXTENSION_LANG = {
    ".py": "python",
    ".md": "markdown",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
}
ALLOWED_EXTENSIONS = set(EXTENSION_LANG)

# Individual files to skip even if their extension matches
SKIP_FILES = {
    "export_codebase.py",  # this script itself
    "quant_codebase_context.md",  # the output file
    ".pre-commit-config.yaml",  # tooling detail, not arch signal
}


def _allowed(filename: str) -> bool:
    return os.path.splitext(filename)[1] in ALLOWED_EXTENSIONS


def collect_files(root: str) -> list[str]:
    result = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS)
        for f in sorted(files):
            if f in SKIP_FILES:
                continue
            if _allowed(f):
                result.append(os.path.join(dirpath, f))
    return result


def render_tree(root: str) -> str:
    lines = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS)
        rel = os.path.relpath(dirpath, root)
        # '.' means we're at the root itself; subdirs need +1 for the implicit root sep
        level = 0 if rel == "." else rel.count(os.sep) + 1
        indent = "    " * level
        folder = os.path.basename(root) if level == 0 else os.path.basename(dirpath)
        lines.append(f"{indent}{folder}/")
        sub = "    " * (level + 1)
        for f in sorted(files):
            if f in SKIP_FILES:
                continue
            if _allowed(f):
                lines.append(f"{sub}{f}")
    return "\n".join(lines)


def git_log_summary(root: str, n: int = 8) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", root, "log", "--oneline", f"-{n}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "(git log unavailable)"
    except Exception:
        return "(git log unavailable)"


def main() -> None:
    files = collect_files(ROOT_DIR)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as out:
        # Header
        out.write("# Quant Codebase — Architect Context\n\n")
        out.write(f"**Exported:** {datetime.now().strftime('%Y-%m-%d %H:%M')}  \n")
        out.write(f"**Files included:** {len(files)}\n\n")

        # Recent git history (gives Gemini commit-message context)
        out.write("## Recent Git History\n\n```\n")
        out.write(git_log_summary(ROOT_DIR))
        out.write("\n```\n\n")

        # Directory tree
        out.write("## Project Structure\n\n```\n")
        out.write(render_tree(ROOT_DIR))
        out.write("\n```\n\n")

        # File contents
        out.write("## Codebase Contents\n\n")
        for filepath in files:
            rel = os.path.relpath(filepath, ROOT_DIR)
            ext = os.path.splitext(filepath)[1]
            lang = EXTENSION_LANG.get(ext, "")
            out.write(f"### {rel}\n\n```{lang}\n")
            try:
                with open(filepath, encoding="utf-8", errors="replace") as f:
                    out.write(f.read())
            except Exception as e:
                out.write(f"# Error reading file: {e}\n")
            out.write("\n```\n\n")

    size_kb = os.path.getsize(OUTPUT_FILE) // 1024
    print(f"Done: {OUTPUT_FILE}")
    print(f"Files: {len(files)}   Size: {size_kb} KB")
    print("Drag quant_codebase_context.md into Gemini's chat window.")


if __name__ == "__main__":
    main()
