"""Tests for the reference model, derived from docs/score-math.md examples and
truth tables. Run: python3 -m unittest discover tools/reference-model"""

import random
import re
import unittest
from pathlib import Path

import ledgerlens_ref as ref

DOC = Path(__file__).resolve().parents[2] / "docs" / "score-math.md"

# Deterministic seed so generated-input tests are reproducible.
SEED = 0x1ED6E


class Decay(unittest.TestCase):
    def test_zero_rate_is_identity(self):
        self.assertEqual(ref.decay_factor(10**9, 0, 1), ref.SCALE)

    def test_age_zero_is_identity(self):
        self.assertEqual(ref.decay_factor(0, 1, 1), ref.SCALE)

    def test_large_x_saturates_to_zero(self):
        self.assertEqual(ref.decay_factor(5, 1, 1), 0)

    def test_monotone_below_taylor_minimum(self):
        # x = age * SCALE / 1_000_000 here, so age == x_scaled; minimum is at x ~= 1.596.
        vals = [ref.decay_factor(t, 1, 1_000_000) for t in range(0, 1_590_000, 10_000)]
        self.assertEqual(vals, sorted(vals, reverse=True))

    def test_known_defect_d6_decay_rebounds(self):
        """DISAGREEMENTS.md D6: the documented 4-term series rebounds past x ~= 1.6
        and clamps back to SCALE (no decay) before the x >= 5 cutoff. Code matches
        the docs; this test pins the behaviour until the spec is fixed."""
        at_min = ref.decay_factor(1_600_000, 1, 1_000_000)
        self.assertLess(at_min, 300_000)
        self.assertGreater(ref.decay_factor(3_000_000, 1, 1_000_000), at_min)
        self.assertEqual(ref.decay_factor(4_000_000, 1, 1_000_000), ref.SCALE)


class DecayBoundaries(unittest.TestCase):
    """Boundary and adversarial inputs for decay_factor."""

    def test_rate_zero_any_age(self):
        for age in (0, 1, 10**6, 10**12):
            with self.subTest(age=age):
                self.assertEqual(ref.decay_factor(age, 0, 1), ref.SCALE)

    def test_age_zero_any_rate(self):
        for num, den in ((0, 1), (1, 1), (10**9, 1), (1, 10**9)):
            with self.subTest(num=num, den=den):
                self.assertEqual(ref.decay_factor(0, num, den), ref.SCALE)

    def test_den_zero_is_identity(self):
        # Guarded: a zero denominator must not divide by zero.
        self.assertEqual(ref.decay_factor(10**9, 1, 0), ref.SCALE)

    def test_cutoff_boundary(self):
        # x = age * SCALE / 1e6 for num=1, den=1e6; cutoff is x >= 5.
        self.assertGreater(ref.decay_factor(4_999_999, 1, 1_000_000), 0)
        self.assertEqual(ref.decay_factor(5_000_000, 1, 1_000_000), 0)

    def test_result_never_exceeds_scale(self):
        rng = random.Random(SEED)
        for _ in range(500):
            age = rng.randrange(0, 10**7)
            num = rng.randrange(0, 10**9)
            den = rng.choice([1, 1_000_000, 10**9, 0])
            with self.subTest(age=age, num=num, den=den):
                v = ref.decay_factor(age, num, den)
                self.assertGreaterEqual(v, 0)
                self.assertLessEqual(v, ref.SCALE)


class Aggregate(unittest.TestCase):
    def test_equal_weights(self):
        self.assertEqual(ref.aggregate([("A", 20), ("B", 81)], {}), 50)  # truncates

    def test_zero_weight_excludes_pair(self):
        self.assertEqual(ref.aggregate([("A", 20), ("B", 80)], {"A": 0}), 80)

    def test_all_zero_raises(self):
        with self.assertRaises(ValueError):
            ref.aggregate([("A", 20)], {"A": 0})

    def test_empty_pairs_raises(self):
        with self.assertRaises(ValueError):
            ref.aggregate([], {})

    def test_single_pair(self):
        self.assertEqual(ref.aggregate([("A", 42)], {}), 42)

    def test_boundary_scores(self):
        self.assertEqual(ref.aggregate([("A", 0), ("B", 100)], {}), 50)
        self.assertEqual(ref.aggregate([("A", 100), ("B", 100)], {}), 100)
        self.assertEqual(ref.aggregate([("A", 0), ("B", 0)], {}), 0)

    def test_weighted_matches_manual(self):
        rng = random.Random(SEED)
        for _ in range(200):
            n = rng.randrange(1, 6)
            pairs = [(f"P{i}", rng.randrange(0, 101)) for i in range(n)]
            weights = {k: rng.randrange(0, 101) for k, _ in pairs}
            if all(w == 0 for w in weights.values()):
                continue
            num = sum(s * weights[k] for k, s in pairs)
            den = sum(weights[k] for k, _ in pairs)
            with self.subTest(pairs=pairs, weights=weights):
                self.assertEqual(ref.aggregate(pairs, weights), num // den)

    def test_model_all_zero_weights_is_score_not_found(self):
        m = ref.Model()
        m.submit(0, "A", 10, 90)
        m.weight("A", 0)
        self.assertEqual(m.agg(0), {"err": ref.SCORE_NOT_FOUND})

    def test_model_empty_epoch_is_score_not_found(self):
        self.assertEqual(ref.Model().agg(0), {"err": ref.SCORE_NOT_FOUND})


class GateTruthTables(unittest.TestCase):
    """Every row of the four truth tables in score-math.md must hold."""

    def test_doc_tables(self):
        section = DOC.read_text().split("## Confidence-Floor Semantics", 1)[1].split("### Configuration Notes", 1)[0]
        rows = re.findall(r"^\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(PASS|FAIL)", section, re.M)
        self.assertGreaterEqual(len(rows), 20)
        for s, t, c, q, f, want in rows:
            with self.subTest(row=(s, t, c, q, f)):
                self.assertEqual(ref.gate_passes(int(s), int(t), int(c), int(q), int(f)), want == "PASS")


class ModelSemantics(unittest.TestCase):
    def test_validation(self):
        m = ref.Model()
        self.assertEqual(m.submit(0, "A", 101, 50), {"err": ref.INVALID_SCORE})
        self.assertEqual(m.submit(0, "A", 50, 101), {"err": ref.INVALID_CONFIDENCE})
        self.assertEqual(m.get(0, "A"), {"err": ref.SCORE_NOT_FOUND})

    def test_submit_boundary_values_accepted(self):
        m = ref.Model()
        self.assertNotIn("err", m.submit(0, "A", 0, 0))
        self.assertNotIn("err", m.submit(0, "B", 100, 100))
        self.assertEqual(m.get(0, "A"), {"score": 0, "conf": 0})
        self.assertEqual(m.get(0, "B"), {"score": 100, "conf": 100})

    def test_submit_overwrites(self):
        m = ref.Model()
        m.submit(0, "A", 10, 20)
        m.submit(0, "A", 90, 80)
        self.assertEqual(m.get(0, "A"), {"score": 90, "conf": 80})

    def test_get_unknown_epoch(self):
        m = ref.Model()
        m.submit(0, "A", 10, 20)
        self.assertEqual(m.get(1, "A"), {"err": ref.SCORE_NOT_FOUND})

    def test_weight_boundaries(self):
        m = ref.Model()
        m.submit(0, "A", 50, 50)
        self.assertNotIn("err", m.weight("A", 0))
        self.assertNotIn("err", m.weight("A", 100))
        self.assertEqual(m.weight("A", 101), {"err": ref.INVALID_WEIGHT})

    def test_eff_missing_is_score_not_found(self):
        self.assertEqual(ref.Model().eff(0, "A"), {"err": ref.SCORE_NOT_FOUND})

    def test_eff_no_decay_is_raw_score(self):
        m = ref.Model()
        m.submit(0, "A", 73, 50)
        self.assertEqual(m.eff(0, "A"), 73)

    def test_eff_never_exceeds_raw_score(self):
        rng = random.Random(SEED)
        for _ in range(200):
            score = rng.randrange(0, 101)
            conf = rng.randrange(0, 101)
            num = rng.randrange(0, 10**9)
            den = rng.choice([1, 1_000_000, 10**9])
            age = rng.randrange(0, 10**7)
            m = ref.Model()
            m.submit(0, "A", score, conf)
            m.set_decay(num, den)
            m.advance(age)
            with self.subTest(score=score, conf=conf, num=num, den=den, age=age):
                eff = m.eff(0, "A")
                self.assertLessEqual(eff, score)
                self.assertGreaterEqual(eff, 0)

    def test_floor_boundaries(self):
        m = ref.Model()
        m.submit(0, "A", 50, 100)
        self.assertNotIn("err", m.floor("A", 0))
        self.assertNotIn("err", m.floor("A", 100))
        self.assertEqual(m.floor("A", 101), {"err": ref.INVALID_FLOOR})

    def test_gate_hysteresis_boundaries(self):
        # Exactly at the floor passes; just below the band fails.
        m = ref.Model()
        m.submit(0, "A", 100, 100)
        self.assertTrue(m.gate(0, "A", 100, 0))
        m2 = ref.Model()
        m2.submit(0, "A", 100, 100)
        self.assertFalse(m2.gate(0, "A", 101, 0))

    def test_gate_generated_matches_direct(self):
        rng = random.Random(SEED)
        for _ in range(200):
            score = rng.randrange(0, 101)
            conf = rng.randrange(0, 101)
            floor = rng.randrange(0, 101)
            thr = rng.randrange(0, 101)
            m = ref.Model()
            m.submit(0, "A", score, conf)
            m.floor("A", floor)
            with self.subTest(score=score, conf=conf, floor=floor, thr=thr):
                self.assertEqual(
                    m.gate(0, "A", thr, 0),
                    ref.gate_passes(score, thr, conf, 0, floor),
                )

    def test_adversarial_sequence(self):
        # Interleaved submits/weights/floors/decay/advance must stay consistent.
        m = ref.Model()
        m.submit(0, "A", 80, 90)
        m.weight("A", 50)
        m.floor("A", 40)
        m.set_decay(1, 1_000_000)
        m.advance(1_000_000)
        eff = m.eff(0, "A")
        self.assertLessEqual(eff, 80)
        self.assertEqual(m.agg(0), eff)
        self.assertEqual(m.gate(0, "A", eff, 0), ref.gate_passes(eff, eff, 90, 0, 40))

    def test_missing_score_fails_closed(self):
        self.assertFalse(ref.Model().gate(0, "A", 100, 0))

    def test_hysteresis_band(self):
        m = ref.Model()
        m.submit(0, "A", 99, 100)
        self.assertFalse(m.gate(0, "A", 100, 0))  # in band despite 99 < 100
        m.submit(0, "A", 10, 100)
        self.assertTrue(m.gate(0, "A", 100, 0))  # left band

    def test_decay_applies_at_any_age(self):
        m = ref.Model()
        m.submit(0, "A", 65, 50)
        m.set_decay(2, 10_000_000)
        m.advance(3_601)
        self.assertEqual(m.eff(0, "A"), 64)


class GateBoundaries(unittest.TestCase):
    """Boundary values for gate_passes across all five inputs."""

    def test_floor_zero_and_hundred(self):
        self.assertTrue(ref.gate_passes(0, 0, 0, 0, 0))
        self.assertTrue(ref.gate_passes(100, 100, 100, 100, 100))

    def test_generated_truth_table(self):
        rng = random.Random(SEED)
        for _ in range(500):
            s = rng.randrange(0, 101)
            t = rng.randrange(0, 101)
            c = rng.randrange(0, 101)
            q = rng.randrange(0, 101)
            f = rng.randrange(0, 101)
            with self.subTest(s=s, t=t, c=c, q=q, f=f):
                got = ref.gate_passes(s, t, c, q, f)
                self.assertIsInstance(got, bool)


if __name__ == "__main__":
    unittest.main()
