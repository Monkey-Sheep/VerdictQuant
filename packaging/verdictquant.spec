from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

root = Path(SPECPATH).parent
datas = [(str(root / "prompt_engineering"), "prompt_engineering")]
datas.extend((str(root / "pa_agent" / "monitoring" / name), "pa_agent/monitoring") for name in ("policy.json", "calendars.json"))
datas.extend(collect_data_files("tzdata"))
datas.append((str(root / "pa_agent" / "gui" / "theme" / "dark.qss"), "pa_agent/gui/theme"))
hiddenimports = collect_submodules("pa_agent")
hiddenimports.extend(["win32cred", "win32crypt"])

for package in ("akshare", "baostock", "curl_cffi", "tvDatafeed"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas.extend(package_datas)
    hiddenimports.extend(package_hidden)
    globals().setdefault("extra_binaries", []).extend(package_binaries)

hiddenimports = [
    module
    for module in hiddenimports
    if ".tests" not in module.lower()
    and not module.startswith("pyqtgraph.opengl")
]
excludes = [
    "PyInstaller",
    "black",
    "hypothesis",
    "pytest",
    "ruff",
    "OpenGL",
    "pyqtgraph.opengl",
]

gui_a = Analysis(
    [str(root / "run.py")],
    pathex=[str(root)],
    binaries=globals().get("extra_binaries", []),
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
cli_a = Analysis(
    [str(root / "packaging" / "quant_cli_entry.py")],
    pathex=[str(root)],
    binaries=globals().get("extra_binaries", []),
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
updater_a = Analysis(
    [str(root / "packaging" / "update_entry.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    excludes=["PyQt6", "akshare", "baostock", "numpy", "pandas", "tvDatafeed"],
    noarchive=False,
)

gui_pyz = PYZ(gui_a.pure)
gui_exe = EXE(
    gui_pyz,
    gui_a.scripts,
    [],
    exclude_binaries=True,
    name="VerdictQuant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
cli_pyz = PYZ(cli_a.pure)
cli_exe = EXE(
    cli_pyz,
    cli_a.scripts,
    [],
    exclude_binaries=True,
    name="VerdictQuantCLI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
updater_pyz = PYZ(updater_a.pure)
updater_exe = EXE(
    updater_pyz,
    updater_a.scripts,
    updater_a.binaries,
    updater_a.datas,
    [],
    name="VerdictQuantUpdater",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
coll = COLLECT(
    gui_exe,
    gui_a.binaries,
    gui_a.datas,
    cli_exe,
    cli_a.binaries,
    cli_a.datas,
    updater_exe,
    strip=False,
    upx=True,
    name="VerdictQuant",
)
