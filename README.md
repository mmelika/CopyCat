# CopyCat 🐱

> **Paper-trade alongside the best Polymarket traders — in real time.**

CopyCat is an open-source TypeScript bot that monitors a target user's Polymarket trades via WebSocket and automatically mirrors them proportionally into a virtual portfolio, with a live local dashboard.

No real funds. No risk. Just signal.

---

## What It Does

- **Monitors** a target Polymarket trader's activity in real time via the CLOB WebSocket API (~100ms latency)
- **Mirrors** every trade proportionally — if the target bets 10% of their portfolio, you paper-trade 10% of yours
- **Tracks** your virtual portfolio with unrealized P&L, open positions, and a live trade feed
- **Displays** everything on a zero-dependency local dashboard at `localhost:3000`

---

## Demo

```
┌─────────────────────────────────────────────────────────┐
│  CopyCat — @GamblingIsAllYouNeed          ● LIVE        │
├─────────────────────────────────────────────────────────┤
│  Portfolio: $1,000.00   P&L: +$42.30 (+4.2%)  [Reset]  │
├─────────────────────────────────────────────────────────┤
│  OPEN POSITIONS                                         │
│  Market              Side  Shares  Avg Price  Value     │
│  ─────────────────── ────  ──────  ─────────  ──────    │
│  Will Trump win...   YES   120     $0.62      $74.40    │
├─────────────────────────────────────────────────────────┤
│  TRADE FEED                                             │
│  12:04:01  BUY  YES  "Will Trump..."   $50 @ 0.62      │
└─────────────────────────────────────────────────────────┘
```

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                TypeScript / Node.js                  │
│                                                     │
│  ┌──────────────┐    ┌───────────────────────────┐  │
│  │ WS Monitor   │───▶│  Paper Trade Engine       │  │
│  │              │    │  - Proportional sizing    │  │
│  │ Polymarket   │    │  - Virtual portfolio      │  │
│  │ CLOB WS API  │    │  - P&L tracking           │  │
│  └──────────────┘    └──────────────┬────────────┘  │
│                                     │               │
│                      ┌──────────────▼────────────┐  │
│                      │  Express + SSE Dashboard  │  │
│                      │  localhost:3000            │  │
│                      └───────────────────────────┘  │
└─────────────────────────────────────────────────────┘
         │ WebSocket
         ▼
  wss://clob.polymarket.com
```

| Module | Responsibility |
|--------|---------------|
| `src/monitor.ts` | WebSocket client — connects to Polymarket CLOB WS, filters target user's trades |
| `src/engine.ts` | Paper trade engine — virtual portfolio, proportional sizing, P&L |
| `src/server.ts` | Express server — serves dashboard, streams updates via SSE |
| `src/dashboard/index.html` | Zero-dependency UI — positions table, trade feed, live P&L |
| `src/config.ts` | Central config — target user, starting balance, API URLs |

---

## Quick Start

**Prerequisites:** Node.js 18+

```bash
git clone https://github.com/yourusername/copycat.git
cd copycat
npm install
npm start
```

Then open `http://localhost:3000`.

---

## How Proportional Sizing Works

When the target trader makes a trade:

```
ourSize = (targetTradeSize / targetPortfolioValue) × ourVirtualBalance
```

**Example:** Target has $5,000 portfolio and bets $500 (10%). You have $1,000. You paper-trade $100.

This keeps position sizing relative regardless of portfolio size difference.

---

## Configuration

Edit `src/config.ts`:

```ts
export const config = {
  TARGET_USERNAME: 'GamblingIsAllYouNeed',  // Polymarket username to follow
  STARTING_BALANCE: 1000,                   // Virtual USDC starting balance
  DASHBOARD_PORT: 3000,                     // Local dashboard port
  WS_RECONNECT_BASE_MS: 1000,              // Reconnect backoff (exponential)
  WS_RECONNECT_MAX_MS: 30000,
} as const;
```

---

## Resilience

| Scenario | Behavior |
|----------|----------|
| WebSocket disconnect | Auto-reconnect with exponential backoff (1s → 2s → 4s → max 30s) |
| Target user not found | Crash on startup with a clear error message |
| Malformed trade event | Log and skip — never crashes the bot |
| Network blip | Dashboard shows `● RECONNECTING`, resumes automatically |

---

## Tech Stack

| Layer | Tech |
|-------|------|
| Runtime | Node.js + TypeScript |
| WebSocket | `ws` |
| HTTP / SSE | `express` |
| Dashboard | Vanilla HTML — no build step |
| Polymarket data | Gamma REST API + CLOB WebSocket |
| Tests | Jest + ts-jest |

---

## Development

```bash
npm run dev    # Watch mode with ts-node
npm test       # Run test suite (Jest)
npm run build  # Compile to dist/
```

Test coverage targets: Gamma API client, paper trade engine, WebSocket parser.

---

## Roadmap

- [ ] Real trading via Polymarket CLOB API + wallet signing
- [ ] Follow multiple traders simultaneously
- [ ] Stop-loss and max position size limits
- [ ] Historical backtest mode against past trade data
- [ ] Configurable target via CLI arg or `.env`

---

## Disclaimer

CopyCat is a **paper trading research tool**. It does not execute real trades or handle real funds. Past performance of any tracked trader is not indicative of future results. Use at your own risk.

---

## License

MIT
