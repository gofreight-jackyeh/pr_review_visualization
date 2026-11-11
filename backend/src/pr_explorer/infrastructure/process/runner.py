"""Injectable subprocess runner so external CLIs (git, gh, claude, node) can be faked in tests."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, args: list[str], cwd: str | None = None, input: str | None = None, timeout: float | None = None,
            env: dict[str, str] | None = None) -> RunResult: ...


class SubprocessRunner:
    """Runs a fixed argv (never a shell string)."""

    def run(self, args, cwd=None, input=None, timeout=None, env=None) -> RunResult:
        try:
            p = subprocess.run(args, cwd=cwd, input=input, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout, env=env,
                               check=False)
        except FileNotFoundError as exc:
            return RunResult(127, "", f"{args[0]}: not found ({exc})")
        except subprocess.TimeoutExpired:
            return RunResult(124, "", f"{args[0]}: timed out after {timeout}s")
        return RunResult(p.returncode, p.stdout, p.stderr)
