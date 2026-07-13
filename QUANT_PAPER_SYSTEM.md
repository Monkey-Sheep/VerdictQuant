# VerdictQuant Automated Quant Paper System

## Product boundary

This build is a paper-only automated stock-analysis and validation system.
It never connects to a bank, broker account, wallet, or live order endpoint.
Version 1.0.0 is the engineering release baseline, not a claim of profitability.

The upstream projects remain the foundation:

- VerdictQuant supplies guarded two-stage price-action analysis.
- Backtrader reference strategies provide independent historical comparison.
- RQAlpha supplies A-share market-rule validation when installed.
- vn.py PaperAccount supplies the reference semantics for simulated order crossing.
- Qlib remains the factor and walk-forward research engine.

The local integration adds only the contracts and controls needed to make these
tools work as one product: normalized signals, risk sizing, a SQLite paper ledger,
next-bar execution, market rules, validation metrics, CLI, and GUI.

## First use

1. Start VerdictQuant.
2. Open `AI 模型设置`, select the supported provider, enter the API key, and save.
   The key is stored in Windows Credential Manager, not in the project JSON.
3. Open `综合研究中心 -> 自动量化模拟`.
4. Choose `美股` or `A股`, enter any valid stock code, and click
   `分析并进入模拟`.
5. Run `结算后续K线` after later bars close, or add the stock to the automatic
   watchlist and run a watch cycle.

No broker configuration is required.

## CLI for AI callers

```powershell
verdictquant-paper --pretty init
verdictquant-paper --pretty doctor
verdictquant-paper --pretty analyze --market us --symbol NVDA --exchange NASDAQ --timeframe 1d
verdictquant-paper --pretty analyze --market a-share --symbol 600519 --timeframe 1d
verdictquant-paper --pretty settle
verdictquant-paper --pretty watch-add --market us --symbol ASML --exchange NASDAQ
verdictquant-paper --pretty cycle
verdictquant-paper --pretty status
verdictquant-paper --pretty backup
```

Every response includes `paper_only=true` and `live_execution=false`.

## Simulation invariants

- The analysis bar can never fill its own signal; entry is eligible only on a later closed bar.
- Stocks are long-only by default.
- A buy requires a stop below entry and the minimum configured confidence.
- Position size is capped by risk-per-trade, account cash, order fraction, and symbol fraction.
- US shares use one-share lots; A-shares use 100-share lots and T+1 exits.
- Market, limit, and stop orders include configurable slippage and fees.
- Fill prices are rounded adversely to the market tick and gap fills are resized
  against the actual fill price, cash, symbol cap, and stop risk.
- If stop and target are both touched in one OHLC bar, the simulator uses the stop first.
- Repeated analysis of the same symbol/base bar is idempotent and cannot duplicate an order.
- Concurrent settlement cannot fill or close the same paper position twice.
- Zero-volume, non-finite, or malformed OHLC bars cannot create fills.
- A newer closed-bar opinion cancels an older unfilled buy thesis for the same symbol.
- Analysis-only calls with `queue=false` record evidence without cancelling or
  creating paper orders.
- Add-on entries are disabled: one symbol has one auditable entry thesis and one
  entry-fee basis at a time.
- Entry confidence gates never block a risk-reducing exit from an existing long
  position. An automatic stop/target/time exit cancels stale pending sell orders
  for that position in the same transaction.
- Net realized P/L includes both entry and exit fees. Every closed trade retains
  its source signal, model/input fingerprint, exit reason, and calibration evidence.
- Live promotion is impossible from this module; 30 closed trades only marks the sample as reviewable.

The status report includes expectancy, profit factor, realized drawdown, exit-reason
and model breakdowns, estimated-win-rate Brier calibration, and recent outcomes.
`promotion_ready` remains permanently false and `live_promotion_allowed` is always
false. A sample becoming reviewable does not authorize a real account.

## Runtime data

Source and packaged Windows runs use the same per-user local portfolio under
`%LOCALAPPDATA%\VerdictQuant\records\quant`. Linux uses
`$XDG_DATA_HOME/verdictquant` (or `~/.local/share/verdictquant`) and macOS
uses `~/Library/Application Support/VerdictQuant`. Therefore, cloning or
upgrading the source does not create a second default paper account.

The first run creates a permanent random `profile_id` and deterministic IDs for
the US and A-share currency ledgers. Restarting or upgrading never changes those
IDs. Existing checkout-local data is imported once only when it contains more
activity than a pristine local target. The previous target is preserved before
that import.

`VERDICTQUANT_DATA_HOME`, `VERDICTQUANT_QUANT_HOME`, `VERDICTQUANT_QUANT_DB`, and
`VERDICTQUANT_QUANT_CONFIG` are explicit advanced/test overrides. The legacy
`PA_AGENT_*` names remain read-only upgrade aliases. Using an override
intentionally creates a separate data boundary.

The default paper accounts are `100,000 USD` for US stocks and `1,000,000 CNY`
for A-shares. They are independent validation accounts, not representations of
the user's real holdings.

## Account maintenance

The GUI exposes `备份账户`, `恢复账户`, and `重置账户`. The equivalent CLI is:

```powershell
verdictquant-paper --pretty backup --output D:\Backups\paper-account.zip
verdictquant-paper --pretty status
verdictquant-paper --pretty restore --archive D:\Backups\paper-account.zip --confirm "RESTORE <profile_id>"
verdictquant-paper --pretty reset --confirm "RESET <profile_id>"
```

Restore and reset require the exact current-profile confirmation phrase returned
by `status`. Both create an automatic safety backup before changing the ledger.
Restore checks the archive allowlist, size limits, SHA-256 hashes, paper-only
marker, encryption flags, decompression ratio, config model, database schema,
profile identity, and SQLite integrity.

Backups contain the paper database, risk configuration, and watchlist. They do
not contain model API keys, Windows Credential Manager secrets, brokerage
credentials, or any live-account information.

## Windows release

Run:

```powershell
scripts\build_windows_release.ps1
```

The distributable entry point is:

```text
dist\VerdictQuant\VerdictQuant.exe
dist\VerdictQuant\VerdictQuantCLI.exe
```

Recipients can run the executable and configure only their model API key for
PA analysis and local paper validation. Optional external research engines can
be shipped or installed separately according to their licenses.

The packaged CLI is directly callable by Codex or another local automation:

```powershell
.\VerdictQuantCLI.exe --pretty doctor
.\VerdictQuantCLI.exe --pretty status
.\VerdictQuantCLI.exe --pretty analyze --market us --symbol NVDA --exchange NASDAQ
```

The release build runs all non-live tests, a packaged CLI doctor check in an
isolated temporary paper account, and atomically writes `BUILD-MANIFEST.json`,
`SHA256SUMS.txt`, an independent `VERIFY_RELEASE.ps1`, and a standards-compliant
final archive.

## Evidence limits that remain by design

- OHLC bars cannot prove queue position, partial liquidity, bid/ask depth, or an
  A-share limit-up/limit-down order-book lock. RQAlpha or another pinned upstream
  engine must independently validate those cases.
- Splits, dividends, symbol changes, delistings, survivorship bias, and data-vendor
  revisions require adjusted-data and external walk-forward evidence.
- LLM confidence is not treated as calibrated probability. The ledger measures
  calibration after outcomes and keeps automatic live promotion disabled.
- No engineering release can prove future profitability. Only sufficiently long,
  out-of-sample paper results with costs and regime coverage can support a later
  manual review.
