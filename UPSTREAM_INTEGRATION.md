# VerdictQuant Upstream Integration Contract

VerdictQuant uses PA Agent as its modified upstream price-action core. The
VerdictQuant host adds the safety boundary, evidence routing, local paper
portfolio, packaging, and user-facing workbench. The first modification under
the VerdictQuant name was made on 2026-07-13.

VerdictQuant does not replace the quant, market-data, simulation, or order-book
engines with locally invented substitutes. No upstream project endorses this
derivative.

## Rules

1. Every signal, backtest, paper fill, or market snapshot must identify the
   upstream project that produced it.
2. Local code may normalize inputs and outputs, route candidates, enforce
   permissions, record evidence, and present the combined result.
3. Local code must not silently reimplement an upstream project's strategy or
   matching engine and present it under the upstream name.
4. PA Agent remains the upstream two-stage chart-analysis engine. Its result is
   an opinion, not an execution authority.
5. A-share execution semantics remain delegated to an A-share-aware engine.
6. Polymarket account state, order-book walking, fills, and fees remain
   delegated to prediction-market engines; the chart-analysis core can only contribute an
   underlying BTC/ETH price-action opinion.
7. Real bank, broker, wallet, private-key, and live-order integrations remain
   prohibited in this build.
8. The user-reported VOO/QQQM portfolio and fund `001437` remain read-only
   decision context. They are never merged into paper accounts or inferred from
   a bank/broker connection.

## Included source projects

The program's integrated-engine tab shows repository, role, license, installation
state, and integration type for:

- VerdictQuant
- PA Agent
- TradingAgents
- TradingView Screener
- Microsoft Qlib
- Backtrader
- RQAlpha
- Polymarket Paper Trader
- NautilusTrader

## Future open-source release

PA Agent is AGPL-3.0-or-later, so VerdictQuant remains AGPL-3.0-or-later and a
public derivative must comply with AGPL.
RQAlpha's upstream terms state that commercial use requires separate permission;
it must remain an optional user-supplied private-research adapter or be replaced
before a general commercial release. GPL/LGPL adapter packaging also requires a
release-specific compliance review.

The source registry is an engineering inventory, not legal advice. Before a
public or commercial release, verify every pinned revision and distribution
artifact again.
