#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
#   test-tier.sh — run one test tier, enforce its budget, publish its report
#
#   Usage:
#     scripts/test-tier.sh fast
#     scripts/test-tier.sh standard
#     scripts/test-tier.sh nightly
#
#   Options:
#     --base <rev>    diff the manifest against <rev> and fail on an unreviewed
#                     tier or budget change (CI passes the PR base SHA here)
#     --no-budget     report the budget but do not fail on it (laptops are not
#                     the reference runner; see docs/test-tiers.md)
#     --no-build      skip the pre-build step (the build is timed separately and
#                     is not part of the execution budget)
#     --fail-fast     stop at the first failing unit instead of running the tier
#     --list          print the units in the tier and exit
#     --dry-run       print the commands the tier would run and exit
#     --verify-runtime
#                     after the tier runs, independently re-measure the actual
#                     runtime of every unit in the tier and flag any unit that
#                     exceeds the tier's budget by a meaningful margin, no matter
#                     how it was classified. A flag is a hard failure: it prints
#                     an attributable re-classification proposal and requires a
#                     review (see docs/test-tiers.md). Implied for `fast`.
#     -- <args>       extra arguments appended to every command in the tier
#                     (e.g. `-- --nocapture`, `-- --test-threads=1`)
#
#   What it does:
#     1. asks tools/test-tiering.py for the tier's unit list (from test-tiers.toml)
#     2. pre-builds the tier's test targets once, timed separately from the
#        budget, so a slow machine's compile time is not mistaken for a slow test
#     3. runs each unit as its own command, timing it, so the report can attribute
#        cost to a unit rather than to a tier as a whole
#     4. renders target/test-tiers/<tier>-report.md, which fails the command when
#        the tier blows its budget, a unit fails, a unit did not run, or the tier
#        moved without review
#
#   The manifest (test-tiers.toml) is the single source of truth for what is in
#   each tier; this script never hard-codes a test list. Adding a test to a tier
#   means editing the manifest, which CI checks.
#
#   Docs: docs/test-tiers.md
# ──────────────────────────────────────────────────────────────────────────────
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TIER=""
BASE=""
ENFORCE_BUDGET=1
DO_BUILD=1
FAIL_FAST=0
MODE=run
VERIFY_RUNTIME=0
EXTRA=()

usage() {
    sed -n '3,32p' "$0" | sed 's/^#\s\?//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        fast|standard|nightly) TIER="$1"; shift ;;
        --base) BASE="${2:?--base needs a revision}"; shift 2 ;;
        --no-budget) ENFORCE_BUDGET=0; shift ;;
        --no-build) DO_BUILD=0; shift ;;
        --fail-fast) FAIL_FAST=1; shift ;;
        --list) MODE=list; shift ;;
        --dry-run) MODE=dry-run; shift ;;
        --verify-runtime) VERIFY_RUNTIME=1; shift ;;
        -h|--help) usage; exit 0 ;;
        --) shift; EXTRA=("$@"); break ;;
        *) echo "test-tier: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -z "$TIER" ]]; then
    echo "test-tier: no tier given" >&2
    usage >&2
    exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "test-tier: python3 is required (python3 >= 3.11 for tomllib)" >&2
    exit 127
fi

TIERING="python3 tools/test-tiering.py"
OUT_DIR="target/test-tiers"
LOG_DIR="$OUT_DIR/logs/$TIER"
RESULTS="$OUT_DIR/$TIER-results.tsv"
REPORT="$OUT_DIR/$TIER-report.md"
VERIFY_REPORT="$OUT_DIR/$TIER-runtime-verify.md"

# The fast tier is the one whose whole point is a tight budget, so its runtime
# verification is not optional: a slow test wearing fast-tier markers must never
# slip through a plain `scripts/test-tier.sh fast`.
if [[ "$TIER" == "fast" ]]; then
    VERIFY_RUNTIME=1
fi
PLAN="$(mktemp)"
trap 'rm -f "$PLAN"' EXIT

# ── 1. the tier's unit list, straight from the manifest ───────────────────────
if ! $TIERING plan --tier "$TIER" "${EXTRA[@]+"${EXTRA[@]}"}" > "$PLAN"; then
    echo "test-tier: could not build the plan for tier '$TIER'" >&2
    exit 1
fi

if [[ "$MODE" == "list" ]]; then
    grep -v '^#' "$PLAN" | while IFS=$'\t' read -r id _; do
        [[ "$id" == "__build__" ]] && continue
        printf '%s\n' "$id"
    done
    exit 0
fi

if [[ "$MODE" == "dry-run" ]]; then
    cat "$PLAN"
    exit 0
fi

mkdir -p "$LOG_DIR"
: > "$RESULTS"

now() { python3 -c 'import time; print("%.3f" % time.monotonic())'; }

UNIT_COUNT=0
FAILED=0
STOPPED=0
BUILD_SECONDS=0

echo "==> tier: $TIER"
echo "    manifest: test-tiers.toml"
echo "    results:  $RESULTS"
echo "    report:   $REPORT"
echo ""

# ── 2. pre-build, timed separately from the execution budget ──────────────────
BUILD_CMD=""
while IFS=$'\t' read -r id cmd; do
    [[ "$id" == "__build__" ]] && BUILD_CMD="$cmd"
done < "$PLAN"

if [[ -n "$BUILD_CMD" ]]; then
    if [[ "$DO_BUILD" == "1" ]]; then
        echo "==> building tier targets (not part of the execution budget)"
        build_start="$(now)"
        if ! bash -c "$BUILD_CMD" > "$LOG_DIR/../$TIER-build.log" 2>&1; then
            echo "test-tier: build failed; see $LOG_DIR/../$TIER-build.log" >&2
            tail -30 "$LOG_DIR/../$TIER-build.log" >&2
            exit 1
        fi
        BUILD_SECONDS="$(python3 -c "print('%.3f' % ($(now) - $build_start))")"
        echo "    build: ${BUILD_SECONDS}s"
        echo ""
    else
        echo "==> skipping pre-build (--no-build)"
        echo ""
    fi
fi

# ── 3. run every unit, timed on its own ───────────────────────────────────────
while IFS=$'\t' read -r id cmd; do
    [[ "$id" == "__build__" || "$id" == \#* ]] && continue
    UNIT_COUNT=$((UNIT_COUNT + 1))
    safe="$(printf '%s' "$id" | tr -c 'A-Za-z0-9._-' '_')"
    log="$LOG_DIR/$safe.log"
    printf '    [%2d] %-72s ' "$UNIT_COUNT" "$id"
    start="$(now)"
    if bash -c "$cmd" > "$log" 2>&1; then
        status="pass"
    else
        status="fail"
        FAILED=$((FAILED + 1))
    fi
    seconds="$(python3 -c "print('%.3f' % ($(now) - $start))")"
    printf '%ss  %s\n' "$seconds" "$status"
    printf '%s\t%s\t%s\t%s\n' "$id" "$status" "$seconds" "$log" >> "$RESULTS"
    if [[ "$status" == "fail" ]]; then
        echo "         ---- last 20 lines of $log ----"
        tail -20 "$log" | sed 's/^/         /'
        if [[ "$FAIL_FAST" == "1" ]]; then
            STOPPED=1
            break
        fi
    fi
done < "$PLAN"

if [[ "$STOPPED" == "1" ]]; then
    echo ""
    echo "test-tier: stopped after the first failure (--fail-fast)"
fi

# ── 4. the report decides pass/fail ───────────────────────────────────────────
echo ""
REPORT_ARGS=(report --tier "$TIER" --results "$RESULTS" --output "$REPORT"
             --build-seconds "$BUILD_SECONDS")
[[ "$ENFORCE_BUDGET" == "0" ]] && REPORT_ARGS+=(--no-budget)
[[ -n "$BASE" ]] && REPORT_ARGS+=(--base "$BASE")

$TIERING "${REPORT_ARGS[@]}"
STATUS=$?

# ── 5. independent runtime verification ───────────────────────────────────────
# The report above trusts the tier a unit was classified into. This step does
# not: it re-measures each unit's actual runtime and flags anything that blows
# the tier's budget, regardless of how it was classified. A flag is a hard
# failure with an attributable, review-requiring proposal — never a silent note.
if [[ "$VERIFY_RUNTIME" == "1" && "$STOPPED" == "0" ]]; then
    echo ""
    echo "==> verifying actual runtime of tier '$TIER' units (independent of classification)"
    VERIFY_ARGS=(verify-runtime --tier "$TIER" --results "$RESULTS"
                 --output "$VERIFY_REPORT")
    [[ -n "$BASE" ]] && VERIFY_ARGS+=(--base "$BASE")

    $TIERING "${VERIFY_ARGS[@]}"
    VERIFY_STATUS=$?

    if [[ "$VERIFY_STATUS" -ne 0 ]]; then
        echo "" >&2
        echo "test-tier: RUNTIME VERIFICATION FAILED for tier '$TIER'." >&2
        echo "test-tier: one or more units exceed the '$TIER' budget despite their" >&2
        echo "test-tier: classification. This requires review and a re-classification" >&2
        echo "test-tier: proposal; see $VERIFY_REPORT" >&2
        echo "test-tier: (proposal: move the flagged unit(s) to a slower tier in" >&2
        echo "test-tier:  test-tiers.toml, or make them genuinely fast)." >&2
        STATUS=1
    fi
fi

echo ""
if [[ "$UNIT_COUNT" -gt 0 ]]; then
    echo "test-tier: $TIER ran $UNIT_COUNT unit(s), $FAILED failure(s)."
fi
echo "test-tier: report written to $REPORT"
if [[ "$VERIFY_RUNTIME" == "1" ]]; then
    echo "test-tier: runtime verification written to $VERIFY_REPORT"
fi
exit "$STATUS"
