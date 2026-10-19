"""The deterministic gate is the last line of defence after the model chooses.

These lock in the behaviour that matters: unit conversion, and that the gate
revokes on a genuine contradiction while passing when evidence is merely absent.
"""
from lowprice.phase1 import gate


class TestParseSizeMl:
    def test_litres(self):
        assert gate.parse_size_ml("1 LITER") == 1000.0
        assert gate.parse_size_ml("2L") == 2000.0

    def test_ounces(self):
        assert gate.parse_size_ml("12 OZ") == 354.882

    def test_millilitres(self):
        assert gate.parse_size_ml("330ml") == 330.0

    def test_unparseable_is_none(self):
        assert gate.parse_size_ml("") is None
        assert gate.parse_size_ml("ECH") is None


class TestSizesMatch:
    def test_same_quantity_across_units(self):
        # 1 L and 33.8 OZ are the same drink, spelled differently.
        assert gate.sizes_match("1 LITER", "33.8 OZ") is True

    def test_genuinely_different(self):
        assert gate.sizes_match("1 LITER", "500 ml") is False

    def test_no_evidence_is_none(self):
        # "not legible" must not be treated as a mismatch.
        assert gate.sizes_match("1 LITER", "not legible") is None


def _verdict(**obs_overrides):
    """A verdict whose single observation passes everything, plus overrides."""
    from lowprice.phase1.verify import Verdict

    obs = {
        "index": 0, "flavour_ok": True, "container_ok": True, "size_ok": True,
        "packcount_ok": True, "clean_ok": True, "eligible": True,
        "has_overlay": False, "shows_pack": False, "detected_pack_count": 1,
        "detected_unit_size": "1 LITER",
    }
    obs.update(obs_overrides)
    return Verdict(best_index=0, confidence=0.9, reason="", required={},
                   observations=[obs])


class TestCheck:
    """`check` should revoke on contradiction, never on missing information."""

    def test_clean_verdict_passes(self):
        v = _verdict()
        ok, _ = gate.check(v, unit_size="1 LITER", pack_count=12)
        assert ok is True

    def test_wrong_pack_count_revoked(self):
        v = _verdict(shows_pack=True, detected_pack_count=24)
        ok, reason = gate.check(v, unit_size="1 LITER", pack_count=12)
        assert ok is False
        assert "pack" in reason.lower()

    def test_illegible_size_passes(self):
        v = _verdict(detected_unit_size="not legible")
        ok, _ = gate.check(v, unit_size="1 LITER", pack_count=12)
        assert ok is True, "absent evidence must not be treated as a mismatch"

    def test_no_choice_passes_through(self):
        from lowprice.phase1.verify import Verdict
        v = Verdict(best_index=None, confidence=0.0, reason="", required={},
                    observations=[])  # model declined outright
        ok, _ = gate.check(v, unit_size="1 LITER", pack_count=12)
        assert ok is True
