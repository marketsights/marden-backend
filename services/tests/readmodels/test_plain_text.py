"""Model versions stay out of the text the screens show (readmodels.plain_text): every read-model doc is
written without them, except Track record (its models table), and the keys that hold versions or links."""

from __future__ import annotations

import re

import pytest

from marden.readmodels.plain_text import strip_doc, strip_text

SHOWN_VERSION = re.compile(r"\bv0(\.\d)?\b")


@pytest.mark.parametrize(("text", "plain"), [
    ("Default recovery assumption (price v0.2)", "Default recovery assumption"),
    ("Powered-shell waterfall (recovery v0.2 (model v0))", "Powered-shell waterfall"),
    ("the 55c default that price v0.1 assumed for every facility",
     "the 55c default that the price model assumed for every facility"),
    ("Not derived: unsecured or unsited debt needs the issuer's balance-sheet waterfall, not in model v0.",
     "Not derived: unsecured or unsited debt needs the issuer's balance-sheet waterfall, not modelled yet."),
    ("No yield: non-USD note (no foreign-currency discount curve in v0.1).",
     "No yield: non-USD note (no foreign-currency discount curve yet)."),
    ("Ranked 1 of 15, with 54% odds of a 3-point move in 90 days (odds v0, uncalibrated).",
     "Ranked 1 of 15, with 54% odds of a 3-point move in 90 days (uncalibrated)."),
    ("Recovery v0.1", "Recovery model"),
    ("H100: $40,625 new, $5,896 at 4 years (gpu_model v0)", "H100: $40,625 new, $5,896 at 4 years"),
    ("against the stated HY (pricing-v0.1 Tier curves)", "against the stated HY (pricing method Tier curves)"),
    ("no version here", "no version here"),
])
def test_versions_leave_the_text(text, plain):
    assert strip_text(text) == plain
    assert not SHOWN_VERSION.search(strip_text(text))


def test_links_version_keys_and_track_record_keep_their_versions():
    assert strip_text("docs/methods/pricing-v0.1.md") == "docs/methods/pricing-v0.1.md"
    assert strip_text("see docs/methods/pricing-v0.1.md (price v0.2)") == "see docs/methods/pricing-v0.1.md"
    doc = {"recoveryDetail": {"method": {"value": "Default recovery assumption (price v0.2)", "tag": "M",
                                         "methodVersion": "price v0.2", "sourceUrl": "docs/methods/pricing-v0.1.md"}},
           "notes": ["not in model v0", 3, None], "version": "odds v0"}
    out = strip_doc(doc, "instrument_detail")
    assert out["recoveryDetail"]["method"] == {"value": "Default recovery assumption", "tag": "M",
                                               "methodVersion": "price v0.2",
                                               "sourceUrl": "docs/methods/pricing-v0.1.md"}
    assert out["notes"] == ["not modelled yet", 3, None] and out["version"] == "odds v0"
    models = {"models": [{"model": "price", "version": "price v0.2", "calibration": "price bands 'model v0'"}]}
    assert strip_doc(models, "track_record") == models


def test_upsert_writes_docs_without_versions(conn):
    from marden.readmodels.rebuild import upsert_doc

    upsert_doc(conn, "instrument_detail", "X", {"recovery": {"value": "55c assumed (price v0.2)",
                                                            "methodVersion": "price v0.2"}})
    doc = conn.execute("SELECT doc FROM read_model WHERE view = 'instrument_detail' AND key = 'X'").fetchone()["doc"]
    assert doc == {"recovery": {"value": "55c assumed", "methodVersion": "price v0.2"}}
