"""merge_rows()'s outer join: every id from EITHER source must appear
exactly once in the output, carrying whichever fields that source actually
had -- neither all products.xlsx nor all_products_class.xlsx is a complete
list on its own (22 ids only in the former, 78 only in the latter, of 5,735
total real ids across both), so dropping either source's unique ids would
silently lose real products from the merged reference sheet.
"""
from build_master_sheet import merge_rows


class TestMergeRows:
    def test_an_id_in_both_sources_gets_fields_from_both(self):
        catalogue = {"1": {"name": "WIDGET", "pack": 12, "size": "1 OZ", "retail": 1.99, "price": 12.0}}
        classes = {"1": {"name": "WIDGET", "category": "FOOD", "subcategory": "SNACKS", "otp_level": None}}
        merged = merge_rows(catalogue, classes)
        assert merged == [{
            "id": "1", "name": "WIDGET", "pack": 12, "size": "1 OZ", "retail": 1.99, "price": 12.0,
            "category": "FOOD", "subcategory": "SNACKS", "otp_level": None,
            "in_catalogue_file": True, "in_class_file": True,
        }]

    def test_an_id_only_in_the_catalogue_file_keeps_its_pack_and_size(self):
        catalogue = {"2": {"name": "GADGET", "pack": 6, "size": "2OZ", "retail": 3.99, "price": 20.0}}
        classes = {}
        merged = merge_rows(catalogue, classes)
        assert len(merged) == 1
        assert merged[0]["id"] == "2"
        assert merged[0]["pack"] == 6
        assert merged[0]["category"] is None
        assert merged[0]["in_catalogue_file"] is True
        assert merged[0]["in_class_file"] is False

    def test_an_id_only_in_the_class_file_keeps_its_category(self):
        catalogue = {}
        classes = {"3": {"name": "THING", "category": "DRINK", "subcategory": None, "otp_level": None}}
        merged = merge_rows(catalogue, classes)
        assert len(merged) == 1
        assert merged[0]["id"] == "3"
        assert merged[0]["category"] == "DRINK"
        assert merged[0]["pack"] is None
        assert merged[0]["in_catalogue_file"] is False
        assert merged[0]["in_class_file"] is True

    def test_no_id_is_ever_dropped(self):
        catalogue = {str(i): {"name": f"cat-{i}"} for i in range(50)}
        classes = {str(i): {"name": f"cls-{i}"} for i in range(25, 75)}
        merged = merge_rows(catalogue, classes)
        expected_ids = set(catalogue) | set(classes)
        assert {r["id"] for r in merged} == expected_ids
        assert len(merged) == len(expected_ids)  # no duplicates either

    def test_catalogue_name_is_preferred_when_both_sources_have_one(self):
        # The catalogue file is the pipeline's own source of truth for
        # naming -- the class export's name is only a fallback for ids the
        # catalogue file doesn't have at all.
        catalogue = {"1": {"name": "CATALOGUE NAME"}}
        classes = {"1": {"name": "CLASS FILE NAME"}}
        merged = merge_rows(catalogue, classes)
        assert merged[0]["name"] == "CATALOGUE NAME"

    def test_numeric_ids_sort_numerically_not_lexicographically(self):
        catalogue = {"20": {"name": "B"}, "100": {"name": "A"}, "3": {"name": "C"}}
        merged = merge_rows(catalogue, {})
        assert [r["id"] for r in merged] == ["3", "20", "100"]

    def test_a_non_numeric_id_does_not_crash_the_sort(self):
        catalogue = {"20": {"name": "B"}, "MEMOBOOK 75 SHEETS": {"name": "A"}}
        merged = merge_rows(catalogue, {})
        ids = [r["id"] for r in merged]
        assert set(ids) == {"20", "MEMOBOOK 75 SHEETS"}
