#!/usr/bin/env python3
"""Test-suite tiering for LedgerLens: manifest, budgets, and coverage gates.

Background (issue #1244): the workspace carries a very large test suite, spread
over ~130 `src/test_*.rs` modules in `contracts/ledgerlens-score/`, three
workspace crates of cross-contract integration tests, a few shell suites, and a
python suite.  There was no formal answer to three questions:

  1. Which tests are cheap enough to run on every push, and which are not?
  2. Does every test actually belong to some run, or can one quietly fall out of
     the suite (an undeclared `mod`, a file nobody wired up)?
  3. If a test moves from the cheap tier to the slow one, did anybody mean it?

This tool answers all three from one machine-readable manifest,
`test-tiers.toml` at the repository root:

  * every discovered test *unit* (a `#[cfg(test)]` module of a library target,
    an integration-test target, a doctest target, a shell/python suite, or a
    stray test file that belongs to no cargo target) is classified with a
    `kind` (its purpose: smoke, unit, integration, property, stress, soak,
    mutation) and a `tier` (fast / standard / nightly / disabled);
  * every execution tier has an enforced wall-clock budget;
  * `check` fails when a discovered test is missing from the manifest, when a
    manifest entry no longer matches what is on disk, when a test is classified
    but unreachable from every execution tier, or when a tier move is not
    accompanied by a review annotation;
  * `plan` turns a tier into the exact list of commands to run, and `report`
    turns the measured run into the budget/tier-move report that CI publishes.

Tiers are a partition: a test unit belongs to exactly one execution tier, and
`fast ∪ standard ∪ nightly` is exactly the set of units that are actually
compiled and run.  `disabled` is a classification, not a run: it records a test
file that exists in the tree but is not wired into any cargo target, which
requires a reason.  Pinning that state is the point — 81 of the 138
test-carrying modules in `contracts/ledgerlens-score/src/` are not declared in
`src/lib.rs`, and 87 units across the workspace have never executed, so their
tests never run.  The manifest records each of them explicitly, so the next
person to add `mod test_foo;` is forced to classify the newly-live suite, and
un-wiring a suite again fails CI.

Usage:
    python3 tools/test-tiering.py check                 # gate: manifest is complete and consistent
    python3 tools/test-tiering.py check --base main     # ... and no tier move since `main` is unreviewed
    python3 tools/test-tiering.py check --selftest      # ... and prove this checker still detects a bad manifest
    python3 tools/test-tiering.py bootstrap --write     # re-generate the unit section of the manifest
    python3 tools/test-tiering.py plan --tier fast      # print the commands for one tier
    python3 tools/test-tiering.py report --tier fast --results <tsv> --output <md>
    python3 tools/test-tiering.py verify-runtime --tier fast --results <tsv>
    python3 tools/test-tiering.py selftest              # unit tests for this checker

`scripts/test-tier.sh` is the developer-facing wrapper around `plan` + `report`;
see `docs/test-tiers.md`.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:  # Python 3.11+ ships a TOML reader in the standard library.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - environment guard
    sys.exit(
        "test-tiers: python3 >= 3.11 is required (tomllib is missing).\n"
        "            CI runners and the documented dev setup both satisfy this."
    )

SCHEMA_VERSION = 1
MANIFEST_NAME = "test-tiers.toml"

# The execution tiers, in increasing order of cost. `nightly` is the catch-all:
# it executes everything the cheaper tiers do not.
EXECUTION_TIERS = ("fast", "standard", "nightly")

# Classification states a unit can be in. `disabled` is not a run: it is a test
# file that is present in the tree but not compiled by any cargo target.
STATES = EXECUTION_TIERS + ("disabled",)

# Purpose taxonomy, mapped to the tier a purpose is expected to cost. This
# table is the *fallback* used by `bootstrap`; the manifest's own [kinds] table
# is authoritative at check time (and `selftest` pins the two together).
KIND_DEFAULT_TIER = {
    "smoke": "fast",
    "unit": "fast",
    "integration": "standard",
    "property": "standard",
    "stress": "nightly",
    "soak": "nightly",
    "mutation": "nightly",
}

# Units whose purpose is to be the kill-suite of a cargo-mutants shard. Every
# other shard profile's kill-suite is recorded through `mutation_shards`
# instead, because those modules are ordinary behavioural tests that happen to
# be the ones cargo-mutants re-runs.
MUTATION_KIND_UNITS = {
    "contracts/ledgerlens-score/src/test_verkle.rs",
    "contracts/ledgerlens-score/src/test_zk_range_proof.rs",
}

# Units that are the "if this breaks, stop" set: the core entry-point surface,
# the advertised interface, the error surface, and the executable invariants.
# Small, cheap, and on the critical path of every behavioural change.
SMOKE_UNITS = {
    "contracts/ledgerlens-score/src/test.rs",
    "contracts/ledgerlens-score/src/test_interface.rs",
    "contracts/ledgerlens-score/src/test_public_error_snapshots.rs",
    "contracts/ledgerlens-score/src/test_invariants.rs",
    "contracts/ledgerlens-score/src/test_fail_closed_invariants.rs",
    "contracts/ledgerlens-score/src/test_slo_defaults.rs",
    "contracts/ledgerlens-score/src/test_incident_event_topics.rs",
    "tests/test_tiers.test.sh",
}

# Bootstrap classification rules, most specific first: the first rule whose
# needle appears in the unit id wins. The rules are deliberately coarse — the
# output is meant to be hand-reviewed (see `docs/test-tiers.md`
# § Classification workflow), and every classification the heuristic is unsure
# about is meant to be corrected by a human with a `reason` recorded next to it.
KIND_RULES = (
    # Randomised / generated-input suites that assert a property over many
    # cases rather than a fixed example.
    ("property", ("chaos", "fuzz", "adversarial_sequences", "aggregate_invariants",
                  "monotonicity", "bounded_drift", "iqr_rejection", "gdpr_accumulator",
                  "cluster_boundaries", "volatility", "property", "invariants")),
    # Resource-exhaustion and hostile-input suites: they assert bounded work.
    ("stress", ("dos_", "memory_exhaustion", "panic_guards", "high_cardinality",
                "watchlist", "malformed_proof", "corpus", "stress", "worst_case",
                "exhaustion")),
    # Long-horizon suites: many ledger timestamps / TTL renewals / history depth.
    ("soak", ("ttl_rent_manager", "history", "epoch", "decay", "soak",
              "long_running", "regression_history")),
    # Multi-contract / lifecycle suites: more than one contract, or the
    # deployment lifecycle itself.
    ("integration", ("composability", "conformance", "replay", "deploy",
                     "consensus", "failover", "multisig", "cross_contract",
                     "sdk_", "lifecycle", "attestation_domain")),
    # Contract governance and upgrade paths are cross-entry-point flows rather
    # than single-function unit tests.
    ("integration", ("upgrade", "parameter_governance", "param_timelock",
                     "governance", "escrow", "dispute", "embargo")),
)

# Cost signals used to bump a unit one tier up during bootstrap. A unit's tier
# is set by the cost of its slowest test, not its fastest: a 1000-iteration
# fuzz loop inside an otherwise cheap module still makes the module expensive.
HEAVY_LOOP_MIN = 500
MODERATE_LOOP_MIN = 200
HEAVY_CONST_MIN = 1000
MODERATE_CONST_MIN = 200
# A constant is only a cost signal when its *name* says it counts something.
# Without this filter every `const START_TS: u64 = 1_700_000_000;` and every
# `const DAY: u64 = 86_400;` in the suite looks like a billion-case property
# test, and the bootstrap demotes half the crate to `nightly`.
COUNT_NAME_RE = re.compile(
    r"const\s+[A-Z0-9_]*(?:COUNT|CASES|CASE_COUNT|SEEDS|ROUNDS|SEQUENCES|ITERATIONS"
    r"|SAMPLES|ATTEMPTS|CYCLES|TRIALS|REPEATS|ROUNDS_PER_[A-Z_]+)\s*:\s*u\w+\s*=\s*([0-9_]+)")
# A "case count" above this is a configuration value that happens to look like
# a count (a nanosecond epoch, a TTL in seconds), not an iteration bound.
PLAUSIBLE_CASE_MAX = 1_000_000

LOOP_RE = re.compile(r"for\s+_\s+in\s+0\.\.([0-9_]+)")
TEST_MARKER_RE = re.compile(r"#\[(test|cfg\(test\)|test_case)")
DOC_FENCE_RE = re.compile(r"^\s*(///|//!).*```")
MOD_DECL_RE = re.compile(r"^(\s*)(//\s*)?(pub\s+)?mod\s+(\w+)\s*;", re.M)


# ── small data model ──────────────────────────────────────────────────────────


class Unit:
    """One classified test unit."""

    def __init__(self, uid: str, runner: str, wired: bool, package: str | None = None,
                 command: str | None = None, tests: int = 0, note: str = "",
                 target: str = "lib") -> None:
        self.id = uid
        self.runner = runner          # "cargo" | "script"
        self.wired = wired            # compiled and executed by some cargo target
        self.package = package
        self.command = command        # set for runner == "script"
        self.tests = tests            # #[test] count, for reporting only
        self.note = note              # discovery evidence (why it is/isn't wired)
        self.kind: str | None = None
        self.tier: str | None = None
        self.reason: str | None = None
        self.review_note = ""         # why a human overrode the bootstrap proposal
        self.reviewed: dict | None = None
        self.mutation_shards: list[str] = []
        # Which cargo target runs this unit: "lib" for a `#[cfg(test)] mod` inside
        # src/, "test" for a `tests/<name>.rs` integration target, "doc" for
        # doctests. The three need different cargo invocations.
        self.target = target

    @property
    def stem(self) -> str:
        return Path(self.id.split("::")[0]).stem


class Benchmark:
    """A criterion benchmark: classified, but deliberately outside the tiers."""

    def __init__(self, uid: str, package: str, target: str) -> None:
        self.id = uid
        self.package = package
        self.target = target
        self.kind: str | None = None
        self.reason: str | None = None
        self.command: str | None = None


class TieringError(Exception):
    """A problem with the invocation itself, rather than with the manifest."""


class Violation:
    def __init__(self, code: str, message: str, hint: str = "") -> None:
        self.code = code
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        out = f"  [{self.code}] {self.message}"
        if self.hint:
            out += f"\n        {self.hint}"
        return out


# ── manifest IO ───────────────────────────────────────────────────────────────


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_manifest(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def units_from_manifest(manifest: dict) -> list[Unit]:
    units = []
    for entry in manifest.get("unit", []):
        unit = Unit(entry["id"], entry.get("runner", "cargo"), wired=True)
        unit.kind = entry.get("kind")
        unit.tier = entry.get("tier")
        unit.reason = entry.get("reason")
        unit.review_note = entry.get("note", "")
        unit.reviewed = entry.get("reviewed")
        unit.mutation_shards = list(entry.get("mutation_shards", []))
        unit.command = entry.get("command")
        units.append(unit)
    return units


def benchmarks_from_manifest(manifest: dict) -> list[Benchmark]:
    benches = []
    for entry in manifest.get("benchmark", []):
        bench = Benchmark(entry["id"], entry.get("package", ""), entry.get("target", ""))
        bench.kind = entry.get("kind")
        bench.reason = entry.get("reason")
        bench.command = entry.get("command")
        benches.append(bench)
    return benches


# ── discovery ─────────────────────────────────────────────────────────────────


def workspace_packages(root: Path) -> list[tuple[str, Path]]:
    """Return [(package name, directory)] for every workspace member."""
    workspace = load_manifest(root / "Cargo.toml").get("workspace", {})
    packages = []
    for member in workspace.get("members", []):
        directory = (root / member).resolve()
        manifest_path = directory / "Cargo.toml"
        if not manifest_path.is_file():
            continue
        name = load_manifest(manifest_path).get("package", {}).get("name")
        if name:
            packages.append((name, directory))
    return packages


def mutation_shard_map(root: Path) -> dict[str, list[str]]:
    """Map test-module stem -> sorted shard names from `.cargo/mutants.toml`.

    `additional_cargo_args` in a shard profile names the test targets the
    mutated code is checked against, e.g. `--test test_cooldown`. Those names
    are the mutation kill-suite; the manifest records the cross-reference so
    the two files cannot drift apart silently.
    """
    path = root / ".cargo" / "mutants.toml"
    if not path.is_file():
        return {}
    mapping: dict[str, set[str]] = {}
    shard = None
    for line in path.read_text().splitlines():
        profile = re.match(r"^\[profile\.([\w-]+)\]", line)
        if profile:
            shard = profile.group(1)
            continue
        target = re.match(r'^\s*"--test",?\s*"([\w-]+)",?\s*$', line)
        if target and shard:
            mapping.setdefault(target.group(1), set()).add(shard)
    return {stem: sorted(shards) for stem, shards in mapping.items()}


def declared_modules(root_file: Path) -> tuple[set[str], set[str]]:
    """Return (declared, commented_out) module stems of a crate root file."""
    if not root_file.is_file():
        return set(), set()
    declared, commented = set(), set()
    for match in MOD_DECL_RE.finditer(root_file.read_text()):
        stem, is_comment = match.group(4), bool(match.group(2))
        (commented if is_comment else declared).add(stem)
    return declared, commented


def cost_signals_for(path: Path) -> list[str]:
    """Cost signals from a *Rust* source file, and nothing for anything else.

    The patterns behind `cost_signals` are Rust syntax (`for _ in 0..N`,
    `const N_CASES: usize`), so a shell or Python suite should not be scanned
    for them: a shell test that *writes* a Rust fixture full of loop bounds is
    not itself a slow test.
    """
    if path.suffix != ".rs" or not path.is_file():
        return []
    return cost_signals(path.read_text())


def cost_signals(text: str) -> list[str]:
    """Return heavy/moderate cost signals found in a test module's source.

    These are the objective inputs the bootstrap uses to bump a unit one tier
    up: a module that runs thousands of contract calls in a single test costs
    far more wall-clock than its test count suggests, and the budget is spent
    on the slowest test in the module.
    """
    signals = []
    for number, line in enumerate(text.splitlines(), start=1):
        loop = LOOP_RE.search(line)
        if loop:
            bound = int(loop.group(1).replace("_", ""))
            if bound >= MODERATE_LOOP_MIN:
                signals.append((bound, f"loop bound {bound} at line {number}"))
        const = COUNT_NAME_RE.search(line)
        if const:
            bound = int(const.group(1).replace("_", ""))
            if MODERATE_CONST_MIN <= bound <= PLAUSIBLE_CASE_MAX:
                signals.append((bound, f"case count {bound} at line {number}"))
    return [text for _, text in signals]


def is_heavy(signals: list[str]) -> bool:
    heavy = 0
    for signal in signals:
        bound = int(re.search(r"(\d+)", signal).group(1))
        if bound >= HEAVY_LOOP_MIN or bound >= HEAVY_CONST_MIN:
            heavy += 1
        if heavy >= 1:
            return True
    return False


def discover(root: Path) -> list[Unit]:
    """Find every test unit in the tree, wired or not."""
    def rel(path: Path) -> str:
        return str(path.relative_to(root))

    units: list[Unit] = []
    for package, directory in workspace_packages(root):
        src = directory / "src"
        if src.is_dir():
            crate_root = src / "lib.rs"
            declared, commented = declared_modules(crate_root) if crate_root.is_file() else (set(), set())
            for path in sorted(src.glob("*.rs")):
                if path.name in ("lib.rs", "main.rs", "build.rs"):
                    continue
                text = path.read_text()
                if not TEST_MARKER_RE.search(text):
                    continue
                stem = path.stem
                if stem in declared:
                    wired, note = True, "declared in src/lib.rs"
                elif stem in commented:
                    wired, note = False, "mod declaration is commented out in src/lib.rs"
                else:
                    wired, note = False, "no mod declaration in src/lib.rs"
                units.append(Unit(rel(path), "cargo", wired, package,
                                  tests=text.count("#[test]"), note=note))
            # Doctests are a target of their own: every doc example is compiled
            # and run as an individual test. A package opts out with
            # `[lib] doctest = false`; one with no examples costs nothing to
            # record but a real `none`, so only claim the unit when a doc
            # comment actually contains a fenced example.
            lib = load_manifest(directory / "Cargo.toml").get("lib", {})
            if lib.get("doctest", True):
                fences = any(
                    DOC_FENCE_RE.match(line)
                    for path in src.glob("*.rs")
                    for line in path.read_text(errors="ignore").splitlines()
                )
                if fences:
                    units.append(Unit(f"{rel(directory)}::doctests", "cargo", True, package,
                                      note="doc-comment examples", target="doc"))
        tests_dir = directory / "tests"
        if tests_dir.is_dir():
            for path in sorted(tests_dir.glob("*.rs")):
                units.append(Unit(rel(path), "cargo", True, package,
                                  tests=path.read_text().count("#[test]"),
                                  note="integration test target", target="test"))
    units.extend(discover_script_suites(root))
    units.extend(discover_strays(root))
    units.sort(key=lambda unit: unit.id)
    return units


def discover_script_suites(root: Path) -> list[Unit]:
    """Shell and python suites. Their command is recorded in the manifest.

    Discovery rule: everything under `tests/` is a test, whatever it is called
    (`tests/deploy_script_rpc_failures.sh` is a failure-mode suite even though
    it does not say "test" in its name), while outside `tests/` only the
    explicit opt-in patterns count. Checkers live in `tools/` and are not tests.
    """
    patterns = (
        "tests/*.sh", "tests/*.py", "tests/**/*.sh", "tests/**/*.py",
        "scripts/*.test.sh", "tools/*.test.sh", "tools/**/*.test.sh", "tools/**/test_*.py",
    )
    seen: dict[str, Unit] = {}
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if not path.is_file() or path.name == "__init__.py":
                continue
            uid = str(path.relative_to(root))
            if uid in seen:
                continue
            text = path.read_text(errors="ignore")
            count = len(re.findall(r"^\s*def test_", text, re.M))
            count += len(re.findall(r"^test_", text, re.M))
            seen[uid] = Unit(uid, "script", True, None, tests=count,
                             note="shell/python suite")
    return list(seen.values())


def discover_strays(root: Path) -> list[Unit]:
    """Rust test files under tests/ that belong to no cargo target.

    Only a direct `tests/<name>.rs` file is a target; a file inside a
    subdirectory of a package's `tests/` dir (e.g. `tests/common/mod.rs`) is
    a support module compiled *into* another target, and a crate root is not a
    test unit. Recording either as a unit would ask the runner to execute
    targets that do not exist.
    """
    strays = []
    owned = set()
    package_dirs = []
    for _, directory in workspace_packages(root):
        package_dirs.append(directory)
        tests_dir = directory / "tests"
        if tests_dir.is_dir():
            owned.update(path.resolve() for path in tests_dir.glob("*.rs"))
    for path in sorted((root / "tests").rglob("*.rs")):
        if path.resolve() in owned:
            continue
        if path.name in ("lib.rs", "main.rs", "mod.rs"):
            continue  # a crate root or module file, never a target of its own
        if any(directory in path.resolve().parents for directory in package_dirs):
            # Inside a package: either a support module of another target, or a
            # src module of the package's library.
            continue
        if not TEST_MARKER_RE.search(path.read_text(errors="ignore")):
            continue
        strays.append(Unit(str(path.relative_to(root)), "cargo", False, None,
                           tests=path.read_text(errors="ignore").count("#[test]"),
                           note="under tests/ but inside no cargo package target"))
    return strays


def discover_benchmarks(root: Path) -> list[Benchmark]:
    benches = []
    for package, directory in workspace_packages(root):
        benches_dir = directory / "benches"
        if not benches_dir.is_dir():
            continue
        for path in sorted(benches_dir.glob("*.rs")):
            bench = Benchmark(str(path.relative_to(root)), package, path.stem)
            bench.command = f"cargo bench -p {package} --bench {path.stem}"
            benches.append(bench)
    return benches


# ── bootstrap: classification heuristics ──────────────────────────────────────


def classify(unit: Unit, root: Path) -> tuple[str, str, str | None]:
    """Return (kind, tier, reason) for a freshly discovered unit."""
    stem = unit.stem
    lowered = unit.id.lower()
    if unit.id in MUTATION_KIND_UNITS:
        kind = "mutation"
    elif unit.id in SMOKE_UNITS:
        kind = "smoke"
    else:
        kind = "unit"
        for candidate, needles in KIND_RULES:
            if any(needle in lowered for needle in needles):
                kind = candidate
                break
        if unit.runner == "script" and "deploy" in lowered:
            kind = "integration"
        if "::doctests" in unit.id:
            kind = "smoke"
    tier = KIND_DEFAULT_TIER[kind]
    reason = None
    if not unit.wired:
        return kind, "disabled", f"not wired: {unit.note}"
    signals = cost_signals_for(root / unit.id)
    if signals and tier in EXECUTION_TIERS:
        bump = {v: i for i, v in enumerate(EXECUTION_TIERS)}
        target = EXECUTION_TIERS[min(bump[tier] + 1, len(EXECUTION_TIERS) - 1)]
        if is_heavy(signals):
            reason = f"cost signal: {', '.join(signals[:3])}"
            tier = target
    if kind == "smoke" and "::doctests" in unit.id and tier == "fast":
        tier = "standard"
        reason = "each doc example compiles as its own test binary"
    return kind, tier, reason


def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{k} = {toml_value(v)}" for k, v in value.items())
        return "{ " + inner + " }"
    return json.dumps(str(value))


def render_manifest(root: Path, manifest: dict, units: list[Unit], benches: list[Benchmark]) -> str:
    """Re-render the manifest, preserving the hand-written header verbatim.

    Everything from the first `[[unit]]` onwards is machine-written, whether or
    not the generated marker is present: a manifest that has lost its marker must
    be repaired, not have its whole inventory duplicated underneath a second copy
    of itself.
    """
    path = root / MANIFEST_NAME
    original = path.read_text()
    if GENERATED_MARKER in original:
        head = original.split(GENERATED_MARKER)[0].rstrip("\n")
    else:
        head = re.split(r"^\[\[(?:unit|benchmark)\]\]$", original, maxsplit=1,
                        flags=re.M)[0].rstrip("\n")
    lines = [head, "", GENERATED_MARKER, ""]
    for unit in units:
        lines.append("[[unit]]")
        lines.append(f"id = {toml_value(unit.id)}")
        lines.append(f"kind = {toml_value(unit.kind)}")
        lines.append(f"tier = {toml_value(unit.tier)}")
        if unit.mutation_shards:
            lines.append(f"mutation_shards = {toml_value(unit.mutation_shards)}")
        if unit.command:
            lines.append(f"command = {toml_value(unit.command)}")
        if unit.reason:
            lines.append(f"reason = {toml_value(unit.reason)}")
        if unit.review_note:
            lines.append(f"note = {toml_value(unit.review_note)}")
        if unit.reviewed:
            lines.append(f"reviewed = {toml_value(unit.reviewed)}")
        lines.append("")
    for bench in benches:
        lines.append("[[benchmark]]")
        lines.append(f"id = {toml_value(bench.id)}")
        lines.append(f"kind = {toml_value(bench.kind)}")
        lines.append(f"command = {toml_value(bench.command)}")
        if bench.reason:
            lines.append(f"reason = {toml_value(bench.reason)}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


GENERATED_MARKER = "# ── BEGIN GENERATED: unit inventory (tools/test-tiering.py bootstrap) ──"


def bootstrap(root: Path, write: bool) -> int:
    path = root / MANIFEST_NAME
    if not path.is_file():
        sys.exit(f"test-tiers: {path} not found; nothing to bootstrap from")
    existing = {unit.id: unit for unit in units_from_manifest(load_manifest(path))}
    existing_bench = {bench.id: bench for bench in benchmarks_from_manifest(load_manifest(path))}
    shards = mutation_shard_map(root)
    units = discover(root)
    for unit in units:
        prior = existing.get(unit.id)
        if prior is not None:
            # Hand-reviewed fields always win over the heuristics. `runner` and
            # the wired state are discovered, never carried over: they are
            # properties of the tree, not of the manifest.
            unit.kind = prior.kind
            unit.tier = prior.tier
            unit.reason = prior.reason
            unit.review_note = prior.review_note
            unit.reviewed = prior.reviewed
            unit.mutation_shards = prior.mutation_shards
            if unit.runner == "script" and prior.command:
                unit.command = prior.command
        else:
            unit.kind, unit.tier, unit.reason = classify(unit, root)
        if not unit.mutation_shards:
            unit.mutation_shards = shards.get(unit.stem, [])
        if unit.runner == "script" and not unit.command:
            unit.command = default_script_command(unit.id)
    benches = discover_benchmarks(root)
    for bench in benches:
        prior = existing_bench.get(bench.id)
        if prior is not None:
            bench.kind, bench.reason = prior.kind, prior.reason
        else:
            bench.kind = "stress"
            bench.reason = ("criterion measurement rather than an assertion: classified but "
                            "deliberately outside the tier partition (docs/test-tiers.md)")
    rendered = render_manifest(root, load_manifest(path), units, benches)
    if write:
        path.write_text(rendered)
        print(f"test-tiers: wrote {path} ({len(units)} units, {len(benches)} benchmarks)")
        return 0
    if rendered != path.read_text():
        print("test-tiers: manifest unit inventory is stale")
        print("          run: python3 tools/test-tiering.py bootstrap --write")
        return 1
    print("test-tiers: manifest unit inventory is up to date")
    return 0


def default_script_command(uid: str) -> str:
    if uid.endswith(".py"):
        return f"python3 -m unittest discover -s {Path(uid).parent} -p '{Path(uid).name}'"
    return f"bash {uid}"


# ── the gate ──────────────────────────────────────────────────────────────────


def hydrate(root: Path, units: list[Unit], discovered: dict[str, Unit]) -> None:
    """Take the tree-derived facts about a unit from discovery, not the manifest.

    `runner`, the wired state, the owning package and the test count are
    properties of the repository, so they are always re-derived from the tree:
    the manifest is not allowed to disagree with reality about them, and a
    hand-edited manifest that gets them wrong still plans the right command.
    """
    for unit in units:
        found = discovered.get(unit.id)
        if found is None:
            continue
        unit.runner = found.runner
        unit.wired = found.wired
        unit.package = found.package
        unit.tests = found.tests
        unit.note = found.note or unit.note
        unit.target = found.target


def validate(root: Path, manifest: dict, units: list[Unit],
             benches: list[Benchmark]) -> tuple[list[Violation], list[str]]:
    """Return (violations, warnings).

    Violations fail CI. Warnings are surfaced loudly but do not fail: they mark
    pre-existing defects this manifest now makes *visible* (a cargo-mutants
    kill-suite that no cargo target compiles, a fast-tier unit that carries a
    heavy cost signal) and are tracked as follow-ups rather than blocking a PR
    that did not cause them.
    """
    problems: list[Violation] = []
    warnings: list[str] = []
    discovered = {unit.id: unit for unit in discover(root)}

    if manifest.get("schema_version") != SCHEMA_VERSION:
        problems.append(Violation(
            "schema", f"schema_version must be {SCHEMA_VERSION}, manifest says "
                      f"{manifest.get('schema_version')!r}"))

    tiers = manifest.get("tiers", {})
    for tier in EXECUTION_TIERS:
        table = tiers.get(tier)
        if not isinstance(table, dict):
            problems.append(Violation("tiers", f"missing [tiers.{tier}] table"))
            continue
        for key in ("description", "command", "execution_budget_seconds", "build_budget_seconds"):
            if key not in table:
                problems.append(Violation("tiers", f"[tiers.{tier}] is missing `{key}`"))
        runner_script = str(table.get("command", "")).split()
        if runner_script and not (root / runner_script[0]).is_file():
            problems.append(Violation("tiers", f"[tiers.{tier}].command points at a missing "
                                               f"script: {runner_script[0]}"))
    budgets = [tiers.get(t, {}).get("execution_budget_seconds", 0) for t in EXECUTION_TIERS]
    if any(b <= 0 for b in budgets):
        problems.append(Violation("budget", "every tier needs a positive execution_budget_seconds"))
    elif not budgets == sorted(budgets):
        problems.append(Violation("budget",
                                  f"execution budgets must increase with cost, got {budgets}"))
    if "disabled" in tiers:
        problems.append(Violation("tiers", "`disabled` is a classification state, not a tier; "
                                           "remove [tiers.disabled]"))

    kinds = manifest.get("kinds", {})
    for kind in KIND_DEFAULT_TIER:
        table = kinds.get(kind)
        if not isinstance(table, dict):
            problems.append(Violation("kinds", f"missing [kinds.{kind}] table"))
            continue
        if "description" not in table or "default_tier" not in table:
            problems.append(Violation("kinds", f"[kinds.{kind}] needs `description` and "
                                               f"`default_tier`"))
        elif table["default_tier"] not in STATES:
            problems.append(Violation("kinds", f"[kinds.{kind}].default_tier is not a tier: "
                                               f"{table['default_tier']!r}"))
    for kind in kinds:
        if kind not in KIND_DEFAULT_TIER:
            problems.append(Violation("kinds", f"unknown kind in [kinds.{kind}]"))

    # ── per-unit validation ────────────────────────────────────────────────────
    hydrate(root, units, discovered)
    seen: dict[str, Unit] = {}
    for unit in units:
        if unit.id in seen:
            problems.append(Violation("duplicate", f"unit {unit.id} is listed twice"))
            continue
        seen[unit.id] = unit
        if unit.kind not in KIND_DEFAULT_TIER:
            problems.append(Violation("kind", f"{unit.id}: kind {unit.kind!r} is not one of "
                                              f"{sorted(KIND_DEFAULT_TIER)}"))
        if unit.tier not in STATES:
            problems.append(Violation("tier", f"{unit.id}: tier {unit.tier!r} is not one of "
                                              f"{list(STATES)}"))
        if unit.runner == "script":
            if not unit.command:
                problems.append(Violation("command", f"{unit.id}: script unit needs a `command`"))
        elif unit.command:
            problems.append(Violation("command", f"{unit.id}: only script units may set `command`"))
        if unit.reviewed is not None:
            missing = [k for k in ("by", "on") if k not in unit.reviewed]
            if missing:
                problems.append(Violation("review", f"{unit.id}: reviewed is missing {missing}"))
        if unit.tier == "disabled" and not unit.reason:
            problems.append(Violation("disabled", f"{unit.id}: a disabled unit must say why"))
        default = kinds.get(unit.kind, {}).get("default_tier")
        if default and unit.tier in EXECUTION_TIERS and unit.tier != default and not unit.reason:
            problems.append(Violation("tier", f"{unit.id}: kind {unit.kind} defaults to {default} "
                                               f"but the unit is {unit.tier} without a `reason`"))

    # ── discovery reconciliation (a test omitted from the manifest fails here) ──
    for uid in sorted(set(discovered) - set(seen)):
        problems.append(Violation("omitted", f"test unit {uid} is not classified in "
                                             f"{MANIFEST_NAME} ({discovered[uid].note})",
                                  hint="run: python3 tools/test-tiering.py bootstrap --write, then "
                                       "review the generated classification"))
    for uid in sorted(set(seen) - set(discovered)):
        problems.append(Violation("stale", f"{uid} is classified but no such test unit exists"))

    # ── wiring state: the manifest pins which files a cargo target compiles ────
    for uid in sorted(set(seen) & set(discovered)):
        wired, tier = discovered[uid].wired, seen[uid].tier
        if wired and tier == "disabled":
            problems.append(Violation("disabled", f"{uid}: a cargo target compiles this file, so "
                                                  f"it cannot be classified disabled"))
        if not wired and tier != "disabled":
            problems.append(Violation("wired", f"{uid}: {discovered[uid].note}, so it is not "
                                               f"compiled by any cargo target, but it is "
                                               f"classified {tier!r}",
                                      hint="add the `mod` declaration and classify the suite, or "
                                           "record it as disabled with a reason"))

    # ── coverage of tiers: nothing may be unreachable from every tier ─────────
    for tier in EXECUTION_TIERS:
        if not [unit for unit in units if unit.tier == tier]:
            problems.append(Violation("coverage", f"tier {tier} is empty; every tier must have "
                                                  f"at least one unit"))
    runnable = {unit.id for unit in discovered.values() if unit.wired}
    covered = {unit.id for unit in units if unit.tier in EXECUTION_TIERS}
    for uid in sorted(runnable - covered):
        problems.append(Violation("coverage", f"{uid} is compiled by a cargo target but no "
                                              f"execution tier runs it"))
    for uid in sorted(covered - runnable):
        problems.append(Violation("coverage", f"{uid} is assigned to a tier but is not compiled "
                                              f"by any cargo target"))

    # ── cargo-mutants kill-suite cross-reference ───────────────────────────────
    shards = mutation_shard_map(root)
    for stem, shard_names in sorted(shards.items()):
        matches = [unit for unit in units if unit.stem == stem]
        if not matches:
            problems.append(Violation("mutation", f".cargo/mutants.toml shard(s) "
                                                  f"{', '.join(shard_names)} run `--test {stem}`, "
                                                  f"which is not in the manifest"))
            continue
        for unit in matches:
            if not unit.mutation_shards:
                problems.append(Violation("mutation", f"{unit.id} is a cargo-mutants kill-suite "
                                                      f"for {', '.join(shard_names)} but records no "
                                                      f"`mutation_shards`"))
            if not unit.wired:
                # Pre-existing: these modules are not declared in src/lib.rs, so
                # `cargo mutants --test <name>` has no target to run. Visible
                # here rather than fixed, because fixing it means deciding
                # whether the suite still compiles at all.
                warnings.append(f"{unit.id} is a cargo-mutants kill-suite for "
                                f"{', '.join(shard_names)} but no cargo target compiles it, so "
                                f"those mutants cannot be killed by it "
                                f"(see docs/test-tiers.md § Disabled inventory)")
    for unit in units:
        if unit.mutation_shards and unit.stem not in shards:
            problems.append(Violation("mutation", f"{unit.id} claims mutation_shards "
                                                  f"{unit.mutation_shards} that "
                                                  f".cargo/mutants.toml does not define"))
    for unit in units:
        if unit.tier == "fast" and (root / unit.id.split("::")[0]).is_file():
            signals = cost_signals_for(root / unit.id.split("::")[0])
            if is_heavy(signals):
                warnings.append(f"{unit.id} is in the fast tier but carries a heavy cost signal "
                                f"({', '.join(signals[:2])}); if it pushes the fast budget over, "
                                f"move it to `standard` in the same change")

    # ── benchmarks: classified, but outside the tiers by policy ───────────────
    declared_benches = {bench.id: bench for bench in benches}
    for bench in discover_benchmarks(root):
        if bench.id not in declared_benches:
            problems.append(Violation("benchmark", f"benchmark {bench.id} is not classified"))
            continue
        entry = declared_benches[bench.id]
        if entry.kind not in KIND_DEFAULT_TIER:
            problems.append(Violation("benchmark", f"{bench.id}: kind {entry.kind!r} is not a "
                                                  f"known kind"))
        if not entry.command:
            problems.append(Violation("benchmark", f"{bench.id}: needs a `command`"))
        if not entry.reason:
            problems.append(Violation("benchmark", f"{bench.id}: needs a `reason` (why it is "
                                                  f"outside the tiers)"))
    for uid in sorted(set(declared_benches) - {bench.id for bench in discover_benchmarks(root)}):
        problems.append(Violation("benchmark", f"{uid} is classified but no such benchmark exists"))
    return problems, warnings


def load_base_manifest(base: str) -> dict | None:
    """Load the manifest as of `base` (a git rev, or a path).

    Returns None when the revision exists but has no manifest — the inventory is
    new in this change, so there is nothing to diff. A *bad* revision is an
    error, not a silent pass: an unresolvable `--base` would otherwise turn the
    review gate off without anyone noticing.
    """
    if not base.strip():
        # `--base ""` happens when a caller forwards an unset CI variable. It
        # must not silently mean "no base": that would turn the review gate off.
        raise TieringError("--base was empty. Omit the flag to skip the review diff; "
                           "a CI caller that resolves an empty base SHA should not pass it")
    if "/" in base or base.endswith(".toml") or Path(base).is_file():
        if not Path(base).is_file():
            raise TieringError(f"--base {base!r} is not a file and not a git revision")
        return load_manifest(Path(base))
    try:
        done = subprocess.run(["git", "show", f"{base}:{MANIFEST_NAME}"],
                              capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        stderr = getattr(error, "stderr", b"").decode().strip()
        if "does not exist in" in stderr or "exists on disk, but not in" in stderr:
            # The revision is fine; the inventory is new in this change, so there
            # is no prior classification to hold this one to.
            print(f"test-tiers: {MANIFEST_NAME} does not exist at {base!r} — the inventory is "
                  f"new here, so there are no prior tier moves to review.")
            return None
        raise TieringError(
            f"--base {base!r} is not a readable git revision ({stderr or error}). In CI this is "
            f"usually a shallow clone: fetch the base commit (`actions/checkout` with "
            f"`fetch-depth: 0`) or pass a manifest path."
        ) from error
    return tomllib.loads(done.stdout.decode())


def review_moves(current: dict, base: dict | None) -> tuple[list[str], list[str]]:
    """Return (unreviewed tier moves, reviewed tier moves) between two manifests."""
    if base is None:
        return [], []
    now = {unit["id"]: unit for unit in current.get("unit", [])}
    before = {unit["id"]: unit for unit in base.get("unit", [])}
    unreviewed, reviewed = [], []
    for uid in sorted(set(now) & set(before)):
        old, new = before[uid], now[uid]
        if old.get("tier") == new.get("tier") and old.get("kind") == new.get("kind"):
            continue
        change = f"{old.get('kind')}/{old.get('tier')} -> {new.get('kind')}/{new.get('tier')}"
        row = f"{uid}: {change}"
        (reviewed if new.get("reviewed") else unreviewed).append(row)
    for tier, table in current.get("tiers", {}).items():
        old_table = base.get("tiers", {}).get(tier)
        if not old_table or not isinstance(table, dict):
            continue
        changed = [key for key, value in table.items()
                   if key != "reviewed" and old_table.get(key) != value]
        if not changed:
            continue
        row = f"[tiers.{tier}]: {', '.join(sorted(changed))}"
        (reviewed if table.get("reviewed") else unreviewed).append(row)
    return unreviewed, reviewed


def check(root: Path, base: str | None, run_selftest: bool) -> int:
    path = root / MANIFEST_NAME
    if not path.is_file():
        print(f"test-tiers: {path} is missing")
        return 1
    manifest = load_manifest(path)
    units = units_from_manifest(manifest)
    benches = benchmarks_from_manifest(manifest)
    problems, warnings = validate(root, manifest, units, benches)

    unreviewed: list[str] = []
    reviewed: list[str] = []
    if base is not None:
        unreviewed, reviewed = review_moves(manifest, load_base_manifest(base))

    if warnings:
        print(f"test-tiers: {len(warnings)} warning(s) — pre-existing issues this manifest "
              f"makes visible, tracked as follow-ups:")
        for warning in warnings:
            print(f"  [warning] {warning}")
        print("")
    if problems or unreviewed:
        print(f"test-tiers: FAIL ({MANIFEST_NAME})")
        for problem in problems:
            print(problem)
        for row in unreviewed:
            print(f"  [unreviewed-move] {row}")
            print("        record the decision on the unit: "
                  "reviewed = { by = \"…\", on = \"#<issue>\", note = \"…\" }")
        print("")
        print(f"  {len(units)} units classified, {len(problems) + len(unreviewed)} problem(s).")
        print("  See docs/test-tiers.md for the classification and review workflow.")
        return 1

    by_tier = {tier: [unit for unit in units if unit.tier == tier] for tier in EXECUTION_TIERS}
    print(f"test-tiers: OK — {len(units)} units classified, every test reachable from a tier.")
    for tier in EXECUTION_TIERS:
        table = manifest["tiers"][tier]
        count = len(by_tier[tier])
        tests = sum(unit.tests for unit in by_tier[tier])
        print(f"    {tier:<9} {count:>3} units / {tests:>4} tests   budget "
              f"{table['execution_budget_seconds']}s")
    disabled = [unit for unit in units if unit.tier == "disabled"]
    print(f"    {'disabled':<9} {len(disabled):>3} units   (present in the tree, not compiled)")
    print(f"    review    {len(reviewed)} tier move(s) recorded since base" if base else
          "    review    not evaluated (no --base)")
    for unit in disabled[:5]:
        print(f"      - {unit.id}: {unit.note}")
    if len(disabled) > 5:
        print(f"      … {len(disabled) - 5} more, all listed in {MANIFEST_NAME} "
              f"(docs/test-tiers.md § Disabled inventory)")
    if run_selftest:
        print("")
        if selftest() != 0:
            return 1
    return 0


# ── plan: turn a tier into commands ───────────────────────────────────────────


def nextest_available() -> bool:
    try:
        return subprocess.run(["cargo", "nextest", "--version"],
                              capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


def unit_command(unit: Unit, root: Path, use_nextest: bool, extra: list[str]) -> str:
    if unit.runner == "script":
        return " ".join([unit.command or "", *extra]).strip()
    package = unit.package or ""
    if unit.target == "doc":
        # cargo-nextest does not run doctests; always use the built-in harness.
        return " ".join(["cargo", "test", "-p", package, "--doc", *extra])
    if unit.target == "test":
        # A `tests/<name>.rs` file is a target of its own; its test names are not
        # namespaced by the file stem, so filter on the binary instead.
        if use_nextest:
            return " ".join(["cargo", "nextest", "run", "-p", package,
                             "-E", shlex.quote(f"binary({unit.stem})"), *extra])
        return " ".join(["cargo", "test", "-p", package, "--test", unit.stem, *extra])
    if use_nextest:
        return " ".join(["cargo", "nextest", "run", "-p", package,
                         "-E", shlex.quote(f"test(/^{re.escape(unit.stem)}::/)"), *extra])
    return " ".join(["cargo", "test", "-p", package, "--lib",
                     shlex.quote(f"{unit.stem}::"), *extra])


def plan(root: Path, tier: str, extra: list[str], as_json: bool) -> int:
    if tier not in EXECUTION_TIERS:
        print(f"test-tiers: unknown tier {tier!r}; expected one of {list(EXECUTION_TIERS)}",
              file=sys.stderr)
        return 2
    manifest = load_manifest(root / MANIFEST_NAME)
    units = [unit for unit in units_from_manifest(manifest) if unit.tier == tier]
    hydrate(root, units, {unit.id: unit for unit in discover(root)})
    use_nextest = nextest_available()
    rows = []
    for unit in units:
        rows.append({
            "id": unit.id,
            "kind": unit.kind,
            "runner": unit.runner,
            "package": unit.package,
            "target": unit.target,
            "command": unit_command(unit, root, use_nextest, extra),
        })
    packages = sorted({row["package"] for row in rows if row["package"]})
    build = f"cargo test --no-run {' '.join('-p ' + name for name in packages)}".strip()
    if as_json:
        print(json.dumps({"tier": tier, "build": build, "units": rows}, indent=2))
        return 0
    print(f"# tier {tier}: {len(rows)} unit(s), provider "
          f"{'cargo-nextest' if use_nextest else 'cargo test'}")
    print(f"__build__\t{build}")
    for row in rows:
        print(f"{row['id']}\t{row['command']}")
    return 0


# ── report: budgets, results, and tier-move review ────────────────────────────


def read_results(path: Path) -> dict[str, dict]:
    results: dict[str, dict] = {}
    if not path.is_file():
        return results
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        uid, status, seconds = parts[0], parts[1], parts[2]
        try:
            duration = float(seconds)
        except ValueError:
            duration = 0.0
        results[uid] = {"status": status, "seconds": duration,
                        "log": parts[3] if len(parts) > 3 else ""}
    return results


def human_seconds(value: float) -> str:
    return f"{value:.1f}s" if value < 600 else f"{int(value // 60)}m{int(value % 60):02d}s"


# ── runtime verification: measured cost, not declared markers ─────────────────
#
# The tier a unit is *classified* into is a declaration: it comes from the
# manifest, which a contributor can set (or mis-set) by hand. The tier a unit
# *belongs* in is a measurement. `verify-runtime` closes that gap: it takes the
# measured wall-clock of every unit in a tier and flags any unit whose own
# runtime exceeds that tier's per-unit budget by a meaningful margin, no matter
# what its name, kind, or manifest entry says. A slow test wearing fast-tier
# markers is caught here even though the classification gate cannot see it.
#
# The flag is not a silent report: it is a required-review marker. `verify-runtime`
# exits non-zero, and `report` fails the run, until the unit is either moved to a
# slower tier (with the usual `reviewed` annotation) or the overrun is explicitly
# waived with a `reviewed` annotation on the unit. Either way a human signs off.

# How far over the per-unit budget a unit must be before it is flagged. A unit
# that is merely close to the budget is noise (shared-runner jitter); a unit
# that is this much over is a genuine misclassification.
RUNTIME_OVERRUN_MARGIN = 1.5
# Absolute floor: a unit that runs for less than this is never flagged, even if
# it is over the (tiny) per-unit budget, because sub-second noise dominates.
RUNTIME_OVERRUN_FLOOR_SECONDS = 2.0


class RuntimeFlag:
    """A unit whose measured runtime does not fit the tier it was classified in."""

    def __init__(self, uid: str, tier: str, seconds: float, budget: float,
                 proposed_tier: str, reviewed: dict | None) -> None:
        self.id = uid
        self.tier = tier
        self.seconds = seconds
        self.budget = budget
        self.proposed_tier = proposed_tier
        self.reviewed = reviewed

    @property
    def waived(self) -> bool:
        return self.reviewed is not None

    def action(self) -> str:
        """The visible, attributable action this flag demands."""
        if self.waived:
            by = self.reviewed.get("by", "?")
            on = self.reviewed.get("on", "?")
            return (f"waived by {by} on {on}: overrun accepted, keep in `{self.tier}`")
        return (f"REQUIRED REVIEW: move `{self.id}` from `{self.tier}` to "
                f"`{self.proposed_tier}` (or record `reviewed = {{ by = \"…\", on = \"#…\" }}` "
                f"on the unit to waive)")

    def __str__(self) -> str:
        return (f"{self.id}: {human_seconds(self.seconds)} in `{self.tier}` "
                f"(per-unit budget {human_seconds(self.budget)}, "
                f"{self.seconds / self.budget:.1f}x)")


def per_unit_budget(manifest: dict, tier: str) -> float:
    """The per-unit time budget for a tier.

    The tier's `execution_budget_seconds` is the budget for the whole tier, so
    the per-unit budget is that divided by the number of units in the tier. A
    single unit that eats the whole tier budget is exactly the misclassification
    this check exists to catch.
    """
    table = manifest.get("tiers", {}).get(tier, {})
    total = float(table.get("execution_budget_seconds", 0) or 0)
    count = sum(1 for unit in manifest.get("unit", []) if unit.get("tier") == tier)
    return total / count if count else total


def proposed_tier_for(tier: str) -> str:
    """The next slower tier a flagged unit should move to."""
    if tier not in EXECUTION_TIERS:
        return EXECUTION_TIERS[-1]
    index = EXECUTION_TIERS.index(tier)
    return EXECUTION_TIERS[min(index + 1, len(EXECUTION_TIERS) - 1)]


def verify_runtime(manifest: dict, tier: str, results: dict[str, dict],
                   margin: float = RUNTIME_OVERRUN_MARGIN,
                   floor: float = RUNTIME_OVERRUN_FLOOR_SECONDS) -> list[RuntimeFlag]:
    """Flag units in `tier` whose measured runtime exceeds the per-unit budget.

    Classification is deliberately ignored: the only inputs are the measured
    seconds and the tier's budget. A unit whose manifest entry claims `fast`
    but which measures 30s against a 5s per-unit budget is flagged, whatever
    its name or kind says.
    """
    budget = per_unit_budget(manifest, tier)
    if budget <= 0:
        return []
    threshold = max(budget * margin, floor)
    flags = []
    for entry in manifest.get("unit", []):
        if entry.get("tier") != tier:
            continue
        result = results.get(entry["id"])
        if result is None:
            continue
        seconds = float(result.get("seconds", 0.0))
        if seconds > threshold:
            flags.append(RuntimeFlag(entry["id"], tier, seconds, budget,
                                     proposed_tier_for(tier), entry.get("reviewed")))
    flags.sort(key=lambda flag: -flag.seconds)
    return flags


def verify_runtime_report(root: Path, tier: str, results_path: Path,
                          margin: float, floor: float, output: Path | None) -> int:
    """Render the runtime-verification report and return the exit code."""
    manifest = load_manifest(root / MANIFEST_NAME)
    results = read_results(results_path)
    flags = verify_runtime(manifest, tier, results, margin, floor)
    budget = per_unit_budget(manifest, tier)
    unwaived = [flag for flag in flags if not flag.waived]

    lines = [f"# Runtime verification — {tier}", ""]
    lines.append(f"Per-unit budget: {human_seconds(budget)} "
                 f"(tier budget / units in tier) · flag threshold: "
                 f"{human_seconds(max(budget * margin, floor))} "
                 f"({margin:g}x, floor {human_seconds(floor)})")
    lines.append("")
    if not flags:
        lines += ["No unit in this tier exceeds its measured budget. "
                  "Every fast-tier unit is genuinely fast.", ""]
    else:
        lines += ["| Unit | Measured | Budget | Overrun | Action |",
                  "|---|---|---|---|---|"]
        for flag in flags:
            lines.append(f"| `{flag.id}` | {human_seconds(flag.seconds)} | "
                         f"{human_seconds(flag.budget)} | "
                         f"{flag.seconds / flag.budget:.1f}x | {flag.action()} |")
        lines.append("")
        if unwaived:
            lines += [f"> **{len(unwaived)} unit(s) require review.** A test classified into "
                      f"`{tier}` measured far over its budget. Move it to a slower tier in "
                      f"`{MANIFEST_NAME}`, or record a `reviewed` annotation to waive the "
                      f"overrun. This is not a silent report: the run fails until it is "
                      f"resolved.", ""]
    text = "\n".join(lines)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text)
    print(text)
    return 1 if unwaived else 0


def report(root: Path, tier: str, results_path: Path, base: str | None,
           output: Path | None, no_budget: bool, build_seconds: float) -> int:
    manifest = load_manifest(root / MANIFEST_NAME)
    units = [unit for unit in units_from_manifest(manifest) if unit.tier == tier]
    hydrate(root, units, {unit.id: unit for unit in discover(root)})
    table = manifest["tiers"][tier]
    budget = float(table["execution_budget_seconds"])
    results = read_results(results_path)

    measured = sum(result["seconds"] for result in results.values())
    failed = sorted(uid for uid, result in results.items() if result["status"] != "pass")
    missing = sorted(unit.id for unit in units if unit.id not in results)
    over_budget = measured > budget and not no_budget
    unreviewed, reviewed = ([], [])
    if base is not None:
        unreviewed, reviewed = review_moves(manifest, load_base_manifest(base))

    rows = []
    for unit in units:
        result = results.get(unit.id)
        seconds = result["seconds"] if result else None
        rows.append((unit, result, seconds))
    rows.sort(key=lambda row: -(row[2] or 0.0))

    lines = [f"# Test tier report — {tier}", ""]
    lines.append(f"Manifest: `{MANIFEST_NAME}` · reference runner: "
                 f"{manifest.get('runner', {}).get('model', 'see manifest')}")
    lines.append("")
    status = "PASS"
    if failed or missing:
        status = "FAIL"
    elif over_budget or unreviewed:
        status = "FAIL"
    lines += [
        "| | |",
        "|---|---|",
        f"| Status | **{status}** |",
        f"| Execution budget | {human_seconds(budget)} |",
        f"| Measured execution | {human_seconds(measured)} |",
        f"| Build (not budgeted) | {human_seconds(build_seconds)} |",
        f"| Units | {len(units)} ({len(units) - len(failed) - len(missing)} passed, "
        f"{len(failed)} failed, {len(missing)} not run) |",
        f"| Unreviewed tier moves | {len(unreviewed)} |",
        "",
    ]
    if over_budget:
        lines += [f"> **Budget exceeded.** `{tier}` took {human_seconds(measured)}, budget is "
                  f"{human_seconds(budget)}.", "",
                  "> Either the suite got slower — in which case the budget was right and the "
                  "regression needs fixing — or the budget is stale and should be re-baselined "
                  "in `test-tiers.toml` (a budget change needs a `reviewed` annotation).", ""]
    if no_budget:
        lines += ["> Budget enforcement disabled for this run (`--no-budget`).", ""]
    lines += ["## Units by measured cost", "",
              "| Unit | Kind | Tests | Time | Result |", "|---|---|---|---|---|"]
    for unit, result, seconds in rows:
        if result is None:
            lines.append(f"| `{unit.id}` | {unit.kind} | {unit.tests or '—'} | — | "
                         f"**not run** |")
        else:
            mark = "pass" if result["status"] == "pass" else f"**{result['status']}**"
            lines.append(f"| `{unit.id}` | {unit.kind} | {unit.tests or '—'} | "
                         f"{human_seconds(seconds or 0.0)} | {mark} |")
    if failed:
        lines += ["", "## Failures", ""]
        for uid in failed:
            result = results[uid]
            lines.append(f"- `{uid}` — {result['status']}"
                         + (f" (log: `{result['log']}`)" if result["log"] else ""))
    if missing:
        shown = missing[:10]
        lines += ["", "## Not run", ""]
        lines += [f"- `{uid}` — the tier runner did not produce a result for this unit"
                  for uid in shown]
        if len(missing) > len(shown):
            lines.append(f"- … and {len(missing) - len(shown)} more")
    lines += ["", "## Tier changes in this report", ""]
    if base is None:
        lines.append("Not evaluated: no `--base` revision was supplied.")
    elif not (unreviewed or reviewed):
        lines.append(f"No tier or budget changes since `{base}`.")
    else:
        lines += ["| Change | Reviewed |", "|---|---|"]
        for row in sorted(set(reviewed)):
            lines.append(f"| {row} | yes |")
        for row in sorted(set(unreviewed)):
            lines.append(f"| {row} | **no** |")
    lines.append("")
    text = "\n".join(lines)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text)
    print(text)
    return 1 if (failed or missing or over_budget or unreviewed) else 0


# ── self-test ─────────────────────────────────────────────────────────────────


FIXTURE_UNITS = """# ── BEGIN GENERATED: unit inventory (tools/test-tiering.py bootstrap) ──

[[unit]]
id = "contracts/demo/src/test_smoke.rs"
kind = "smoke"
tier = "fast"

[[unit]]
id = "contracts/demo/src/test_cheap.rs"
kind = "unit"
tier = "fast"

[[unit]]
id = "contracts/demo/src/test_heavy.rs"
kind = "unit"
tier = "standard"
reason = "cost signal: loop bound 1000 at line 4"

[[unit]]
id = "contracts/demo/src/test_orphan.rs"
kind = "unit"
tier = "disabled"
reason = "not wired: no mod declaration in src/lib.rs"

[[unit]]
id = "contracts/demo/src/test_standard.rs"
kind = "integration"
tier = "standard"

[[unit]]
id = "contracts/demo/src/test_slow.rs"
kind = "stress"
tier = "nightly"
"""

FIXTURE_HEAD = """schema_version = 1

[tiers.fast]
description = "fixture"
command = "scripts/test-tier.sh fast"
execution_budget_seconds = 60
build_budget_seconds = 300

[tiers.standard]
description = "fixture"
command = "scripts/test-tier.sh standard"
execution_budget_seconds = 600
build_budget_seconds = 600

[tiers.nightly]
description = "fixture"
command = "scripts/test-tier.sh nightly"
execution_budget_seconds = 3600
build_budget_seconds = 1800

[kinds.smoke]
description = "fixture"
default_tier = "fast"

[kinds.unit]
description = "fixture"
default_tier = "fast"

[kinds.integration]
description = "fixture"
default_tier = "standard"

[kinds.property]
description = "fixture"
default_tier = "standard"

[kinds.stress]
description = "fixture"
default_tier = "nightly"

[kinds.soak]
description = "fixture"
default_tier = "nightly"

[kinds.mutation]
description = "fixture"
default_tier = "nightly"
"""


def write_fixture(root: Path, manifest_text: str = FIXTURE_HEAD + FIXTURE_UNITS) -> None:
    (root / "contracts" / "demo" / "src").mkdir(parents=True, exist_ok=True)
    (root / "Cargo.toml").write_text(
        '[workspace]\nresolver = "2"\nmembers = ["contracts/demo"]\n')
    (root / "contracts" / "demo" / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n\n[lib]\n'
        'name = "demo"\npath = "src/lib.rs"\n')
    (root / "contracts" / "demo" / "src" / "lib.rs").write_text(
        "mod test_smoke;\nmod test_cheap;\nmod test_heavy;\nmod test_standard;\n"
        "mod test_slow;\n// mod test_orphan;\n")
    bodies = {
        "test_smoke": "#[test]\nfn smoke() {}\n",
        "test_cheap": "#[test]\nfn cheap() {}\n",
        "test_heavy": "#[test]\nfn heavy() {\n    for _ in 0..1000 {}\n}\n",
        "test_standard": "#[test]\nfn standard() {}\n",
        "test_slow": "#[test]\nfn slow() {}\n",
        "test_orphan": "#[test]\nfn orphan() {}\n",
    }
    for stem, body in bodies.items():
        (root / "contracts" / "demo" / "src" / f"{stem}.rs").write_text(body)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "test-tier.sh").write_text("#!/bin/sh\n")
    (root / MANIFEST_NAME).write_text(manifest_text)


class TierCheckerSelfTest(unittest.TestCase):
    """Proves the gate still detects an unclassified test and an unreviewed move."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        write_fixture(self.root)
        self.addCleanup(self._tmp.cleanup)

    def run_check(self, manifest_text: str | None = None) -> int:
        if manifest_text is not None:
            (self.root / MANIFEST_NAME).write_text(manifest_text)
        manifest = load_manifest(self.root / MANIFEST_NAME)
        units = units_from_manifest(manifest)
        benches = benchmarks_from_manifest(manifest)
        problems, _ = validate(self.root, manifest, units, benches)
        return 1 if problems else 0

    def test_clean_fixture_passes(self) -> None:
        self.assertEqual(self.run_check(), 0)

    def test_omitted_unit_fails(self) -> None:
        text = (self.root / MANIFEST_NAME).read_text().split(
            '[[unit]]\nid = "contracts/demo/src/test_orphan.rs"')[0]
        self.assertEqual(self.run_check(text), 1,
                         "an unclassified test unit must fail the gate")

    def test_newly_wired_module_fails(self) -> None:
        lib = self.root / "contracts" / "demo" / "src" / "lib.rs"
        lib.write_text(lib.read_text().replace("// mod test_orphan;", "mod test_orphan;"))
        self.assertEqual(self.run_check(), 1,
                         "wiring a module without classifying it must fail the gate")

    def test_disabled_without_reason_fails(self) -> None:
        text = (self.root / MANIFEST_NAME).read_text().replace(
            'tier = "disabled"\nreason = "not wired: no mod declaration in src/lib.rs"',
            'tier = "disabled"')
        self.assertEqual(self.run_check(text), 1)

    def test_tier_change_without_review_fails(self) -> None:
        # The base copy says the unit used to be in `fast`; the working copy has
        # it in `standard` with no annotation, which is exactly the move a PR
        # would make to dodge the fast budget.
        current = load_manifest(self.root / MANIFEST_NAME)
        base = load_manifest(self.root / MANIFEST_NAME)
        base["unit"] = [dict(entry) for entry in current["unit"]]
        for entry in base["unit"]:
            if entry["id"].endswith("test_heavy.rs"):
                entry["tier"] = "fast"
        unreviewed, _ = review_moves(current, base)
        self.assertTrue(unreviewed, "a tier move without `reviewed` must be reported")
        for entry in current["unit"]:
            if entry["id"].endswith("test_heavy.rs"):
                entry["reviewed"] = {"by": "maintainer", "on": "#1244"}
        unreviewed, reviewed = review_moves(current, base)
        self.assertEqual(unreviewed, [])
        self.assertTrue(reviewed, "a reviewed tier move must be reported as reviewed")

    def test_budget_change_requires_review(self) -> None:
        current = load_manifest(self.root / MANIFEST_NAME)
        base = load_manifest(self.root / MANIFEST_NAME)
        current["tiers"]["fast"]["execution_budget_seconds"] = 30
        unreviewed, _ = review_moves(current, base)
        self.assertTrue(any("tiers.fast" in row for row in unreviewed))
        current["tiers"]["fast"]["reviewed"] = {"by": "maintainer", "on": "#1244"}
        unreviewed, reviewed = review_moves(current, base)
        self.assertEqual(unreviewed, [])
        self.assertTrue(reviewed)

    def test_report_fails_over_budget(self) -> None:
        results = self.root / "results.tsv"
        results.write_text("contracts/demo/src/test_smoke.rs\tpass\t40.0\n"
                           "contracts/demo/src/test_cheap.rs\tpass\t30.0\n")
        code = report(self.root, "fast", results, None,
                      self.root / "report.md", no_budget=False, build_seconds=1.0)
        self.assertEqual(code, 1, "exceeding the budget must fail the report")
        text = (self.root / "report.md").read_text()
        self.assertIn("Budget exceeded", text)
        self.assertIn("contracts/demo/src/test_heavy.rs", text)
        self.assertIn("not run", text)

    def test_report_passes_within_budget(self) -> None:
        results = self.root / "results.tsv"
        results.write_text("contracts/demo/src/test_smoke.rs\tpass\t1.0\n"
                           "contracts/demo/src/test_cheap.rs\tpass\t2.0\n")
        code = report(self.root, "fast", results, None, None, no_budget=False, build_seconds=1.0)
        self.assertEqual(code, 0)

    def test_report_fails_on_failed_unit(self) -> None:
        results = self.root / "results.tsv"
        results.write_text("contracts/demo/src/test_smoke.rs\tpass\t1.0\n"
                           "contracts/demo/src/test_cheap.rs\tfail\t2.0\tlogs/cheap.log\n")
        code = report(self.root, "fast", results, None, None, no_budget=False, build_seconds=1.0)
        self.assertEqual(code, 1)

    def test_verify_runtime_flags_disguised_slow_test(self) -> None:
        # A test classified `fast` (fast-tier name, fast-tier kind) that actually
        # takes far longer than the per-unit budget must be flagged, regardless
        # of how it was classified.
        manifest = load_manifest(self.root / MANIFEST_NAME)
        results = {
            "contracts/demo/src/test_smoke.rs": {"status": "pass", "seconds": 0.5},
            "contracts/demo/src/test_cheap.rs": {"status": "pass", "seconds": 30.0},
        }
        flags = verify_runtime(manifest, "fast", results)
        self.assertEqual([flag.id for flag in flags],
                         ["contracts/demo/src/test_cheap.rs"],
                         "a slow test disguised as fast must be flagged")
        self.assertEqual(flags[0].proposed_tier, "standard")
        self.assertIn("REQUIRED REVIEW", flags[0].action())

    def test_verify_runtime_ignores_fast_units(self) -> None:
        manifest = load_manifest(self.root / MANIFEST_NAME)
        results = {
            "contracts/demo/src/test_smoke.rs": {"status": "pass", "seconds": 0.5},
            "contracts/demo/src/test_cheap.rs": {"status": "pass", "seconds": 0.6},
        }
        self.assertEqual(verify_runtime(manifest, "fast", results), [])

    def test_verify_runtime_waiver_is_attributable(self) -> None:
        manifest = load_manifest(self.root / MANIFEST_NAME)
        for entry in manifest["unit"]:
            if entry["id"].endswith("test_cheap.rs"):
                entry["reviewed"] = {"by": "maintainer", "on": "#1456"}
        results = {"contracts/demo/src/test_cheap.rs": {"status": "pass", "seconds": 30.0}}
        flags = verify_runtime(manifest, "fast", results)
        self.assertEqual(len(flags), 1)
        self.assertTrue(flags[0].waived)
        self.assertIn("waived by maintainer", flags[0].action())

    def test_verify_runtime_report_fails_on_unwaived_flag(self) -> None:
        results = self.root / "results.tsv"
        results.write_text("contracts/demo/src/test_smoke.rs\tpass\t0.5\n"
                           "contracts/demo/src/test_cheap.rs\tpass\t30.0\n")
        code = verify_runtime_report(self.root, "fast", results,
                                     RUNTIME_OVERRUN_MARGIN, RUNTIME_OVERRUN_FLOOR_SECONDS,
                                     self.root / "verify.md")
        self.assertEqual(code, 1, "an unwaived overrun must fail verification")
        text = (self.root / "verify.md").read_text()
        self.assertIn("REQUIRED REVIEW", text)
        self.assertIn("contracts/demo/src/test_cheap.rs", text)

    def test_verify_runtime_report_passes_when_clean(self) -> None:
        results = self.root / "results.tsv"
        results.write_text("contracts/demo/src/test_smoke.rs\tpass\t0.5\n"
                           "contracts/demo/src/test_cheap.rs\tpass\t0.6\n")
        code = verify_runtime_report(self.root, "fast", results,
                                     RUNTIME_OVERRUN_MARGIN, RUNTIME_OVERRUN_FLOOR_SECONDS,
                                     None)
        self.assertEqual(code, 0)

    def test_plan_emits_one_command_per_unit(self) -> None:
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            plan(self.root, "fast", [], as_json=False)
        out = buffer.getvalue()
        self.assertIn("contracts/demo/src/test_smoke.rs", out)
        self.assertIn("__build__", out)
        self.assertEqual(out.count("cargo test -p demo --lib"), 2,
                         "each fast-tier unit must get its own command, for per-unit timing")

    def test_repository_manifest_validates(self) -> None:
        root = repo_root()
        manifest = load_manifest(root / MANIFEST_NAME)
        units = units_from_manifest(manifest)
        benches = benchmarks_from_manifest(manifest)
        self.assertEqual(validate(root, manifest, units, benches)[0], [],
                         "the checked-in manifest must satisfy the gate")
        for kind, table in manifest["kinds"].items():
            self.assertEqual(table["default_tier"], KIND_DEFAULT_TIER[kind],
                             f"[kinds.{kind}].default_tier drifted from the tool's table")

    def test_manifest_documents_every_kind_and_tier(self) -> None:
        manifest = load_manifest(repo_root() / MANIFEST_NAME)
        self.assertEqual(sorted(manifest["kinds"]), sorted(KIND_DEFAULT_TIER))
        self.assertEqual(sorted(manifest["tiers"]), sorted(EXECUTION_TIERS))
        for tier in EXECUTION_TIERS:
            self.assertGreater(manifest["tiers"][tier]["execution_budget_seconds"], 0)


def selftest() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TierCheckerSelfTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=repo_root(),
                        help="repository root to inspect (default: this checkout)")
    sub = parser.add_subparsers(dest="command", required=True)

    check_parser = sub.add_parser("check", help="validate the manifest (the CI gate)")
    check_parser.add_argument("--base", help="git rev or manifest path to diff tier moves against")
    check_parser.add_argument("--selftest", action="store_true",
                              help="also run this checker's own tests")
    check_parser.add_argument("--manifest", type=Path, help="manifest to validate")

    boot_parser = sub.add_parser("bootstrap", help="regenerate the manifest unit inventory")
    boot_parser.add_argument("--write", action="store_true", help="write the manifest back")

    plan_parser = sub.add_parser("plan", help="print the commands for one tier")
    plan_parser.add_argument("--tier", required=True, choices=list(EXECUTION_TIERS))
    plan_parser.add_argument("--json", action="store_true", help="emit JSON instead of TSV")
    plan_parser.add_argument("extra", nargs="*", help="extra arguments for each command")

    report_parser = sub.add_parser("report", help="render the budget / tier-move report")
    report_parser.add_argument("--tier", required=True, choices=list(EXECUTION_TIERS))
    report_parser.add_argument("--results", type=Path, required=True,
                               help="TSV of `<id>\\t<status>\\t<seconds>[\\t<log>]`")
    report_parser.add_argument("--output", type=Path, help="markdown report path")
    report_parser.add_argument("--base", help="git rev or manifest path to diff tier moves against")
    report_parser.add_argument("--no-budget", action="store_true",
                               help="report the budget but do not fail on it")
    report_parser.add_argument("--build-seconds", type=float, default=0.0,
                               help="build wall-clock, recorded in the report")

    verify_parser = sub.add_parser(
        "verify-runtime",
        help="flag units whose measured runtime exceeds their tier's per-unit budget")
    verify_parser.add_argument("--tier", required=True, choices=list(EXECUTION_TIERS))
    verify_parser.add_argument("--results", type=Path, required=True,
                               help="TSV of `<id>\\t<status>\\t<seconds>[\\t<log>]`")
    verify_parser.add_argument("--output", type=Path, help="markdown report path")
    verify_parser.add_argument("--margin", type=float, default=RUNTIME_OVERRUN_MARGIN,
                               help="flag units over this multiple of the per-unit budget")
    verify_parser.add_argument("--floor", type=float, default=RUNTIME_OVERRUN_FLOOR_SECONDS,
                               help="never flag a unit faster than this many seconds")

    sub.add_parser("selftest", help="run this checker's own tests")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    try:
        return dispatch(args, root, argv)
    except TieringError as error:
        print(f"test-tiers: {error}", file=sys.stderr)
        return 2


def dispatch(args, root: Path, argv: list[str]) -> int:
    if args.command == "check":
        if args.manifest is not None:
            manifest = load_manifest(args.manifest)
            units = units_from_manifest(manifest)
            benches = benchmarks_from_manifest(manifest)
            return 1 if validate(root, manifest, units, benches)[0] else 0
        return check(root, args.base, args.selftest)
    if args.command == "bootstrap":
        return bootstrap(root, args.write)
    if args.command == "plan":
        return plan(root, args.tier, args.extra, args.json)
    if args.command == "report":
        return report(root, args.tier, args.results, args.base, args.output,
                      args.no_budget, args.build_seconds)
    if args.command == "verify-runtime":
        return verify_runtime_report(root, args.tier, args.results, args.margin,
                                     args.floor, args.output)
    if args.command == "selftest":
        return selftest()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
