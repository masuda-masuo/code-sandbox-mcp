def _is_wsl() -> bool:
    """Return True when running inside WSL or a Docker container on WSL2.

    Detection priority:
    1. ``WSL_DISTRO_NAME`` env var — set in native WSL sessions.
    2. ``/proc/version`` containing ``microsoft`` — Docker containers
       running on the WSL2 backend share the WSL2 kernel and have this
       string in ``/proc/version`` even though ``WSL_DISTRO_NAME`` is
       not inherited by the container.
    """
    if sys.platform == "win32":
        return False
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    # Docker containers on the WSL2 backend share the WSL2 kernel.
    # /proc/version contains "microsoft" in that case.
    try:
        with open("/proc/version") as _f:
            return "microsoft" in _f.read().lower()
    except OSError:
        return False
