"""Phase 2 safety guards, exercised against a mock of the real Woopsa DOM.

`tests/fixtures_woopsa.html` reproduces the parts that matter: a debounced
search over a fixed item list, the two-slot image panel with its toggle and
Master/Distributor badge, and an Edit/Save pair that swaps visibility on save.

The two behaviours under test are the ones that would cause real damage:
uploading against an ambiguous name match, and saving into the shared MASTER
image slot.
"""
import ast
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

from lowprice.phase2 import oms_selectors as sel  # noqa: E402
from lowprice.phase2 import oms_uploader as u  # noqa: E402

FIXTURE = (Path(__file__).parent / "fixtures_woopsa.html").resolve().as_uri()


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    """A real JPEG named the way the uploader requires ("<item-id>.jpg").

    Made here rather than read from library_enhanced/, which is generated
    and gitignored -- depending on it made these tests pass only on a
    machine that had already run the pipeline.
    """
    from PIL import Image

    path = tmp_path_factory.mktemp("img") / "1050.jpg"
    Image.new("RGB", (64, 64), "white").save(path, "JPEG")
    return path


def _is_dry_run_check(test_node: ast.expr) -> bool:
    """True for the AST of an `if dry_run:` test -- both run_upload() and
    worker.py's upload_jobs() use a plain `dry_run` name, not a negation or
    comparison, so this only needs to match that one shape."""
    return isinstance(test_node, ast.Name) and test_node.id == "dry_run"


def _calls(source: str, func_name: str) -> bool:
    """True if `source` contains a call to a function/method named
    func_name, anywhere -- covers both a bare name (save_changes(...)) and
    an attribute access (oms.save_changes(...))."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", None)
        if name == func_name:
            return True
    return False


@pytest.fixture
def page():
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        # The fixture is a single file, so there is nowhere to navigate to.
        original_url, sel.PRODUCT_SEARCH_URL = sel.PRODUCT_SEARCH_URL, ""
        original_timeout, sel.DEFAULT_TIMEOUT_MS = sel.DEFAULT_TIMEOUT_MS, 4000
        original_save, sel.SAVE_TIMEOUT_MS = sel.SAVE_TIMEOUT_MS, 4000
        pg.goto(FIXTURE)
        yield pg
        sel.PRODUCT_SEARCH_URL = original_url
        sel.DEFAULT_TIMEOUT_MS = original_timeout
        sel.SAVE_TIMEOUT_MS = original_save
        browser.close()


class TestAmbiguityGuard:
    """0 or >1 matches must never be resolved by guessing."""

    def test_prefix_collision_resolves_to_the_exact_item(self, page):
        """"1049" also matches 10496, but only one row IS item 1049.

        Woopsa searches item #, description, UPC and vendor item # together, so
        short numeric ids routinely return several rows. Refusing them all
        blocked 23 real products; the uploader now picks the row whose item
        number is exactly the query, and still refuses when that is not unique.
        """
        assert u.find_product(page, "1049") is True
        header = page.locator(sel.PRODUCT_NAME_ON_PAGE).first.inner_text().strip()
        # The real OMS separates id and name with a newline, the fixture with a
        # space; both must resolve to item 1049 and not the 10496 decoy.
        assert header.startswith("#1049") and not header.startswith("#10496")

    def test_multiple_name_matches_refused(self, page):
        with pytest.raises(u.UploadError, match="ambiguous"):
            u.find_product(page, "COKE")

    def test_no_match_refused(self, page):
        with pytest.raises(u.UploadError, match="0 matches"):
            u.find_product(page, "NONEXISTENT-ITEM-XYZ")

    def test_unique_match_proceeds(self, page):
        assert u.find_product(page, "103313") is True


class TestDistributorSlotGuard:
    """Saving into MASTER would overwrite an image other distributors read."""

    def test_toggle_switches_to_distributor(self, page, image):
        u.find_product(page, "1050")
        u.attach_image(page, image)
        u.enable_distributor_image(page)
        badge = page.locator(sel.DISTRIBUTOR_BADGE).first.inner_text()
        assert "distributor" in badge.lower()

    def test_broken_toggle_blocks_save(self, page, image):
        u.find_product(page, "1050")
        u.attach_image(page, image)
        page.evaluate("document.getElementById('tg').onclick = null")
        with pytest.raises(u.UploadError, match="refusing to save"):
            u.enable_distributor_image(page)

    def test_save_refuses_while_master_active(self, page, image):
        """The guard sits next to the irreversible action, not only upstream."""
        u.find_product(page, "1050")
        u.attach_image(page, image)
        with pytest.raises(u.UploadError, match="refusing to save"):
            u.save_changes(page, dry_run=False)

    def test_dry_run_also_checks_the_slot(self, page, image):
        u.find_product(page, "1050")
        u.attach_image(page, image)
        with pytest.raises(u.UploadError, match="refusing to save"):
            u.save_changes(page, dry_run=True)


class TestSave:
    def test_dry_run_does_not_click_save(self, page, image):
        u.find_product(page, "103313")
        u.attach_image(page, image)
        u.enable_distributor_image(page)
        assert "dry_run" in u.save_changes(page, dry_run=True)
        assert "hidden" not in (page.locator("#saveBtn").get_attribute("class") or "")

    def test_real_save_confirms(self, page, image):
        u.find_product(page, "1050")
        u.attach_image(page, image)
        u.enable_distributor_image(page)
        assert "saved" in u.save_changes(page, dry_run=False)

    def test_file_actually_attached(self, page, image):
        u.find_product(page, "1050")
        u.attach_image(page, image)
        name = page.evaluate('document.getElementById("uploaded")?.files[0]?.name')
        assert name == "1050.jpg"


class TestSaveChangesNotInTheRealUploadPath:
    """Regression guard for a real bug: run_upload()'s (and worker.py's) real
    upload branch used to call save_changes() unconditionally after a
    successful attach_image(). attach_image() already persists the upload --
    confirmed directly in the OMS UI, and documented in both functions'
    comments -- so a save_changes() timeout (its #saveBtn doesn't apply to
    this action) was marking a product FAILED even though the image had
    already uploaded successfully.

    A full behavioral test would need a real OMS session (run_upload()
    requires config.BROWSER_PROFILE_DIR, a saved login, and a live OMS_URL --
    the other tests in this file bypass that by calling attach_image() /
    save_changes() directly against the fixture, never through run_upload()
    itself), so this is a structural check instead: the real (non-dry-run)
    branch's source must not reference save_changes.
    """

    def _real_branch_source(self, fn) -> str:
        import ast
        import inspect
        import textwrap

        source = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(source)
        func_node = tree.body[0]
        assert isinstance(func_node, ast.FunctionDef)

        # Both functions have more than one `if dry_run:` (e.g. a closing
        # log line), so identify the right one by its else branch actually
        # containing the attach_image() call, not just by being first --
        # that's the real upload branch, the one save_changes() must not be
        # in. Walking the AST (rather than a text/regex slice) means only
        # actual call expressions are ever checked, so an explanatory
        # comment mentioning save_changes() -- like the ones right next to
        # this branch in both functions -- can never produce a false
        # positive.
        candidates = [
            node for node in ast.walk(func_node)
            if isinstance(node, ast.If) and _is_dry_run_check(node.test) and node.orelse
        ]
        for node in candidates:
            branch = ast.unparse(node.orelse)
            if _calls(branch, "attach_image"):
                return branch
        raise AssertionError(
            f"no `if dry_run: ... else:` branch calling attach_image found in "
            f"{fn.__qualname__} -- test needs updating"
        )

    def test_run_upload_real_branch_does_not_call_save_changes(self):
        branch = self._real_branch_source(u.run_upload)
        assert not _calls(branch, "save_changes"), (
            "run_upload()'s real upload branch calls save_changes() again -- "
            "this is the bug that marked successful attaches as FAILED"
        )
        assert _calls(branch, "attach_image")  # sanity: found the right branch

    def test_worker_upload_real_branch_does_not_call_save_changes(self):
        import worker

        branch = self._real_branch_source(worker.upload_jobs)
        assert not _calls(branch, "save_changes"), (
            "worker.py's real upload branch calls oms.save_changes() again -- "
            "this is the bug that marked successful attaches as FAILED"
        )
        assert _calls(branch, "attach_image")  # sanity: found the right branch
