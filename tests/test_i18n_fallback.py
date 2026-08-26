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


def test_no_locale_defines_the_same_key_twice():
    """A duplicate key in a dict literal is legal Python and the LAST one wins,
    silently. Adding a key that already exists therefore does not fail, does
    not warn, and changes that string everywhere it is used — in the locale
    where the duplicate was added.

    It has to be an AST scan: by the time `import i18n` returns, the dict has
    already collapsed to one value per key and the evidence is gone."""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "i18n.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    dupes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        seen = {}
        for key in node.keys:
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            if key.value in seen:
                dupes.append((key.value, seen[key.value], key.lineno))
            else:
                seen[key.value] = key.lineno

    new = sorted(set(k for k, _, _ in dupes) - _KNOWN_DUPLICATE_KEYS)
    assert not new, (
        "keys newly defined twice in the same dict — the second silently "
        "replaces the first everywhere it is used: " + ", ".join(new))

    # And the baseline may only shrink.
    present = set(k for k, _, _ in dupes)
    assert present <= _KNOWN_DUPLICATE_KEYS, present - _KNOWN_DUPLICATE_KEYS
    stale = _KNOWN_DUPLICATE_KEYS - present
    assert not stale, (
        f"these are no longer duplicated: {sorted(stale)} — remove them from "
        "_KNOWN_DUPLICATE_KEYS so the baseline keeps ratcheting down")


def test_the_known_duplicates_are_the_ones_that_actually_disagree():
    """Documents WHICH of the baselined duplicates are live defects.

    Sixteen of the thirty-eight define the same string twice, which is clutter.
    The other twenty-two define DIFFERENT strings, and the second wins — so
    every call site that meant the first one is rendering the wrong words, in
    all five languages. `select_all` renders "All" where a caller wanted
    "Select All"; `per_day` renders "%/day" where a caller wanted "per day".

    Fixing them needs a decision per call site about which meaning was
    intended — two meanings shared one key, and the answer is two keys, not a
    better single string. Booked rather than guessed."""
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "i18n.py").read_text(encoding="utf-8")
    differing = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Dict):
            continue
        seen = {}
        for k, v in zip(node.keys, node.values):
            if not isinstance(k, ast.Constant) or not isinstance(k.value, str):
                continue
            val = ast.literal_eval(v) if isinstance(v, ast.Constant) else None
            if k.value in seen and seen[k.value] != val:
                differing.add(k.value)
            seen[k.value] = val
    assert differing <= _DISAGREEING_DUPLICATE_KEYS, (
        "a baselined duplicate started disagreeing with itself: "
        f"{sorted(differing - _DISAGREEING_DUPLICATE_KEYS)}")
    assert len(differing) <= len(_DISAGREEING_DUPLICATE_KEYS)


# ── duplicate keys, baselined ────────────────────────────────────────────
#
# Found when adding `tag_name` to all five locales and discovering it was
# already there. A duplicate key in a Python dict literal is legal and the
# LAST one silently wins, so the addition would have changed that string
# everywhere it is used, in every language, with no error anywhere.
#
# The scan then found thirty-eight already present. They may only go DOWN.
_KNOWN_DUPLICATE_KEYS = {
    "description", "dry_run", "execution_history", "loading", "per_day",
    "search", "select_all", "status", "thresholds",
}

# The subset whose two definitions DISAGREE. These are live defects rather
# than clutter: some call site renders words nobody chose for it, in all five
# languages. `select_all` resolves to "All" where a caller wanted "Select
# All"; `per_day` resolves to "%/day" where a caller wanted "per day";
# `thresholds` drops its "(%)".
#
# Fixing them needs a decision per call site about which meaning was intended.
# Two meanings shared one key, and the answer is two keys, not a better single
# string — so it is booked rather than guessed at here.
_DISAGREEING_DUPLICATE_KEYS = {
    "dry_run", "execution_history", "loading", "per_day", "search",
    "select_all", "thresholds",
}
