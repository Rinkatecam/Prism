"""Tests for `_format_ps_output` — the multi-stream PowerShell output
formatter that surfaces Write-Host / Write-Warning / Write-Verbose into
the workflow modal.

The 2026-05-21 bug: scripts that used `Write-Host "Done"` showed no
output in the workflow detail modal because `_run_ps` only returned the
pipeline success stream from `ps.invoke()`. On PowerShell 5.1+ Write-Host
routes to the information stream, which was being silently dropped.
These tests pin the new contract: ALL streams are surfaced.
"""

from __future__ import annotations

from types import SimpleNamespace

from workflow_engine import _format_ps_output


def _streams(*, error=None, warning=None, information=None,
             verbose=None, debug=None):
    """Build a stand-in ``ps.streams`` namespace with the requested
    records. Anything not specified defaults to empty so each test only
    has to declare the stream it cares about."""
    return SimpleNamespace(
        error=error or [],
        warning=warning or [],
        information=information or [],
        verbose=verbose or [],
        debug=debug or [],
    )


def _ps(streams):
    """Minimal `ps` object: just needs a `.streams` attribute."""
    return SimpleNamespace(streams=streams)


# ─────────────────────────────────────────────────────────────────────
# Single-stream behaviour
# ─────────────────────────────────────────────────────────────────────


def test_success_stream_only():
    """Pipeline output (Write-Output / implicit) renders as-is, no prefix."""
    result = ["Service status: Running", "Up: 3 days"]
    out = _format_ps_output(result, _ps(_streams()))
    assert out == "Service status: Running\nUp: 3 days"


def test_information_stream_only_no_pipeline_output():
    """The SRV01/operator-reported case: `Write-Host "Hello"` alone.
    Pipeline result is empty, but the information stream has the line.
    Before the fix this produced "" — silent black-box from the operator
    perspective."""
    out = _format_ps_output([], _ps(_streams(information=["Hello, world"])))
    assert "Hello, world" in out
    assert out  # not empty


def test_warning_stream_alone_is_visible():
    out = _format_ps_output([], _ps(_streams(warning=["disk getting full"])))
    assert "[warning]" in out
    assert "disk getting full" in out


def test_verbose_stream_alone_is_visible():
    out = _format_ps_output([], _ps(_streams(verbose=["entering function foo"])))
    assert "[verbose]" in out
    assert "entering function foo" in out


def test_error_stream_alone_is_visible():
    """Non-terminating errors (Write-Error) still need to surface even
    when had_errors is False — operator wants to see them."""
    out = _format_ps_output([], _ps(_streams(error=["access denied for X"])))
    assert "[error]" in out
    assert "access denied for X" in out


# ─────────────────────────────────────────────────────────────────────
# Multi-stream combinations
# ─────────────────────────────────────────────────────────────────────


def test_success_plus_info_no_prefix_when_only_info():
    """If pipeline result is empty but info has content, info renders
    without a prefix (it IS the main output, no need to disambiguate)."""
    out = _format_ps_output([], _ps(_streams(information=["progress: 50%"])))
    assert "progress: 50%" in out
    assert "[info]" not in out


def test_success_plus_info_gets_prefix():
    """When BOTH streams have content, info gets a [info] prefix so
    operators can tell them apart."""
    result = ["pipeline output line"]
    out = _format_ps_output(
        result,
        _ps(_streams(information=["info line"])),
    )
    assert "pipeline output line" in out
    assert "[info]\ninfo line" in out


def test_all_streams_render_in_stable_order():
    """Success → information → warning → verbose → error. Stable order
    matters so the modal renders consistently from run to run."""
    result = ["main"]
    out = _format_ps_output(
        result,
        _ps(_streams(
            information=["i"],
            warning=["w"],
            verbose=["v"],
            error=["e"],
        )),
    )
    # Find each section by its prefix
    pos_main = out.find("main")
    pos_info = out.find("[info]")
    pos_warn = out.find("[warning]")
    pos_verb = out.find("[verbose]")
    pos_err = out.find("[error]")
    assert pos_main < pos_info < pos_warn < pos_verb < pos_err


# ─────────────────────────────────────────────────────────────────────
# Robustness — operator scripts and pypsrp behaviour
# ─────────────────────────────────────────────────────────────────────


def test_correlation_id_prelude_record_is_filtered_out():
    """The S3-7 prelude writes ``[PrismCorrId=<id>]`` to the information
    stream so on-target events carry the correlation ID. We must NOT
    show this internal record to operators in the modal — it's noise."""
    out = _format_ps_output(
        ["real output"],
        _ps(_streams(information=["[PrismCorrId=abc123]", "Write-Host line"])),
    )
    assert "PrismCorrId" not in out
    assert "Write-Host line" in out


def test_empty_everything_yields_empty_string():
    out = _format_ps_output([], _ps(_streams()))
    assert out == ""


def test_none_records_are_tolerated():
    """Defensive: a None record in any stream must not crash the formatter."""
    out = _format_ps_output(
        [None, "actual line"],
        _ps(_streams(information=[None, "info line"])),
    )
    assert "actual line" in out
    assert "info line" in out


def test_missing_stream_attribute_is_tolerated():
    """Older pypsrp builds (or mocks) may lack one of the stream
    attributes. The formatter must not crash — falling back to whatever
    streams it can read."""
    streams = SimpleNamespace(error=[], warning=[])  # no information / verbose / debug
    # We expect this to NOT raise; either it returns empty or partial output.
    out = _format_ps_output(["x"], _ps(streams))
    assert "x" in out


# ─────────────────────────────────────────────────────────────────────
# Record-attribute extraction — the Start-ADSyncSyncCycle bug
# ─────────────────────────────────────────────────────────────────────


class _FakeInformationRecord:
    """Mimics pypsrp's InformationRecord which has a ``message_data``
    attribute. ``str(record)`` on the real thing often returns "None"
    when MessageData is unset, hiding the actual content."""
    def __init__(self, message_data):
        self.message_data = message_data
    def __str__(self):
        return "None"  # the documented bug — str() lies about the contents


def test_extracts_message_data_from_information_record():
    """The 2026-05-22 ``Start-ADSyncSyncCycle`` report: the modal showed
    ``[info]\\nNone`` because we did ``str(record)`` instead of reading
    the ``message_data`` attribute. Reproducer + fix:"""
    rec = _FakeInformationRecord("[PrismCorrId=abc] real prelude output")
    out = _format_ps_output([], _ps(_streams(information=[rec])))
    # PrismCorrId records are filtered, so this specific record gets dropped
    # — but the point is _extract_record_text reads message_data, not str()
    assert "None" not in out

    # And a regular Write-Host record (no PrismCorrId) makes it through
    rec2 = _FakeInformationRecord("Hello from Write-Host")
    out = _format_ps_output([], _ps(_streams(information=[rec2])))
    assert "Hello from Write-Host" in out
    assert "None" not in out


def test_dotted_type_name_str_is_filtered_as_noise():
    """The other half of the bug: ``str(<SchedulerOperationStatus>)``
    returns ``Microsoft.IdentityManagement.PowerShell.ObjectModel.
    SchedulerOperationStatus`` — the type name, not the value. That's
    useless noise. The fix wraps user scripts in ``| Out-String`` upstream
    (in ``_run_ps``), but if a record DOES slip through with a bare type
    name, the extractor recognises it and drops it."""
    class _BareTypeName:
        def __str__(self):
            return "Microsoft.IdentityManagement.PowerShell.ObjectModel.SchedulerOperationStatus"
        # No message_data / message attributes — only str() works
        message_data = None
        message = None

    out = _format_ps_output([], _ps(_streams(information=[_BareTypeName()])))
    assert "Microsoft." not in out, (
        "bare type-name str() must be filtered as noise"
    )


def test_record_with_none_str_is_filtered():
    """A record whose ``str()`` returns literal "None" must produce
    nothing (not ``[info]\\nNone``)."""
    class _NoneStr:
        message_data = None
        message = None
        def __str__(self): return "None"

    out = _format_ps_output([], _ps(_streams(information=[_NoneStr()])))
    assert out == ""


def test_error_record_via_error_details_message():
    """ErrorRecord uses ``error_details.message`` in pypsrp. The
    extractor should walk that path before falling back to str()."""
    class _ErrDetails:
        message = "Access is denied"
    class _ErrRec:
        message_data = None
        message = None
        error_details = _ErrDetails()
        def __str__(self): return "<ErrorRecord at 0x...>"

    out = _format_ps_output([], _ps(_streams(error=[_ErrRec()])))
    assert "[error]" in out
    assert "Access is denied" in out


# ─────────────────────────────────────────────────────────────────────
# Out-String wrapping — done by _run_ps itself, not by _format_ps_output
# but worth pinning that the wrapping is in place so future refactors
# don't silently revert.
# ─────────────────────────────────────────────────────────────────────


def test_run_ps_wraps_script_in_out_string():
    """`_run_ps` must wrap the user script in ``& { ... } | Out-String``
    so .NET objects format like a console session. Without this the
    SchedulerOperationStatus / Service / Process objects round-trip
    through pypsrp as their type name instead of their value."""
    import inspect
    import workflow_engine
    src = inspect.getsource(workflow_engine._run_ps)
    assert "Out-String" in src, (
        "_run_ps must wrap user scripts through Out-String so PS objects "
        "render with their console representation"
    )
    assert "& {" in src or "&{" in src, (
        "_run_ps must use a script block (& { ... }) wrapper so multi-line "
        "user scripts work correctly when piped to Out-String"
    )
