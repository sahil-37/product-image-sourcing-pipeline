"""Query construction. The parenthetical in these catalogue names is a vendor
code ("12PK COKE CAN (COKE)"), not part of the product, so it is stripped from
the SEARCH QUERY only — never from the name shown to the vision model.
"""
from lowprice.phase1.normalize import normalize_name


class TestNormalizeName:
    def test_original_is_never_mutated(self):
        raw = "12PK COKE CAN (COKE)"
        assert normalize_name(raw).original == raw

    def test_parenthetical_stripped_from_query(self):
        assert "(" not in normalize_name("12PK COKE CAN (COKE)").query

    def test_asterisks_do_not_leak_into_query(self):
        q = normalize_name("1L POLAR SELTZER ORIGINAL*****").query
        assert "*" not in q

    def test_pack_token_extracted(self):
        assert "12pack" in normalize_name("12PK COKE CAN").packs
