"""Flavour matching for background generation.

Two traps this guards against, both found in real catalogue rows:
  * the trailing parenthetical is a VENDOR code — "12PK SPRITE CAN (COKE)" is
    Sprite distributed by Coke, and must not get a cola background;
  * a real flavour must beat a product-category fallback, so "SELTZER LEMON"
    resolves to lemon rather than generic seltzer bubbles.
"""
import pytest

from lowprice import enhance


@pytest.mark.parametrize("name,expected", [
    ("1L POLAR SELTZER LEMON", "lemon"),
    ("1L POLAR SELTZER BLACK CHERRY", "black cherry"),
    ("1L POLAR SELTZER CRAN-LIME", "cran-lime"),
    ("1L POLAR SELTZER RUBY RED GRAP", "ruby red"),
    ("2L FANTA ORANGE (COKE)", "orange"),
    ("1L POLAR TONIC (POLAR)", "tonic"),
])
def test_flavour_resolution(name, expected):
    prompt = enhance._background_prompt(name)
    assert enhance.config.FLAVOR_BACKGROUND_HINTS[expected] in prompt


def test_vendor_code_is_not_a_flavour():
    """Sprite is distributed by Coke; it must not read as a cola."""
    prompt = enhance._background_prompt("12PK SPRITE CAN (COKE)")
    assert enhance.config.FLAVOR_BACKGROUND_HINTS["coke"] not in prompt


def test_specific_flavour_beats_category_fallback():
    lemon = enhance.config.FLAVOR_BACKGROUND_HINTS["lemon"]
    seltzer = enhance.config.FLAVOR_BACKGROUND_HINTS["seltzer"]
    prompt = enhance._background_prompt("1L POLAR SELTZER LEMON")
    assert lemon in prompt and seltzer not in prompt
