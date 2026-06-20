"""Regression tests for two PR111 defects.

1. **Token double-consume.**  ``dry_run -> approve -> execute`` burned the
   confirmation token at the *approve* step (both ``sandbox_approve`` and the
   dashboard called ``verify_and_consume``), so the very operation that was
   approved could no longer execute.  Approval is now non-consuming; the single
   one-time consume happens at execute, and only once the verify gate passes.

2. **Verify gate vs. missing tools.**  ``run_verify`` failed the submit gate on
   any ``not_available``/``error`` layer with no strict/lenient switch, so a
   sandbox image lacking ruff/pyright/pytest/semgrep could never submit — and
   the interactive ``verify`` was wrongly strict too.  The gate is now strict
   only for ``submit`` and only for layers that are actually gated.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from code_sandbox_mcp import token as token_mod
from code_sandbox_mcp.token import (
    generate_token,
    verify_token,
    verify_and_consume,
    reject_token,
)
from code_sandbox_mcp.edit_verify import VerifyResult, run_verify
from code_sandbox_mcp.server import submit, sandbox_approve, read_file_range


def _decode(result: str) -> dict:
    return json.loads(result)


def _fresh_token() -> str:
    token_mod._store.clear()
    return generate_token("submit", "details", "abc123def456", "run1")


# ---------------------------------------------------------------------------
# Bug 1 — token lifecycle
# ---------------------------------------------------------------------------


class TestVerifyTokenNonConsuming:
    """verify_token peeks; verify_and_consume is the single terminal consume."""

    def test_verify_token_does_not_consume(self) -> None:
        tok = _fresh_token()
        assert verify_token(tok) is not None
        assert verify_token(tok) is not None  # still valid after peeking
        assert verify_and_consume(tok) is not None  # the one real consume
        assert verify_token(tok) is None  # now exhausted
        assert verify_and_consume(tok) is None

    def test_reject_after_peek_invalidates(self) -> None:
        tok = _fresh_token()
        assert verify_token(tok) is not None
        assert reject_token(tok) is True
        assert verify_token(tok) is None

    def test_sandbox_approve_is_non_consuming(self) -> None:
        tok = _fresh_token()
        with patch("code_sandbox_mcp.server.record_boundary_crossing"):
            approval = _decode(sandbox_approve(tok))
        assert approval["status"] == "ok"
        # The approved token must still be consumable exactly once.
        assert verify_and_consume(tok) is not None


class TestApproveThenExecute:
    """The previously-broken dry_run -> approve -> execute path."""

    @patch("code_sandbox_mcp.server.run_verify")
    @patch("code_sandbox_mcp.server._docker")
    def test_full_flow_pushes(
        self,
        mock_docker: MagicMock,
        mock_verify: MagicMock,
    ) -> None:
        token_mod._store.clear()

        container = MagicMock()
        container.exec_run.side_effect = [
            # dry_run: git status / diff
            (0, (b"M file.py\n---DIFF---\n 1 file changed", b"")),
            # execute: git add / commit / push / rev-parse
            (0, (b"", b"")),
            (0, (b"", b"")),
            (0, (b"", b"")),
            (0, (b"deadbee0000\n", b"")),
        ]
        client = MagicMock()
        client.containers.get.return_value = container
        mock_docker.return_value = client
        mock_verify.return_value = {"status": "ok", "gate_passed": True}

        with patch("code_sandbox_mcp.server.record_boundary_crossing"):
            dry = _decode(submit(
                container_id="abc123def456", repo="o/r", branch="b",
                message="m", working_dir="/repo", dry_run=True,
            ))
            token = dry["confirmation_token"]

            approved = _decode(sandbox_approve(token))
            assert approved["status"] == "ok"

            executed = _decode(submit(
                container_id="abc123def456", repo="o/r", branch="b",
                message="m", working_dir="/repo", dry_run=False, token=token,
            ))

        # Must NOT be rejected as "already used" — the whole point of the fix.
        assert executed["status"] == "pushed", executed
        assert executed["sha"] == "deadbee"

    @patch("code_sandbox_mcp.server.run_verify")
    @patch("code_sandbox_mcp.server._docker")
    def test_failed_gate_preserves_token(
        self,
        mock_docker: MagicMock,
        mock_verify: MagicMock,
    ) -> None:
        """A failed verify gate must not waste the token."""
        token_mod._store.clear()
        container = MagicMock()
        container.exec_run.side_effect = [
            (0, (b"M file.py\n---DIFF---\n 1 file changed", b"")),
        ]
        client = MagicMock()
        client.containers.get.return_value = container
        mock_docker.return_value = client
        mock_verify.return_value = {
            "status": "failed",
            "gate_passed": False,
            "gate_fail_reasons": ["lint (ruff): 1 error(s)"],
        }

        with patch("code_sandbox_mcp.server.record_boundary_crossing"):
            dry = _decode(submit(
                container_id="abc123def456", repo="o/r", branch="b",
                message="m", working_dir="/repo", dry_run=True,
            ))
            token = dry["confirmation_token"]
            rejected = _decode(submit(
                container_id="abc123def456", repo="o/r", branch="b",
                message="m", working_dir="/repo", dry_run=False, token=token,
            ))

        assert rejected["status"] == "rejected"
        # Token survived the failed gate and can still be consumed.
        assert verify_and_consume(token) is not None


# ---------------------------------------------------------------------------
# Bug 2 — strict / lenient verify gate
# ---------------------------------------------------------------------------


def _run_verify_with_layers(layers: dict[str, VerifyResult], **kwargs) -> dict:
    """Call the real run_verify with a single python language and the given
    per-layer VerifyResults, by mocking detection and dispatch."""
    client = MagicMock()
    client.containers.get.return_value = MagicMock()
    with patch("code_sandbox_mcp.edit_verify.detect_languages",
               return_value=({"python"}, "")), \
         patch("code_sandbox_mcp.edit_verify._dispatch_layer",
               side_effect=lambda c, p, lang, layer: layers[layer]):
        return run_verify(client, "abc123def456", "/repo", **kwargs)


_CLEAN = {
    "lint": VerifyResult(tool="ruff", status="ok"),
    "type": VerifyResult(tool="pyright", status="ok"),
    "test": VerifyResult(tool="pytest", status="ok", detail='{"status": "ok"}'),
    "scan": VerifyResult(tool="semgrep", status="ok"),
}


class TestStrictLenientGate:
    def test_strict_fails_on_missing_gating_tool(self) -> None:
        layers = dict(_CLEAN)
        layers["lint"] = VerifyResult(tool="ruff", status="not_available")
        result = _run_verify_with_layers(layers, strict=True)
        assert result["gate_passed"] is False
        assert result["incomplete"] is True
        assert any("verification incomplete" in r
                   for r in result["gate_fail_reasons"])

    def test_lenient_passes_but_flags_incomplete(self) -> None:
        layers = dict(_CLEAN)
        layers["lint"] = VerifyResult(tool="ruff", status="not_available")
        result = _run_verify_with_layers(layers, strict=False)
        assert result["gate_passed"] is True
        assert result["incomplete"] is True

    def test_strict_ignores_absence_of_non_gating_layer(self) -> None:
        """gate_on_type_error defaults to False, so a missing type checker
        must not block submit even in strict mode."""
        layers = dict(_CLEAN)
        layers["type"] = VerifyResult(tool="pyright", status="not_available")
        result = _run_verify_with_layers(layers, strict=True)
        assert result["gate_passed"] is True
        assert result["incomplete"] is True  # still reported

    def test_strict_clean_passes(self) -> None:
        result = _run_verify_with_layers(dict(_CLEAN), strict=True)
        assert result["gate_passed"] is True
        assert result["incomplete"] is False


# ---------------------------------------------------------------------------
# Pre-existing F821 in read_file_range (fixed alongside)
# ---------------------------------------------------------------------------


class TestReadFileRange:
    """read_file_range referenced an undefined `container` (NameError at
    runtime).  It must now pass the resolved container handle through."""

    @patch("code_sandbox_mcp.server.read_file_lines")
    @patch("code_sandbox_mcp.server._docker")
    def test_passes_container_handle(
        self,
        mock_docker: MagicMock,
        mock_read: MagicMock,
    ) -> None:
        container = MagicMock(name="container")
        client = MagicMock()
        client.containers.get.return_value = container
        mock_docker.return_value = client
        mock_read.return_value = {
            "content": "x", "total_lines": 1, "shown": 1,
            "has_more": False, "next_offset": None,
        }

        out = json.loads(read_file_range("abc123def456", "/f.py", 0, 10))

        assert "error" not in out
        # The resolved container object — not a NameError — is forwarded.
        assert mock_read.call_args.args[0] is container
