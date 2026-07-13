"""Resolve the paper-only CLI executable for source and frozen GUI runs."""
from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path


def quant_cli_command(
    arguments: Sequence[str],
    *,
    executable: str | None = None,
    frozen: bool | None = None,
) -> list[str]:
    """Return a complete command for the local paper CLI.

    A source checkout uses ``python -m``. The packaged GUI must invoke the
    sibling console executable because its own frozen executable cannot act as
    a Python interpreter.
    """

    runtime = Path(executable or sys.executable)
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if not is_frozen:
        return [str(runtime), "-m", "pa_agent.quant.cli", *arguments]

    cli_name = "VerdictQuantCLI.exe" if runtime.suffix.lower() == ".exe" else "VerdictQuantCLI"
    cli_path = runtime.with_name(cli_name)
    if not cli_path.is_file():
        raise FileNotFoundError(
            f"Packaged paper CLI is missing beside the GUI: {cli_path}"
        )
    return [str(cli_path), *arguments]
