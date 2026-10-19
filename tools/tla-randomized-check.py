#!/usr/bin/env python3
"""Spike prototype: randomized/statistical model checking of the LedgerLens TLA+ spec.

This script is part of the spike for issue #1454. It is NOT wired into CI and
does not modify the existing exhaustive TLC model-checking job. Its purpose is
to make the spike's comparison reproducible:

  1. Run TLC in *simulation mode* (randomized exploration of the state space)
     against ``spec/LedgerLens.tla`` using a larger configuration than the
     exhaustive check covers, recording seeds and the behaviors explored, and
     reporting any invariant/property violations found.
  2. Run TLC in *exhaustive mode* at increasing bounds and measure wall-clock
     time, so the cost of randomized exploration can be compared against simply
     raising the exhaustive bounds.

Usage
-----
    # Randomized (simulation) run against the larger config.
    tools/tla-randomized-check.py simulate \
        --spec spec/LedgerLens.tla \
        --config spec/LedgerLens.large.cfg \
        --runs 1000 \
        --seed 42 \
        --out spike-results/simulate.json

    # Cost comparison: exhaustive TLC at a series of bounds.
    tools/tla-randomized-check.py cost-compare \
        --spec spec/LedgerLens.tla \
        --configs spec/LedgerLens.cfg spec/LedgerLens.large.cfg \
        --out spike-results/cost-compare.json

    # Convenience: run both and print a summary.
    tools/tla-randomized-check.py all

Requirements
------------
* A TLA+ toolbox / tla2tools.jar available. Resolution order:
    1. ``--tla2tools`` CLI flag
    2. ``$TLA2TOOLS_JAR`` environment variable
    3. ``tools/tla2tools.jar`` relative to the repo root
    4. ``tla2tools.jar`` on ``$PATH``
* Java on ``$PATH``.

The script never mutates the spec or the existing configs; it only reads them
and writes result artifacts under ``--out`` (default ``spike-results/``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SPEC = REPO_ROOT / "spec" / "LedgerLens.tla"
DEFAULT_CONFIG = REPO_ROOT / "spec" / "LedgerLens.cfg"
DEFAULT_LARGE_CONFIG = REPO_ROOT / "spec" / "LedgerLens.large.cfg"
DEFAULT_OUT_DIR = REPO_ROOT / "spike-results"


# ---------------------------------------------------------------------------
# TLC invocation helpers
# ---------------------------------------------------------------------------


def resolve_tla2tools(explicit: Optional[str]) -> Path:
    """Locate tla2tools.jar following the documented resolution order."""
    candidates: List[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("TLA2TOOLS_JAR")
    if env:
        candidates.append(Path(env))
    candidates.append(REPO_ROOT / "tools" / "tla2tools.jar")
    on_path = shutil.which("tla2tools.jar")
    if on_path:
        candidates.append(Path(on_path))

    for cand in candidates:
        if cand.is_file():
            return cand.resolve()

    raise SystemExit(
        "Could not locate tla2tools.jar. Pass --tla2tools, set $TLA2TOOLS_JAR, "
        "or place it at tools/tla2tools.jar."
    )


def ensure_java() -> None:
    if shutil.which("java") is None:
        raise SystemExit("java not found on PATH; TLC requires a JVM.")


@dataclass
class TlcRun:
    """Result of a single TLC invocation."""

    mode: str  # "simulate" | "exhaustive"
    config: str
    seed: Optional[int]
    runs: Optional[int]
    wall_seconds: float
    exit_code: int
    states_generated: Optional[int] = None
    states_distinct: Optional[int] = None
    states_left_on_queue: Optional[int] = None
    depth: Optional[int] = None
    violations: List[str] = field(default_factory=list)
    stdout_tail: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.violations


_NUM_RE = re.compile(r"([0-9][0-9,]*)")
_VIOLATION_RE = re.compile(
    r"(Invariant .* is violated|Temporal property .* is violated|"
    r"Error: .*violated|Deadlock reached|Assertion failed)",
    re.IGNORECASE,
)


def _parse_count(line: str) -> Optional[int]:
    m = _NUM_RE.search(line)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


def parse_tlc_output(stdout: str) -> dict:
    """Extract the standard TLC summary counters and any violations."""
    result = {
        "states_generated": None,
        "states_distinct": None,
        "states_left_on_queue": None,
        "depth": None,
        "violations": [],
    }
    for line in stdout.splitlines():
        low = line.lower()
        if "states generated" in low:
            result["states_generated"] = _parse_count(line)
        elif "distinct states found" in low:
            result["states_distinct"] = _parse_count(line)
        elif "states left on queue" in low:
            result["states_left_on_queue"] = _parse_count(line)
        elif low.startswith("depth:") or " depth " in low:
            val = _parse_count(line)
            if val is not None:
                result["depth"] = val
        if _VIOLATION_RE.search(line):
            result["violations"].append(line.strip())
    return result


def run_tlc(
    jar: Path,
    spec: Path,
    config: Path,
    *,
    mode: str,
    seed: Optional[int] = None,
    runs: Optional[int] = None,
    timeout: Optional[float] = None,
) -> TlcRun:
    """Invoke TLC in either simulation or exhaustive mode."""
    cmd: List[str] = ["java", "-jar", str(jar), "-config", str(config)]
    if mode == "simulate":
        cmd.append("-simulate")
        if seed is not None:
            cmd.extend(["-seed", str(seed)])
        if runs is not None:
            cmd.extend(["-depth", "1000000"])  # let runs drive termination
            cmd.extend(["-simulate_num", str(runs)])
    elif mode == "exhaustive":
        # Default TLC behavior; nothing extra needed.
        pass
    else:
        raise ValueError(f"unknown mode: {mode}")
    cmd.append(str(spec))

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(spec.parent),
        )
        stdout = proc.stdout
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        exit_code = 124
    wall = time.monotonic() - start

    parsed = parse_tlc_output(stdout)
    tail = "\n".join(stdout.splitlines()[-40:])
    return TlcRun(
        mode=mode,
        config=str(config),
        seed=seed,
        runs=runs,
        wall_seconds=round(wall, 3),
        exit_code=exit_code,
        states_generated=parsed["states_generated"],
        states_distinct=parsed["states_distinct"],
        states_left_on_queue=parsed["states_left_on_queue"],
        depth=parsed["depth"],
        violations=parsed["violations"],
        stdout_tail=tail,
    )


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_simulate(args: argparse.Namespace) -> int:
    ensure_java()
    jar = resolve_tla2tools(args.tla2tools)
    spec = Path(args.spec).resolve()
    config = Path(args.config).resolve()
    if not spec.is_file():
        raise SystemExit(f"spec not found: {spec}")
    if not config.is_file():
        raise SystemExit(f"config not found: {config}")

    out_dir = Path(args.out).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)

    runs: List[TlcRun] = []
    base_seed = args.seed
    for i in range(args.repeats):
        seed = base_seed + i if base_seed is not None else None
        print(
            f"[simulate] config={config.name} seed={seed} runs={args.runs} "
            f"({i + 1}/{args.repeats})",
            file=sys.stderr,
        )
        run = run_tlc(
            jar,
            spec,
            config,
            mode="simulate",
            seed=seed,
            runs=args.runs,
            timeout=args.timeout,
        )
        runs.append(run)
        status = "OK" if run.ok else "VIOLATION/ERROR"
        print(
            f"  -> {status} in {run.wall_seconds:.1f}s "
            f"(states_generated={run.states_generated}, "
            f"distinct={run.states_distinct})",
            file=sys.stderr,
        )

    payload = {
        "mode": "simulate",
        "spec": str(spec),
        "config": str(config),
        "runs_per_seed": args.runs,
        "repeats": args.repeats,
        "results": [asdict(r) for r in runs],
        "any_violation": any(not r.ok for r in runs),
    }
    Path(args.out).write_text(json.dumps(payload, indent=2))
    print(f"wrote {args.out}", file=sys.stderr)
    return 0 if not payload["any_violation"] else 1


def cmd_cost_compare(args: argparse.Namespace) -> int:
    ensure_java()
    jar = resolve_tla2tools(args.tla2tools)
    spec = Path(args.spec).resolve()

    configs = [Path(c).resolve() for c in args.configs]
    for c in configs:
        if not c.is_file():
            raise SystemExit(f"config not found: {c}")

    out_dir = Path(args.out).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)

    results: List[TlcRun] = []
    for config in configs:
        print(f"[cost-compare] exhaustive config={config.name}", file=sys.stderr)
        run = run_tlc(
            jar,
            spec,
            config,
            mode="exhaustive",
            timeout=args.timeout,
        )
        results.append(run)
        print(
            f"  -> exit={run.exit_code} in {run.wall_seconds:.1f}s "
            f"(distinct={run.states_distinct})",
            file=sys.stderr,
        )

    payload = {
        "mode": "cost-compare",
        "spec": str(spec),
        "results": [asdict(r) for r in results],
    }
    Path(args.out).write_text(json.dumps(payload, indent=2))
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    """Run simulate against the large config, then cost-compare."""
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    sim_args = argparse.Namespace(
        tla2tools=args.tla2tools,
        spec=str(args.spec),
        config=str(args.large_config),
        runs=args.runs,
        seed=args.seed,
        repeats=args.repeats,
        timeout=args.timeout,
        out=str(out_dir / "simulate.json"),
    )
    sim_rc = cmd_simulate(sim_args)

    cost_args = argparse.Namespace(
        tla2tools=args.tla2tools,
        spec=str(args.spec),
        configs=[str(args.small_config), str(args.large_config)],
        timeout=args.timeout,
        out=str(out_dir / "cost-compare.json"),
    )
    cost_rc = cmd_cost_compare(cost_args)

    print(
        f"\nSummary artifacts written under {out_dir}\n"
        f"  simulate.json      (rc={sim_rc})\n"
        f"  cost-compare.json  (rc={cost_rc})",
        file=sys.stderr,
    )
    return 0 if sim_rc == 0 and cost_rc == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Spike prototype for issue #1454: randomized/statistical model "
            "checking of the LedgerLens TLA+ spec, plus a cost comparison "
            "against raising exhaustive TLC bounds. Not wired into CI."
        )
    )
    p.add_argument(
        "--tla2tools",
        default=None,
        help="Path to tla2tools.jar (default: auto-resolve).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sim = sub.add_parser(
        "simulate",
        help="Run TLC in simulation mode against a (larger) config.",
    )
    sim.add_argument("--spec", default=str(DEFAULT_SPEC))
    sim.add_argument(
        "--config",
        default=str(DEFAULT_LARGE_CONFIG),
        help="Config to simulate (default: spec/LedgerLens.large.cfg).",
    )
    sim.add_argument(
        "--runs",
        type=int,
        default=1000,
        help="Number of simulation runs per seed (default: 1000).",
    )
    sim.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Base seed; each repeat uses seed+i (default: 42).",
    )
    sim.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="Number of independent seeds to try (default: 1).",
    )
    sim.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Per-run timeout in seconds (default: none).",
    )
    sim.add_argument(
        "--out",
        default=str(DEFAULT_OUT_DIR / "simulate.json"),
        help="Output JSON path.",
    )
    sim.set_defaults(func=cmd_simulate)

    cost = sub.add_parser(
        "cost-compare",
        help="Measure exhaustive TLC wall-clock time across configs.",
    )
    cost.add_argument("--spec", default=str(DEFAULT_SPEC))
    cost.add_argument(
        "--configs",
        nargs="+",
        default=[str(DEFAULT_CONFIG), str(DEFAULT_LARGE_CONFIG)],
        help="Configs to run exhaustively, in increasing bound order.",
    )
    cost.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Per-run timeout in seconds (default: none).",
    )
    cost.add_argument(
        "--out",
        default=str(DEFAULT_OUT_DIR / "cost-compare.json"),
        help="Output JSON path.",
    )
    cost.set_defaults(func=cmd_cost_compare)

    allp = sub.add_parser(
        "all",
        help="Run simulate against the large config, then cost-compare.",
    )
    allp.add_argument("--spec", default=str(DEFAULT_SPEC))
    allp.add_argument("--small-config", default=str(DEFAULT_CONFIG))
    allp.add_argument("--large-config", default=str(DEFAULT_LARGE_CONFIG))
    allp.add_argument("--runs", type=int, default=1000)
    allp.add_argument("--seed", type=int, default=42)
    allp.add_argument("--repeats", type=int, default=1)
    allp.add_argument("--timeout", type=float, default=None)
    allp.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    allp.set_defaults(func=cmd_all)

    return p


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())