"""Tests for the ``motim mcp`` CLI command and ``motim doctor`` MCP check."""

from __future__ import annotations

import json

from click.testing import CliRunner

from motim.cli.main import cli


def test_mcp_command_is_registered():
    runner = CliRunner()
    result = runner.invoke(cli, ["mcp", "--help"])
    assert result.exit_code == 0, result.output
    assert "MCP" in result.output or "Model Context Protocol" in result.output
    assert "--db" in result.output


def test_doctor_reports_mcp_check():
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "checks" in payload
    # mcp check appears in the doctor output
    assert any("mcp" in name.lower() for name in payload["checks"])


def test_doctor_mcp_does_not_drag_all_ok(tmp_path, monkeypatch):
    """The optional MCP check should not affect the overall all_ok verdict.

    When mcp is installed (as in this test environment), the value should
    still be considered informational. We verify by stripping it out and
    confirming all_ok is computed from the non-optional checks only.
    """
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor", "--json"])
    payload = json.loads(result.output)
    non_optional = {k: v for k, v in payload["checks"].items() if "optional" not in k.lower()}
    expected_all_ok = all(non_optional.values())
    assert payload["all_ok"] == expected_all_ok
