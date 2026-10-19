"""should_skip_resourcing() is the guard that stops /browse's "Source images"
from re-spending a search+vision call on an item that's already resolved --
and, more importantly, from letting ordinary search-result variance downgrade
an already-good accept. This exact bug shipped once: re-running an
already-accepted Tide product (109076) through this code path turned up a
different, worse search result and overwrote a confidence-1.0 accept with a
no_match, purely because the run happened to be repeated.
"""
import json
import sys

import pytest

import run_source_job
from run_source_job import main, should_skip_resourcing


class TestShouldSkipResourcing:
    def test_no_prior_record_is_not_skipped(self):
        assert should_skip_resourcing(None, redo=False) is False

    def test_accepted_is_protected(self):
        prior = {"status": "accepted", "confidence": 1.0}
        assert should_skip_resourcing(prior, redo=False) is True

    def test_review_is_protected(self):
        prior = {"status": "review", "confidence": 0.6}
        assert should_skip_resourcing(prior, redo=False) is True

    def test_no_match_is_retriable(self):
        # Nothing usable was found yet -- nothing to lose by trying again.
        prior = {"status": "no_match", "confidence": 0.0}
        assert should_skip_resourcing(prior, redo=False) is False

    def test_error_is_retriable(self):
        prior = {"status": "error", "confidence": 0.0}
        assert should_skip_resourcing(prior, redo=False) is False

    def test_redo_overrides_an_accepted_protection(self):
        prior = {"status": "accepted", "confidence": 1.0}
        assert should_skip_resourcing(prior, redo=True) is False

    def test_redo_with_no_prior_is_still_not_skipped(self):
        assert should_skip_resourcing(None, redo=True) is False


class TestMainRecordsCrashes:
    """main()'s docstring promises it never raises past itself without first
    recording the crash in the status file -- so the app's poll loop always
    has something to show instead of spinning forever. load_products() raises
    SystemExit (not a plain Exception) on a missing catalogue file, which is
    the regression: a bare `except Exception` would let that slip past
    silently, leaving the status file stuck on "running".
    """

    def test_a_missing_catalogue_file_is_recorded_as_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_source_job, "RUNS_DIR", tmp_path)
        monkeypatch.setattr(
            sys, "argv",
            ["run_source_job.py", "test-run", "1,2", "--catalogue-file", str(tmp_path / "missing.xlsx")],
        )

        with pytest.raises(SystemExit):
            main()

        status = json.loads((tmp_path / "test-run.json").read_text())
        assert status["status"] == "error"
        assert "missing.xlsx" in status["error"]


class TestMainDoneCountAtCompletion:
    """When one of the requested ids isn't in the catalogue file, the run
    still finishes and reports status="done" -- done must equal total at
    that point, or any progress display watching the ratio shows a
    completed run stuck short of 100% forever.
    """

    def test_done_equals_total_when_an_id_is_missing_from_the_catalogue(
        self, tmp_path, monkeypatch,
    ):
        catalogue = tmp_path / "catalogue.xlsx"
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Item Number", "Description", "Pack", "Size / UOM"])
        ws.append([100, "FIRST PRODUCT", 12, "12 OZ"])
        wb.save(catalogue)

        monkeypatch.setattr(run_source_job, "RUNS_DIR", tmp_path)
        monkeypatch.setattr(
            run_source_job, "process_product",
            lambda item_id, name, **kw: {
                "id": item_id, "name": name, "status": "accepted",
                "confidence": 1.0, "source": "", "image": "",
            },
        )
        # main() shells out to push_to_web.py after a non-skipped result;
        # stub it out rather than actually invoking it.
        monkeypatch.setattr(
            run_source_job.subprocess, "run",
            lambda *a, **kw: type("R", (), {"returncode": 0, "stderr": ""})(),
        )
        monkeypatch.setattr(
            sys, "argv",
            # "999" is not in the catalogue above -- the case that exposed
            # the undercount.
            ["run_source_job.py", "test-run-2", "100,999", "--catalogue-file", str(catalogue)],
        )

        main()

        status = json.loads((tmp_path / "test-run-2.json").read_text())
        assert status["status"] == "done"
        assert status["done"] == status["total"] == 2
