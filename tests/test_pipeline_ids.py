"""--ids scopes a run to specific item numbers, ignoring every other row --
the mechanism /browse's "Source images" uses to source just a selection
instead of a full/limit-N CLI run.
"""
import json

import pytest

from lowprice import config, pipeline


@pytest.fixture
def catalogue_csv(tmp_path):
    path = tmp_path / "catalogue.csv"
    path.write_text(
        "Item Number,Description,Pack,Size / UOM\n"
        "100,FIRST PRODUCT,12,12 OZ\n"
        "200,SECOND PRODUCT,1,1 LITER\n"
        "300,THIRD PRODUCT,6,6 OZ\n"
    )
    return path


class TestLoadProductsThenFilterByIds:
    """run()'s own filter step, exercised the way it actually runs: load all
    rows, then keep only the ones whose id is in the wanted set."""

    def test_ids_keeps_only_matching_rows(self, catalogue_csv):
        rows = pipeline.load_products(catalogue_csv)
        wanted = {"100", "300"}
        kept = [r for r in rows if r["id"] in wanted]
        assert {r["id"] for r in kept} == {"100", "300"}

    def test_ids_not_in_catalogue_are_silently_absent(self, catalogue_csv):
        rows = pipeline.load_products(catalogue_csv)
        wanted = {"100", "999999"}
        kept = [r for r in rows if r["id"] in wanted]
        # "999999" simply never matches a row -- the caller (run_source_job.py)
        # is the one that diffs wanted against found and reports the gap.
        assert [r["id"] for r in kept] == ["100"]

    def test_empty_ids_keeps_nothing(self, catalogue_csv):
        rows = pipeline.load_products(catalogue_csv)
        kept = [r for r in rows if r["id"] in set()]
        assert kept == []


class TestRunWithIds:
    """The actual run() entry point, with process_product stubbed out so this
    never makes a real search/vision call."""

    def test_only_wanted_ids_are_processed(self, catalogue_csv, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "MANIFEST_PATH", tmp_path / "manifest.json")
        processed = []

        def fake_process_product(item_id, name, **kwargs):
            processed.append(item_id)
            return {"id": item_id, "name": name, "status": "accepted",
                     "confidence": 1.0, "source": "test", "image": ""}

        monkeypatch.setattr(pipeline, "process_product", fake_process_product)

        pipeline.run(catalogue_csv, mock=False, limit=None, redo=False, ids={"100", "300"})

        assert sorted(processed) == ["100", "300"]

    def test_ids_and_limit_compose_ids_first(self, catalogue_csv, tmp_path, monkeypatch):
        """limit applies to the already-id-filtered list, not the full catalogue --
        otherwise --ids "200,300" --limit 1 could process row 100 instead."""
        monkeypatch.setattr(config, "MANIFEST_PATH", tmp_path / "manifest.json")
        processed = []
        monkeypatch.setattr(
            pipeline, "process_product",
            lambda item_id, name, **kw: processed.append(item_id) or
            {"id": item_id, "name": name, "status": "accepted", "confidence": 1.0,
             "source": "test", "image": ""},
        )

        pipeline.run(catalogue_csv, mock=False, limit=1, redo=False, ids={"200", "300"})

        assert processed == ["200"]  # first of the id-filtered set, not "100"

    def test_ids_still_respects_manifest_skip(self, catalogue_csv, tmp_path, monkeypatch):
        """An already-accepted id in the selection should still be skipped
        without --redo, same as a normal (non-ids) run."""
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps({
            "100": {"id": "100", "name": "FIRST PRODUCT", "status": "accepted",
                     "confidence": 1.0, "source": "prior", "image": "library/100.jpg"},
        }))
        monkeypatch.setattr(config, "MANIFEST_PATH", manifest_path)
        processed = []
        monkeypatch.setattr(
            pipeline, "process_product",
            lambda item_id, name, **kw: processed.append(item_id) or
            {"id": item_id, "name": name, "status": "accepted", "confidence": 1.0,
             "source": "test", "image": ""},
        )

        pipeline.run(catalogue_csv, mock=False, limit=None, redo=False, ids={"100", "200"})

        assert processed == ["200"]  # 100 was already accepted, so skipped
