"""OMS-specific selectors — THE ONLY FILE YOU SHOULD NEED TO EDIT.

Everything here describes YOUR order-management system's DOM. The upload logic
in oms_uploader.py is generic and reads from this file, so when the OMS changes
its markup you change these strings and nothing else.

------------------------------------------------------------------------------
HOW TO FILL THIS IN (5 minutes, one time)
------------------------------------------------------------------------------
1. Record yourself doing ONE upload by hand:

       python -m playwright codegen "<your-oms-url>"

   A browser and an inspector window open. Log in, search for a product by
   name, open it, upload an image, save. Then close the browser.

2. The inspector shows generated Python. Copy the selector out of each line
   into the matching slot below. E.g. if codegen produced

       page.get_by_placeholder("Search products").fill("COKE")
       page.get_by_role("button", name="Upload image").click()

   then set
       SEARCH_INPUT = 'placeholder=Search products'
       UPLOAD_BUTTON = 'role=button[name="Upload image"]'

3. Verify without touching live data:

       python oms_uploader.py --login          # save a session once
       python oms_uploader.py --dry-run --limit 1

Selector syntax: any Playwright selector string works —
  'css=#product-image'            CSS
  'text=Save'                     visible text
  'role=button[name="Save"]'      ARIA role (most robust)
  'placeholder=Search products'   placeholder attribute
  'xpath=//div[@id="x"]'          XPath (last resort — brittle)

Leave a value as "" and the uploader will tell you which one is missing rather
than clicking something random.
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# Login page
# --------------------------------------------------------------------------
# Woopsa uses passwordless email auth: you enter an address, it emails a code.
# There is no password field, so login CANNOT be fully automated — the emailed
# code is the entire credential. `--login` prefills the address and submits, then
# hands the browser to the operator for the code.
#
# The address is not a secret (it's the account identifier, not the credential),
# but it lives in .env so this file stays free of account details.
LOGIN_USERNAME_INPUT = 'css=input[name="email_phone"]'
LOGIN_PASSWORD_INPUT = ""  # no password field exists on this OMS
# These are MUI buttons whose label is a nested <span>, so they expose no ARIA
# accessible name and role= selectors do not match them. `text=` matches the
# span too (two hits); :has-text on the button element is the precise form.
LOGIN_SUBMIT_BUTTON = 'css=button[type="submit"]:has-text("Sign in")'

# Some tenants show an age-gate interstitial before the login form.
LOGIN_AGE_GATE_BUTTON = 'button:has-text("I am Over 21")'

# A selector present ONLY when logged in. The search box lives inside the admin
# shell, so if it's absent we're looking at a login page or an error.
LOGGED_IN_MARKER = "css=#searchInput"

# --------------------------------------------------------------------------
# Finding a product
# --------------------------------------------------------------------------
# Page to start each product lookup from. Relative to OMS_URL, or absolute.
PRODUCT_SEARCH_URL = "/admin/product-v1"

SEARCH_INPUT = "css=#searchInput"

# Woopsa filters as you type (debounced ~450ms), so there is nothing to submit.
SEARCH_SUBMIT = ""

# Rows in the result list. The uploader counts these: 0 or >1 matches means
# ambiguous, and it SKIPS rather than guess.
#
# Scoped to #resultList on purpose. The item number also appears in the editor
# panel (.ed-head, .sum-card, .sum-sub) and in the #resultList wrapper itself —
# an unscoped selector would count those too and make every search look
# ambiguous. `#resultList > div.res-row` matches exactly one element per product.
SEARCH_RESULT_ROWS = "css=#resultList > div.res-row"

# Within a result row, the element to click to open the product. Leave "" to
# click the row itself.
SEARCH_RESULT_LINK = ""

# Element on the opened product that carries the item number, used to confirm
# the right product loaded before anything is uploaded. .ed-head is the sticky
# header — it reads "#109536  ZYRTEC 1PK 20CT  ACTIVE ...".
PRODUCT_NAME_ON_PAGE = "css=#edSticky .ed-head"

# --------------------------------------------------------------------------
# Uploading the image
# --------------------------------------------------------------------------
# WHICH IMAGE SLOT — Woopsa Item Maintenance shows TWO dropzones per product:
#
#   MASTER      : the shared catalogue image. NOT ours to change — writing here
#                 would overwrite a record other distributors also read.
#   DISTRIBUTOR : our own image override. THIS is the target.
#
# The "Show Distributor Image" toggle switches which slot is displayed, and the
# badge beside it reads Master / Distributor accordingly. The uploader turns the
# toggle ON first so the file lands in the distributor slot, then verifies the
# badge before attaching anything.
IMAGE_SLOT = "distributor"  # "distributor" | "master" — do not change casually

# The "Show Distributor Image" toggle. Not a real <input type=checkbox> — it's a
# styled div, so is_checked() won't work on it and the badge below is the only
# reliable way to read its state.
DISTRIBUTOR_TOGGLE = 'css=#pv1ImageSlot div[title="Toggle which image is shown"]'

# The badge beside the toggle. Reads "Master" (class badge-dim) or "Distributor"
# (class badge-ok). Matched on the base .badge class so it's found in either
# state — the uploader reads its TEXT to decide, not its class.
#
# This is the hard pre-save gate: if it doesn't say Distributor, we don't save.
DISTRIBUTOR_BADGE = "css=#pv1ImageSlot span.badge"

# The distributor slot's <img>. Its src is the honest per-product signal: when a
# distributor image exists it points at the stored file, e.g.
#   .../product-images/<Distributor_Name>/100001.jpg
# and it is absent entirely when the slot is empty.
#
# The badge cannot be used for this. It reflects which slot is being VIEWED, and
# a product that already has an image loads showing "Distributor" before we
# upload anything — so a badge check passes for the wrong reason.
DISTRIBUTOR_IMAGE = 'css=#pv1ImageSlot img[alt="Distributor"]'

# Optional: a button that must be clicked before the image area is editable.
# NOT needed on Woopsa — clicking the distributor dropzone opens the file picker
# directly, no "Edit" required. Leave empty.
UPLOAD_OPEN_BUTTON = ""

# ---- Two ways to hand Playwright the file. Set exactly ONE. ----
#
# 1. FILE_INPUT — a real <input type="file"> in the DOM. Playwright sets the
#    file on it directly; nothing is clicked and no OS dialog opens. This is the
#    most reliable option when the input exists, even if it's hidden by CSS.
#
#    On Woopsa there are TWO of these (master + distributor), so this selector
#    MUST be scoped to the distributor one. The uploader refuses to run if the
#    selector matches more than one input.
FILE_INPUT = ""

# 2. FILE_CHOOSER_BUTTON — the clickable dropzone itself, when there is no
#    reachable <input>. Playwright intercepts the native file chooser that the
#    click opens, so no OS dialog is ever really shown.
#
#    On Woopsa this is the "Click to upload" area under the DISTRIBUTOR label.
#    Scope it to the distributor dropzone, not the master one above it.
#
#    The image panel is built from inline styles with no classes, so the only
#    stable hook is the dropzone's own title attribute — which conveniently says
#    "distributor" outright. The master zone has cursor:default and no click
#    handler at all, so it cannot be uploaded to even by accident.
FILE_CHOOSER_BUTTON = 'css=#pv1ImageSlot div[title="Click to upload a new distributor image"]'

# The button that commits the change. This is the ONLY click suppressed by
# --dry-run.
SAVE_BUTTON = "css=#saveBtn"

# Confirmation that the save actually succeeded (a toast, a success banner).
# Strongly recommended: without it the uploader cannot tell a save from a
# silent validation failure.
# Woopsa hides #saveBtn / #cancelEditBtn and un-hides #editBtn once a save
# completes, so "the Edit button is visible again" is the success signal.
SAVE_SUCCESS_MARKER = "css=#editBtn:not(.hidden)"

# Optional: an error/validation banner. If this appears, the upload is recorded
# as failed rather than succeeded.
SAVE_ERROR_MARKER = ""

# Woopsa keeps edit state per session, so a run that failed part-way leaves the
# product dirty and pops an "unsaved changes" dialog on the NEXT navigation.
# Left alone it blocks every following product, so the uploader dismisses it by
# discarding — safe here because the only edit we ever make is the image, and a
# half-finished one is exactly what we want thrown away.
UNSAVED_DIALOG_DISCARD = "css=#unsavedDiscardBtn"

# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------
DEFAULT_TIMEOUT_MS = 15_000
SAVE_TIMEOUT_MS = 30_000  # saves with an image upload can be slow


def missing_required() -> list[str]:
    """Names of the selectors that must be set before a real run.

    Login selectors are excluded: `--login` supports logging in by hand, which
    is the only option under SSO.
    """
    required = {
        "SEARCH_INPUT": SEARCH_INPUT,
        "SEARCH_RESULT_ROWS": SEARCH_RESULT_ROWS,
        "SAVE_BUTTON": SAVE_BUTTON,
    }
    names = [name for name, value in required.items() if not value.strip()]

    # One of these two has to be present, depending on how the OMS takes files.
    if not FILE_INPUT.strip() and not FILE_CHOOSER_BUTTON.strip():
        names.append("FILE_INPUT (or FILE_CHOOSER_BUTTON)")

    return names
