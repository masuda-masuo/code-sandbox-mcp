"""Startup dependency diagnostics CLI (Issue #906).

One-shot read-only probe of Docker connectivity, egress-proxy readiness,
and (optionally) systemd service status.  Never mutates Docker, services,
proxy state, or configuration.

Usage::

    python -m sunaba.diagnose [--json] [--timeout SECONDS]
    sunaba-diagnose            [--json] [--timeout SECONDS]

Exit codes: 0 = ready, 1 = not ready, 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any

import docker.errors

import docker
from sunaba.proxy_client import CONTROL_SECRET_ENV, CONTROL_TOKEN_HEADER, CONTROL_URL_ENV
from sunaba.proxy_lifecycle import PROXY_CONTAINER_NAME


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sanitize_endpoint(endpoint: str) -> str:
    """Strip credentials, query string, and fragment from an endpoint URL.

    Unix socket URLs (scheme ``unix://``) are returned as-is because they
    have no credentials to strip and ``urlunparse`` would mangle the
    triple-slash prefix.
    """
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(str(endpoint))
    # Unix sockets: preserve the original spelling (no netloc to strip)
    if parsed.scheme == "unix":
        return str(endpoint)
    # Rebuild without netloc credentials, query, fragment
    clean_netloc = parsed.hostname or ""
    if parsed.port:
        clean_netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=clean_netloc, query="", fragment=""))
def _get_docker_endpoint(client: Any) -> str:
    """Extract and sanitize the Docker daemon endpoint."""
    try:
        raw = getattr(client, "base_url", None)
        base_url = str(raw) if isinstance(raw, str) else ""
    except Exception:
        base_url = ""
    if not base_url:
        base_url = os.environ.get("DOCKER_HOST", "")
    if not base_url:
        base_url = "unix:///var/run/docker.sock"
    return _sanitize_endpoint(base_url)


def _classify_docker_error(exc: BaseException) -> str:
    """Classify a Docker-related exception into a diagnostic kind."""
    # Walk the cause chain to find the root classification
    current: BaseException | None = exc
    while current is not None:
        # Check the exception itself
        kind = _classify_single_error(current)
        if kind is not None:
            return kind
        # Also check args for wrapped exceptions (DockerException(perm_err, ...))
        for arg in getattr(current, "args", ()):
            if isinstance(arg, BaseException):
                kind = _classify_single_error(arg)
                if kind is not None:
                    return kind
        # Walk the cause/context chain
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return "unknown"


def _classify_single_error(exc: BaseException) -> str | None:
    """Classify a single exception (no chain walking). Returns None if not classifiable.

    Priority: check explicit errno first to disambiguate OS-level error codes,
    then fall back to exception type.  EACCES/EPERM => permission;
    ENOENT/ECONNREFUSED => unreachable; timeout => timeout.
    """
    import errno
    errno_val = getattr(exc, "errno", None)
    if errno_val is not None:
        if errno_val in (errno.EACCES, errno.EPERM):
            return "permission"
        if errno_val in (errno.ENOENT, errno.ECONNREFUSED):
            return "unreachable"
    # Type-based fallbacks for exceptions without explicit errno
    if isinstance(exc, PermissionError):
        return "permission"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return None


def _docker_timeout_value(timeout: float) -> float:
    """Preserve the caller's sub-second timeout for docker-py.

    docker-py accepts float values for timeout.  We pass the float
    directly rather than clamping to an integer, so a 0.3s caller
    deadline stays 0.3s (not silently rounded up to 1s).
    """
    return max(0.001, float(timeout))


# ---------------------------------------------------------------------------
# Probe: Docker
# ---------------------------------------------------------------------------

def probe_docker(timeout: float) -> dict[str, Any]:
    """Probe Docker daemon connectivity via the SDK.

    Returns a check dict with status 'ok', 'error', or 'unknown'.
    """
    try:
        client = docker.from_env(timeout=_docker_timeout_value(timeout))  # type: ignore[arg-type]  # docker-py accepts float at runtime
    except Exception as exc:
        kind = _classify_docker_error(exc)
        docker_host = os.environ.get("DOCKER_HOST", "")
        endpoint = _sanitize_endpoint(docker_host) if docker_host else _sanitize_endpoint("unix:///var/run/docker.sock")
        detail = f"Docker daemon unreachable ({kind})"
        return {
            "name": "docker",
            "status": "error",
            "detail": detail,
            "next_action": _docker_next_action(kind),
            "endpoint": endpoint,
            "error_kind": kind,
        }

    # Success path: ping and version
    endpoint = _get_docker_endpoint(client)
    try:
        client.ping()
    except Exception as exc:
        kind = _classify_docker_error(exc)
        return {
            "name": "docker",
            "status": "error",
            "detail": f"Docker daemon unreachable ({kind})",
            "next_action": _docker_next_action(kind),
            "endpoint": endpoint,
            "error_kind": kind,
        }

    try:
        version_info = client.version()
        version_str = version_info.get("Version", "unknown") if isinstance(version_info, dict) else "unknown"
    except Exception as exc:
        kind = _classify_docker_error(exc)
        return {
            "name": "docker",
            "status": "error",
            "detail": f"Docker version negotiation failed ({kind})",
            "next_action": _docker_next_action(kind),
            "endpoint": endpoint,
            "error_kind": kind,
        }

    # Verify we can list containers (read-only existence check)
    try:
        client.containers.get("nonexistent_probe_marker_906")
    except docker.errors.NotFound:
        pass  # Expected - container doesn't exist
    except Exception as exc:
        kind = _classify_docker_error(exc)
        return {
            "name": "docker",
            "status": "error",
            "detail": f"Docker API error ({kind})",
            "next_action": _docker_next_action(kind),
            "endpoint": endpoint,
            "error_kind": kind,
        }

    return {
        "name": "docker",
        "status": "ok",
        "detail": f"Docker daemon reachable, version {version_str}",
        "next_action": "",
        "endpoint": endpoint,
    }


def _docker_next_action(kind: str) -> str:
    """Conservative next-action text for Docker errors."""
    if kind == "unreachable":
        return "Verify Docker is installed and the daemon is running. Check DOCKER_HOST env var."
    if kind == "permission":
        return "Access to the Docker endpoint is denied. Inspect endpoint ownership and access policy."
    if kind == "timeout":
        return "Docker daemon is slow to respond. Check system load and Docker daemon health."
    return "Check Docker daemon logs for unexpected errors."


# ---------------------------------------------------------------------------
# Probe: Egress Proxy
# ---------------------------------------------------------------------------

def probe_egress_proxy(timeout: float) -> dict[str, Any]:
    """Probe egress-proxy readiness via control URL or sidecar inspection.

    Returns a check dict with status 'ok', 'error', 'unknown', or 'skipped'.
    """
    from sunaba.proxy_lifecycle import egress_proxy_enabled

    # Check if proxy is enabled
    if not egress_proxy_enabled():
        return {
            "name": "egress_proxy",
            "status": "skipped",
            "detail": "Egress proxy is disabled",
            "next_action": "",
        }

    # Check for explicit control URL
    control_url = os.environ.get(CONTROL_URL_ENV, "").strip()
    if control_url:
        return _probe_control_url(control_url, timeout)

    # No control URL: inspect sidecar container via Docker
    return _probe_sidecar_container(timeout)


def _probe_control_url(control_url: str, timeout: float) -> dict[str, Any]:
    """Probe via the explicit control URL (credentialed POST to /version)."""
    secret = os.environ.get(CONTROL_SECRET_ENV, "")
    version_url = control_url.rstrip("/") + "/version"

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if secret:
        headers[CONTROL_TOKEN_HEADER] = secret

    request = urllib.request.Request(version_url, data=b"{}", headers=headers, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            _status_code = resp.status
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return {
                "name": "egress_proxy",
                "status": "error",
                "detail": "Proxy control API rejected credentials",
                "next_action": "Verify SUNABA_PROXY_CONTROL_SECRET matches the sidecar configuration.",
                "error_kind": "auth",
            }
        return {
            "name": "egress_proxy",
            "status": "error",
            "detail": f"Proxy control API returned HTTP {exc.code}",
            "next_action": "Check proxy sidecar logs and control API availability.",
            "error_kind": "unknown",
        }
    except (urllib.error.URLError, TimeoutError, OSError):
        return {
            "name": "egress_proxy",
            "status": "error",
            "detail": "Proxy control API unreachable",
            "next_action": "Verify the egress proxy sidecar is running and the control URL is correct.",
            "error_kind": "timeout",
        }

    # Parse response
    try:
        data = json.loads(body)
        if not isinstance(data, dict) or not data.get("proxy_fingerprint"):
            return {
                "name": "egress_proxy",
                "status": "error",
                "detail": "Proxy returned empty or missing fingerprint",
                "next_action": "Check proxy sidecar logs for startup errors.",
                "error_kind": "unknown",
            }
    except (json.JSONDecodeError, ValueError):
        return {
            "name": "egress_proxy",
            "status": "unknown",
            "detail": "Proxy returned malformed response",
            "next_action": "Check proxy sidecar version and logs.",
            "error_kind": "unknown",
        }

    return {
        "name": "egress_proxy",
        "status": "ok",
        "detail": "Egress proxy control API responding",
        "next_action": "",
    }


def _probe_sidecar_container(timeout: float) -> dict[str, Any]:
    """Inspect the sidecar container via Docker metadata."""
    try:
        client = docker.from_env(timeout=_docker_timeout_value(timeout))  # type: ignore[arg-type]  # docker-py accepts float at runtime
    except Exception:
        return {
            "name": "egress_proxy",
            "status": "unknown",
            "detail": "Docker unavailable, cannot inspect proxy sidecar",
            "next_action": "Ensure Docker is running to inspect the egress proxy sidecar.",
        }

    try:
        container = client.containers.get(PROXY_CONTAINER_NAME)
    except docker.errors.NotFound:
        return {
            "name": "egress_proxy",
            "status": "error",
            "detail": f"Sidecar container '{PROXY_CONTAINER_NAME}' not found",
            "next_action": (
                f"The managed sidecar '{PROXY_CONTAINER_NAME}' is missing. "
                "Inspect sunaba logs and start a normal networked sunaba workflow."
            ),
        }
    except Exception:
        return {
            "name": "egress_proxy",
            "status": "unknown",
            "detail": "Cannot inspect proxy sidecar container",
            "next_action": "Check Docker daemon and sidecar container status.",
        }

    state = container.attrs.get("State", {})
    running = state.get("Running", False)
    status = state.get("Status", "")
    health = state.get("Health")

    if running and health and health.get("Status") == "healthy":
        return {
            "name": "egress_proxy",
            "status": "ok",
            "detail": "Egress proxy sidecar running and healthy",
            "next_action": "",
        }

    if not running:
        return {
            "name": "egress_proxy",
            "status": "error",
            "detail": f"Egress proxy sidecar stopped (status: {status})",
            "next_action": (
                f"The sidecar '{PROXY_CONTAINER_NAME}' is stopped. "
                "Inspect sunaba logs and start a normal networked sunaba workflow."
            ),
        }

    # Running but no health evidence
    return {
        "name": "egress_proxy",
        "status": "unknown",
        "detail": f"Egress proxy sidecar running but health status unknown (status: {status})",
        "next_action": "Check sidecar logs for health check configuration.",
    }


# ---------------------------------------------------------------------------
# Probe: Sunaba Service
# ---------------------------------------------------------------------------

def probe_sunaba_service(timeout: float) -> dict[str, Any]:
    """Probe systemd service status via systemctl.

    Returns status 'ok', 'unknown', or 'skipped'.
    """
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return {
            "name": "sunaba_service",
            "status": "skipped",
            "detail": "systemctl not available",
            "next_action": "",
        }

    try:
        result = subprocess.run(
            [systemctl, "--user", "is-active", "sunaba.service"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = result.stdout.strip().lower()

        if result.returncode == 0 and stdout == "active":
            return {
                "name": "sunaba_service",
                "status": "ok",
                "detail": "sunaba.service is active",
                "next_action": "",
            }

        # Inactive or failed
        action_hint = f"sunaba.service is {stdout or 'unknown'}. "
        if stdout in ("inactive", "failed"):
            action_hint += (
                "Check logs with: journalctl --user -u sunaba.service -n 50\n"
                "Restart with: systemctl --user restart sunaba.service"
            )
        else:
            action_hint += "Check service status with: systemctl --user status sunaba.service"

        return {
            "name": "sunaba_service",
            "status": "unknown" if stdout == "unknown" else "error",
            "detail": f"sunaba.service status: {stdout or 'unknown'}",
            "next_action": action_hint,
        }

    except subprocess.TimeoutExpired:
        return {
            "name": "sunaba_service",
            "status": "unknown",
            "detail": "systemctl timed out",
            "next_action": "Check if the user bus is available and systemd is responsive.",
        }
    except Exception:
        return {
            "name": "sunaba_service",
            "status": "unknown",
            "detail": "Could not query service status",
            "next_action": "Check systemd user session availability.",
        }


# ---------------------------------------------------------------------------
# Process-isolated probe runner
# ---------------------------------------------------------------------------

def _child_worker(probe_fn: Any, timeout: float, conn: Any) -> None:
    """Run probe_fn in a child process and send the result on conn."""
    try:
        result = probe_fn(timeout)
        conn.send(result)
    except Exception:
        conn.send({
            "name": getattr(probe_fn, "__name__", "unknown"),
            "status": "error",
            "detail": "Probe raised an unexpected error",
            "next_action": "Check probe implementation.",
            "error_kind": "unknown",
        })
    finally:
        conn.close()


_PROBES: dict[str, Any] = {
    "docker": probe_docker,
    "egress_proxy": probe_egress_proxy,
    "sunaba_service": probe_sunaba_service,
}


def run_probe(name: str, timeout: float) -> dict[str, Any]:
    """Run a named probe in an isolated child process with a real deadline.

    On timeout the child is killed and reaped; a sanitized timeout check is
    returned.  Never raises.
    """
    probe_fn = _PROBES.get(name)
    if probe_fn is None:
        return {
            "name": name,
            "status": "error",
            "detail": f"Unknown probe: {name}",
            "next_action": "",
        }

    parent_conn, child_conn = multiprocessing.Pipe(duplex=False)
    p = multiprocessing.Process(target=_child_worker, args=(probe_fn, timeout, child_conn))
    p.start()
    child_conn.close()  # Parent doesn't write

    # Wait for result with deadline -- only process launch/reap overhead
    # outside the requested timeout, not a full extra second.
    deadline = timeout + 0.5
    if p.exitcode is None and parent_conn.poll(deadline):
        try:
            result = parent_conn.recv()
        except EOFError:
            result = {
                "name": name,
                "status": "error",
                "detail": "Probe process closed connection unexpectedly",
                "next_action": "",
                "error_kind": "unknown",
            }
    elif p.exitcode is None:
        # Deadline exceeded: kill the child
        if p.pid is not None:
            try:
                os.kill(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        p.join(timeout=1.0)
        result = {
            "name": name,
            "status": "error",
            "detail": f"Probe '{name}' timed out after {timeout}s",
            "next_action": f"The {name} check exceeded its time budget. Increase --timeout or check the dependency.",
            "error_kind": "timeout",
        }
    else:
        # Process already exited
        try:
            result = parent_conn.recv()
        except (EOFError, OSError):
            result = {
                "name": name,
                "status": "error",
                "detail": f"Probe '{name}' exited without producing output",
                "next_action": "",
                "error_kind": "unknown",
            }

    parent_conn.close()
    # Ensure child is reaped
    if p.is_alive():
        if p.pid is not None:
            try:
                os.kill(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        p.join(timeout=1.0)

    return result


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def diagnose(timeout: float = 3.0) -> dict[str, Any]:
    """Run all dependency probes and assemble a diagnostic report.

    ``timeout`` is the per-check budget in seconds.  Must be finite and
    positive; 0, negative, NaN, or Inf raises ValueError.
    """
    if timeout <= 0 or timeout != timeout:  # NaN check
        raise ValueError(f"timeout must be positive and finite, got {timeout}")
    if timeout == float("inf"):
        raise ValueError("timeout must be finite")

    check_names = ["docker", "egress_proxy", "sunaba_service"]
    checks = [run_probe(name, timeout) for name in check_names]

    # Readiness: Docker must be ok AND (proxy ok OR proxy skipped/disabled)
    docker_ok = any(c["name"] == "docker" and c["status"] == "ok" for c in checks)
    proxy = next(c for c in checks if c["name"] == "egress_proxy")
    proxy_ready = proxy["status"] in ("ok", "skipped")
    ready = docker_ok and proxy_ready

    return {
        "checks": checks,
        "ready": ready,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point for ``python -m sunaba.diagnose`` and ``sunaba-diagnose``."""
    parser = argparse.ArgumentParser(
        prog="sunaba-diagnose",
        description="Startup dependency diagnostics for sunaba",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output report as JSON",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="Per-check timeout in seconds (default: 3.0)",
    )

    args = parser.parse_args()

    # Validate timeout
    if args.timeout <= 0 or args.timeout != args.timeout or args.timeout == float("inf"):
        print(f"error: --timeout must be positive and finite, got {args.timeout}", file=sys.stderr)
        sys.exit(2)

    try:
        report = diagnose(timeout=args.timeout)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.json_output:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)

    sys.exit(0 if report["ready"] else 1)


def _print_human(report: dict[str, Any]) -> None:
    """Pretty-print the diagnostic report for human consumption."""
    status_symbols = {
        "ok": "✓",
        "error": "✗",
        "unknown": "?",
        "skipped": "–",
    }

    for check in report["checks"]:
        symbol = status_symbols.get(check["status"], "?")
        line = f"  {symbol} {check['name']}: {check['status']}"
        if check.get("detail"):
            line += f" — {check['detail']}"
        print(line)
        if check.get("next_action"):
            print(f"    → {check['next_action']}")

    ready_text = "READY" if report["ready"] else "NOT READY"
    print(f"\n{ready_text}")


if __name__ == "__main__":
    main()
