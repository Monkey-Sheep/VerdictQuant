# Headless Analysis

`verdictquant-analyze` runs VerdictQuant's existing K-line and two-stage AI pipeline
without opening the PyQt desktop application. It is intended for scheduled or
Codex-controlled research jobs.

## Boundaries

- **US equities:** supported through public TradingView K-lines. Pass the
  listing exchange when known, such as `NASDAQ` or `NYSE`.
- **A-shares:** supported through AkShare for `1h`, `4h`, and `1d` bars, with
  an automatic Baostock fallback when the East Money endpoint is unavailable.
- **Crypto:** supported through TradingView. `--predict-next-bar` requests
  bullish, bearish, and neutral probabilities for the next candle.
- **Polymarket:** VerdictQuant can supply an underlying BTC/ETH price-action signal
  for short-duration directional markets. It still has no prediction-market
  metadata, CLOB, complete-set, fee, or resolution-risk model, so Polymarket
  scanning and paper trading remain a separate pipeline.
- **Execution:** unsupported by this command. It never connects to a brokerage,
  bank, MT5 terminal, or order API.
- **Research scope:** price action and risk-structure analysis only. News,
  fundamentals, valuation, portfolio concentration, and catalyst research must
  be supplied by a separate portfolio workflow.

## Examples

Fetch public data without using model tokens:

```powershell
uv run --frozen verdictquant-analyze `
  --market us --symbol COIN --exchange NASDAQ --timeframe 1d `
  --bar-count 100 --fetch-only --pretty
```

Run the full two-stage analysis and write a stable JSON summary:

```powershell
uv run --frozen verdictquant-analyze `
  --market us --symbol COIN --exchange NASDAQ --timeframe 1d `
  --bar-count 100 --output records\headless\latest-COIN-1d.json --pretty
```

Analyze an A-share:

```powershell
uv run --frozen verdictquant-analyze `
  --market a-share --symbol 600519 --timeframe 1d `
  --bar-count 100 --output records\headless\latest-600519-1d.json --pretty
```

Generate a five-minute BTC direction input for a Polymarket model:

```powershell
uv run --frozen verdictquant-analyze `
  --market crypto --symbol BTCUSDT --exchange BINANCE --timeframe 5m `
  --bar-count 100 --predict-next-bar `
  --output records\headless\latest-BTCUSDT-5m.json --pretty
```

When a valid next-bar prediction is returned, the first prediction for that
symbol, timeframe, and base candle is written immutably under
`records\forecasts\pending`. After the next candle closes, score it with:

```powershell
uv run --frozen verdictquant-forecast evaluate --pretty
uv run --frozen verdictquant-forecast summary --pretty
```

The evaluator records accuracy, three-class Brier score, log loss, and a
binary up/down Brier score that excludes the model's neutral probability. The
summary also reports confidence bins, expected calibration error, and model-level
breakdowns. Each forecast stores the model, decision stance, strategy files, and
an SHA-256 fingerprint of the exact two-stage analysis input.
Forecast history is evidence for calibration and retirement decisions; it is
not permission to place an order.

The AI key is loaded from the hardened Windows Credential Manager entry. The
command deliberately has no `--api-key` option, so a secret cannot leak into
shell history or scheduler arguments. Full analysis records remain under
`records\pending`; `--output` writes a compact machine-readable summary.

Consumers should gate on `status == "ok"` and `signal.actionable == true`.
Narrative model text is retained for review, but it must not override that
structured gate or be treated as an order instruction.
