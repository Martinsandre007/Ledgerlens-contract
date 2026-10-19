# LedgerLens reference model

An independent Python model of the score contract's observable behaviour, written **only** from
[`docs/score-math.md`](../../docs/score-math.md), [`docs/interface-spec.md`](../../docs/interface-spec.md)
and the glossary. It never reads the contract source. It serves two purposes:

1. **Differential testing.** Generated operation sequences run through the model and through the real
   contract ([`driver/`](driver)), and any difference fails. That catches places where the code and
   the documentation disagree. Findings and how each was resolved are in
   [`DISAGREEMENTS.md`](DISAGREEMENTS.md).
2. **Integrator companion.** Import `ledgerlens_ref` to check your own assumptions about validation,
   decay, aggregation, hysteresis and gate decisions without running a Soroban environment.

## Covered behaviour

| Operation | Model | Contract call |
|---|---|---|
| `submit` | score/confidence range validation, hysteresis band update | `submit_score` |
| `weight`, `decay`, `floor` | pair weights, decay rate, global confidence floor | `set_pair_weight`, `set_decay_rate`, `set_global_min_confidence` |
| `get`, `eff`, `agg` | raw score, decayed effective score, weighted aggregate | `get_score`, `get_effective_score`, `get_aggregate_score` |
| `gate` | fail-closed gate with strict score check and confidence floors | `query_risk_gate_with_confidence` |

Out of scope for now: multisig/consensus, embargo, delegation, finality buffer, rate limiting.
The generator always advances past the 1-hour cooldown before submitting.

## Coverage of the model itself

Claiming to model an operation is only meaningful if the fixtures actually exercise that operation's
real input space. [`COVERAGE.md`](COVERAGE.md) audits every operation listed above: it characterizes
what the current unit tests and differential generator actually exercise for each one, compares that
against the operation's real input space (including documented boundary and adversarial values), and
records where coverage was materially narrow and how it was extended. It also records any genuine
model/contract disagreement that the extended coverage surfaced, and on which side it was fixed.

**Bar for future additions.** Any new operation, branch or validation rule added to the reference
model must ship with fixtures that exercise a genuinely representative range of inputs for it, not
just the happy path: documented boundaries (inclusive/exclusive edges, zero, max, saturation points),
adversarial values (out-of-range, stale, exactly-at-threshold, off-by-one), and the property-based
generator wired up to produce them. Add the new row to `COVERAGE.md` in the same change; a modeled
function without a characterized, representative fixture set is treated as a coverage gap.
=======

## Usage

```bash ignore
# unit tests (doc truth tables, decay/aggregate examples)
python3 -m unittest discover tools/reference-model

# differential run: build the driver, then compare
cargo build --release -p reference-model-driver
python3 tools/reference-model/run_diff.py --sequences 100000 --seed 1
```

Using the model directly:

```python
import ledgerlens_ref as ref
m = ref.Model()
m.submit(0, "XLM_USDC", 60, 90)
assert m.gate(0, "XLM_USDC", 70, 80)  # 60 < 70 and 90 >= 80
```

The nightly workflow `.github/workflows/reference-model.yml` runs 100,000 sequences with a fresh seed
each night. On a failure, the run prints the shortest failing op sequence per operation type. Record
it in `DISAGREEMENTS.md` and classify it before changing the code, the docs, or the model.

When you add or change a modeled operation, update [`COVERAGE.md`](COVERAGE.md) alongside the code so
the claimed coverage and the fixtures that back it stay in sync.
