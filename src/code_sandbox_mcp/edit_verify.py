"""Edit/Verify subsystem: minimal edit loop primitives for sandbox containers.

Provides low-level file editing and verification tools that operate on
disposable sandbox containers (not the real repository).  These tools
form the core of the minimal edit loop:

    search_in_container → read_file_range → apply_patch
    → lint/type_check → rerun_failed

By sending only diffs and reading only the needed lines, each iteration
consumes only hundreds of tokens instead of thousands.
"""

from __future__ import annotations

import json
import re
from typing import Any


# ---------------------------------------------------------------------------
# Unified diff parsing and application
# ---------------------------------------------------------------------------

#: Regex for unified diff hunk headers: ``@@ -old_start,old_count +new_start,new_count @@``
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

#: Regex for hunk body lines: `` ` ` (context), ``-`` (remove), ``+`` (add)
_HUNK_LINE_RE = re.compile(r"^([ +\-])")


def _parse_hunks(diff_text: str) -> list[dict[str, Any]]:
    """Parse a unified diff string into a list of hunk dicts.

    Each hunk dict has:
    - ``old_start`` (int): 1-indexed start line in original file
    - ``old_count`` (int): number of original lines in hunk
    - ``new_start`` (int): 1-indexed start line in new file
    - ``new_count`` (int): number of new lines in hunk
    - ``lines`` (list[str]): hunk body lines with ``+``, ``-``, `` `` prefixes
    """
    hunks: list[dict[str, Any]] = []
    for line in diff_text.split("\n"):
        m = _HUNK_HEADER_RE.match(line)
        if m:
            hunks.append({
                "old_start": int(m.group(1)),
                "old_count": int(m.group(2) or 1),
                "new_start": int(m.group(3)),
                "new_count": int(m.group(4) or 1),
                "lines": [],
            })
            continue
        if hunks and _HUNK_LINE_RE.match(line):
            hunks[-1]["lines"].append(line)
    return hunks


def apply_unified_diff(content: str, diff_text: str) -> str:
    """Apply a unified diff to *content* and return the result.

    Args:
        content: Original file content (string with newlines).
        diff_text: Unified diff string.

    Returns:
        The patched content.

    Raises:
        ValueError: If the diff is malformed or cannot be applied
            (e.g. context lines do not match).
    """
    if not diff_text.strip():
        return content  # Empty diff → no change

    hunks = _parse_hunks(diff_text)
    if not hunks:
        return content  # No hunks → no change

    lines = content.split("\n")
    # Track the original number of lines for trailing-newline detection.
    original_ends_with_newline = content.endswith("\n")

    # Apply hunks in reverse order (bottom to top) so line offsets in
    # earlier hunks remain valid.
    for hunk in reversed(hunks):
        old_start = hunk["old_start"] - 1  # Convert to 0-indexed
        old_count = hunk["old_count"]
        new_start = hunk["new_start"] - 1
        hunk_lines = hunk["lines"]

        # --- Validate context ---
        # Walk through the hunk lines and check that context lines
        # and removal lines match the original content.
        idx = old_start
        for hline in hunk_lines:
            if idx >= len(lines) and not hline.startswith("+"):
                raise ValueError(
                    f"Hunk references line {idx + 1} but file has only "
                    f"{len(lines)} line(s)"
                )
            if hline.startswith(" ") or hline.startswith("-"):
                expected = hline[1:]  # Strip prefix
                actual = lines[idx]
                if actual != expected:
                    raise ValueError(
                        f"Context mismatch at line {idx + 1}:\n"
                        f"  expected: {expected!r}\n"
                        f"  actual:   {actual!r}"
                    )
                idx += 1
            elif hline.startswith("+"):
                pass  # Addition, nothing to check
            elif hline.startswith("\\"):
                pass  # No-newline marker, skip

        # --- Apply the hunk ---
        # Remove old lines, insert new lines.
        before = lines[:old_start]
        after = lines[old_start + old_count:] if old_start + old_count <= len(lines) else []

        new_lines: list[str] = []
        for hline in hunk_lines:
            if hline.startswith(" ") or hline.startswith("-"):
                if not hline.startswith("-"):
                    new_lines.append(hline[1:])  # Context: keep
                # Removal: skip
            elif hline.startswith("+"):
                new_lines.append(hline[1:])  # Addition: insert
            # \\ no-newline markers are ignored

        lines = before + new_lines + after

    # Preserve trailing newline behaviour
    result = "\n".join(lines)
    if original_ends_with_newline:
        result += "\n"
    return result


# ---------------------------------------------------------------------------
# Container file operations
# ---------------------------------------------------------------------------


def _read_file(client: Any, container_id: str, file_path: str) -> str:
    """Read the full content of *file_path* from the sandbox container.

    Returns:
        File content as a string.

    Raises:
        ValueError: Container not found or file read error.
    """
    try:
        container = client.containers.get(container_id)
    except Exception as e:
        raise ValueError(f"Container {container_id[:12]} not found: {e}") from e

    # Use cat to read the file content
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", f"cat {_quote_path(file_path)}"],
        stdout=True,
        stderr=True,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if exit_code != 0:
        raise ValueError(
            f"Failed to read {file_path}: exit code {exit_code}"
            f"\n{stderr_text}"
        )
    return stdout_text


def _write_file(client: Any, container_id: str, file_path: str, content: str) -> None:
    """Write *content* to *file_path* in the sandbox container."""
    try:
        container = client.containers.get(container_id)
    except Exception as e:
        raise ValueError(f"Container {container_id[:12]} not found: {e}") from e

    # Write via base64 to avoid shell escaping issues
    import base64

    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    cmd = (
        f"echo {encoded} | base64 -d > {_quote_path(file_path)}"
    )
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
    )
    _, stderr_part = output if isinstance(output, tuple) else (None, output)
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if exit_code != 0:
        raise ValueError(
            f"Failed to write {file_path}: exit code {exit_code}"
            f"\n{stderr_text}"
        )


def _quote_path(path: str) -> str:
    """Shell-escape a file path for use in a command string."""
    import shlex

    return shlex.quote(path)


# ---------------------------------------------------------------------------
# Public API: called by @mcp.tool() handlers in server.py
# ---------------------------------------------------------------------------


def apply_patch_to_file(
    client: Any,
    container_id: str,
    file_path: str,
    diff_content: str,
) -> str:
    """Apply a unified diff to a file inside the sandbox container.

    This reads the current file from the container, applies the unified
    diff, and writes the result back.  The caller (the AI) sends only a
    compact diff instead of the full file content, reducing token cost
    by 1-2 orders of magnitude.

    Args:
        client: Docker client instance.
        container_id: 12-character container ID prefix.
        file_path: Path to the file inside the container.
        diff_content: Unified diff string to apply.

    Returns:
        Success message or error description.
    """
    try:
        current = _read_file(client, container_id, file_path)
    except ValueError as e:
        return f"Error: {e}"

    try:
        patched = apply_unified_diff(current, diff_content)
    except ValueError as e:
        return f"Error: failed to apply diff: {e}"

    try:
        _write_file(client, container_id, file_path, patched)
    except ValueError as e:
        return f"Error: {e}"

    return (
        f"Patch applied successfully to {file_path} "
        f"in container {container_id[:12]}"
    )


def read_file_lines(
    client: Any,
    container_id: str,
    file_path: str,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """Read *limit* lines from *file_path* starting at *offset*.

    Returns a dict with:
    - ``content`` (str): the requested lines joined by newline
    - ``total_lines`` (int): total number of lines in the file
    - ``shown`` (int): number of lines returned
    - ``has_more`` (bool): whether there are more lines after this range
    - ``next_offset`` (int | None): offset for the next page (if any)
    - ``error`` (str | None): error message if the read failed

    Args:
        client: Docker client instance.
        container_id: 12-character container ID prefix.
        file_path: Path to the file inside the container.
        offset: 0-indexed line offset to start reading from.
        limit: Maximum number of lines to return.
    """
    try:
        content = _read_file(client, container_id, file_path)
    except ValueError as e:
        return {"error": str(e)}

    lines = content.split("\n")
    total = len(lines)
    page_lines = lines[offset:offset + limit]
    next_offset = offset + limit
    has_more = next_offset < total

    return {
        "content": "\n".join(page_lines),
        "total_lines": total,
        "shown": len(page_lines),
        "has_more": has_more,
        "next_offset": next_offset if has_more else None,
        "error": None,
    }


def lint_file(
    client: Any,
    container_id: str,
    file_path: str,
) -> list[dict[str, Any]]:
    """Run a linter on *file_path* inside the container.

    Detects the file type from its extension and chooses an appropriate
    linter.  Returns a list of dicts, each with:
    - ``file`` (str): file path
    - ``line`` (int): line number
    - ``rule`` (str): rule identifier (e.g. ``"F401"``, ``"unused-import"``)
    - ``message`` (str): human-readable message

    If no suitable linter is found, returns a single entry with
    ``rule`` set to ``"no-linter"``.

    Currently supported:
    - ``.py`` files → ``ruff check`` (falls back to ``pylint``)
    - ``.js``, ``.ts``, ``.jsx``, ``.tsx`` files → ``eslint``
    """
    try:
        container = client.containers.get(container_id)
    except Exception as e:
        return [{"file": file_path, "line": 0, "rule": "error", "message": str(e)}]

    ext = _get_extension(file_path)
    results: list[dict[str, Any]] = []

    if ext == ".py":
        results = _run_ruff(container, file_path)
        if not results:
            results = _run_pylint(container, file_path)
    elif ext in (".js", ".ts", ".jsx", ".tsx"):
        results = _run_eslint(container, file_path)
    else:
        results = [{
            "file": file_path,
            "line": 0,
            "rule": "no-linter",
            "message": f"No linter configured for {ext} files",
        }]

    return results


def type_check_file(
    client: Any,
    container_id: str,
    file_path: str,
) -> list[dict[str, Any]]:
    """Run a type checker on *file_path* inside the container.

    Returns the same structure as :func:`lint_file`.

    Currently supported:
    - ``.py`` files → ``mypy`` (falls back to ``pyright``)
    - ``.ts``, ``.tsx`` files → ``tsc --noEmit``
    """
    try:
        container = client.containers.get(container_id)
    except Exception as e:
        return [{"file": file_path, "line": 0, "rule": "error", "message": str(e)}]

    ext = _get_extension(file_path)
    results: list[dict[str, Any]] = []

    if ext == ".py":
        results = _run_mypy(container, file_path)
        if not results:
            results = _run_pyright(container, file_path)
    elif ext in (".ts", ".tsx"):
        results = _run_tsc(container, file_path)
    else:
        results = [{
            "file": file_path,
            "line": 0,
            "rule": "no-typechecker",
            "message": f"No type checker configured for {ext} files",
        }]

    return results


# ---------------------------------------------------------------------------
# Extension helper
# ---------------------------------------------------------------------------


def _get_extension(file_path: str) -> str:
    """Return the lowercase file extension including the dot."""
    _, dot_ext = file_path.rstrip("/").rsplit(".", 1) if "." in file_path else ("", "")
    return f".{dot_ext.lower()}" if dot_ext else ""


# ---------------------------------------------------------------------------
# Linter runners
# ---------------------------------------------------------------------------


def _run_ruff(container: Any, file_path: str) -> list[dict[str, Any]]:
    """Run ``ruff check --output-format json`` and parse results."""
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", f"ruff check --output-format json {_quote_path(file_path)} 2>/dev/null || true"],
        stdout=True,
        stderr=True,
    )
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    return _parse_ruff_output(stdout_text, file_path)


def _parse_ruff_output(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse ruff JSON output into the common result format."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        issues = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(issues, list):
        return []

    results: list[dict[str, Any]] = []
    for issue in issues:
        results.append({
            "file": issue.get("filename", file_path),
            "line": int(issue.get("location", {}).get("row", 0)),
            "rule": issue.get("code", "unknown"),
            "message": issue.get("message", ""),
        })
    return results


def _run_pylint(container: Any, file_path: str) -> list[dict[str, Any]]:
    """Run ``pylint --output-format json`` and parse results."""
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", f"pylint --output-format json {_quote_path(file_path)} 2>/dev/null || true"],
        stdout=True,
        stderr=True,
    )
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    return _parse_pylint_output(stdout_text, file_path)


def _parse_pylint_output(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse pylint JSON output into the common result format."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        issues = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(issues, list):
        return []

    results: list[dict[str, Any]] = []
    for issue in issues:
        results.append({
            "file": issue.get("path", file_path),
            "line": int(issue.get("line", 0)),
            "rule": issue.get("symbol", issue.get("message-id", "unknown")),
            "message": issue.get("message", ""),
        })
    return results


def _run_eslint(container: Any, file_path: str) -> list[dict[str, Any]]:
    """Run ``eslint --format json`` and parse results."""
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", f"eslint --format json {_quote_path(file_path)} 2>/dev/null || true"],
        stdout=True,
        stderr=True,
    )
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    return _parse_eslint_output(stdout_text, file_path)


def _parse_eslint_output(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse eslint JSON output into the common result format."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []

    results: list[dict[str, Any]] = []
    for result in data:
        fpath = result.get("filePath", file_path)
        for msg in result.get("messages", []):
            results.append({
                "file": fpath,
                "line": int(msg.get("line", 0)),
                "rule": msg.get("ruleId", "unknown"),
                "message": msg.get("message", ""),
            })
    return results


# ---------------------------------------------------------------------------
# Type checker runners
# ---------------------------------------------------------------------------


def _run_mypy(container: Any, file_path: str) -> list[dict[str, Any]]:
    """Run ``mypy --show-error-codes`` and parse results."""
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", f"mypy --show-error-codes {_quote_path(file_path)} 2>/dev/null || true"],
        stdout=True,
        stderr=True,
    )
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    return _parse_mypy_output(stdout_text, file_path)


#: Regex for mypy output: ``file:line:column: severity: message [error-code]``
_MYPY_LINE_RE = re.compile(
    r"^(.+?):(\d+):\d+:\s*(error|warning|note):\s*(.+?)(?:\s+\[([^\]]+)\])?\s*$"
)


def _parse_mypy_output(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse mypy text output into the common result format."""
    results: list[dict[str, Any]] = []
    for line in raw.split("\n"):
        m = _MYPY_LINE_RE.match(line)
        if m:
            results.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "rule": m.group(5) or m.group(3),  # error code or severity
                "message": m.group(4),
            })
    return results


def _run_pyright(container: Any, file_path: str) -> list[dict[str, Any]]:
    """Run ``pyright --outputjson`` and parse results."""
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", f"pyright --outputjson {_quote_path(file_path)} 2>/dev/null || true"],
        stdout=True,
        stderr=True,
    )
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    return _parse_pyright_output(stdout_text, file_path)


def _parse_pyright_output(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse pyright JSON output into the common result format."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []

    results: list[dict[str, Any]] = []
    for diag in data.get("generalDiagnostics", []):
        results.append({
            "file": diag.get("file", file_path),
            "line": int(diag.get("range", {}).get("start", {}).get("line", 0)) + 1,
            "rule": diag.get("rule", "unknown"),
            "message": diag.get("message", ""),
        })
    return results


def _run_tsc(container: Any, file_path: str) -> list[dict[str, Any]]:
    """Run ``tsc --noEmit`` and parse results."""
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", f"npx tsc --noEmit {_quote_path(file_path)} 2>&1 || true"],
        stdout=True,
        stderr=True,
    )
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""

    # Try JSON output first, then fall back to text parsing
    parsed = _parse_tsc_json(stdout_text, file_path)
    if not parsed:
        parsed = _parse_tsc_text(stdout_text, file_path)
    return parsed


#: Regex for tsc text output: ``file(line,col): error TSXXXX: message``
_TSC_TEXT_RE = re.compile(
    r"^(.+?)\((\d+)(?:,\d+)?\):\s*(error|warning)\s+(TS\d+):\s*(.+)$"
)


def _parse_tsc_text(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse tsc text output into the common result format."""
    results: list[dict[str, Any]] = []
    for line in raw.split("\n"):
        m = _TSC_TEXT_RE.match(line)
        if m:
            results.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "rule": m.group(4),
                "message": m.group(5),
            })
    return results


def _parse_tsc_json(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse tsc JSON output (``--listFiles`` style) if available."""
    # tsc does not output JSON by default; this is a fallback
    # for when tsoutputformat or similar is used.
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []

    results: list[dict[str, Any]] = []
    if isinstance(data, dict):
        for diag in data.get("diagnostics", []):
            results.append({
                "file": diag.get("file", {}).get("fileName", file_path),
                "line": int(diag.get("file", {}).get("line", 0)),
                "rule": diag.get("code", "unknown"),
                "message": diag.get("messageText", ""),
            })
    return results
