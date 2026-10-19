#!/usr/bin/env bash
# Black-box tests for tools/test-tiering.py (issue #1244).
#
# The python unit tests in the tool itself cover the validation rules. These
# cover the parts a shell test can see and the unit tests cannot: argument
# parsing, exit codes, and the messages a CI log is read through. The failure
# modes asserted here are the ones the gate exists for:
#
#   1. a test that exists but no tier runs            -> check exits 1
#   2. a module wired into lib.rs but unclassified    -> check exits 1
#   3. a tier moved without a `reviewed` annotation   -> check --base exits 1
#   4. a --base revision that cannot be resolved      -> exit 2, not a silent pass
#   5. a --base revision predating the manifest       -> exit 0, with a notice
#   6. a tier over budget / a failing unit / a unit that never ran
#                                                    -> report exits 1
#   7. a deliberately slow test disguised with fast-tier markers
#      (a fast-looking name and a fast-tier classification) is caught by the
#      independent runtime verification, not trusted because of its markers
#                                                    -> verify exits 1
#
# Nothing here compiles or runs a contract test: the fixture is a throwaway
# workspace, so this suite costs a fraction of a second and belongs in the fast
# tier.
#
# Usage: tests/test_tiers.test.sh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOL="$ROOT_DIR/tools/test-tiering.py"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# ── Throwaway workspace ──────────────────────────────────────────────────────
REPO="$TMP_DIR/demo"
mkdir -p "$REPO/contracts/demo/src" "$REPO/scripts" "$REPO/tools"
cat > "$REPO/Cargo.toml" <<'EOF'
[workspace]
resolver = "2"
members = ["contracts/demo"]
EOF
cat > "$REPO/contracts/demo/Cargo.toml" <<'EOF'
[package]
name = "demo"
version = "0.1.0"
edition = "2021"

[lib]
name = "demo"
path = "src/lib.rs"
EOF
cat > "$REPO/contracts/demo/src/lib.rs" <<'EOF'
mod test_alpha;
mod test_beta;
EOF
printf '#[test]\nfn alpha() {}\n' > "$REPO/contracts/demo/src/test_alpha.rs"
printf '#[test]\nfn beta() {}\n' > "$REPO/contracts/demo/src/test_beta.rs"
printf '#[test]\nfn slow() {\n    for _ in 0..1000 {}\n}\n' > "$REPO/contracts/demo/src/test_slow.rs"
printf '#!/bin/sh\n' > "$REPO/scripts/test-tier.sh"
chmod +x "$REPO/scripts/test-tier.sh"
printf 'def helper():\n    pass\n' > "$REPO/tools/helper.py"

MANIFEST="$REPO/test-tiers.toml"
write_manifest() {
  cat > "$MANIFEST"
}
write_manifest <<'EOF'
schema_version = 1

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

[[unit]]
id = "contracts/demo/src/test_alpha.rs"
kind = "smoke"
tier = "fast"

[[unit]]
id = "contracts/demo/src/test_cheap.rs"
kind = "unit"
tier = "fast"

[[unit]]
id = "contracts/demo/src/test_beta.rs"
kind = "integration"
tier = "standard"
reason = "deliberately in the slower tier for this test"

[[unit]]
id = "contracts/demo/src/test_slow.rs"
kind = "stress"
tier = "nightly"
EOF
BASE_MANIFEST="$TMP_DIR/base-tiers.toml"
cp "$MANIFEST" "$BASE_MANIFEST"

# ── Helpers ──────────────────────────────────────────────────────────────────
pass=0
fail=0
LAST_OUTPUT=""

# run_tool <desc> <expected-exit> <args...>
run_tool() {
  local desc="$1" expected="$2"
  shift 2
  local actual=0
  set +e
  LAST_OUTPUT="$(python3 "$TOOL" --root "$REPO" "$@" 2>&1)"
  actual=$?
  set -e
  if [ "$actual" -eq "$expected" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "FAIL: $desc (expected exit $expected, got $actual)"
    printf '%s\n' "$LAST_OUTPUT"
  fi
}

assert_contains() {
  local needle="$1"
  if grep -Fq "$needle" <<<"$LAST_OUTPUT"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "FAIL: expected output to contain '$needle'"
    printf '%s\n' "$LAST_OUTPUT"
  fi
}

# ── 1. The happy path ────────────────────────────────────────────────────────
run_tool "a fully classified manifest passes" 0 check
assert_contains "OK"
assert_contains "4 units classified"

# ── 2. A test that no tier runs ──────────────────────────────────────────────
printf '#[test]\nfn gamma() {}\n' > "$REPO/contracts/demo/src/test_gamma.rs"
run_tool "an unclassified test file fails the gate" 1 check
assert_contains "contracts/demo/src/test_gamma.rs"
rm "$REPO/contracts/demo/src/test_gamma.rs"

# ── 3. A module wired into lib.rs but unclassified ───────────────────────────
printf '#[test]\nfn delta() {}\n' > "$REPO/contracts/demo/src/test_delta.rs"
printf 'mod test_alpha;\nmod test_beta;\nmod test_delta;\n' > "$REPO/contracts/demo/src/lib.rs"
run_tool "wiring an unclassified module fails the gate" 1 check
assert_contains "contracts/demo/src/test_delta.rs"
assert_contains "is not classified in test-tiers.toml (declared in src/lib.rs)"
printf 'mod test_alpha;\nmod test_beta;\nmod test_cheap;\nmod test_slow;\n' > "$REPO/contracts/demo/src/lib.rs"
rm "$REPO/contracts/demo/src/test_delta.rs"

# ── 4. A tier move without review ────────────────────────────────────────────
python3 - "$MANIFEST" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
text = path.read_text().replace(
    'id = "contracts/demo/src/test_alpha.rs"\nkind = "smoke"\ntier = "fast"',
    'id = "contracts/demo/src/test_alpha.rs"\nkind = "smoke"\ntier = "nightly"\n'
    'reason = "moved"')
path.write_text(text)
PY
run_tool "an unreviewed tier move fails" 1 check --base "$BASE_MANIFEST"
assert_contains "unreviewed-move"
assert_contains "test_alpha.rs"
assert_contains "reviewed = {"

python3 - "$MANIFEST" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
text = path.read_text().replace(
    'tier = "nightly"\nreason = "moved"',
    'tier = "nightly"\nreason = "moved"\n'
    'reviewed = { by = "maintainer", on = "#1244", note = "reviewed in the test" }')
path.write_text(text)
PY
run_tool "a reviewed tier move passes and is reported" 0 check --base "$BASE_MANIFEST"
assert_contains "1 tier move(s) recorded since base"

# ── 5. A budget change needs the same annotation ─────────────────────────────
python3 - "$MANIFEST" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
path.write_text(path.read_text().replace(
    "[tiers.fast]\ndescription = \"fixture\"\ncommand = \"scripts/test-tier.sh fast\"\n"
    "execution_budget_seconds = 60",
    "[tiers.fast]\ndescription = \"fixture\"\ncommand = \"scripts/test-tier.sh fast\"\n"
    "execution_budget_seconds = 120"))
PY
run_tool "an unreviewed budget change fails" 1 check --base "$BASE_MANIFEST"
assert_contains "[tiers.fast]"
cp "$BASE_MANIFEST" "$MANIFEST"

# ── 6. A --base revision that cannot be resolved must not be a silent pass ──
run_tool "an unresolvable --base revision is an error" 2 check --base nosuchrevision1234
assert_contains "not a readable git revision"
run_tool "a --base path that does not exist is an error" 2 check --base "$TMP_DIR/absent.toml"

# ── 7. A --base revision from before the manifest existed ────────────────────
git -C "$REPO" init -q
git -C "$REPO" config user.email "tier-test@example.com"
git -C "$REPO" config user.name "Tier Test"
rm "$MANIFEST"
git -C "$REPO" add -A
git -C "$REPO" commit -q -m "before the manifest existed"
write_manifest < "$BASE_MANIFEST"
run_tool "a base revision predating the manifest passes with a notice" 0 check --base HEAD
assert_contains "the inventory is new here"

# ── 8. The plan is one command per unit, plus a build ────────────────────────
run_tool "plan lists the tier" 0 plan --tier fast
assert_contains "__build__"
assert_contains "contracts/demo/src/test_alpha.rs"
assert_contains "cargo test -p demo --lib test_alpha::"
assert_contains "2 unit(s)"
if [ "$(grep -c 'cargo test -p demo' <<<"$LAST_OUTPUT")" -eq 2 ]; then
  pass=$((pass + 1))
else
  fail=$((fail + 1))
  echo "FAIL: the fast tier plan must contain one cargo command per unit, for per-unit timing"
  printf '%s\n' "$LAST_OUTPUT"
fi
assert_contains "test_cheap::"
run_tool "a checker in tools/ is not a test unit" 0 plan --tier fast
if grep -Fq "tools/helper.py" <<<"$LAST_OUTPUT"; then
  fail=$((fail + 1))
  echo "FAIL: tools/helper.py is a checker, not a test; it must not appear in a tier"
  printf '%s\n' "$LAST_OUTPUT"
else
  pass=$((pass + 1))
fi
run_tool "plan rejects an unknown tier" 2 plan --tier bogus
run_tool "plan renders the standard tier's integration command" 0 plan --tier standard
assert_contains "cargo test -p demo --lib test_beta::"

# ── 9. The report decides pass/fail ──────────────────────────────────────────
# The fast tier's budget is 60s in this fixture, so 70.5s of measured execution
# is over it and 1.5s is not.
RESULTS="$TMP_DIR/results.tsv"
OVER="$TMP_DIR/over.tsv"
printf 'contracts/demo/src/test_alpha.rs\tpass\t70.0\ncontracts/demo/src/test_cheap.rs\tpass\t0.5\n' > "$OVER"
WITHIN="$TMP_DIR/within.tsv"
printf 'contracts/demo/src/test_alpha.rs\tpass\t1.0\ncontracts/demo/src/test_cheap.rs\tpass\t0.5\n' > "$WITHIN"

run_tool "a tier over budget fails the report" 1 \
  report --tier fast --results "$OVER" --output "$TMP_DIR/report.md" --build-seconds 5.0
assert_contains "Budget exceeded"
assert_contains "Measured execution | 70.5s"

run_tool "a tier within budget passes the report" 0 \
  report --tier fast --results "$WITHIN" --output "$TMP_DIR/report.md" --build-seconds 5.0

printf 'contracts/demo/src/test_alpha.rs\tpass\t1.0\n' > "$RESULTS"
run_tool "a unit the runner never reported fails the report" 1 \
  report --tier fast --results "$RESULTS" --output "$TMP_DIR/report.md" --build-seconds 5.0
assert_contains "not run"
assert_contains "test_cheap.rs"

printf 'contracts/demo/src/test_alpha.rs\tfail\t1.0\tlogs/alpha.log\ncontracts/demo/src/test_cheap.rs\tpass\t0.5\n' > "$RESULTS"
run_tool "a failing unit fails the report" 1 \
  report --tier fast --results "$RESULTS" --output "$TMP_DIR/report.md" --build-seconds 5.0
assert_contains "logs/alpha.log"

run_tool "--no-budget reports an over-budget run without failing" 0 \
  report --tier fast --results "$OVER" --output "$TMP_DIR/report.md" --no-budget \
  --build-seconds 5.0
assert_contains "Budget enforcement disabled"
assert_contains "70.5s"

# ── 10. bootstrap writes on request and is a clean no-op otherwise ──────────
# The hand-written manifest above is the starting point for the failure cases,
# not a bootstrap artefact, so normalise it first: --write must produce exactly
# what a second, no-flag run considers up to date, and must not touch the
# hand-written header or the hand-set classifications.
run_tool "bootstrap --write regenerates the inventory" 0 bootstrap --write
assert_contains "(4 units, 0 benchmarks)"
NORMALISED="$(cat "$MANIFEST")"
run_tool "bootstrap reports the inventory is up to date" 0 bootstrap
assert_contains "up to date"
if [ "$NORMALISED" = "$(cat "$MANIFEST")" ]; then
  pass=$((pass + 1))
else
  fail=$((fail + 1))
  echo "FAIL: bootstrap rewrote the manifest without --write"
fi
if grep -Fq "deliberately in the slower tier for this test" "$MANIFEST"; then
  pass=$((pass + 1))
else
  fail=$((fail + 1))
  echo "FAIL: bootstrap discarded a hand-written reason"
fi

# ── 11. A slow test disguised with fast-tier markers is caught ──────────────
# Regression check for #1456. The tiering mechanism classifies by attribute
# (the manifest's `tier` field, which a contributor sets directly), so a test
# can be *labelled* fast without being fast. This seeds exactly that: a test
# whose name looks fast (`test_quick_*`) and whose manifest entry says
# `tier = "fast"`, but whose measured runtime blows past the fast tier's
# budget. The independent runtime verification must flag it regardless of how
# it was classified, and the flag must be a visible, attributable failure.
DISGUISED="$REPO/contracts/demo/src/test_quick_disguised.rs"
printf '#[test]\nfn quick_disguised() {\n    for _ in 0..100000000 {}\n}\n' > "$DISGUISED"
printf 'mod test_alpha;\nmod test_beta;\nmod test_cheap;\nmod test_slow;\nmod test_quick_disguised;\n' \
  > "$REPO/contracts/demo/src/lib.rs"
python3 - "$MANIFEST" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
text = path.read_text()
text += (
    '\n[[unit]]\n'
    'id = "contracts/demo/src/test_quick_disguised.rs"\n'
    'kind = "smoke"\n'
    'tier = "fast"\n'
)
path.write_text(text)
PY

# The manifest alone considers it fast: classification is attribute-based and
# does not look at runtime, so `check` passes. That is the gap being guarded.
run_tool "a fast-labelled slow test still passes the attribute-only check" 0 check

# Runtime verification measures the fast tier and flags the disguised test.
DISGUISED_RESULTS="$TMP_DIR/disguised.tsv"
printf 'contracts/demo/src/test_alpha.rs\tpass\t0.4\ncontracts/demo/src/test_cheap.rs\tpass\t0.3\ncontracts/demo/src/test_quick_disguised.rs\tpass\t180.0\n' \
  > "$DISGUISED_RESULTS"
run_tool "runtime verification flags a disguised slow fast-tier test" 1 \
  verify --tier fast --results "$DISGUISED_RESULTS" --output "$TMP_DIR/verify.md"
assert_contains "contracts/demo/src/test_quick_disguised.rs"
assert_contains "fast"
assert_contains "180.0"
# The flag must be attributable: it names the offending unit and the tier it
# was wrongly classified into, so a reviewer can act on it.
assert_contains "reclassif"
if grep -Fq "test_quick_disguised.rs" "$TMP_DIR/verify.md"; then
  pass=$((pass + 1))
else
  fail=$((fail + 1))
  echo "FAIL: the verification report must name the flagged unit for review"
fi

# A fast tier whose measured runtimes are all within budget is not flagged.
run_tool "runtime verification passes a genuinely fast tier" 0 \
  verify --tier fast --results "$WITHIN" --output "$TMP_DIR/verify.md"

# Clean up the seeded unit so later assertions see the original inventory.
rm "$DISGUISED"
printf 'mod test_alpha;\nmod test_beta;\nmod test_cheap;\nmod test_slow;\n' \
  > "$REPO/contracts/demo/src/lib.rs"
python3 - "$MANIFEST" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
text = path.read_text().replace(
    '\n[[unit]]\n'
    'id = "contracts/demo/src/test_quick_disguised.rs"\n'
    'kind = "smoke"\n'
    'tier = "fast"\n',
    '')
path.write_text(text)
PY

echo ""
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
