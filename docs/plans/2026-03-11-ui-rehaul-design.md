# UI Rehaul Design

**Date:** 2026-03-11
**Goal:** Complete visual and structural rehaul of the CopyPelosi dashboard.

## Summary

Replace the current single-page scrollable layout with a tabbed interface built around the core use case: watching a Polymarket trader's live activity alongside your paper copy trades, with permanent visibility of key portfolio stats.

Aesthetic: modern crypto exchange (Hyperliquid / Coinbase Advanced style). Cyan `#00d4ff` accent, IBM Plex Mono for all numbers, Manrope for labels, dark `#060b10` background.

---

## Layout Structure

```
┌─────────────────────────────────────────────────────────┐
│ TOPBAR (sticky)                                         │
├─────────────────────────────────────────────────────────┤
│ STATS STRIP (sticky)                                    │
├─────────────────────────────────────────────────────────┤
│ TAB BAR: Live · Portfolio · History · Settings          │
├─────────────────────────────────────────────────────────┤
│ TAB CONTENT AREA                                        │
└─────────────────────────────────────────────────────────┘
```

---

## Topbar

Sticky. Two zones:

- **Left:** `CopyPelosi` (bold, 18px) · `Polymarket copy monitor` (muted subtitle, uppercase, 11px)
- **Right:** engine status pill (animated green dot `RUNNING` / red `PAUSED` / amber `STALE`) · sync latency chip (e.g. `47ms`, cyan) · `Force Sync` button · `Pause / Resume` button

Remove: clock, "last sync" text label. The heartbeat age is surfaced in the latency chip instead.

---

## Stats Strip

Sticky below topbar. Always visible regardless of active tab. 5 columns separated by subtle vertical dividers:

| NAV | Total Invested | Unrealized P/L | Fill Rate | Open Positions |
|-----|----------------|----------------|-----------|----------------|
| $5,234.10 | $1,820.00 | +$84.30 | 94% | 12 |

- All numbers: IBM Plex Mono
- Unrealized P/L: green if positive, red if negative
- Labels: cyan, 11px uppercase
- Background: slightly elevated from page bg, subtle bottom border

---

## Tab Bar

Four tabs: **Live · Portfolio · History · Settings**

- Cyan underline on active tab, no pill/background
- Sits below the stats strip

---

## Tab: Live

Default tab. Two equal-width columns, live refresh every 5s. No cards — tables render directly on the page background. A subtle vertical border separates the two columns.

### Left — Leader
Header: `@{target_handle}` + resolved wallet address (truncated to 10 chars)

1. **Current Positions** table
   - Columns: Market (truncated), Outcome, Shares, Value, Price
2. **Recent Trades** table
   - Columns: Time (PT), Market, Outcome, Side, Amount

### Right — Your Copies
Header: `Paper Portfolio` + fill rate chip

1. **Open Positions** table
   - Columns: Market (truncated), Outcome, Shares, Cost, Value, P/L (colored)
2. **Recent Orders** table
   - Columns: Time (PT), Market, Outcome, Side, USD, Price, Status

Table style: 12px monospace rows, alternating `rgba(255,255,255,0.02)` row shading, column headers in uppercase muted labels.

---

## Tab: Portfolio

- **Portfolio Curve** (full width, ~420px tall)
- **Daily Performance** table below (Date, Day Change, Closed P/L, Portfolio Value)

### Chart Spec (upgrade from current)

- Area chart, fill from **baseline value** (starting NAV) not y=0 — fill represents P/L only
- Line: 2px, no spline smoothing (sharp)
- Fill: gradient from line color → transparent going down
- Line/fill color: green (`#00c805`) if above baseline, red (`#ff5a63`) if below
- **Y-axis on the right** with dollar labels (`$5,200`, `$5,400`, etc.)
- Subtle horizontal gridlines at each y-axis tick (`rgba(255,255,255,0.04)`)
- Hover tooltip: `$5,284 · +$284` (value + delta from baseline)
- Remove floating annotation label — y-axis handles labeling
- X-axis: sparse time labels (not every point)

---

## Tab: History

Two stacked sections:

1. **Closed Trades**
   - Columns: Exit Time (PT), Market, Outcome, Shares, Cost, Proceeds, P/L
2. **Sync History**
   - Columns: Time (PT), Status, Trades Seen, New, Copied, Latency

---

## Tab: Settings

Fully inline — settings modal is removed entirely. Organized in 3 groups with section labels.

### Target
- Target Handle
- Target Wallet (optional)
- Reference Wallet

### Capital
- Paper Starting Balance
- Paper Cash Balance
- **Add Paper Money** — input field + `+ Add` button. Clicking adds the entered amount to the current cash balance immediately.

### Execution
- Max Total Exposure USD
- Slippage Bps
- Sync Interval ms
- Trade Fetch Limit
- Copy Sells (toggle: `1` / `0`)

`Save Settings` button at bottom.

---

## Visual Design Tokens

| Token | Value |
|-------|-------|
| Background | `#060b10` |
| Panel | `rgba(15, 23, 31, 0.94)` |
| Accent (cyan) | `#00d4ff` |
| Positive | `#00c805` |
| Negative | `#ff5a63` |
| Text | `#f5f8fb` |
| Muted | `#98a7b5` |
| Faint | `#667786` |
| Border | `rgba(255, 255, 255, 0.08)` |
| Font (UI) | Manrope |
| Font (numbers) | IBM Plex Mono |

Cyan replaces the existing blue (`#4aa3ff`) accent everywhere (buttons, chips, active tab, focus rings, fill rate, latency chip).

---

## Changes from Current UI

| Current | New |
|---------|-----|
| Single scrollable page | 4 tabs |
| Overview grid + hero panel + signal panel | Stats strip (always visible) |
| Pulse cards (4 boxes) | Removed — data folded into stats strip |
| Settings modal | Settings tab (inline) |
| Engine log + analysis panel | Removed from main view (log stays in History) |
| Portfolio chart fills to y=0 | Fills to baseline NAV |
| Blue accent | Cyan `#00d4ff` accent |
| Trade book as section card | Live tab right column |
| Execution Brief text blocks | Removed |

### Removed entirely
- Execution Brief analysis blocks
- Pulse cards section
- Overview hero/signal panels
- Settings modal overlay
- Clock display in topbar
