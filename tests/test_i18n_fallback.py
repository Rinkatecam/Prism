"""i18n English-fallback regression test (council audit P0).

get_translations returned a partial language dict as-is, so any key a locale
had not translated rendered as a BLANK label. It must now merge each locale
over the English base so missing keys fall back to English text.
"""

from __future__ import annotations

import i18n


def test_unknown_language_returns_english():
    assert i18n.get_translations("xx-unknown") == i18n.get_translations("en")


def test_missing_key_falls_back_to_english(monkeypatch):
    en = i18n.TRANSLATIONS["en"]
    sample_key = next(iter(en))
    # A locale that translated exactly one bespoke key and nothing else.
    partial = {"__unit_test_only_key__": "unique"}
    monkeypatch.setitem(i18n.TRANSLATIONS, "zz-test", partial)
    if hasattr(i18n, "_MERGED_CACHE"):
        i18n._MERGED_CACHE.clear()

    merged = i18n.get_translations("zz-test")
    # Missing English keys fall back to English rather than vanishing.
    assert merged[sample_key] == en[sample_key]
    # The locale's own translation is preserved.
    assert merged["__unit_test_only_key__"] == "unique"


def test_all_real_locales_cover_every_english_key():
    en = i18n.TRANSLATIONS["en"]
    for lang in i18n.TRANSLATIONS:
        merged = i18n.get_translations(lang)
        missing = [k for k in en if k not in merged]
        assert not missing, f"{lang!r} still missing {len(missing)} keys: {missing[:5]}"
