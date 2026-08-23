"""Tests for sandbox_initialize(apply_from_container=...) -- Issue #889.

The carry is exercised with a mocked docker client (no real daemon): the old
container's ``git diff --binary HEAD`` is faked, the patch is shuttled through
the real ``copy_file`` mechanism into the new container, and ``git apply
--3way`` / ``git reset -q`` are asserted on the new container's mocked exec.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from sunaba.tools.container.lifecycle import sandbox_initialize

OLD_CID = "0f0d8d07a070"
NEW_CID = "new123456789"


def _make_container(cid: str, working_dir: str = "/workspace") -> MagicMock:
    c = MagicMock()
    c.id = cid
    c.labels = {}
    # The repo root is recorded in the container's WorkingDir at creation
    # time (sandbox_initialize passes clone_dest as working_dir), so
    # resolve_git_root reads it back from attrs -- give the mock a real one.
    c.attrs = {"Config": {"WorkingDir": working_dir}}
    return c


def _make_client(old_container: MagicMock, new_container: MagicMock) -> MagicMock:
    client = MagicMock()
    client.containers.run.return_value = new_container
    client.containers.list.return_value = []
    # copy_file looks the new container up by id; the carry looks the old up.
    def _get(cid: str) -> MagicMock:
        if cid == OLD_CID:
            return old_container
        return new_container

    client.containers.get.side_effect = _get
    return client


def _fake_exec_factory(
    patch_bytes: bytes = b"",
    untracked: bytes = b"",
    conflict_files: bytes = b"",
    changed: bytes = b"",
    conflict_apply: bool = False,
    base: bytes = b"HEAD\n",
    commits: bytes = b"0\n",
    manifest: bytes = b"",
    revparse_branch: bytes = b"main\n",
):
    """Return an exec_run side_effect that answers the git commands we issue.

    *base* / *commits* drive the checkpointed-commit path (finding 2): the
    merge-base answer and the ``rev-list --count <base>..HEAD`` answer.
    *manifest* is what the dependency-install manifest probe sees (so pip
    install actually runs when a python project is present). *revparse_branch*
    is the answer to ``git rev-parse --abbrev-ref HEAD`` used by the branch
    checkout path.
    """

    def fake(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)):
            c = " ".join(cmd)
        else:
            c = str(cmd)

        # The real docker SDK returns (stdout, stderr) tuples when demux=True
        # (which the branch/pr checkout and pip-install paths use); callers
        # unpack those.  Mirror that shape so the fake behaves like docker.
        def _ret(payload: bytes):
            if kwargs.get("demux"):
                return (0, (payload, b""))
            return (0, payload)

        if "git merge-base HEAD origin/HEAD" in c:
            return _ret(base)
        if "git rev-list --count" in c:
            return _ret(commits)
        if "git diff --binary" in c:
            return _ret(patch_bytes)
        if "for f in" in c:
            # The manifest probe (``cd <dir> && for f in pyproject.toml ...``).
            return _ret(manifest)
        if "git rev-parse --abbrev-ref HEAD" in c:
            return _ret(revparse_branch)
        if "git ls-files --others --exclude-standard" in c:
            return _ret(untracked)
        if "git apply --3way" in c:
            if conflict_apply:
                return (1, b"error: patch does not apply cleanly") if not kwargs.get("demux") else (1, (b"error: patch does not apply cleanly", b""))
            return _ret(b"")
        if "git diff --name-only --diff-filter=U" in c:
            return _ret(conflict_files)
        if "git diff --name-only" in c:
            return _ret(changed)
        if "git reset -q" in c:
            return _ret(b"")
        if "id -u" in c or "id -g" in c:
            # copy_file resolves the container user uid/gid via `id`.
            return _ret(b"1000\n")
        # Everything else (clone, mkdir/chown, manifest probe, copy probes).
        return _ret(b"")

    return fake


def _cmd_strings(container: MagicMock) -> list[str]:
    out = []
    for call in container.exec_run.call_args_list:
        cmd = call.args[0]
        last = cmd[-1] if isinstance(cmd, (list, tuple)) else cmd
        out.append(last if isinstance(last, str) else str(last))
    return out


@contextmanager
def _patched(client: MagicMock):
    """Patch the docker client + its side effects so init runs headless."""
    with patch("sunaba.tools.container._docker", return_value=client), patch(
        "sunaba.tools.common._docker", return_value=client
    ), patch(
        "sunaba.tools.file._docker", return_value=client
    ), patch(
        "sunaba.tools.container.lifecycle._ensure_image"
    ), patch(
        "sunaba.tools.container.lifecycle.validate_image_ref"
    ), patch(
        "sunaba.tools.container.lifecycle.build_secure_run_kwargs",
        return_value={},
    ), patch(
        "sunaba.tools.container.lifecycle._resolve_image_ref",
        lambda x: x,
    ), patch(
        "sunaba.proxy_lifecycle.egress_proxy_enabled",
        return_value=False,
    ), patch(
        # The branch/pr checkout paths resolve a host token and the default
        # branch via network; keep them offline for a headless test.
        "sunaba.tools.container.clone._resolve_vcs_token",
        return_value=None,
    ), patch(
        "sunaba.tools.container.clone._resolve_default_branch",
        return_value="main",
    ), patch(
        "sunaba.tools.container.clone.record_copy",
    ), patch(
        "sunaba.tools.container.lifecycle.record_initialize"
    ), patch(
        "sunaba.tools.container.lifecycle.record_initialize_complete"
    ), patch(
        "sunaba.tools.container.lifecycle.record_stop"
    ) as mock_stop, patch(
        "sunaba.tools.file.record_copy"
    ):
        yield mock_stop


def test_carried_two_files_clean():
    """A 2-file patch applies cleanly and the worktree carries the change."""
    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    patch_bytes = (
        b"diff --git a/src/a.py b/src/a.py\n+line_a\n"
        b"diff --git a/src/b.py b/src/b.py\n+line_b\n"
    )
    changed = b"src/a.py\nsrc/b.py\n"
    fake = _fake_exec_factory(patch_bytes, changed=changed)
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:"), result
    assert "carried 2 file(s)" in result, result
    assert "2 applied cleanly" in result, result
    cmds = _cmd_strings(new)
    assert any("git apply --3way /tmp/sunaba-carry.patch" in c for c in cmds)
    assert any("git reset -q" in c for c in cmds)
    # The patch was placed in the new container via copy_file.
    assert any("sunaba-carry.patch" in c for c in cmds)


def test_conflict_reported_and_not_error():
    """A path that conflicts is reported, not turned into an Error."""
    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    patch_bytes = b"diff --git a/src/x.py b/src/x.py\n+line\n"
    changed = b"src/x.py\n"
    conflict_files = b"src/x.py\n"
    fake = _fake_exec_factory(
        patch_bytes,
        conflict_files=conflict_files,
        changed=changed,
        conflict_apply=True,
    )
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:"), result
    assert "1 with conflicts: src/x.py" in result, result
    cmds = _cmd_strings(new)
    # Reset still runs so the index is clean even with conflict markers left.
    assert any("git reset -q" in c for c in cmds)


def test_apply_without_clone_errors():
    """apply_from_container without a clone is an Error naming the argument."""
    client = _make_client(_make_container(OLD_CID), _make_container(NEW_CID))

    with _patched(client):
        result = sandbox_initialize(apply_from_container=OLD_CID)

    assert result.startswith("Error:"), result
    assert "apply_from_container" in result
    # No container should have been created for a no-clone carry request.
    client.containers.run.assert_not_called()


def test_unknown_old_container_errors_and_cleans_up():
    """An unknown old container yields an Error and no half-init container."""
    from docker.errors import NotFound

    new = _make_container(NEW_CID)
    new.exec_run.side_effect = _fake_exec_factory(b"")
    old = _make_container(OLD_CID)
    old.status = "running"
    client = _make_client(old, new)

    def _get(cid: str) -> MagicMock:
        if cid == OLD_CID:
            raise NotFound("no such container")
        return new

    client.containers.get.side_effect = _get

    with _patched(client) as stop:
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert result.startswith("Error:"), result
    # The new container was created but torn down -- same path as other
    # init failures, so nothing is left running.
    assert new.remove.called
    assert stop.called


def test_git_apply_fatal_error_is_reported():
    """A non-conflict apply failure is a real Error, not a silent carry."""
    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    # apply --3way fails AND there are no conflicted (U) paths -> real error.
    fake = _fake_exec_factory(
        patch_bytes=b"diff --git a/src/z.py b/src/z.py\n+line\n",
        conflict_files=b"",
        changed=b"",
        conflict_apply=True,
    )
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client) as stop:
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert result.startswith("Error:"), result
    assert "git apply failed" in result, result
    # The new container was created but torn down on the carry error.
    assert new.remove.called
    assert stop.called


def test_empty_patch_and_untracked_reported():
    """An empty diff is not an error; untracked files are named, not carried."""
    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    fake = _fake_exec_factory(
        patch_bytes=b"",
        untracked=b"a.txt\nb.txt\n",
        changed=b"",
    )
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:"), result
    assert "old container had no uncommitted changes" in result, result
    assert "untracked not carried: a.txt, b.txt" in result, result
    # No patch was copied / applied when there was nothing to carry.
    cmds = _cmd_strings(new)
    assert not any("git apply --3way" in c for c in cmds)


def test_carry_runs_before_install_on_branch_path():
    """On the branch path the carry (git apply) must run BEFORE the dep
    install, so a carried change to pyproject.toml is what pip sees
    (Issue #889 finding 1)."""

    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    patch_bytes = (
        b"diff --git a/pyproject.toml b/pyproject.toml\n+carried-dep\n"
    )
    changed = b"pyproject.toml\n"
    # The manifest probe reports a python project so pip install actually
    # runs and we can assert ordering against it.
    fake = _fake_exec_factory(
        patch_bytes,
        changed=changed,
        manifest=b"pyproject.toml\n",
        revparse_branch=b"feature\n",
    )
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            branch="feature",
            apply_from_container=OLD_CID,
            pip_extras="[dev]",
        )

    assert not result.startswith("Error:"), result
    assert "carried 1 file(s)" in result, result
    cmds = _cmd_strings(new)
    apply_idx = next(i for i, c in enumerate(cmds) if "git apply --3way" in c)
    install_idx = next(i for i, c in enumerate(cmds) if "pip install" in c)
    # The carry must land before dependency install on the branch path.
    assert apply_idx < install_idx, cmds


def test_checkpointed_commit_carried():
    """A local (checkpointed) commit made by `checkpoint` plus an uncommitted
    edit both land, and the report states how many commits were folded in
    (Issue #889 finding 2)."""

    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    patch_bytes = (
        b"diff --git a/pyproject.toml b/pyproject.toml\n"
        b"--- a/pyproject.toml\n+++ b/pyproject.toml\n@@ -1 +1 @@\n+carried\n"
    )
    changed = b"pyproject.toml\n"
    # merge-base returns a real base (not HEAD) so rev-list reports a commit.
    fake = _fake_exec_factory(
        patch_bytes,
        changed=changed,
        base=b"baseabc123\n",
        commits=b"1\n",
    )
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:"), result
    assert "carried 1 file(s)" in result, result
    assert "(1 checkpointed commit(s) included)" in result, result
    cmds = _cmd_strings(new)
    assert any("git apply --3way /tmp/sunaba-carry.patch" in c for c in cmds)


def test_untracked_probe_uses_repo_root():
    """The untracked-file probe must run inside `cd <repo_root> &&`, matching
    every other git command in _carry_worktree (Issue #889 finding 3) -- it
    must not rely on the container's WORKDIR."""

    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    fake = _fake_exec_factory(
        patch_bytes=b"",
        untracked=b"a.txt\nb.txt\n",
        changed=b"",
    )
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:"), result
    # The probe is issued against the OLD (source) container.
    old_cmds = _cmd_strings(old)
    assert any(
        "cd /workspace && git ls-files --others --exclude-standard" in c
        for c in old_cmds
    ), old_cmds
    # And it is not issued without the cd prefix (the regression we guard).
    assert not any(
        c == "git ls-files --others --exclude-standard" for c in old_cmds
    ), old_cmds


def test_source_diff_runs_in_source_repo_root_not_new_clone_dest():
    """Finding 3 (round-2 form): the source container's diff/merge-base/untracked
    probes must run in the SOURCE container's own repo root, not the new
    container's clone_dest.  When the old container was created with a
    non-default clone_dest that differs from the new call's clone_dest, the
    carry must still land -- the old-side commands must cd into the old
    container's repo root, and the new-side commands into the new one.
    (Issue #889 finding 3.)"""

    OLD_WD = "/srv/old/repo"
    NEW_WD = "/workspace"
    assert OLD_WD != NEW_WD

    old = _make_container(OLD_CID, working_dir=OLD_WD)
    old.status = "running"
    new = _make_container(NEW_CID, working_dir=NEW_WD)
    patch_bytes = (
        b"diff --git a/src/a.py b/src/a.py\n+line_a\n"
    )
    changed = b"src/a.py\n"
    fake = _fake_exec_factory(patch_bytes, changed=changed)
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:"), result
    assert "carried 1 file(s)" in result, result

    old_cmds = _cmd_strings(old)
    new_cmds = _cmd_strings(new)

    # Source-side commands (merge-base, binary diff, untracked probe) must
    # run in the OLD container's repo root.
    assert any(
        f"cd {OLD_WD} && git merge-base HEAD origin/HEAD" in c for c in old_cmds
    ), old_cmds
    assert any(
        f"cd {OLD_WD} && git diff --binary" in c for c in old_cmds
    ), old_cmds
    assert any(
        f"cd {OLD_WD} && git ls-files --others --exclude-standard" in c
        for c in old_cmds
    ), old_cmds
    # And must NOT have run in the new container's clone_dest.
    assert not any(NEW_WD in c and "git" in c for c in old_cmds), old_cmds

    # Destination-side commands (apply, reset) must run in the NEW container's
    # repo root, never the old's.
    assert any(
        f"cd {NEW_WD} && git apply --3way /tmp/sunaba-carry.patch" in c
        for c in new_cmds
    ), new_cmds
    assert any(
        f"cd {NEW_WD} && git reset -q" in c for c in new_cmds
    ), new_cmds
    assert not any(OLD_WD in c for c in new_cmds), new_cmds


def test_fallback_base_reported_when_origin_head_missing():
    """When origin/HEAD is unresolvable in the source container, the diff
    base falls back to HEAD -- the init result must say so explicitly,
    since checkpointed commits sitting on top of the (missing) tracking ref
    are silently excluded from the carry otherwise (Issue #889 finding 3)."""

    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    patch_bytes = b"diff --git a/src/a.py b/src/a.py\n+line_a\n"
    changed = b"src/a.py\n"
    # Empty merge-base output simulates origin/HEAD being unresolvable.
    fake = _fake_exec_factory(patch_bytes, changed=changed, base=b"")
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:"), result
    assert "carried 1 file(s)" in result, result
    assert (
        "diff base: HEAD — origin/HEAD missing, checkpointed commits "
        "not included" in result
    ), result
