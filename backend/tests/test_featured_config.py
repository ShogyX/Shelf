"""`_clean_featured` is the only logic behind the Featured-this-week endpoints: it normalizes
untrusted admin input (method allow-list, rotateHours clamp, string-coercing the list fields)."""
from app.routers.settings import _clean_featured


def test_defaults_and_method_allowlist():
    assert _clean_featured({}) == {"method": "popular", "categories": [], "media": [], "rotateHours": 0}
    # An unknown method falls back to "popular" rather than passing through.
    assert _clean_featured({"method": "bogus"})["method"] == "popular"
    assert _clean_featured({"method": "NEWEST"})["method"] == "newest"  # case-insensitive


def test_rotate_clamped_and_lists_coerced():
    assert _clean_featured({"rotateHours": -5})["rotateHours"] == 0
    assert _clean_featured({"rotateHours": 99999})["rotateHours"] == 24 * 30  # capped at 30 days
    assert _clean_featured({"rotateHours": "oops"})["rotateHours"] == 0
    # List fields keep only strings/ints (coerced to str); junk is dropped.
    out = _clean_featured({"categories": ["Fantasy", 7, None, {"x": 1}], "media": ["Book"]})
    assert out["categories"] == ["Fantasy", "7"]
    assert out["media"] == ["Book"]


if __name__ == "__main__":
    test_defaults_and_method_allowlist()
    test_rotate_clamped_and_lists_coerced()
    print("ok")
