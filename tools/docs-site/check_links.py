#!/usr/bin/env python3
"""Fail on broken internal links in markdown documentation (issue #1231).

Checks every relative link/image target in README.md, CONTRIBUTING.md,
SECURITY.md, CHANGELOG.md and docs/**/*.md resolves to an existing file or
directory in the repository. External links (http, https, mailto) and pure
in-page anchors are left to lychee, which runs with caching and an allowlist.

Usage: python3 tools/docs-site/check_links.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
TOP_LEVEL = ["README.md", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md"]
# The artifact index (issue #1457) is generated and lives in docs/; it is
# checked by tools/docs-site/check_artifacts.py, not by this link checker.
LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REF_DEF = re.compile(r"^\s*\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)", re.M)
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def targets(text: str):
    text = CODE.sub("", text)
    yield from LINK.findall(text)
    yield from REF_DEF.findall(text)


def main() -> int:
    files = [ROOT / f for f in TOP_LEVEL if (ROOT / f).exists()]
    files += sorted((ROOT / "docs").rglob("*.md"))
    files = [f for f in files if f.name != "artifact-index.md"]
    broken = []
    for f in files:
        for t in targets(f.read_text(encoding="utf-8")):
            if re.match(r"^[a-z][a-z0-9+.-]*:", t, re.I) or t.startswith("#"):
                continue
            path = unquote(t.split("#", 1)[0].split("?", 1)[0])
            base = ROOT if path.startswith("/") else f.parent
            if not (base / path.lstrip("/")).exists():
                broken.append(f"{f.relative_to(ROOT)}: broken link -> {t}")
    for b in broken:
        print(f"error: {b}", file=sys.stderr)
    print(f"checked {len(files)} files: {len(broken)} broken internal link(s)")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
