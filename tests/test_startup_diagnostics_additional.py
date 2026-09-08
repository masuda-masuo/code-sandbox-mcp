"""Additional tests for startup diagnostics (Issue #906).

Covers gaps found during orchestrator inspection:
- probe_docker version failure classification
- _classify_single_error errno semantics
- Sub-second timeout preservation for docker-py
- probe_sunaba_service TimeoutExpired / unexpected exception
- HTTP 401 auth denial in control API
"""

from __future__ import annotations

import errno
import json
from unittest.mock import MagicMock, patch

import pytest

import sunaba.diagnose as diagnose_mod
from sunaba.proxy_client import CONTROL_SECRET_ENV, CONTROL_URL_ENV


@pytest.fixture()
def control_env(monkeypatch):
    """Explicit URL and secret for control-API tests only."""
    url = "http://127.0.0.1:9099"
    secret = "test-control-secret-906"
    monkeypatch.setenv(CONTROL_URL_ENV, url)
    monkeypatch.setenv(CONTROL_SECRET_ENV, secret)
    return {"url": url, "secret": secret}


# ---------------------------------------------------------------------------
# probe_docker: version failure
# ---------------------------------------------------------------------------

class TestProbeDockerVersionFailure:
    """A failure from client.version() is a failed Docker dependency probe."""

    def test_version_failure_returns_error(self):
        """Version negotiation failure must not return status=ok with version unknown."""
        import docker.errors

        with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
            c = MagicMock()
            mock_from_env.return_value = c
            c.ping.return_value = True
            c.version.side_effect = docker.errors.DockerException("version failed")
            c.containers.get.side_effect = docker.errors.NotFound("no")
            check = diagnose_mod.probe_docker(timeout=3)

        assert check["status"] == "error"
        assert "version" in check["detail"].lower()
        assert check["error_kind"] == "unknown"

    def test_version_permission_error_classified(self):
        """PermissionError during version negotiation is classified as permission."""
        import docker.errors

        with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
            c = MagicMock()
            mock_from_env.return_value = c
            c.ping.return_value = True
            c.version.side_effect = PermissionError(errno.EACCES, "denied")
            c.containers.get.side_effect = docker.errors.NotFound("no")
            check = diagnose_mod.probe_docker(timeout=3)

        assert check["status"] == "error"
        assert check["error_kind"] == "permission"


# ---------------------------------------------------------------------------
# _classify_single_error: errno semantics
# ---------------------------------------------------------------------------

class TestClassifySingleError:
    """Classification by errno semantics: EACCES/EPERM => permission,
    ENOENT/ECONNREFUSED => unreachable, timeout => timeout, unexpected => unknown."""

    @pytest.mark.parametrize("errno_val", [errno.EACCES, errno.EPERM])
    def test_eacces_eperm_is_permission(self, errno_val):
        exc = OSError("denied")
        exc.errno = errno_val
        assert diagnose_mod._classify_single_error(exc) == "permission"

    @pytest.mark.parametrize("errno_val", [errno.ENOENT, errno.ECONNREFUSED])
    def test_enoent_econnrefused_is_unreachable(self, errno_val):
        exc = OSError("not found")
        exc.errno = errno_val
        assert diagnose_mod._classify_single_error(exc) == "unreachable"

    def test_timeout_is_timeout(self):
        assert diagnose_mod._classify_single_error(TimeoutError()) == "timeout"

    def test_unexpected_returns_none(self):
        assert diagnose_mod._classify_single_error(RuntimeError("oops")) is None

    def test_permission_error_without_errno_is_permission(self):
        """PermissionError without explicit errno still classified by type."""
        assert diagnose_mod._classify_single_error(PermissionError()) == "permission"

    def test_permission_error_with_enoent_errno_is_unreachable(self):
        """PermissionError wrapping ENOENT: errno wins over type."""
        exc = PermissionError(errno.ENOENT, "not found")
        assert diagnose_mod._classify_single_error(exc) == "unreachable"

    def test_os_error_with_eacces_errno_is_permission(self):
        """OSError with EACCES errno is permission, not unreachable."""
        exc = OSError("denied")
        exc.errno = errno.EACCES
        assert diagnose_mod._classify_single_error(exc) == "permission"


# ---------------------------------------------------------------------------
# _classify_docker_error: chain walking
# ---------------------------------------------------------------------------

class TestClassifyDockerErrorChain:
    """Wrapped exception traversal and secret redaction."""

    def test_cause_chain_resolves(self):
        import docker.errors

        inner = PermissionError(errno.EACCES, "denied")
        outer = docker.errors.DockerException("wrapper")
        outer.__cause__ = inner
        assert diagnose_mod._classify_docker_error(outer) == "permission"

    def test_context_chain_resolves(self):
        import docker.errors

        inner = ConnectionRefusedError(errno.ECONNREFUSED, "refused")
        outer = docker.errors.DockerException("wrapper")
        outer.__context__ = inner
        assert diagnose_mod._classify_docker_error(outer) == "unreachable"

    def test_args_wrapped_exception_resolves(self):
        import docker.errors

        inner = TimeoutError("timed out")
        outer = docker.errors.DockerException(inner, "wrapper")
        assert diagnose_mod._classify_docker_error(outer) == "timeout"


# ---------------------------------------------------------------------------
# Sub-second timeout preservation
# ---------------------------------------------------------------------------

class TestSubSecondTimeout:
    """docker.from_env receives the caller's sub-second float, not a clamped int."""

    def test_subsecond_timeout_passed_through(self):
        with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
            c = MagicMock()
            mock_from_env.return_value = c
            c.ping.return_value = True
            c.version.return_value = {"Version": "24.0.0"}
            c.containers.get.side_effect = __import__("docker").errors.NotFound("no")
            diagnose_mod.probe_docker(timeout=0.3)

        call_kwargs = mock_from_env.call_args
        timeout_arg = call_kwargs[1].get("timeout") or call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1]["timeout"]
        assert timeout_arg == pytest.approx(0.3, abs=0.01)

    def test_very_small_timeout_not_clamped_to_one(self):
        with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
            c = MagicMock()
            mock_from_env.return_value = c
            c.ping.return_value = True
            c.version.return_value = {"Version": "24.0.0"}
            c.containers.get.side_effect = __import__("docker").errors.NotFound("no")
            diagnose_mod.probe_docker(timeout=0.1)

        call_kwargs = mock_from_env.call_args
        timeout_arg = call_kwargs[1].get("timeout") or call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1]["timeout"]
        assert timeout_arg < 1.0


# ---------------------------------------------------------------------------
# probe_sunaba_service: TimeoutExpired and unexpected exception
# ---------------------------------------------------------------------------

class TestProbeSunabaServiceTimeout:
    def test_timeout_expired_degrades_to_unknown(self):
        with patch("sunaba.diagnose.shutil.which", return_value="/usr/bin/systemctl"):
            with patch("sunaba.diagnose.subprocess.run") as m:
                m.side_effect = __import__("subprocess").TimeoutExpired("systemctl", 3)
                check = diagnose_mod.probe_sunaba_service(timeout=3)
        assert check["status"] == "unknown"
        assert "timed out" in check["detail"].lower()
        assert check["next_action"]  # non-empty guidance


class TestProbeSunabaServiceUnexpected:
    def test_unexpected_exception_degrades_to_unknown(self):
        with patch("sunaba.diagnose.shutil.which", return_value="/usr/bin/systemctl"):
            with patch("sunaba.diagnose.subprocess.run") as m:
                m.side_effect = RuntimeError("dbus exploded")
                check = diagnose_mod.probe_sunaba_service(timeout=3)
        assert check["status"] == "unknown"
        assert check["next_action"]  # non-empty guidance
        assert "dbus" not in check["detail"].lower()  # secret/sensitive detail not leaked


# ---------------------------------------------------------------------------
# HTTP 401 auth denial
# ---------------------------------------------------------------------------

class TestControlUrl401Auth:
    def test_401_treated_as_auth_denial(self, control_env):
        import urllib.error

        import docker.errors

        with patch("sunaba.proxy_lifecycle.egress_proxy_enabled", return_value=True):
            with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
                c = MagicMock()
                mock_from_env.return_value = c
                c.ping.return_value = True
                c.version.return_value = {"Version": "24.0.0"}
                c.containers.get.side_effect = docker.errors.NotFound("no")
                with patch("sunaba.diagnose.urllib.request.urlopen") as u:
                    u.side_effect = urllib.error.HTTPError(
                        url=control_env["url"] + "/version",
                        code=401,
                        msg="Unauthorized",
                        hdrs=None,
                        fp=MagicMock(read=lambda: b"auth-error-body"),
                    )
                    check = diagnose_mod.probe_egress_proxy(timeout=3)
        assert check["status"] == "error"
        assert check.get("error_kind") == "auth"
        assert "auth-error-body" not in json.dumps(check)

    def test_403_treated_as_auth_denial(self, control_env):
        import urllib.error

        import docker.errors

        with patch("sunaba.proxy_lifecycle.egress_proxy_enabled", return_value=True):
            with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
                c = MagicMock()
                mock_from_env.return_value = c
                c.ping.return_value = True
                c.version.return_value = {"Version": "24.0.0"}
                c.containers.get.side_effect = docker.errors.NotFound("no")
                with patch("sunaba.diagnose.urllib.request.urlopen") as u:
                    u.side_effect = urllib.error.HTTPError(
                        url=control_env["url"] + "/version",
                        code=403,
                        msg="Forbidden",
                        hdrs=None,
                        fp=MagicMock(read=lambda: b"forbidden-body"),
                    )
                    check = diagnose_mod.probe_egress_proxy(timeout=3)
        assert check["status"] == "error"
        assert check.get("error_kind") == "auth"
        assert "forbidden-body" not in json.dumps(check)


# ---------------------------------------------------------------------------
# Removed raw lifecycle suggestions
# ---------------------------------------------------------------------------

class TestNoRawLifecycleSuggestions:
    def test_no_docker_run_in_sidecar_missing(self):
        """Sidecar missing next_action must not mention 'docker run'."""
        import docker.errors

        with patch("sunaba.proxy_lifecycle.egress_proxy_enabled", return_value=True):
            with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
                c = MagicMock()
                mock_from_env.return_value = c
                c.ping.return_value = True
                c.version.return_value = {"Version": "24.0.0"}
                c.containers.get.side_effect = docker.errors.NotFound("no")
                check = diagnose_mod.probe_egress_proxy(timeout=3)
        action = check.get("next_action", "").lower()
        assert "docker run" not in action
        assert "docker start" not in action
        assert "sunaba logs" in action or "networked" in action

    def test_no_docker_start_in_sidecar_stopped(self):
        """Sidecar stopped next_action must not mention 'docker start'."""
        with patch("sunaba.proxy_lifecycle.egress_proxy_enabled", return_value=True):
            with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
                c = MagicMock()
                mock_from_env.return_value = c
                c.ping.return_value = True
                c.version.return_value = {"Version": "24.0.0"}
                mock_ct = MagicMock()
                mock_ct.attrs = {
                    "State": {
                        "Running": False,
                        "Status": "exited",
                        "Health": None,
                    }
                }
                c.containers.get.return_value = mock_ct
                check = diagnose_mod.probe_egress_proxy(timeout=3)
        action = check.get("next_action", "").lower()
        assert "docker start" not in action
        assert "docker run" not in action
        assert "sunaba logs" in action or "networked" in action

    def test_no_docker_group_membership_in_permission(self):
        """Permission next_action must not suggest docker group membership."""
        with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
            err = PermissionError(errno.EACCES, "denied")
            mock_from_env.side_effect = err
            check = diagnose_mod.probe_docker(timeout=3)
        action = check.get("next_action", "").lower()
        assert "docker group" not in action
        assert "chmod" not in action
        assert "sudo" not in action


# ---------------------------------------------------------------------------
# _sanitize_endpoint: Unix and TCP endpoint preservation
# ---------------------------------------------------------------------------

class TestSanitizeEndpointUnix:
    """Unix socket endpoints must preserve the triple-slash spelling."""

    def test_unix_docker_sock_preserved(self):
        """Exact input unix:///var/run/docker.sock must sanitize to the same value."""
        result = diagnose_mod._sanitize_endpoint("unix:///var/run/docker.sock")
        assert result == "unix:///var/run/docker.sock"

    def test_unix_roundtrip_via_probe_docker(self):
        """_get_docker_endpoint uses _sanitize_endpoint; default must stay triple-slash."""
        import os

        # Ensure DOCKER_HOST is unset so the default fires
        with patch.dict(os.environ, {}, clear=True):
            with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
                c = MagicMock()
                mock_from_env.return_value = c
                c.ping.return_value = True
                c.version.return_value = {"Version": "24.0.0"}
                c.containers.get.side_effect = __import__("docker").errors.NotFound("no")
                check = diagnose_mod.probe_docker(timeout=3)
        assert check["endpoint"] == "unix:///var/run/docker.sock"


class TestSanitizeEndpointTCP:
    """TCP endpoints must strip credentials, query, fragment while preserving host:port."""

    def test_creds_query_fragment_stripped(self):
        """tcp://admin:secret@myhost:2376?q=1#f -> tcp://myhost:2376"""
        result = diagnose_mod._sanitize_endpoint("tcp://admin:secret@myhost:2376?q=1#f")
        assert result == "tcp://myhost:2376"

    def test_no_creds_preserved(self):
        """Plain tcp://host:port is unchanged."""
        result = diagnose_mod._sanitize_endpoint("tcp://myhost:2376")
        assert result == "tcp://myhost:2376"

    def test_fragment_only_stripped(self):
        """tcp://host:port#frag -> tcp://host:port"""
        result = diagnose_mod._sanitize_endpoint("tcp://myhost:2376#section")
        assert result == "tcp://myhost:2376"

    def test_query_only_stripped(self):
        """tcp://host:port?k=v -> tcp://host:port"""
        result = diagnose_mod._sanitize_endpoint("tcp://myhost:2376?timeout=5")
        assert result == "tcp://myhost:2376"
