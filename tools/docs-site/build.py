#!/usr/bin/env python3
"""Assemble the mdBook source for the versioned documentation site (issue #1231).

Copies README.md and docs/ into an mdBook source tree, generates SUMMARY.md with
a sidebar grouped by audience, stamps every page with the contract version it
applies to, and writes book.toml. `mdbook build` is then run on the output dir.

A page may override its banner with a first-line comment:
    <!-- applies-to: contract v4 and later -->

Usage: python3 tools/docs-site/build.py [--out target/docs-site] [--version latest]
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
ARTIFACTS_INDEX = DOCS / "artifacts-index.md"
ARTIFACTS_CHECKER = ROOT / "tools/artifacts-index/check.py"

# First matching keyword wins; unmatched pages land under "Reference".
AUDIENCES = [
    ("Integrators", ["integration", "typed-client", "score-query", "interface-", "asset-pair",
                     "event-schema", "errors", "score-math", "deprecation", "risk-score-schema",
                     "sdk-conformance", "commit-reveal", "attestation-spec", "privacy"]),
    ("Operators", ["runbook", "operations/", "ops-", "operator", "incident", "deploy", "upgrade",
                   "key-rotation", "slo-", "network-matrix", "release", "config", "reconciliation",
                   "production", "host-version", "resource-budget"]),
    ("Auditors", ["audit", "security", "threat", "invariant", "replay-protection", "griefing",
                  "zk-", "verkle", "dos-", "constant-time", "reports/", "critical-state",
                  "cross-contract", "capability"]),
    ("Contributors", ["adr/", "review", "mutation", "build-lints", "wasm", "reproducible",
                      "module-ownership", "storage-layout", "fuzzer", "governance", "glossary",
                      "spike", "aggregator", "EVENT_", "batch-", "threshold-"]),
]


def contract_version() -> str:
    src = (ROOT / "contracts/ledgerlens-score/src/constants.rs").read_text()
    return re.search(r"CONTRACT_VERSION: u32 = (\d+);", src).group(1)


def title(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip().replace("[", "").replace("]", "")
    return path.stem.replace("-", " ").replace("_", " ").title()


def audience(rel: str) -> str:
    low = rel.lower()
    for name, keys in AUDIENCES:
        if any(k.lower() in low for k in keys):
            return name
    return "Reference"


def check_artifacts_index() -> None:
    """Fail the docs build if the artifact index is stale or incomplete (issue #1457)."""
    if not ARTIFACTS_INDEX.exists():
        sys.exit(f"error: {ARTIFACTS_INDEX.relative_to(ROOT)} is missing")
    if not ARTIFACTS_CHECKER.exists():
        sys.exit(f"error: {ARTIFACTS_CHECKER.relative_to(ROOT)} is missing")
    result = subprocess.run(
        [sys.executable, str(ARTIFACTS_CHECKER), "--check"],
        cwd=ROOT,
    )
    if result.returncode != 0:
        sys.exit(
            "error: artifact index freshness/completeness check failed; "
            "regenerate docs/artifacts-index.md and add any missing artifacts"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "target/docs-site"))
    ap.add_argument("--version", default="latest", help="site version label, e.g. v1.2.0 or latest")
    ap.add_argument("--skip-artifacts-check", action="store_true",
                    help="skip the artifact index freshness/completeness check")
    args = ap.parse_args()

    if not args.skip_artifacts_check:
        check_artifacts_index()

    out = Path(args.out)
    src = out / "src"
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(DOCS, src)
    shutil.copy(ROOT / "README.md", src / "introduction.md")
    (src / "api").mkdir(exist_ok=True)
    (src / "api/index.md").write_text(
        "# API reference\n\nGenerated rustdoc for the stable contract API: "
        "[ledgerlens_score](ledgerlens_score/index.html).\n"
    )

    cv = contract_version()
    default_banner = f"contract v{cv} (`CONTRACT_VERSION = {cv}`), docs {args.version}"
    groups: dict[str, list[tuple[str, str]]] = {n: [] for n, _ in AUDIENCES}
    groups["Reference"] = []
    for page in sorted(src.rglob("*.md")):
        rel = page.relative_to(src).as_posix()
        text = page.read_text(encoding="utf-8")
        m = re.match(r"<!--\s*applies-to:\s*(.+?)\s*-->", text)
        banner = m.group(1) if m else default_banner
        page.write_text(f"> **Applies to:** {banner}\n\n{text}", encoding="utf-8")
        if rel in ("introduction.md", "api/index.md"):
            continue
        groups[audience(rel)].append((title(page), rel))

    lines = ["# Summary", "", "[Introduction](introduction.md)", ""]
    if (src / "artifacts-index.md").exists():
        lines += ["# Artifacts", "", "- [Machine-readable artifacts](artifacts-index.md)", ""]
    for name, pages in groups.items():
        if not pages:
            continue
        lines += [f"# {name}", ""] + [f"- [{t}]({r})" for t, r in sorted(pages)] + [""]
    lines += ["# API", "", "- [API reference](api/index.md)", ""]
    (src / "SUMMARY.md").write_text("\n".join(lines))

    shutil.copytree(HERE / "theme", out / "theme")
    (out / "book.toml").write_text(
        f"""[book]
title = "LedgerLens Contract Docs ({args.version})"
src = "src"
language = "en"

[output.html]
git-repository-url = "https://github.com/Ledger-Lenz/Ledgerlens-contract"
additional-js = ["theme/version-selector.js"]
site-url = "/Ledgerlens-contract/{args.version}/"
"""
    )
    print(f"mdBook source written to {out} ({sum(map(len, groups.values()))} pages)")


if __name__ == "__main__":
    main()
