from __future__ import annotations

from pathlib import Path

import pytest

from pa_agent.quant.launcher import quant_cli_command


def test_source_run_uses_python_module() -> None:
    command = quant_cli_command(
        ["--pretty", "status"],
        executable=r"C:\Python312\python.exe",
        frozen=False,
    )

    assert command == [
        r"C:\Python312\python.exe",
        "-m",
        "pa_agent.quant.cli",
        "--pretty",
        "status",
    ]


def test_frozen_gui_uses_sibling_cli(tmp_path: Path) -> None:
    gui = tmp_path / "VerdictQuant.exe"
    cli = tmp_path / "VerdictQuantCLI.exe"
    cli.write_bytes(b"test")

    command = quant_cli_command(
        ["--pretty", "doctor"],
        executable=str(gui),
        frozen=True,
    )

    assert command == [str(cli), "--pretty", "doctor"]


def test_frozen_gui_fails_closed_when_cli_is_missing(tmp_path: Path) -> None:
    gui = tmp_path / "VerdictQuant.exe"

    with pytest.raises(FileNotFoundError, match="paper CLI is missing"):
        quant_cli_command(
            ["--pretty", "status"],
            executable=str(gui),
            frozen=True,
        )
