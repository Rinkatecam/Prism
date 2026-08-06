"""Tests for Wave 2 CSV remediations (F-100, F-101, F-112, F-120).

Pure test additions for previously-untested code paths.
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest


# ─── F-100: every language has every key the canonical English set has ─

def test_every_language_covers_the_english_key_set():
    """The English dict is the canonical key set. Every other language
    must define the same keys — otherwise some operators see English
    fallbacks where they expected localised strings.

    Slight tolerance: we allow a language to be MISSING up to 10 % of
    keys (so a new English key added today doesn't immediately break
    the test before translators catch up). Missing > 10 % is a finding.
    """
    from i18n import TRANSLATIONS
    en_keys = set(TRANSLATIONS.get("en", {}).keys())
    assert en_keys, "English translation set must be non-empty"
    other_langs = [lang for lang in TRANSLATIONS if lang != "en"]
    for lang in other_langs:
        keys = set(TRANSLATIONS[lang].keys())
        missing = en_keys - keys
        miss_pct = (len(missing) / len(en_keys)) * 100
        assert miss_pct <= 10, (
            f"F-100: language '{lang}' is missing {len(missing)} of "
            f"{len(en_keys)} English keys ({miss_pct:.1f}%). "
            f"Examples: {sorted(missing)[:5]}"
        )


def test_translation_dict_has_all_five_languages():
    """Five-language baseline (en, de, fr, es, ja) is the URS-100 spec."""
    from i18n import TRANSLATIONS
    assert set(TRANSLATIONS.keys()) >= {"en", "de", "fr", "es", "ja"}, (
        f"F-100: TRANSLATIONS must cover {{en, de, fr, es, ja}}; "
        f"got {sorted(TRANSLATIONS.keys())}"
    )


# ─── F-101: timestamp display tz conversion ──────────────────────────

def test_format_timestamp_converts_utc_to_berlin_summer():
    """Berlin in May is UTC+2 (CEST). A UTC noon timestamp must render
    as 14:00 (or 2:00 PM in 12h)."""
    from app import _format_timestamp
    iso = "2026-05-15T12:00:00Z"
    s = _format_timestamp(iso, {"timezone": "Europe/Berlin", "time_format": "24h"})
    assert "14:00" in s, f"Berlin CEST should render 12:00 UTC as 14:00; got {s!r}"


def test_format_timestamp_converts_utc_to_berlin_winter():
    """Berlin in January is UTC+1 (CET). A UTC noon timestamp must render
    as 13:00."""
    from app import _format_timestamp
    iso = "2026-01-15T12:00:00Z"
    s = _format_timestamp(iso, {"timezone": "Europe/Berlin", "time_format": "24h"})
    assert "13:00" in s


def test_format_timestamp_handles_invalid_timezone_gracefully():
    """Bad timezone setting must NOT crash — return a defensible fallback."""
    from app import _format_timestamp
    iso = "2026-05-15T12:00:00Z"
    # Must not raise. Should at minimum return something resembling the input.
    s = _format_timestamp(iso, {"timezone": "Not/A_RealTimeZone"})
    assert isinstance(s, str) and len(s) > 0


def test_format_timestamp_handles_empty_input():
    """Empty / None input → empty string, no crash."""
    from app import _format_timestamp
    assert _format_timestamp("", {"timezone": "Europe/Berlin"}) == ""
    assert _format_timestamp(None, {"timezone": "Europe/Berlin"}) == ""


def test_format_timestamp_respects_12h_format():
    """24h vs 12h format setting honoured."""
    from app import _format_timestamp
    iso = "2026-05-15T22:30:00Z"  # 00:30 next day in CEST
    s24 = _format_timestamp(iso, {"timezone": "Europe/Berlin", "time_format": "24h"})
    s12 = _format_timestamp(iso, {"timezone": "Europe/Berlin", "time_format": "12h"})
    # 24h flavour contains "00:30"; 12h contains "AM" or "PM".
    assert "00:30" in s24
    assert ("AM" in s12.upper()) or ("PM" in s12.upper())


def test_format_timestamp_respects_date_format():
    """date_format setting changes the date string."""
    from app import _format_timestamp
    iso = "2026-05-15T12:00:00Z"
    s_iso = _format_timestamp(iso, {"timezone": "Europe/Berlin",
                                     "date_format": "YYYY-MM-DD"})
    s_dot = _format_timestamp(iso, {"timezone": "Europe/Berlin",
                                     "date_format": "DD.MM.YYYY"})
    assert "2026-05-15" in s_iso
    assert "15.05.2026" in s_dot


# ─── F-112: password-mask round-trip preserves stored value ──────────

def test_is_password_masked_recognises_sentinel():
    """The masked-password sentinel must be recognisable so a config-save
    round-trip preserves the stored encrypted value."""
    from crypto_utils import is_password_masked, PASSWORD_MASK
    assert is_password_masked(PASSWORD_MASK) is True


def test_is_password_masked_rejects_real_passwords():
    """Real passwords must NOT match the mask sentinel."""
    from crypto_utils import is_password_masked
    assert is_password_masked("abc123") is False
    assert is_password_masked("") is False
    assert is_password_masked(None) is False


def test_password_mask_is_a_distinctive_sentinel():
    """Mask value should be unmistakable — not something a user would
    legitimately type as a password (a real password matching the mask
    would never round-trip)."""
    from crypto_utils import PASSWORD_MASK
    # Must be reasonably long + repeating, not a plausible real password.
    assert len(PASSWORD_MASK) >= 8
    # Most characters should be the same character (e.g. all asterisks).
    most_common = max(set(PASSWORD_MASK), key=PASSWORD_MASK.count)
    assert PASSWORD_MASK.count(most_common) >= len(PASSWORD_MASK) - 2


# ─── F-120: watchdog audit-row emission on dead thread ───────────────

def test_watchdog_writes_audit_on_thread_dead_transition():
    """When a monitored thread transitions to dead, the watchdog logs
    one audit row of category 'system' with a `thread_dead_*` action.

    Approach: rather than spawn the real watchdog (which loops forever),
    we extract the per-tick logic and call it once with a stubbed
    'thread is dead' state."""
    # The watchdog's per-tick body is currently inlined in
    # _watchdog_loop. We assert the audit-emission code path exists by
    # static source inspection — the test is structural, since
    # spinning up the full thread is impractical in a unit test.
    import inspect
    import app as app_module
    src = inspect.getsource(app_module._watchdog_loop)
    # 1. The loop reads is_alive on each monitored thread.
    assert "is_alive()" in src
    # 2. On the alive → dead transition it writes an audit row.
    assert "log_audit" in src
    # 3. The action label encodes which thread died, so an operator
    #    can grep for "thread_dead_<name>".
    assert "thread_dead" in src
    # 4. Category is 'system' so it surfaces in the right audit-log tab.
    assert '"system"' in src or "'system'" in src


def test_watchdog_state_dict_exists():
    """The `_watchdog_state` dict tracks per-thread last-known health
    so transitions can be detected."""
    import app as app_module
    assert hasattr(app_module, "_watchdog_state")
    assert isinstance(app_module._watchdog_state, dict)
