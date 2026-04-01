"""Preflight constraint checker for charter tasks.

Checks operational constraints before spawning a task:
- command_available: CLI tool exists on PATH
- file_exists: required file/directory exists
- env_var: environment variable is set
- network_reachable: host:port is reachable (with timeout)

Also auto-infers agent availability from execution.agent.
"""

import os
import shutil
import socket
from pathlib import Path


def check_constraints(charter: dict, instance_root: Path) -> list[str]:
    """Check all constraints from charter.yaml.

    Args:
        charter: Parsed charter.yaml dict.
        instance_root: The instance root directory (for relative path resolution).

    Returns:
        List of failure messages. Empty list = all constraints passed.
    """
    failures = []

    # Auto-infer: check that the declared agent is available
    agent = charter.get("execution", {}).get("agent", "codex")
    if agent == "direct":
        pass  # direct mode runs entrypoint via bash, no binary to check
    elif not shutil.which(agent):
        failures.append(f"agent '{agent}' not found on PATH")

    # Explicit constraints section
    constraints = charter.get("constraints", {})
    if not constraints:
        return failures

    # command_available: list of CLI commands that must exist
    for cmd in _as_list(constraints.get("command_available", [])):
        if not shutil.which(cmd):
            failures.append(f"command not found: {cmd}")

    # file_exists: list of paths (absolute or relative to instance_root)
    for path_str in _as_list(constraints.get("file_exists", [])):
        p = Path(path_str)
        if not p.is_absolute():
            p = instance_root / p
        if not p.exists():
            failures.append(f"required path not found: {path_str}")

    # env_var: list of env vars that must be set
    for var in _as_list(constraints.get("env_var", [])):
        if not os.environ.get(var):
            failures.append(f"env var not set: {var}")

    # network_reachable: list of "host:port" strings
    for endpoint in _as_list(constraints.get("network_reachable", [])):
        if ":" in endpoint:
            host, port_str = endpoint.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                failures.append(f"invalid network endpoint: {endpoint}")
                continue
        else:
            host, port = endpoint, 443
        if not _can_connect(host, port, timeout=5):
            failures.append(f"network unreachable: {endpoint}")

    return failures


def _as_list(val) -> list:
    """Normalize a value to a list (handles string, list, or None)."""
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    return list(val)


def _can_connect(host: str, port: int, timeout: float = 5) -> bool:
    """Try a TCP connection to host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, TimeoutError):
        return False
