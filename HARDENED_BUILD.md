# VerdictQuant Hardened Build

This copy is based on upstream revision
`33170abdfe55d6d09aed5276dd176a4d4b2467c9` and is intended for analysis only.
It does not add order execution or brokerage automation.

The integrated paper-validation release is version `1.0.0`. “Release” means
known engineering issues and non-live checks are closed; it does not mean a
strategy has been proven profitable.

## Security boundaries

- API and notification secrets are stored in the current user's Windows
  Credential Manager under `VerdictQuant/*`. Existing `PA_Agent_Hardened/*`
  entries are copied on first read for upgrade compatibility.
- `config/settings.json` contains non-secret settings only. Legacy plaintext
  secrets are migrated on first load and removed from the JSON file.
- QClaw, WorkBuddy, and Cursor agent routes are rejected by settings,
  client construction, and runtime provider updates.
- Tushare and Cursor SDK are not installed. Legacy Tushare settings migrate to
  AkShare.
- Remote providers require HTTPS. Plain HTTP is accepted only for a literal
  loopback host.
- AI output containing `NaN` or positive/negative infinity is rejected before
  normalization and again before schema validation.
- The default market source is anonymous TradingView so startup does not attach
  to a local MT5 terminal. MT5 remains an explicit user-selected data-only
  feature from the upstream application.

## Reproducible install

The supported runtime is Python 3.12. Dependency versions are sealed in
`uv.lock`; package hashes are exported in `requirements.lock`. The
`tvdatafeed` source dependency is pinned to commit
`e6f6aaa7de439ac6e454d9b26d2760ded8dc4923`.

```powershell
uv sync --frozen --extra dev --python 3.12
uv run --frozen pytest tests\unit\test_settings_round_trip.py `
  tests\unit\test_hardened_policy.py `
  tests\unit\test_client_factory.py `
  tests\unit\test_non_finite_model_output.py -q
```

Start the desktop application with:

```powershell
.\scripts\start_hardened.ps1
```

For unattended analysis without the desktop UI, use `verdictquant-analyze`. See
[`HEADLESS_AUTOMATION.md`](HEADLESS_AUTOMATION.md) for the supported US and
A-share modes. This command reads public market data and produces analysis
records only; it has no brokerage or order-execution integration.

The Windows archive also includes `VerdictQuantCLI.exe`, so another local AI or
scheduler can invoke `doctor`, `status`, `analyze`, `settle`, `cycle`, and account
maintenance without installing Python. Every command remains paper-only and
returns structured JSON.

The package includes the version-matched Chinese tutorial as
`USER_GUIDE_CN.html` and `USER_GUIDE_CN.md`. The tutorial button in both settings
dialogs opens the local copy first, so it remains available offline.

The extracted release can verify every manifested file, size, and SHA-256 hash
without Python:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\VERIFY_RELEASE.ps1
```

The desktop program also contains an integrated research center. It reads the
external US-stock, A-share, and Polymarket paper evidence in the parent finance
workspace. Double-clicking a stock moves the symbol into VerdictQuant's chart tab;
the paper-cycle buttons can execute only the fixed `closed_loop` scripts.

AI callers can read the same evidence or run one allowlisted paper cycle with:

```powershell
uv run --frozen verdictquant-finance --pretty status
uv run --frozen verdictquant-finance --pretty run stocks
uv run --frozen verdictquant-finance --pretty run polymarket
```

`verdictquant-finance` accepts no broker URL, wallet credential, order payload, or
arbitrary command. It remains paper/research only.

The integration is explicitly upstream-first. See
[`UPSTREAM_INTEGRATION.md`](UPSTREAM_INTEGRATION.md). The GUI exposes the source
registry under **综合研究中心 → 集成引擎**, and the same registry is available to
AI callers with:

```powershell
uv run --frozen verdictquant-finance --pretty engines
```

One combined AI entrypoint can also delegate a symbol directly to VerdictQuant's
existing headless two-stage pipeline:

```powershell
uv run --frozen verdictquant-finance --pretty analyze `
  --market us --symbol SPCX --exchange NASDAQ --timeframe 1d
```

Use `--fetch-only` to verify market-data routing without spending model tokens.

Do not paste brokerage passwords, bank credentials, or recovery phrases into
the application. Configure only an AI-provider key that you intend this local
analysis application to use.

## Verification snapshot

- Windows Credential Manager write/read/delete self-test: passed.
- No-credential application bootstrap with anonymous TradingView: passed.
- Hardened security and migration regression tests: passed.
- Dependency audit: no known vulnerabilities in indexed packages. The local
  project and pinned Git-only `tvdatafeed` package are not indexed by PyPI's
  advisory service; the pinned `tvdatafeed` setup and Python sources received a
  separate manual install-time review.
- Full unit suite: 723 passed, 6 Cursor SDK tests intentionally skipped because
  that dependency is excluded.
- Non-live integration/property/end-to-end suites: 79 passed, 7 live tests
  deliberately deselected.
- Release integration modules pass complete Ruff checks; the application,
  packaging entry points, and critical release tests pass bytecode compilation,
  and the working diff has no whitespace errors.
- The complete GUI end-to-end suite also passed five consecutive teardown
  stability runs after hardening the pyqtgraph shutdown path.
- SQLite integrity and foreign keys, closed-bar idempotency, concurrent fill
  exclusion, A-share T+1 waiting, net fee attribution, adverse tick rounding,
  gap risk resizing, zero-volume rejection, signal lineage, confidence
  calibration, backup decompression limits, package hashes, and isolated
  packaged-CLI startup: verified.
- The final archive uses portable `/` ZIP entry names and includes an independent
  UTF-8-aware integrity verifier.
- Live endpoint tests are deliberately excluded and no real account is used.
