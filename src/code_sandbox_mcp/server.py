"""FastMCP server providing Docker sandbox tools - MCP server implementation.

This module defines the FastMCP server and all tool handlers.
"""

from __future__ import annotations

import argparse
import difflib
import io
import json
import logging
import os
import shlex
import tarfile
import tempfile
import time
from pathlib import Path

from docker.errors import APIError, NotFound
from fastmcp import FastMCP

from code_sandbox_mcp.edit_verify import (
    apply_patch_to_file,
    lint_file,
    read_file,
    read_file_lines,
    run_verify,
    search_files,
    transform_file_in_container,
    type_check_file,
    write_file,
)
from code_sandbox_mcp.journal import (
    get_journal_path,
    get_runs,
    read_journal,
    record_boundary_crossing,
    record_copy,
)
from code_sandbox_mcp.output_control import (
    paginate_output,
    truncate_output,
)
from code_sandbox_mcp.result_cache import (
    get_cache_stats,
    invalidate_cache,
)
from code_sandbox_mcp.security import (
    validate_image_ref,
)
from code_sandbox_mcp.token import (
    get_pending_tokens,
    reject_token,
    verify_token,
)
from code_sandbox_mcp.tools.common import _docker
from code_sandbox_mcp.trace import (
    generate_html_trace,
    generate_json_trace,
    get_trace_dir,
)

from .tools.container import (
    rerun_failed,
    run_container_and_exec,
    run_test_environment,
    sandbox_exec_diff,
    sandbox_initialize,
    sandbox_stop,
    sandbox_update_check,
    sandbox_update_start,
    stop_test_environment,
    wait_for_condition,
)
from .tools.exec import (
    sandbox_exec,
    sandbox_exec_background,
    sandbox_exec_check,
)
from .tools.vcs import (
    clone_repo,
    issue_view,
    sandbox_create_pr,
    submit,
)

#: Default Docker image used when no image is specified.
#:
#: Uses the pre-built sandbox image (``docker/Dockerfile.sandbox``) which
#: includes git/gh/uv/ripgrep/ruff/pyright/semgrep and runs as the
#: dedicated ``sandbox`` user (non-root).
#:
#: **このフィールドは直接編集しないこと。**
#: ``docker/Dockerfile.sandbox`` を変更すると CI
#: (``.github/workflows/build-sandbox-image.yml``) が自動で
#: GHCR へ push し、新ダイジェストを書き込んだ PR を作成する。
#:
#: ローカルで試す場合::
#:
#:   docker build -f docker/Dockerfile.sandbox -t code-sandbox-mcp/sandbox:latest .
#:   docker images --digests code-sandbox-mcp/sandbox  # sha256 を取得
#:   # 取得した sha256 を下の文字列に貼り付けてテスト
#:
#: Refs: Issue #56, docs/design.md §2.1, §11, §12

#: Stdio proxy - shared with launcher via this module variable.
#: Shiori repos root path on the host for cp-by-pass git clone (Issue #84).
#: Set via ``--shiori-repos-path`` CLI arg or ``SHIORI_REPOS_PATH`` env var.
#: When set, ``sandbox_initialize`` and ``run_container_and_exec`` can use
#: ``clone_repo`` to copy a pre-cloned repository from this path into the
#: container, bypassing a network ``git clone``.
#: Compiled pattern for validating clone_repo ``owner/name`` format.
#: Sensitive file/directory basenames to exclude from tar archive.

logger: logging.Logger = logging.getLogger(__name__)

mcp = FastMCP("code-sandbox-mcp")


# ---------------------------------------------------------------------------
# Shiori clone helper (Issue #84)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# sandbox_initialize
# ---------------------------------------------------------------------------


sandbox_exec = mcp.tool()(sandbox_exec)
sandbox_exec_background = mcp.tool()(sandbox_exec_background)
sandbox_exec_check = mcp.tool()(sandbox_exec_check)

issue_view = mcp.tool()(issue_view)
submit = mcp.tool()(submit)
sandbox_create_pr = mcp.tool()(sandbox_create_pr)
clone_repo = mcp.tool()(clone_repo)


# Container lifecycle tool registrations
sandbox_initialize = mcp.tool()(sandbox_initialize)
sandbox_stop = mcp.tool()(sandbox_stop)
sandbox_update_start = mcp.tool()(sandbox_update_start)
sandbox_update_check = mcp.tool()(sandbox_update_check)
run_container_and_exec = mcp.tool()(run_container_and_exec)
rerun_failed = mcp.tool()(rerun_failed)
sandbox_exec_diff = mcp.tool()(sandbox_exec_diff)
run_test_environment = mcp.tool()(run_test_environment)
stop_test_environment = mcp.tool()(stop_test_environment)
wait_for_condition = mcp.tool()(wait_for_condition)


def _find_all_matches(text: str, pattern: str) -> list[tuple[int, int]]:
    """Find all non-overlapping occurrences of *pattern* in *text*.

    Returns a list of ``(offset, line_number)`` tuples.
    """
    matches: list[tuple[int, int]] = []
    idx = 0
    while True:
        idx = text.find(pattern, idx)
        if idx == -1:
            break
        line_no = text[:idx].count("\n") + 1
        matches.append((idx, line_no))
        idx += 1
    return matches


def _get_line_indent(line: str) -> int:
    """Return the leading whitespace length of *line*."""
    return len(line) - len(line.lstrip())


def _reindent_lines(lines: list[str], delta: int) -> list[str]:
    """Apply an indentation *delta* (number of spaces) to each line.

    Empty/whitespace-only lines are passed through unchanged.
    A positive *delta* adds leading spaces; a negative *delta* removes them.
    """
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            result.append("")
            continue
        if delta >= 0:
            result.append(" " * delta + line)
        else:
            remove = min(-delta, _get_line_indent(line))
            result.append(line[remove:])
    return result


def _try_whitespace_flexible(
    existing: str, old_str: str, new_str: str,
) -> str | None:
    """Attempt whitespace-flexible matching.

    Strips leading/trailing whitespace from each line of *old_str* and
    slides over the file looking for a block whose stripped lines match.
    When found the file's original indentation is preserved and *new_str*
    is re-indented to fit.

    Returns the new file content on success, or ``None`` if no match
    was found.
    """
    existing_lines = existing.splitlines()
    old_lines = old_str.splitlines()
    old_stripped = [line.strip() for line in old_lines]

    if len(old_lines) > len(existing_lines):
        return None

    matches: list[int] = []
    for i in range(len(existing_lines) - len(old_lines) + 1):
        chunk = existing_lines[i : i + len(old_lines)]
        if [line.strip() for line in chunk] == old_stripped:
            matches.append(i)

    if not matches:
        return None

    if len(matches) > 1:
        line_nos = ", ".join(str(m + 1) for m in matches[:10])
        suffix = "..." if len(matches) > 10 else ""
        return (
            f"Error: old_str matches at {len(matches)} locations "
            f"(lines {line_nos}{suffix}) after whitespace normalization. "
            "Add more surrounding context to make it unique."
        )

    i = matches[0]
    chunk = existing_lines[i : i + len(old_lines)]
    file_first_indent = _get_line_indent(chunk[0])
    old_first_indent = _get_line_indent(old_lines[0])
    delta = file_first_indent - old_first_indent
    reindented = _reindent_lines(new_str.splitlines(), delta)
    new_content = "\n".join(reindented)

    # Build character offsets to do a string-level replacement
    # (preserves trailing whitespace and file structure).
    pos = 0
    line_starts: list[int] = []
    for line in existing_lines:
        line_starts.append(pos)
        pos += len(line) + 1  # +1 for newline
    # offset right after the last matched line
    start_offset = line_starts[i]
    end_idx = i + len(old_lines)
    if end_idx < len(line_starts):
        end_offset = line_starts[end_idx]
    else:
        end_offset = len(existing)

    result = existing[:start_offset] + new_content + existing[end_offset:]
    if existing.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def _build_near_miss_echo(existing: str, old_str: str, dest_path: str) -> str:
    """Build a near-miss error message with the most similar file region.

    Uses :mod:`difflib` to locate the area that best matches *old_str*
    and shows it with line numbers as context for the caller.
    """
    existing_lines = existing.splitlines()

    sm = difflib.SequenceMatcher(None, existing, old_str)
    match = sm.find_longest_match(0, len(existing), 0, len(old_str))

    lines_to_show: list[str] = []

    if match.size >= max(5, len(old_str) * 0.3):
        match_line = existing[: match.a].count("\n") + 1
        match_end = existing[match.a : match.a + match.size].count("\n") + match_line

        ctx_start = max(0, match_line - 4)
        ctx_end = min(len(existing_lines), match_end + 3)

        for i in range(ctx_start, ctx_end):
            prefix = ">>>" if match_line - 1 <= i < match_end else "   "
            lines_to_show.append(f"{prefix} {i + 1:4d} | {existing_lines[i]}")
    else:
        for i in range(min(8, len(existing_lines))):
            lines_to_show.append(f"    {i + 1:4d} | {existing_lines[i]}")

    context_block = "\n".join(lines_to_show)

    return (
        f"Error: old_str not found in {dest_path}.\n"
        f"Most relevant file area:\n"
        f"{context_block}\n"
        "Tip: Use read_file_range first to confirm the exact content "
        "(including whitespace)."
    )


@mcp.tool()
def write_file_sandbox(
    container_id: str,
    file_name: str,
    file_contents: str,
    dest_dir: str = "/home/sandbox",
    start_line: int | None = None,
    end_line: int | None = None,
    append: bool = False,
    old_str: str | None = None,
) -> str:
    """Write a file to the container. Supports full overwrite and partial updates.

    **Mode selection (pick exactly one):**

    ================= ===================================================
    Mode              Parameters
    ================= ===================================================
    Full overwrite    (none of the below) — writes *file_contents* as-is
    Line-range        ``start_line`` [+ ``end_line``] — replace lines
    Append            ``append=True`` — append to existing file
    String replace    ``old_str`` — replace exact text (see matching below)
    ================= ===================================================

    **Full overwrite** (default, backward compatible):
    Writes *file_contents* as the entire file.

    **Line-range replacement** (*start_line* / *end_line*, 1-indexed, inclusive):
    Replaces the specified line range with *file_contents*. Lines outside the
    range are preserved.  When *start_line* is omitted it defaults to line 1;
    when *end_line* is omitted it defaults to the last line of the file.

    **Append** (*append* = True):
    Appends *file_contents* to the end of the existing file.

    **Replace** (*old_str*):
    Replaces *old_str* with *file_contents*.  The matching logic is:

    1. **Exact match** -- if *old_str* appears exactly once, it is replaced.
       If it appears multiple times the call is rejected with the line numbers
       of each match so the caller can add more surrounding context.
    2. **Whitespace-flexible fallback** -- if exact matching fails, leading
       and trailing whitespace is stripped from each line and the search is
       retried.  On success *file_contents* is re-indented to match the
       file's original indentation.
    3. **Near-miss echo** -- if neither strategy finds a match, the most
       similar region of the file is returned with line numbers via
       :func:`difflib.SequenceMatcher`.

    *start_line* / *end_line*, *append*, and *old_str* are mutually exclusive.
    When none of them is specified the file is fully overwritten (original
    behaviour).

    .. hint::

       ``old_str`` mode is the default edit path for AI — it is robust
       (uniqueness check + whitespace-flexible fallback) and avoids the
       ``@@`` header errors that make hand-written diffs fail.  Use
       :func:`read_file_range` first to inspect the target area before
       editing.  For bulk / repetitive / structural / computed changes use
       :func:`transform_file` (imperative).  Reserve :func:`apply_patch` for
       *machine-generated* diffs.

    Args:
        container_id: 12-character container ID prefix.
        file_name: Name of the file to write.
        file_contents: Content to write.
        dest_dir: Destination directory in the container (default: ``/home/sandbox``).
        start_line: Start line for line-range replacement (1-indexed, inclusive).
        end_line: End line for line-range replacement (1-indexed, inclusive).
        append: When True, appends to the end of the file.
        old_str: When specified, replaces this string in the existing file.
            Performs uniqueness check, whitespace-flexible fallback, and near-miss echo (see above).

    Returns:
        Success or error message.

    See also:
        :func:`read_file_range` — inspect file content before editing.
        :func:`transform_file` — imperative edits (bulk / structural / computed).
        :func:`apply_patch` — machine-generated diffs only (deprecated for
        AI-authored edits).
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    dest_path = os.path.join(dest_dir, file_name)

    # Validate mutual exclusivity
    has_line_range = start_line is not None or end_line is not None
    mode_count = sum([append, old_str is not None, has_line_range])
    if mode_count > 1:
        return "Error: start_line/end_line, append, and old_str are mutually exclusive"

    if old_str is not None and old_str == "":
        return "Error: old_str must not be empty"
    if start_line is not None and start_line < 1:
        return "Error: start_line must be >= 1"

    content = file_contents

    # For partial updates, read existing content
    if append or old_str is not None or has_line_range:
        try:
            existing = read_file(container, dest_path)
        except ValueError:
            return f"Error: file {dest_path} not found"
        existing_lines = existing.splitlines()

        # Validate bounds
        if start_line is not None and start_line > len(existing_lines):
            return f"Error: start_line {start_line} exceeds file length ({len(existing_lines)} lines)"
        if end_line is not None:
            if end_line > len(existing_lines):
                return f"Error: end_line {end_line} exceeds file length ({len(existing_lines)} lines)"
            if start_line is not None and start_line > end_line:
                return "Error: start_line is greater than end_line"

        if append:
            sep = "\n" if existing else ""
            content = existing.rstrip("\n") + sep + file_contents
        elif old_str is not None:
            # 1. Exact match with uniqueness check
            exact_matches = _find_all_matches(existing, old_str)
            if len(exact_matches) > 1:
                line_nos = ", ".join(str(m[1]) for m in exact_matches[:10])
                suffix = "..." if len(exact_matches) > 10 else ""
                return (
                    f"Error: old_str matches at {len(exact_matches)} locations "
                    f"(lines {line_nos}{suffix}). "
                    "Add more surrounding context to make it unique."
                )
            if len(exact_matches) == 1:
                idx = exact_matches[0][0]
                content = (
                    existing[:idx]
                    + file_contents
                    + existing[idx + len(old_str) :]
                )
            else:
                # 2. Whitespace-flexible fallback
                result = _try_whitespace_flexible(
                    existing, old_str, file_contents,
                )
                if result is not None:
                    if result.startswith("Error:"):
                        return result
                    content = result
                else:
                    # 3. Near-miss echo
                    return _build_near_miss_echo(existing, old_str, dest_path)
        else:
            start = start_line - 1 if start_line is not None else 0
            end = end_line if end_line is not None else len(existing_lines)
            new_lines = file_contents.splitlines()
            content_lines = existing_lines[:start] + new_lines + existing_lines[end:]
            content = "\n".join(content_lines)
            if file_contents.endswith("\n"):
                content += "\n"

    try:
        write_file(container, container_id[:12], dest_path, content)
    except ValueError as e:
        return f"Error: {e}"
    return f"Written {len(content)} bytes to {dest_path}"


@mcp.tool()
def copy_project(
    container_id: str,
    local_src_dir: str,
    dest_dir: str = "/home/sandbox",
) -> str:
    """Copy a local directory (or file) into the container as a tar archive.

    Creates a tar archive of the local path in a temp directory and
    streams it into the container with ``put_archive``.

    The target directory inside the tar archive is named after the
    source directory itself (i.e. ``/home/sandbox/source_dir_name/...``).

    .. hint::

       For Git repositories already cloned locally, prefer
       :func:`sandbox_initialize` with ``clone_repo`` — it copies
       a pre-cloned repo without network overhead.

    Args:
        container_id: 12-character container ID prefix.
        local_src_dir: Path to the local directory to copy.
        dest_dir: Destination directory in the container (default:
            ``/home/sandbox``).

    Returns:
        Success or error message.

    See also:
        :func:`clone_repo` — clone a remote Git repo inside the container.
        :func:`copy_file` — copy a single file instead of a directory.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    src_path = Path(local_src_dir).resolve()
    if not src_path.exists():
        return f"Error: {local_src_dir} does not exist"
    if not src_path.is_dir():
        return f"Error: {local_src_dir} is not a directory"

    arcname = src_path.name or "project"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar")
    try:
        with tarfile.open(fileobj=tmp.file, mode="w") as tar:
            tar.add(src_path, arcname=arcname)
        tmp.file.close()
        with open(tmp.name, "rb") as f:
            data = f.read()
        buf = io.BytesIO(data)
        try:
            container.put_archive(dest_dir, buf)
        except APIError as e:
            return f"Error: {e}"
        record_copy(
            container_id[:12], "copy_project", local_src_dir, f"{dest_dir}/{arcname}"
        )
        return (
            f"Copied {local_src_dir} to {dest_dir}/{arcname} "
            f"in container {container_id[:12]}"
        )
    finally:
        os.unlink(tmp.name)


@mcp.tool()
def copy_file(
    container_id: str,
    local_src_file: str,
    dest_path: str = "/home/sandbox",
) -> str:
    """Copy a single local file into the container.

    Args:
        container_id: 12-character container ID prefix.
        local_src_file: Path to the local file to copy.
        dest_path: Destination directory or path in the container
            (default: ``/home/sandbox``).

    Returns:
        Success or error message.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    src = Path(local_src_file).resolve()
    if not src.exists():
        return f"Error: {local_src_file} does not exist"
    if not src.is_file():
        return f"Error: {local_src_file} is not a file"

    dest = dest_path
    if not dest.endswith("/") and not dest.endswith(src.name):
        # If dest_path is a directory, include the filename
        dest = str(Path(dest_path) / src.name)

    with open(src, "rb") as f:
        data = f.read()
    buf = io.BytesIO(data)
    try:
        container.put_archive(dest, buf)
    except APIError as e:
        return f"Error: {e}"
    record_copy(container_id[:12], "copy_file", local_src_file, dest)
    return f"Copied {local_src_file} to {dest} in container {container_id[:12]}"


# ---------------------------------------------------------------------------
# run_container_and_exec
# ---------------------------------------------------------------------------


@mcp.tool()
def sandbox_cache_stats() -> str:
    """Return result cache statistics.

    Returns:
        JSON string with cache stats (total_entries, total_size_bytes,
        oldest/newest entry timestamps).
    """
    stats = get_cache_stats()
    return json.dumps(stats, ensure_ascii=False)


@mcp.tool()
def sandbox_cache_invalidate(key: str | None = None) -> str:
    """Invalidate result cache entries.

    Args:
        key: Optional specific cache key to invalidate.
             If omitted, all cache entries are invalidated.

    Returns:
        JSON string with ``invalidated`` count.
    """
    count = invalidate_cache(key=key)
    return json.dumps({"invalidated": count})


@mcp.tool()
def apply_patch(container_id: str, file_path: str, diff_content: str) -> str:
    """Apply a unified diff to a file inside the sandbox container.

    .. warning::

       **Deprecated for AI-authored edits.**  Hand-written unified diffs
       almost always fail on ``@@`` header line counts or context-line
       whitespace, and each failed retry costs a full round-trip — making
       this *more* expensive than the alternatives, not less.  For AI
       editing use :func:`write_file_sandbox` with ``old_str`` (the default
       edit path) or :func:`transform_file` (imperative).  Reserve
       ``apply_patch`` for **machine-generated** diffs (``git diff`` /
       ``diff -u``), where the diff is byte-exact.

    Reads the current file from the container, applies the unified diff,
    and writes the result back.

    Args:
        container_id: 12-character container ID prefix.
        file_path: Path to the file inside the container.
        diff_content: Unified diff string to apply.

    Returns:
        Success message or error description.

    See also:
        :func:`write_file_sandbox` — full overwrite / line-range /
        append / string-replace modes.
        :func:`transform_file` — recommended imperative edit path; also the
        actual implementation that ``apply_patch`` now delegates to.
    """
    client = _docker()
    try:
        client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    return apply_patch_to_file(client, container_id, file_path, diff_content)


@mcp.tool()
def transform_file(
    container_id: str,
    file_path: str,
    code: str,
    max_lines: int = 200,
    offset: int = 0,
    limit: int = 100,
) -> str:
    """Edit a file imperatively by running Python that computes the new text.

    The **imperative** edit path: instead of providing the new bytes
    (:func:`write_file_sandbox`) or a diff (:func:`apply_patch`), you provide
    *code* that transforms the file's content.  Ideal for edits that the
    declarative tools handle poorly — bulk / repetitive / structural / computed
    changes (e.g. a regex applied to every occurrence, renaming a symbol,
    re-indenting, applying a value derived from the existing text).

    *code* must define a top-level callable ``transform(text: str) -> str``.
    It is base64-encoded and executed by a Python runner **inside the
    disposable sandbox container** (never on the host), the result is written
    back, and a **unified diff of the change is returned** so you can verify
    the effect without a separate read-back.

    Passing the program as a single ``code`` string (not a shell command) means
    multibyte characters, quotes, and newlines need no escaping.

    .. hint::

       For a single known string replacement prefer :func:`write_file_sandbox`
       with ``old_str``.  Reach for ``transform_file`` when the edit is better
       expressed as logic than as literal text — many occurrences, a pattern,
       or a value computed from the file.  Always check the returned ``diff``;
       an over-broad pattern can change more than intended.

    Args:
        container_id: 12-character container ID prefix.
        file_path: Absolute path to the file inside the container.
        code: Python source defining ``transform(text: str) -> str``.
            Executed as a **full Python interpreter** (not a restricted DSL):
            ``__builtins__``, ``open()``, ``import``, ``subprocess``, etc.
            are all available inside the disposable sandbox container.
        max_lines: Maximum diff lines to show (summary truncation).
        offset: Line offset for paging through a large diff (0-indexed).
        limit: Maximum diff lines per page.

    Returns:
        JSON string.  On success: ``status="ok"``, ``changed`` (bool),
        ``diff`` (str, paginated) and diff metadata (``shown``,
        ``total_lines``, ``truncated``, ``next_offset``, ``has_more``).
        On failure: ``status="error"`` with ``error`` (and ``traceback`` when
        the caller's code raised).

    See also:
        :func:`write_file_sandbox` — declarative edits (the default path).
        :func:`read_file_range` — inspect file content before editing.
    """
    client = _docker()
    try:
        _ = client.containers.get(container_id)
    except NotFound:
        return json.dumps(
            {"status": "error", "error": f"container {container_id[:12]} not found"}
        )
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    result = transform_file_in_container(client, container_id, file_path, code)

    if result.get("status") == "ok" and result.get("changed"):
        display, meta = truncate_output(
            result.get("diff", ""),
            max_lines=max_lines,
            verbose="full",
        )
        page = paginate_output(display, offset=offset, limit=limit)
        return json.dumps({
            "status": "ok",
            "changed": True,
            "diff": page.content,
            "shown": meta.shown,
            "total_lines": meta.total_lines,
            "truncated": meta.truncated,
            "next_offset": page.next_offset,
            "has_more": page.has_more,
        })

    return json.dumps(result)


@mcp.tool()
def read_file_range(
    container_id: str,
    file_path: str,
    offset: int = 0,
    limit: int = 50,
) -> str:
    """Read lines from *file_path* starting at *offset*.

    Returns a JSON string with:
    - ``content`` (str): the requested lines
    - ``total_lines`` (int): total lines in the file
    - ``shown`` (int): lines returned
    - ``has_more`` (bool): whether more lines exist after this range
    - ``next_offset`` (int | None): offset for pagination

    .. hint::

       Use ``limit=-1`` to read all remaining lines from *offset*
       to end of file in one call.

    Args:
        container_id: 12-character container ID prefix.
        file_path: Path to the file inside the container.
        offset: 0-indexed line offset to start reading from.
        limit: Maximum number of lines to return.  Use ``-1`` to read
            all remaining lines from *offset*.

    Returns:
        JSON string with file content and metadata, or an error
        message beginning with ``"Error:"``.

    See also:
        :func:`search_in_container` — find content across files with
        ripgrep/ast-grep.
        :func:`write_file_sandbox` — edit files after inspection.
    """
    client = _docker()
    try:
        _ = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"error": f"Container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"error": str(e)})

    result = read_file_lines(
        _, file_path, offset=offset, limit=limit
    )
    return json.dumps(result)


@mcp.tool()
def search_in_container(
    container_id: str,
    pattern: str,
    path: str = "/",
    mode: str = "lexical",
    max_results: int = 50,
) -> str:
    """Search for *pattern* inside the container using ripgrep/ast-grep.

    Returns a JSON array of matches, each with:
    - ``file`` (str): file path
    - ``line`` (int): line number
    - ``text`` (str): matching line text

    **Lexical** mode (default) uses ripgrep (``rg``) with regex support,
    falling back to ``grep`` if ripgrep is not installed.

    **Structural** mode uses ``ast-grep`` (``sg``) for AST-aware search
    that ignores whitespace/formatting differences.

    Args:
        container_id: 12-character container ID prefix.
        pattern: Search pattern (regex for lexical, AST pattern for structural).
        path: Directory or file path to search within (default ``"/"``).
        mode: ``"lexical"`` (ripgrep → grep) or ``"structural"`` (ast-grep).
        max_results: Maximum results to return (default 50).

    Returns:
        JSON string with a list of match objects, each with ``file``,
        ``line`` (int), ``text`` fields.  On container-not-found returns
        a JSON object with an ``error`` field.
    """
    client = _docker()
    try:
        _ = client.containers.get(container_id)
    except NotFound:
        return json.dumps([{"error": f"Container {container_id[:12]} not found"}])
    except Exception as e:
        return json.dumps([{"error": str(e)}])

    results = search_files(
        client, container_id, pattern, path=path, mode=mode, max_results=max_results
    )
    return json.dumps(results)


@mcp.tool()
def lint_in_container(container_id: str, file_path: str) -> str:
    """Run a linter on *file_path* inside the container.

    Detects the file type from its extension and chooses an appropriate
    linter.  Returns a JSON array of findings, each with:
    - ``file`` (str): file path
    - ``line`` (int): line number
    - ``rule`` (str): rule identifier (e.g. ``"F401"``)
    - ``message`` (str): human-readable message

    Supported:
    - ``.py`` → ``ruff check`` (falls back to ``pylint``)
    - ``.js``, ``.ts``, ``.jsx``, ``.tsx`` → ``eslint``

    Args:
        container_id: 12-character container ID prefix.
        file_path: Path to the file inside the container.

    Returns:
        JSON string of lint findings, or an error message.
    """
    client = _docker()
    try:
        client.containers.get(container_id)
    except NotFound:
        return json.dumps(
            [
                {
                    "file": file_path,
                    "line": 0,
                    "rule": "error",
                    "message": f"Container {container_id[:12]} not found",
                }
            ]
        )
    except Exception as e:
        return json.dumps(
            [{"file": file_path, "line": 0, "rule": "error", "message": str(e)}]
        )

    results = lint_file(client, container_id, file_path)
    return json.dumps(results)


@mcp.tool()
def type_check_in_container(container_id: str, file_path: str) -> str:
    """Run a type checker on *file_path* inside the container.

    Returns the same format as :func:`lint_in_container`.

    Supported:
    - ``.py`` → ``mypy`` (falls back to ``pyright``)
    - ``.ts``, ``.tsx`` → ``tsc --noEmit``

    Args:
        container_id: 12-character container ID prefix.
        file_path: Path to the file inside the container.

    Returns:
        JSON string of type check findings, or an error message.
    """
    client = _docker()
    try:
        client.containers.get(container_id)
    except NotFound:
        return json.dumps(
            [
                {
                    "file": file_path,
                    "line": 0,
                    "rule": "error",
                    "message": f"Container {container_id[:12]} not found",
                }
            ]
        )
    except Exception as e:
        return json.dumps(
            [{"file": file_path, "line": 0, "rule": "error", "message": str(e)}]
        )

    results = type_check_file(client, container_id, file_path)
    return json.dumps(results)


@mcp.tool()
def verify_in_container(
    container_id: str,
    path: str,
    gate_on_lint_error: bool = True,
    gate_on_type_error: bool = False,
    gate_on_test_fail: bool = True,
    gate_on_scan_error: bool = True,
    gate_on_scan_warning: bool = False,
    language: str | None = None,
) -> str:
    """Run lint + type_check + test + scan as a bundled verification.

    **Use this instead of calling** :func:`lint_in_container` **,**
    :func:`type_check_in_container` **, and pytest separately.**
    A single call runs all four analysis layers, normalises output,
    and returns a gate decision.

    Supports multi-language verification (Python / JS / TS / Go) with
    language-aware dispatch.  Auto-detects project language from *path*
    unless overridden with *language*.

    **Layers:**

    =========== ======== ============================
    Layer       Tool    Notes
    =========== ======== ============================
    lint        ruff    Python lint (``ruff check``)
    type_check  pyright Python type checking
    test        pytest  pytest with json-report
    scan        semgrep Security scanning
    =========== ======== ============================

    **Gate logic:**

    By default the gate fails when any of the following are detected:

    * lint errors (E/F/B/RUF rule codes)
    * test failures
    * semgrep ``ERROR`` findings
    * verification incomplete (tool not available or errored)

    Type-check errors and semgrep ``WARNING`` findings are
    configurable via the ``gate_on_*`` parameters.

    Args:
        container_id: 12-character container ID prefix.
        path: File or directory path inside the container.
        gate_on_lint_error: Whether lint errors fail the gate
            (default ``True``).
        gate_on_type_error: Whether type-check errors fail the gate
            (default ``False``).
        gate_on_test_fail: Whether test failures fail the gate
            (default ``True``).
        gate_on_scan_error: Whether semgrep ERROR findings fail the gate
            (default ``True``).
        gate_on_scan_warning: Whether semgrep WARNING findings fail the gate
            (default ``False``).
        language: Explicit language override (``"python"``, ``"js"``,
            ``"ts"``, ``"go"``).  Skips auto-detection.

    Returns:
        JSON string with:

        * ``status``: ``"ok"`` or ``"failed"``
        * ``gate_passed``: ``True`` if all gate conditions are satisfied
        * ``incomplete``: ``True`` if any layer was not available / errored
        * ``detected_languages``: list of detected language keys
        * ``lint``: list of ``{file, line, rule, severity, message}``
        * ``types``: list of ``{file, line, rule, severity, message}``
        * ``tests``: ``{status, passed, failed, duration, failures?}``
        * ``scan``: list of ``{file, line, rule, severity, message}``
        * ``gate_fail_reasons`` (optional): list of human-readable reasons
    """
    client = _docker()
    try:
        _ = client.containers.get(container_id)
    except NotFound:
        return json.dumps({
            "status": "error",
            "gate_passed": False,
            "error": f"Container {container_id[:12]} not found",
        })
    except Exception as e:
        return json.dumps({
            "status": "error",
            "gate_passed": False,
            "error": str(e),
        })

    result = run_verify(
        client,
        container_id,
        path,
        gate_on_lint_error=gate_on_lint_error,
        gate_on_type_error=gate_on_type_error,
        gate_on_test_fail=gate_on_test_fail,
        gate_on_scan_error=gate_on_scan_error,
        gate_on_scan_warning=gate_on_scan_warning,
        language=language,
    )
    return json.dumps(result)


@mcp.tool()
def sandbox_read_journal(
    run_id: str | None = None,
    max_entries: int = 100,
) -> str:
    """Read the append-only execution journal.

    Returns JSON array of journal entries, optionally filtered by
    *run_id*.  The journal records every container lifecycle event
    (initialize, exec, stop) and boundary-crossing operation.

    Args:
        run_id: If provided, only return entries for this run.
            Omit to see all journal entries.
        max_entries: Maximum number of entries to return
            (most recent first, default 100).

    Returns:
        JSON string with a list of journal entry objects, each
        containing ``ts``, ``run_id``, ``container_id``,
        ``operation``, and operation-specific fields.
    """
    entries = read_journal(run_id=run_id, max_entries=max_entries)
    return json.dumps(entries, ensure_ascii=False)


@mcp.tool()
def sandbox_trace(
    run_id: str,
    format: str = "json",
) -> str:
    """Generate a replay trace for a specific run.

    Creates an HTML or JSON trace file from journal entries for
    *run_id*, enabling post-hoc review of "why did it do that?".

    Args:
        run_id: The run identifier to generate a trace for.
        format: Output format - ``"json"`` or ``"html"``
            (default ``"json"``).

    Returns:
        Path to the generated trace file, or an error message
        beginning with ``"Error:"``.
    """
    if format not in ("json", "html"):
        return "Error: format must be 'json' or 'html'"

    if format == "json":
        path = generate_json_trace(run_id)
    else:
        path = generate_html_trace(run_id)

    if not path:
        return f"Error: run_id {run_id} not found in journal"
    return path


@mcp.tool()
def sandbox_list_runs() -> str:
    """List all runs recorded in the execution journal.

    Returns a JSON array of run summaries, each with ``run_id``,
    ``started``, ``image``, ``operations``, ``boundary_crossings``,
    ``status``, and ``last_ts``.

    Returns:
        JSON string with a list of run summary objects.
    """
    runs = get_runs()
    return json.dumps(runs, ensure_ascii=False)


@mcp.tool()
def sandbox_journal_path() -> str:
    """Return the filesystem path to the execution journal file.

    Returns:
        Absolute path to ``~/.code-sandbox-mcp/journal.log``.
    """
    return get_journal_path()


@mcp.tool()
def sandbox_trace_dir() -> str:
    """Return the filesystem path to the trace output directory.

    Returns:
        Absolute path to ``~/.code-sandbox-mcp/traces/``.
    """
    return get_trace_dir()


@mcp.tool()
def list_files(
    container_id: str,
    path: str = "/home/sandbox",
    max_depth: int = 3,
    pattern: str = "",
) -> str:
    """List files inside the container using ``find``.

    Returns a JSON array of file paths sorted alphabetically.
    Hidden files (dotfiles) and directories under ``.git`` are
    excluded.

    Args:
        container_id: 12-character container ID prefix.
        path: Directory path to list (default ``"/home/sandbox"``).
        max_depth: Maximum directory depth (default 3).
        pattern: Optional glob pattern to filter files
            (e.g. ``"*.py"``, ``"*.md"``).

    Returns:
        JSON string with ``path``, ``total``, and ``files`` list.
        On error returns an ``error`` field.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"error": f"Container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"error": str(e)})

    safe_path = shlex.quote(path)

    name_filter = ""
    if pattern:
        name_filter = f" -name {shlex.quote(pattern)}"

    cmd = (
        f"find {safe_path} -maxdepth {max_depth}"
        f" -not -path '*/\\.*'"
        f" -type f{name_filter}"
        f" | sort"
    )

    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
    )

    stdout_part, stderr_part = (
        output if isinstance(output, tuple) else (output, b"")
    )
    stdout_text = (
        stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    )
    stderr_text = (
        stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
    )

    if exit_code != 0:
        return json.dumps({"error": stderr_text or stdout_text})

    files = [f for f in stdout_text.strip().split("\n") if f]

    return json.dumps({
        "path": path,
        "total": len(files),
        "files": files,
    })


@mcp.tool()
def sandbox_approval_status() -> str:
    """List all pending approval tokens for boundary-crossing operations.

    Returns a JSON array of pending tokens, each with ``token``,
    ``operation``, ``details``, ``container_id``, ``run_id``,
    and ``remaining_seconds``.

    Use :func:`sandbox_approve` or :func:`sandbox_reject` to resolve
    a pending token.  Tokens expire after a configurable TTL (default
    5 minutes).

    Returns:
        JSON string with a list of pending token objects.
    """
    pending = get_pending_tokens()
    # created_at と now は同一クロック (time.monotonic()) なので
    # スリープ/サスペンドの影響を受けず正確な残り時間が計算できる。
    now = time.monotonic()
    for p in pending:
        p["remaining_seconds"] = max(
            0,
            int(p["ttl_seconds"] - (now - p["created_at"])),
        )
        del p["created_at"]
        del p["ttl_seconds"]
    return json.dumps(pending, ensure_ascii=False)


@mcp.tool()
def sandbox_approve(token: str) -> str:
    """Approve a pending boundary-crossing operation.

    Verifies the token and records approval in the execution journal.
    Once approved, the operation that requested the token can proceed.

    Args:
        token: The confirmation token string (from dry_run output,
            ``sandbox_approval_status``, or the dashboard).

    Returns:
        JSON string with ``status`` and metadata, or error details.
    """
    result = verify_token(token)
    if result is None:
        return json.dumps(
            {
                "status": "error",
                "error": "Token invalid, expired, or already used",
            }
        )
    record_boundary_crossing(
        result["container_id"],
        result["operation"],
        result["details"],
        approved=True,
        token=token,
    )
    return json.dumps(
        {
            "status": "ok",
            "operation": result["operation"],
            "details": result["details"],
            "container_id": result["container_id"],
            "run_id": result["run_id"],
        }
    )


@mcp.tool()
def sandbox_reject(token: str) -> str:
    """Reject a pending boundary-crossing operation.

    Removes the token from the pending queue.  The operation that
    requested the token will not be able to proceed without a new
    token.

    Args:
        token: The confirmation token string to reject.

    Returns:
        JSON string with ``status`` and message.
    """
    ok = reject_token(token)
    if not ok:
        return json.dumps(
            {
                "status": "error",
                "error": "Token not found or already resolved",
            }
        )
    return json.dumps(
        {
            "status": "ok",
            "message": "Token rejected",
        }
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the MCP server.

    Exported separately so tests can exercise the parser without
    starting the server.
    """
    parser = argparse.ArgumentParser(description="Code Sandbox MCP Server")
    parser.add_argument(
        "--terminal",
        type=str,
        default=None,
        help="Terminal emulator for update progress windows",
    )
    parser.add_argument(
        "--default-image",
        type=str,
        default=None,
        help="Default Docker image (default: python@sha256:...)",
    )
    parser.add_argument(
        "--update-spec",
        type=str,
        default=".",
        help="Pip install spec for in-place update (default: .)",
    )
    parser.add_argument(
        "--update-log-dir",
        type=str,
        default=None,
        help="Directory for update log files",
    )
    parser.add_argument(
        "--shiori-repos-path",
        type=str,
        default=os.environ.get("SHIORI_REPOS_PATH"),
        help=(
            "Host path to Shiori repos root (e.g. /data/repos). "
            "When set, sandbox_initialize and run_container_and_exec "
            "can use clone_repo to copy a pre-cloned repo into the "
            "container instead of a network git clone. "
            "Also read from SHIORI_REPOS_PATH env var."
        ),
    )
    parser.add_argument(
        "--transport",
        type=str,
        default="stdio",
        choices=["stdio", "sse", "http", "streamable-http"],
        help=(
            "MCP transport protocol (default: stdio). "
            "Use 'sse' or 'http' to avoid the ~60s client timeout. "
            "When using SSE/HTTP, specify --host and --port."
        ),
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host address for HTTP transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port for HTTP transport (default: 8765)",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=0,
        help=(
            "Start the observability web dashboard on localhost "
            "(default: 0 = disabled).  Suggested: 8766."
        ),
    )
    parser.add_argument(
        "--webhook-url",
        type=str,
        default=None,
        help="Webhook URL for push notifications",
    )
    parser.add_argument(
        "--failure-threshold",
        type=int,
        default=5,
        help="Notify after N consecutive failures (default: 5)",
    )
    parser.add_argument(
        "--long-run-seconds",
        type=int,
        default=300,
        help="Notify after this many seconds of execution (default: 300)",
    )
    return parser


def main() -> None:
    """Parse CLI arguments and run the MCP server.

    Supports ``--terminal`` for update progress windows,
    ``--default-image`` for overriding the default Docker image,
    ``--transport`` to select the MCP transport protocol,
    ``--dashboard-port`` for the observability dashboard,
    and ``--webhook-url`` for push notifications.

    HTTP-based transports (``sse``, ``http``, ``streamable-http``)
    are not subject to the ~60-second client timeout that affects
    ``stdio``, making them suitable for long-running Docker
    operations such as ``docker pull`` or ``copy_project`` on
    large directories.
    """
    parser = _build_arg_parser()
    args = parser.parse_args()

    from code_sandbox_mcp.tools import container as _ct_mod
    _ct_mod._TERMINAL = args.terminal
    _ct_mod._UPDATE_SPEC = args.update_spec
    if args.update_log_dir:
        _ct_mod._UPDATE_LOG_DIR = Path(args.update_log_dir)
    if args.default_image:
        validate_image_ref(args.default_image)
        _ct_mod._DEFAULT_IMAGE = args.default_image
    if args.shiori_repos_path:
        _ct_mod._SHIORI_REPOS_PATH = args.shiori_repos_path

    # Configure notifications if webhook is set
    if args.webhook_url or args.failure_threshold != 5 or args.long_run_seconds != 300:
        from code_sandbox_mcp.notify import configure

        configure(
            webhook_url=args.webhook_url,
            failure_threshold=args.failure_threshold,
            long_run_seconds=args.long_run_seconds,
        )

    # Start dashboard if requested
    if args.dashboard_port > 0:
        from code_sandbox_mcp.dashboard import start_dashboard

        msg = start_dashboard(port=args.dashboard_port)
        logger.info(msg)

    transport = args.transport
    if transport == "stdio":
        mcp.run(transport=transport)
    else:
        mcp.run(transport=transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
