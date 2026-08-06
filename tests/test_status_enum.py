"""ServerStatus enum sanity checks."""

from models import ServerStatus


def test_enum_values_match_db_strings():
    assert ServerStatus.HEALTHY == "healthy"
    assert ServerStatus.WARNING.value == "warning"
    assert ServerStatus.CRITICAL == "critical"


def test_str_subclass_compares_to_strings():
    s = ServerStatus.HEALTHY
    assert s == "healthy"
    assert "is " + s == "is healthy"


def test_is_valid_blocks_typos():
    assert ServerStatus.is_valid("healthy") is True
    assert ServerStatus.is_valid("helthy") is False
    assert ServerStatus.is_valid("OK") is False


def test_values_includes_all_states():
    vals = set(ServerStatus.values())
    # If we add a new state to the enum, this guards against forgetting to
    # update DB constraints or template chips.
    assert vals == {
        "healthy", "queued", "updating", "restarting",
        "warning", "critical", "unreachable", "offline",
    }
