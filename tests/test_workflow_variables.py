"""Tests for {{step.X.output}} variable substitution in workflow blocks.

Operators reference output of upstream blocks in notification text fields
using ``{{step.<node_id>.output}}``. Substitution happens just-in-time
inside _execute_graph, AFTER the prior step has run, so the actual
output flows through to email body / webhook message / log entries.

The PowerShell ``script`` field is intentionally excluded — variables
there would bypass the ps_sandbox text-time gate.
"""

from __future__ import annotations

import pytest

import workflow_engine


def test_substitute_step_output():
    executed = {"1": (True, "Service Spooler: Running")}
    out = workflow_engine._substitute_variables(
        "Service status was: {{step.1.output}}",
        executed,
    )
    assert out == "Service status was: Service Spooler: Running"


def test_substitute_step_success_returns_string_bool():
    executed = {"1": (True, "x"), "2": (False, "y")}
    out_t = workflow_engine._substitute_variables("{{step.1.success}}", executed)
    out_f = workflow_engine._substitute_variables("{{step.2.success}}", executed)
    assert out_t == "true"
    assert out_f == "false"


def test_substitute_step_error_empty_on_success():
    executed = {"1": (True, "ok")}
    out = workflow_engine._substitute_variables("error: '{{step.1.error}}'", executed)
    assert out == "error: ''"


def test_substitute_step_error_carries_message_on_failure():
    executed = {"1": (False, "Connection refused")}
    out = workflow_engine._substitute_variables(
        "Failed: {{step.1.error}}", executed,
    )
    assert "Connection refused" in out


def test_substitute_workflow_context():
    out = workflow_engine._substitute_variables(
        "Workflow {{workflow.name}} (id={{workflow.id}})",
        executed={},
        workflow={"name": "Patch Tuesday", "id": 7},
    )
    assert out == "Workflow Patch Tuesday (id=7)"


def test_substitute_unknown_step_yields_marker():
    """Referencing a step that hasn't run (typo or wrong id) must NOT
    produce a misleading silent empty string. Show the operator that
    they referenced something that doesn't exist."""
    out = workflow_engine._substitute_variables(
        "{{step.99.output}}",
        executed={"1": (True, "ok")},
    )
    assert "unknown step" in out
    assert "99" in out


def test_substitute_unknown_field_yields_marker():
    out = workflow_engine._substitute_variables(
        "{{step.1.bogus}}",
        executed={"1": (True, "ok")},
    )
    assert "unknown field" in out


def test_substitute_no_variables_returns_input_unchanged():
    """Fast-path: text without {{ must short-circuit (it's the common
    case — most messages have no variables)."""
    plain = "Just a regular message with {curly} but not double-curly"
    assert workflow_engine._substitute_variables(plain, {}) == plain


def test_substitute_empty_string_is_fine():
    assert workflow_engine._substitute_variables("", {}) == ""
    assert workflow_engine._substitute_variables(None, {}) is None


def test_substitute_config_skips_script_key():
    """Variables in the PowerShell ``script`` field MUST NOT be
    substituted — that would bypass the ps_sandbox text-time check
    (the sandbox sees the template, not the resolved text)."""
    config = {
        "script": "{{step.1.output}}",   # NOT substituted
        "message": "{{step.1.output}}",  # IS substituted
    }
    out = workflow_engine._substitute_config_variables(
        config,
        executed={"1": (True, "DELETE * FROM users")},  # imagine malicious upstream
    )
    assert out["script"] == "{{step.1.output}}"  # untouched
    assert out["message"] == "DELETE * FROM users"


def test_substitute_config_preserves_non_string_values():
    config = {"threshold": 90, "retries": 3, "active": True, "msg": "x"}
    out = workflow_engine._substitute_config_variables(config, executed={})
    assert out == config  # nothing to substitute, everything passes through


def test_substitute_config_returns_new_dict():
    """The original config dict must NOT be mutated by execution-time
    substitution — it's persisted on the canvas as a template."""
    config = {"message": "{{step.1.output}}"}
    out = workflow_engine._substitute_config_variables(
        config, executed={"1": (True, "resolved")},
    )
    assert config["message"] == "{{step.1.output}}"  # unchanged
    assert out["message"] == "resolved"
    assert out is not config


def test_substitute_multiple_references_in_one_string():
    executed = {"1": (True, "alpha"), "2": (True, "beta")}
    out = workflow_engine._substitute_variables(
        "before {{step.1.output}} middle {{step.2.output}} end",
        executed,
    )
    assert out == "before alpha middle beta end"


def test_substitute_whitespace_inside_braces_tolerated():
    """``{{ step.1.output }}`` with leading/trailing whitespace must
    work — operators will inevitably add spaces inside the braces."""
    out = workflow_engine._substitute_variables(
        "{{ step.1.output }}",
        executed={"1": (True, "ok")},
    )
    assert out == "ok"
