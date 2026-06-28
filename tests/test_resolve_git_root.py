"""Tests for resolve_git_root auto-detection."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from code_sandbox_mcp.tools.vcs import resolve_git_root


def _make_container(exec_run_returns: list) -> MagicMock:
    """Build a mock container with a side-effect sequence for exec_run."""
    container = MagicMock()
    container.exec_run.side_effect = exec_run_returns
    return container

# Convenience: metadata file not present (most common in tests)
_NO_META = (1, (b"cat: .sandbox-meta.json: No such file or directory\n", b""))


class TestResolveGitRootExplicit:
    """When working_dir is explicitly set, it is returned unchanged."""

    def test_explicit_path_returned_as_is(self) -> None:
        container = MagicMock()
        result = resolve_git_root(container, "/custom/path")
        assert result == "/custom/path"
        container.exec_run.assert_not_called()

    def test_default_value_triggers_autodetect(self) -> None:
        """'/home/sandbox' matches _DEFAULT_WD, so auto-detection runs."""
        container = _make_container([
            _NO_META,
            (0, (b"/home/sandbox\n", b"")),
        ])
        result = resolve_git_root(container, "/home/sandbox")
        assert result == "/home/sandbox"


class TestResolveGitRootMeta:
    """Step 0: container metadata points to the clone."""

    def test_meta_found_verified(self) -> None:
        container = _make_container([
            # Step 0: metadata found
            (0, (b'{"clone_path": "/custom/path/repo"}\n', b"")),
            # Verify it's a git repo
            (0, (b"/custom/path/repo\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/custom/path/repo"

    def test_meta_path_not_git_falls_through(self) -> None:
        """Metadata exists but path is not a git repo → fall through to Step 1."""
        container = _make_container([
            # Step 0: metadata found
            (0, (b'{"clone_path": "/custom/path/repo"}\n', b"")),
            # Verify: not a git repo
            (128, (b"fatal: not a git repository\n", b"")),
            # Step 1: /tmp/repo scan behind the simplified mock
            _NO_META,
            (0, (b"/tmp/repo/code-sandbox-mcp\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/tmp/repo/code-sandbox-mcp"


class TestResolveGitRootStep1:
    """Step 1: /home/sandbox is a git repository (no metadata)."""

    def test_home_sandbox_is_git_repo(self) -> None:
        container = _make_container([
            _NO_META,
            (0, (b"/home/sandbox\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/home/sandbox"

    def test_home_sandbox_subdir_repo(self) -> None:
        container = _make_container([
            _NO_META,
            (0, (b"/home/sandbox/my-project\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/home/sandbox/my-project"


class TestResolveGitRootStep2:
    """Step 2: fallback to /tmp/repo/*/ scan (no metadata)."""

    def test_tmp_repo_found(self) -> None:
        container = _make_container([
            _NO_META,
            # Step 1: /home/sandbox → not a git repo
            (128, (b"fatal: not a git repository\n", b"")),
            # Step 2: /tmp/repo/ code-sandbox-mcp found
            (0, (b"/tmp/repo/code-sandbox-mcp\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/tmp/repo/code-sandbox-mcp"

    def test_tmp_repo_no_repos_falls_back(self) -> None:
        container = _make_container([
            _NO_META,
            # Step 1: /home/sandbox → not a git repo
            (128, (b"fatal: not a git repository\n", b"")),
            # Step 2: /tmp/repo/ has no .git dirs
            (0, (b"__NO_REPO__\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/home/sandbox"


class TestResolveGitRootErrors:
    """Error handling for unexpected exec_run outputs."""

    def test_meta_bad_json_ignored(self) -> None:
        container = _make_container([
            # Step 0: metadata exists but is invalid JSON
            (0, (b"not json\n", b"")),
            # Step 1: /home/sandbox is a git repo
            (0, (b"/home/sandbox\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/home/sandbox"

    def test_meta_missing_clone_path_ignored(self) -> None:
        container = _make_container([
            # Step 0: metadata exists but no clone_path key
            (0, (b'{"other": "value"}\n', b"")),
            # Step 1: /home/sandbox is a git repo
            (0, (b"/home/sandbox\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/home/sandbox"

    def test_all_steps_fail(self) -> None:
        container = _make_container([
            _NO_META,
            # Step 1: /home/sandbox → not a git repo
            (128, (b"fatal: not a git repository\n", b"")),
            # Step 2: /tmp/repo/ → nothing
            (0, (b"__NO_REPO__\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/home/sandbox"
