"""Tests for lint output parsers (ruff, pylint, eslint, semgrep)."""

from __future__ import annotations

import json

from src.code_sandbox_mcp.edit_verify import (
    _determine_lint_severity,
    _parse_eslint_output,
    _parse_pylint_output,
    _parse_ruff_output,
    _parse_semgrep_output,
)

# ===================================================================
# _parse_ruff_output tests
# ===================================================================




class TestParseRuffOutput:
    """Tests for ruff JSON output parsing."""

    def test_empty_output(self) -> None:
        assert _parse_ruff_output("", "file.py") == []

    def test_single_issue(self) -> None:
        raw = json.dumps(
            [
                {
                    "filename": "test.py",
                    "location": {"row": 5},
                    "code": "F401",
                    "message": "`os` imported but unused",
                },
            ]
        )
        result = _parse_ruff_output(raw, "file.py")
        assert len(result) == 1
        assert result[0]["file"] == "test.py"
        assert result[0]["line"] == 5
        assert result[0]["rule"] == "F401"
        assert "unused" in result[0]["message"]

    def test_multiple_issues(self) -> None:
        raw = json.dumps(
            [
                {
                    "filename": "a.py",
                    "location": {"row": 1},
                    "code": "E302",
                    "message": "blank lines",
                },
                {
                    "filename": "a.py",
                    "location": {"row": 5},
                    "code": "W291",
                    "message": "trailing space",
                },
            ]
        )
        result = _parse_ruff_output(raw, "file.py")
        assert len(result) == 2

    def test_invalid_json(self) -> None:
        assert _parse_ruff_output("not json", "file.py") == []


# ===================================================================
# _parse_pylint_output tests
# ===================================================================




class TestParsePylintOutput:
    """Tests for pylint JSON output parsing."""

    def test_empty_output(self) -> None:
        assert _parse_pylint_output("", "file.py") == []

    def test_single_issue(self) -> None:
        raw = json.dumps(
            [
                {
                    "path": "test.py",
                    "line": 10,
                    "symbol": "unused-import",
                    "message-id": "W0611",
                    "message": "Unused import os",
                },
            ]
        )
        result = _parse_pylint_output(raw, "file.py")
        assert len(result) == 1
        assert result[0]["file"] == "test.py"
        assert result[0]["line"] == 10
        assert result[0]["rule"] == "unused-import"

    def test_invalid_json(self) -> None:
        assert _parse_pylint_output("corrupt", "file.py") == []


# ===================================================================
# _parse_eslint_output tests
# ===================================================================




class TestParseEslintOutput:
    """Tests for eslint JSON output parsing."""

    def test_empty_output(self) -> None:
        assert _parse_eslint_output("", "file.js") == []

    def test_single_issue(self) -> None:
        raw = json.dumps(
            [
                {
                    "filePath": "/app/file.js",
                    "messages": [
                        {
                            "line": 5,
                            "ruleId": "no-unused-vars",
                            "message": "'x' is defined but never used",
                        },
                    ],
                },
            ]
        )
        result = _parse_eslint_output(raw, "file.js")
        assert len(result) == 1
        assert result[0]["file"] == "/app/file.js"
        assert result[0]["line"] == 5
        assert result[0]["rule"] == "no-unused-vars"

    def test_multiple_files(self) -> None:
        raw = json.dumps(
            [
                {
                    "filePath": "a.js",
                    "messages": [{"line": 1, "ruleId": "R1", "message": "m1"}],
                },
                {
                    "filePath": "b.js",
                    "messages": [{"line": 2, "ruleId": "R2", "message": "m2"}],
                },
            ]
        )
        result = _parse_eslint_output(raw, "file.js")
        assert len(result) == 2

    def test_invalid_json(self) -> None:
        assert _parse_eslint_output("bad", "file.js") == []


# ===================================================================
# _parse_pyright_output tests
# ===================================================================




class TestParseSemgrepOutput:
    """Tests for semgrep --json output parsing."""

    def test_empty_output(self) -> None:
        assert _parse_semgrep_output("", "file.py") == []

    def test_single_finding(self) -> None:
        raw = json.dumps(
            {
                "results": [
                    {
                        "check_id": "python.lang.security.audit.sql-injection",
                        "path": "app.py",
                        "start": {"line": 42, "col": 5},
                        "end": {"line": 42, "col": 20},
                        "extra": {
                            "severity": "ERROR",
                            "message": "Detected SQL injection risk",
                        },
                    }
                ],
            }
        )
        result = _parse_semgrep_output(raw, "file.py")
        assert len(result) == 1
        assert result[0]["file"] == "app.py"
        assert result[0]["line"] == 42
        assert result[0]["rule"] == "python.lang.security.audit.sql-injection"
        assert result[0]["severity"] == "ERROR"
        assert "SQL injection" in result[0]["message"]

    def test_multiple_findings_mixed_severity(self) -> None:
        raw = json.dumps(
            {
                "results": [
                    {
                        "check_id": "rule-one",
                        "path": "a.py",
                        "start": {"line": 1},
                        "extra": {"severity": "ERROR", "message": "error msg"},
                    },
                    {
                        "check_id": "rule-two",
                        "path": "b.py",
                        "start": {"line": 5},
                        "extra": {"severity": "WARNING", "message": "warning msg"},
                    },
                    {
                        "check_id": "rule-three",
                        "path": "c.py",
                        "start": {"line": 10},
                        "extra": {"severity": "INFO", "message": "info msg"},
                    },
                ],
            }
        )
        result = _parse_semgrep_output(raw, "file.py")
        assert len(result) == 3
        assert result[0]["severity"] == "ERROR"
        assert result[1]["severity"] == "WARNING"
        assert result[2]["severity"] == "INFO"

    def test_no_results_key(self) -> None:
        raw = json.dumps({"errors": [{"message": "parse error"}]})
        result = _parse_semgrep_output(raw, "file.py")
        assert result == []

    def test_invalid_json(self) -> None:
        assert _parse_semgrep_output("not json", "file.py") == []

    def test_finding_with_missing_fields(self) -> None:
        """Missing severity defaults to WARNING, missing start.line to 0."""
        raw = json.dumps(
            {
                "results": [
                    {
                        "check_id": "rule-minimal",
                        "path": "min.py",
                    }
                ],
            }
        )
        result = _parse_semgrep_output(raw, "file.py")
        assert len(result) == 1
        assert result[0]["file"] == "min.py"
        assert result[0]["line"] == 0
        assert result[0]["severity"] == "WARNING"
        assert result[0]["message"] == ""


# ===================================================================
# run_verify gate logic tests (Issue #54)
# ===================================================================




class TestDetermineLintSeverity:
    """Tests for lint severity mapping from rule codes."""

    def test_error_rules(self) -> None:
        assert _determine_lint_severity("E501") == "error"
        assert _determine_lint_severity("F401") == "error"
        assert _determine_lint_severity("B006") == "error"
        assert _determine_lint_severity("RUF001") == "error"

    def test_warning_rules(self) -> None:
        assert _determine_lint_severity("W291") == "warning"
        assert _determine_lint_severity("S101") == "warning"
        assert _determine_lint_severity("C901") == "warning"
        assert _determine_lint_severity("N801") == "warning"
        assert _determine_lint_severity("D100") == "warning"

    def test_info_rules(self) -> None:
        assert _determine_lint_severity("I001") == "info"
        assert _determine_lint_severity("SIM101") == "info"
        assert _determine_lint_severity("PLW0603") == "info"
        assert _determine_lint_severity("UP006") == "info"
        assert _determine_lint_severity("TCH001") == "info"

    def test_unknown_rule_defaults_to_error(self) -> None:
        assert _determine_lint_severity("XYZ999") == "error"

    def test_empty_rule_defaults_to_error(self) -> None:
        assert _determine_lint_severity("") == "error"

    def test_longest_prefix_match(self) -> None:
        """C90 should match C90 prefix, not C prefix."""
        assert _determine_lint_severity("C901") == "warning"


# ===================================================================
# _parse_semgrep_output tests (Issue #54)
# ===================================================================




class TestLintFileParsers:
    """Edge cases for linter output parsers."""

    def test_ruff_no_issues(self) -> None:
        """Clean ruff output returns empty list."""
        assert _parse_ruff_output("[]", "file.py") == []

    def test_pylint_no_issues(self) -> None:
        assert _parse_pylint_output("[]", "file.py") == []

    def test_eslint_no_issues(self) -> None:
        assert _parse_eslint_output("[]", "file.js") == []

    def test_ruff_non_list_json(self) -> None:
        """Ruff output that is valid JSON but not a list."""
        assert _parse_ruff_output('{"summary": "ok"}', "file.py") == []


# ===================================================================
# type_check_file parsers: edge cases
# ===================================================================

