"""Tests for _normalize_diff_for_git and apply_patch_to_file."""

from __future__ import annotations

# ===================================================================
# _parse_ruff_output tests
# ===================================================================


class _FakeContainer:
    """Emulates the in-container shell for the transform runner."""

    def __init__(self, path_map=None) -> None:
        self.ran = False
        self.path_map = path_map or {}

    def exec_run(self, cmd, **kwargs):
        import base64 as _b64
        import io
        import sys

        self.ran = True
        shell_cmd = cmd[-1]
        blob = shell_cmd.split("echo ", 1)[1].split(" | base64 -d", 1)[0].strip("'\"")
        runner_src = _b64.b64decode(blob).decode("utf-8")

        real_open = open
        pm = self.path_map

        def mapped_open(path, *a, **k):
            return real_open(pm.get(path, path), *a, **k)

        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            try:
                exec(compile(runner_src, "<runner>", "exec"), {"open": mapped_open})
            except SystemExit:
                pass
        finally:
            sys.stdout = old
        return 0, (buf.getvalue().encode("utf-8"), b"")


class _FakeClient:
    def __init__(self, container) -> None:
        self._c = container

    class _Containers:
        def __init__(self, c) -> None:
            self._c = c

        def get(self, _cid):
            return self._c

    @property
    def containers(self):
        return _FakeClient._Containers(self._c)


class TestNormalizeDiffForGit:
    """Pure-function tests for diff normalization (no container/git)."""

    def test_rewrites_headers_to_target(self) -> None:
        from src.code_sandbox_mcp.edit_verify import _normalize_diff_for_git

        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "index 111..222 100644\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,2 +1,2 @@\n a\n-b\n+B\n"
        )
        out = _normalize_diff_for_git(diff)
        assert out is not None
        assert out.startswith("--- a/target\n+++ b/target\n@@")
        # pre-hunk metadata is dropped
        assert "diff --git" not in out
        assert "index 111" not in out
        assert "foo.py" not in out
        # hunk body is preserved
        assert "-b\n+B" in out

    def test_returns_none_without_hunks(self) -> None:
        from src.code_sandbox_mcp.edit_verify import _normalize_diff_for_git

        assert _normalize_diff_for_git("--- a/x\n+++ b/x\n") is None
        assert _normalize_diff_for_git("") is None

    def test_returns_none_for_multi_file_diff(self) -> None:
        from src.code_sandbox_mcp.edit_verify import _normalize_diff_for_git

        multi = (
            "--- a/file1.py\n"
            "+++ b/file1.py\n"
            "@@ -1,2 +1,2 @@\n a\n-b\n+B\n"
            "--- a/file2.py\n"
            "+++ b/file2.py\n"
            "@@ -5,2 +5,2 @@\n x\n-y\n+Y\n"
        )
        assert _normalize_diff_for_git(multi) is None


class TestApplyPatchToFile:
    """Integration tests for the git-apply delegation (requires git)."""

    _POSIX = "/sandbox/x.py"

    def _apply(self, real_path, diff, monkeypatch):  # noqa: ANN001
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify.record_file_write",
            lambda *a, **k: None,
        )
        client = _FakeClient(_FakeContainer({self._POSIX: str(real_path)}))
        from src.code_sandbox_mcp.edit_verify import apply_patch_to_file

        return apply_patch_to_file(client, "abc123", self._POSIX, diff)

    def _read(self, p):  # noqa: ANN001
        with open(p, encoding="utf-8") as fh:  # universal newlines
            return fh.read()

    def test_applies_clean_diff(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "x.py"
        f.write_text("a\nb\nc\n", encoding="utf-8", newline="")
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
        out = self._apply(f, diff, monkeypatch)
        assert "successfully" in out
        assert self._read(f) == "a\nB\nc\n"

    def test_recount_tolerates_wrong_hunk_counts(self, tmp_path, monkeypatch) -> None:
        """--recount fixes off-by-one @@ counts the old strict parser rejected."""
        f = tmp_path / "x.py"
        f.write_text("a\nb\nc\n", encoding="utf-8", newline="")
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,9 +1,9 @@\n a\n-b\n+B\n c\n"
        out = self._apply(f, diff, monkeypatch)
        assert "successfully" in out
        assert self._read(f) == "a\nB\nc\n"

    def test_context_mismatch_is_error(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "x.py"
        f.write_text("a\nb\nc\n", encoding="utf-8", newline="")
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n-WRONG\n+B\n c\n"
        out = self._apply(f, diff, monkeypatch)
        assert out.startswith("Error")

    def test_empty_diff_is_noop(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "x.py"
        f.write_text("a\n", encoding="utf-8", newline="")
        out = self._apply(f, "   ", monkeypatch)
        assert "no changes" in out

    def test_diff_without_hunks_is_error(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "x.py"
        f.write_text("a\n", encoding="utf-8", newline="")
        out = self._apply(f, "--- a/x\n+++ b/x\n", monkeypatch)
        assert out.startswith("Error")
        assert "no hunks" in out


# ===================================================================
# run_verify regression tests (Issue #177)
# ===================================================================

