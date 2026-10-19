"""check_watermarks.py's one pure piece: parsing psql's pipe-delimited
output. Exists because this script caught a real, silent data gap once (7
products live in the DB as accepted/review with a real sourced image and no
watermark -- from an ad-hoc debug script that bypassed the normal sourcing
flow, not from run_source_job.py's own logic) with zero prior signal until
someone happened to open a downloaded ZIP and notice.
"""
from check_watermarks import parse_psql_rows


class TestParsePsqlRows:
    def test_parses_id_batch_image_url_name(self):
        stdout = "109548|drinks|/images/library/drinks/109548.jpg|100 GRAND KING SIZE 24CT\n"
        rows = parse_psql_rows(stdout)
        assert rows == [{
            "id": "109548", "name": "100 GRAND KING SIZE 24CT",
            "batch": "drinks", "image_url": "/images/library/drinks/109548.jpg",
        }]

    def test_parses_multiple_rows(self):
        stdout = (
            "1|drinks|/images/a.jpg|A\n"
            "2|drinks|/images/b.jpg|B\n"
        )
        rows = parse_psql_rows(stdout)
        assert [r["id"] for r in rows] == ["1", "2"]

    def test_empty_output_is_no_rows(self):
        assert parse_psql_rows("") == []
        assert parse_psql_rows("\n\n") == []

    def test_a_pipe_inside_the_name_does_not_break_parsing(self):
        # name is the LAST selected column specifically so this can't happen:
        # split("|", 3) takes id/batch/image_url off the front (none of
        # which can contain "|") and everything left over -- including any
        # embedded "|" -- becomes name intact.
        stdout = "1|drinks|/images/a.jpg|Chips A | B Variety Pack\n"
        rows = parse_psql_rows(stdout)
        assert rows == [{
            "id": "1", "name": "Chips A | B Variety Pack",
            "batch": "drinks", "image_url": "/images/a.jpg",
        }]
