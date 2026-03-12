from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from zoneinfo import ZoneInfo

import dash
import plotly.graph_objects as go
from flask import jsonify
from dash import Input, Output, State, callback_context, dcc, html, no_update

from copytrader import database
from copytrader.config import DB_PATH
from copytrader.engine import CopyTradingEngine, _effective_exposure_cap
from copytrader.polymarket import PolymarketClient


database.init_db(DB_PATH)

base_path = os.environ.get("DASH_URL_BASE_PATHNAME", "/").strip() or "/"
if not base_path.startswith("/"):
    base_path = f"/{base_path}"
if not base_path.endswith("/"):
    base_path = f"{base_path}/"

app = dash.Dash(
    __name__,
    suppress_callback_exceptions=True,
    requests_pathname_prefix=base_path,
    routes_pathname_prefix=base_path,
)
app.title = "CopyPelosi"
server = app.server
MAX_HEARTBEAT_STALE_SECONDS = 30
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")


def parse_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def to_pacific(value: str) -> datetime | None:
    parsed = parse_utc(value)
    if parsed is None:
        return None
    return parsed.astimezone(PACIFIC_TZ)


def fmt_pacific_time(value: str) -> str:
    dt = to_pacific(value)
    return dt.strftime("%m/%d %I:%M:%S %p") if dt else "-"


def fmt_pacific_clock() -> str:
    return datetime.now(PACIFIC_TZ).strftime("%b %d, %Y %I:%M:%S %p %Z")


def fmt_pacific_day(value: str) -> str:
    dt = to_pacific(value)
    return dt.strftime("%Y-%m-%d") if dt else (value or "-")


def engine_runtime_status(app_state: dict, settings: dict) -> tuple[str, str, int | None]:
    engine_status = app_state.get("engine_status", "PAUSED")
    last_sync_at = parse_utc(app_state.get("last_sync_at", ""))
    stale_age_seconds = None

    if engine_status == "RUNNING" and last_sync_at is not None:
        stale_age_seconds = int((datetime.now(timezone.utc) - last_sync_at).total_seconds())
        if stale_age_seconds > MAX_HEARTBEAT_STALE_SECONDS:
            return "STALE", "status-stopped", stale_age_seconds

    if engine_status == "RUNNING" and last_sync_at is None:
        return "STARTING", "status-running", None

    return engine_status, status_class(engine_status), stale_age_seconds


def heartbeat_label(app_state: dict, runtime_status: str, stale_age_seconds: int | None) -> str:
    last_sync_at = app_state.get("last_sync_at")
    if runtime_status == "STALE":
        return f"Heartbeat {stale_age_seconds}s old"
    if runtime_status == "STARTING":
        return "Waiting for heartbeat"
    if last_sync_at:
        parsed = parse_utc(last_sync_at)
        if parsed is not None:
            age_seconds = int((datetime.now(timezone.utc) - parsed).total_seconds())
            return f"Heartbeat {age_seconds}s ago"
    if runtime_status == "PAUSED":
        return "Heartbeat paused"
    return "No heartbeat"


@server.get("/healthz")
def healthz():
    settings = database.get_settings(DB_PATH)
    app_state = database.get_app_state(DB_PATH)
    live_positions = database.fetch_live_source_positions(DB_PATH)
    portfolio = database.portfolio_totals(DB_PATH, live_positions)
    runtime_status, _, stale_age_seconds = engine_runtime_status(app_state, settings)
    return jsonify(
        {
            "status": "degraded" if runtime_status == "STALE" else ("ok" if app_state.get("engine_status") in {"RUNNING", "PAUSED"} else "degraded"),
            "engine_status": app_state.get("engine_status"),
            "runtime_status": runtime_status,
            "last_sync_at": app_state.get("last_sync_at"),
            "stale_age_seconds": stale_age_seconds,
            "last_error": app_state.get("last_error"),
            "net_value": portfolio["net_value"],
            "cash_balance": portfolio["cash_balance"],
        }
    ), 200


@server.get("/audit/profit")
def audit_profit():
    settings = database.get_settings(DB_PATH)
    app_state = database.get_app_state(DB_PATH)
    portfolio = database.portfolio_totals(DB_PATH)
    target = settings.get("target_wallet") or settings.get("target_handle") or app_state.get("resolved_target_wallet") or ""
    client = PolymarketClient()

    try:
        profile = client.resolve_target_wallet(target)
        live_positions = client.fetch_positions(profile["wallet"], limit=200)
        portfolio = database.portfolio_totals(DB_PATH, live_positions)
        verification = database.live_profit_verification(live_positions, DB_PATH)
        return jsonify(
            {
                "status": "ok" if verification["verified"] else "warning",
                "target_wallet": profile["wallet"],
                "target_handle": profile.get("handle") or settings.get("target_handle") or "",
                "server_net_value": verification["displayed_net_value"],
                "server_cash_balance": verification["displayed_cash_balance"],
                "server_marked_positions": verification["displayed_marked_positions"],
                "polymarket_verification": verification,
            }
        ), 200
    except Exception as exc:
        return jsonify(
            {
                "status": "error",
                "error": str(exc),
                "target_wallet": target,
                "server_net_value": portfolio["net_value"],
                "server_cash_balance": portfolio["cash_balance"],
                "server_marked_positions": portfolio["gross_exposure"],
            }
        ), 500


def fmt_currency(value) -> str:
    return f"${float(value):,.2f}"


def fmt_number(value, digits=2) -> str:
    return f"{float(value):,.{digits}f}"


def fmt_signed_currency(value) -> str:
    amount = float(value)
    return f"{'+' if amount > 0 else ''}${amount:,.2f}"


def signed_class(value) -> str:
    amount = float(value or 0)
    if amount > 0:
        return "text-positive"
    if amount < 0:
        return "text-negative"
    return "text-muted"


def short_text(value, limit: int) -> str:
    return (value or "")[:limit]


def polymarket_market_url(market_slug: str) -> str:
    slug = (market_slug or "").strip().strip("/")
    return f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"


def market_link(label: str, market_slug: str, limit: int | None = None):
    text = short_text(label, limit) if limit else (label or market_slug or "Market")
    return html.A(text, href=polymarket_market_url(market_slug), target="_blank", rel="noreferrer", className="market-link")


def status_class(status: str) -> str:
    return "status-running" if status == "RUNNING" else "status-stopped"


def topbar():
    return html.Div(
        className="topbar",
        children=[
            html.Div(
                className="topbar-left",
                children=[
                    html.Div(
                        className="topbar-brand",
                        children=[
                            html.Div("CopyPelosi Exchange", className="topbar-title"),
                            html.Div("Polymarket copy execution monitor", className="topbar-subtitle"),
                        ],
                    ),
                    html.Span(id="status-pill", className="status-running", children="RUNNING"),
                    html.Span(id="execution-pill", className="mode-pill mode-paper", children="PAPER MODE"),
                ],
            ),
            html.Div(
                className="topbar-controls",
                children=[
                    html.Button("Fresh Start", id="fresh-start-btn", className="btn-outline", n_clicks=0),
                    html.Button("Force Sync", id="force-sync-btn", className="btn-outline", n_clicks=0),
                    html.Button("Pause", id="toggle-engine-btn", className="btn-outline", n_clicks=0),
                    html.Button("Settings", id="open-settings-btn", className="btn-accent", n_clicks=0),
                    html.Div(id="refresh-text", className="topbar-meta"),
                    html.Div(id="clock", className="topbar-meta"),
                ],
            ),
        ],
    )


def market_strip():
    item = lambda label, value_id: html.Div(
        className="market-strip-item",
        children=[
            html.Div(label, className="market-strip-label"),
            html.Div(id=value_id, className="market-strip-value mono"),
        ],
    )
    return html.Div(
        className="market-strip",
        children=[
            html.Div(
                className="market-strip-primary",
                children=[
                    html.Div("Lead Account", className="market-strip-label"),
                    html.Div(id="market-lead", className="market-strip-headline"),
                    html.Div(id="market-lead-sub", className="market-strip-sub"),
                ],
            ),
            html.Div(
                className="market-strip-grid",
                children=[
                    item("Heartbeat", "market-heartbeat"),
                    item("Execution", "market-execution"),
                    item("Sync Cadence", "market-cadence"),
                    item("Exposure Cap", "market-exposure"),
                    item("Leader NAV", "market-leader-nav"),
                ],
            ),
        ],
    )


def stat_card(label: str, value_id: str, sub_id: str | None = None, tone_id: str | None = None):
    value_row = [html.Div(id=value_id, className="stat-value")]
    if tone_id:
        value_row.append(html.Div(id=tone_id, className="stat-chip"))
    children = [
        html.Div(label, className="stat-label"),
        html.Div(className="stat-value-row", children=value_row),
    ]
    if sub_id:
        children.append(html.Div(id=sub_id, className="stat-sub"))
    return html.Div(className="stat-card", children=children)


def section(title: str, body_id: str, badge_id: str | None = None, graph: bool = False):
    header_children = [html.Span(title, className="section-title")]
    if badge_id:
        header_children.append(html.Span(id=badge_id, className="badge"))
    body = dcc.Graph(id=body_id, config={"displayModeBar": False}, className="chart") if graph else html.Div(id=body_id)
    return html.Div(
        className="section-card",
        children=[html.Div(className="section-header", children=header_children), body],
    )


def trade_views():
    return html.Div(
        className="section-card",
        children=[
            html.Div(
                className="section-header",
                children=[
                    html.Span("Trade Book", className="section-title"),
                    html.Span(id="trade-book-badge", className="badge"),
                ],
            ),
            dcc.Tabs(
                id="trade-tabs",
                value="open-trades",
                className="trade-tabs",
                children=[
                    dcc.Tab(label="Open Positions", value="open-trades", className="trade-tab", selected_className="trade-tab-selected"),
                    dcc.Tab(label="Closed Trades", value="closed-trades", className="trade-tab", selected_className="trade-tab-selected"),
                ],
            ),
            html.Div(id="trade-book-table", className="trade-book-table"),
        ],
    )


def realized_performance_card():
    return html.Div(
        className="section-card",
        children=[
            html.Div(
                className="section-header",
                children=[
                    html.Span("Realized Performance", className="section-title"),
                    html.Span(id="realized-performance-badge", className="badge"),
                ],
            ),
            html.Div(
                id="realized-performance-panel",
                className="realized-performance-panel",
            ),
        ],
    )


def portfolio_breakdown_card():
    return html.Div(
        className="portfolio-breakdown-card",
        children=[
            html.Div(
                className="section-header",
                children=[
                    html.Span("Portfolio Breakdown", className="section-title"),
                    html.Span(id="portfolio-breakdown-badge", className="badge"),
                ],
            ),
            html.Div(
                className="portfolio-breakdown-summary",
                children=[
                    html.Div(
                        className="portfolio-breakdown-metric",
                        children=[
                            html.Div("Cash", className="portfolio-breakdown-label"),
                            html.Div(id="portfolio-cash-allocation", className="portfolio-breakdown-value"),
                        ],
                    ),
                    html.Div(
                        className="portfolio-breakdown-metric",
                        children=[
                            html.Div("Holdings", className="portfolio-breakdown-label"),
                            html.Div(id="portfolio-holdings-allocation", className="portfolio-breakdown-value"),
                        ],
                    ),
                    html.Div(
                        className="portfolio-breakdown-metric",
                        children=[
                            html.Div("Largest Position", className="portfolio-breakdown-label"),
                            html.Div(id="portfolio-top-holding", className="portfolio-breakdown-value"),
                        ],
                    ),
                ],
            ),
            html.Div(id="portfolio-breakdown-list", className="portfolio-breakdown-list"),
        ],
    )


def settings_modal():
    def text_field(label, field_id, placeholder, help_text=""):
        return html.Div(
            className="settings-field",
            children=[
                html.Div(label, className="settings-label"),
                dcc.Input(id=field_id, className="settings-input", placeholder=placeholder, debounce=False, type="text"),
                html.Div(help_text, className="settings-help") if help_text else None,
            ],
        )

    def number_field(label, field_id, placeholder, help_text="", min_value=None, step=None):
        return html.Div(
            className="settings-field",
            children=[
                html.Div(label, className="settings-label"),
                dcc.Input(
                    id=field_id,
                    className="settings-input",
                    placeholder=placeholder,
                    debounce=False,
                    type="number",
                    min=min_value,
                    step=step,
                ),
                html.Div(help_text, className="settings-help") if help_text else None,
            ],
        )

    def select_field(label, field_id, options, help_text=""):
        return html.Div(
            className="settings-field",
            children=[
                html.Div(label, className="settings-label"),
                dcc.Dropdown(id=field_id, className="settings-select", clearable=False, searchable=False, options=options),
                html.Div(help_text, className="settings-help") if help_text else None,
            ],
        )
    return html.Div(
        id="settings-modal",
        className="modal-overlay hidden",
        children=[
            html.Div(
                className="modal-box",
                children=[
                    html.Div(
                        className="modal-header",
                        children=[
                            html.Div("Copier Settings", className="modal-title"),
                            html.Button("Close", id="close-settings-btn", className="btn-ghost", n_clicks=0),
                        ],
                    ),
                    html.Div(
                        className="modal-body",
                        children=[
                            html.Div("Target", className="modal-section-label"),
                            html.Div(
                                className="modal-row modal-row-3",
                                children=[
                                    text_field("Target Handle", "settings-target-handle", "@GamblingIsAllYouNeed", "Polymarket username to mirror when wallet is not set."),
                                    text_field("Target Wallet (optional)", "settings-target-wallet", "0x...", "Direct wallet override for the account you want to follow."),
                                    text_field("Reference Wallet", "settings-leader-wallet", "0xBEbe...", "Wallet used to estimate leader account size for proportional copy sizing."),
                                ],
                            ),
                            html.Div("Execution", className="modal-section-label"),
                            html.Div(
                                className="modal-row modal-row-2",
                                children=[
                                    select_field(
                                        "Execution Mode",
                                        "settings-execution-mode",
                                        [
                                            {"label": "Paper only", "value": "paper"},
                                            {"label": "Shadow live estimate", "value": "shadow"},
                                        ],
                                        "Paper keeps current behavior. Shadow also logs estimated live fills without placing orders.",
                                    ),
                                    number_field("Shadow Extra Slippage Bps", "settings-shadow-extra-slippage-bps", "15", "Additional simulated slippage applied only to shadow live estimates.", min_value=0, step=1),
                                ],
                            ),
                            html.Div(
                                className="modal-row modal-row-2",
                                children=[
                                    number_field("Paper Starting Balance", "settings-start-balance", "5000", "Baseline used for total return and fresh-start resets.", min_value=0, step=0.01),
                                    number_field("Paper Cash Balance", "settings-cash-balance", "5000", "Current deployable cash in the paper portfolio.", min_value=0, step=0.01),
                                ],
                            ),
                            html.Div(
                                className="modal-row modal-row-2",
                                children=[
                                    number_field("Max Total Exposure USD", "settings-max-exposure", "2500", "Hard cap on marked paper exposure before dynamic step-ups.", min_value=0, step=0.01),
                                    number_field("Paper Slippage Bps", "settings-slippage-bps", "30", "Execution slippage applied to simulated paper fills.", min_value=0, step=1),
                                ],
                            ),
                            html.Div("Sync", className="modal-section-label"),
                            html.Div(
                                className="modal-row modal-row-3",
                                children=[
                                    number_field("Sync Interval ms", "settings-sync-interval", "1200", "Polling interval for target trades and position refreshes.", min_value=500, step=100),
                                    number_field("Trade Fetch Limit", "settings-trade-limit", "10", "Recent source trades inspected on each sync cycle. Max 10.", min_value=1, step=1),
                                    select_field(
                                        "Copy Sell Behavior",
                                        "settings-copy-sells",
                                        [
                                            {"label": "Copy sells", "value": 1},
                                            {"label": "Ignore sells", "value": 0},
                                        ],
                                        "Whether sell events from the followed account should reduce local positions.",
                                    ),
                                ],
                            ),
                        ],
                    ),
                    html.Div(
                        className="modal-footer",
                        children=[
                            html.Button("Save Settings", id="save-settings-btn", className="btn-accent", n_clicks=0),
                        ],
                    ),
                ],
            )
        ],
    )


def portfolio_curve_card():
    return html.Div(
        className="portfolio-card",
        children=[
            dcc.Store(id="portfolio-range-store", data="1D"),
            html.Div(
                className="portfolio-card-top",
                children=[
                    html.Div(
                        className="portfolio-card-copy",
                        children=[
                            html.Div("Profit/Loss", className="portfolio-chart-label"),
                            html.Div(id="portfolio-chart-amount", className="portfolio-chart-amount"),
                            html.Div(id="portfolio-chart-subtitle", className="portfolio-chart-subtitle"),
                        ],
                    ),
                    html.Div(
                        className="portfolio-range-group",
                        children=[
                            html.Button("1D", id="portfolio-range-1d", className="portfolio-range-btn", n_clicks=0),
                            html.Button("1W", id="portfolio-range-1w", className="portfolio-range-btn", n_clicks=0),
                            html.Button("1M", id="portfolio-range-1m", className="portfolio-range-btn", n_clicks=0),
                            html.Button("ALL", id="portfolio-range-all", className="portfolio-range-btn", n_clicks=0),
                        ],
                    ),
                ],
            ),
            dcc.Graph(id="portfolio-chart", config={"displayModeBar": False, "scrollZoom": False}, className="portfolio-graph"),
        ],
    )


app.layout = html.Div(
    [
        dcc.Interval(id="refresh-interval", interval=5000, n_intervals=0),
        dcc.Store(id="settings-modal-store", data={"open": False}),
        topbar(),
        html.Div(
            className="page",
            children=[
                market_strip(),
                html.Div(
                    className="stats-row",
                    children=[
                        stat_card("Execution Model", "stat-target", "stat-target-sub"),
                        stat_card("Pending Copies", "stat-pending", "stat-pending-sub"),
                        stat_card("Active Positions Value", "stat-copied-notional", "stat-copied-sub"),
                        stat_card("Net Liquidation Value", "stat-net-value", "stat-net-sub", "stat-net-chip"),
                    ],
                ),
                html.Div(
                    className="dashboard-grid",
                    children=[
                        html.Div(
                            className="dashboard-col",
                            children=[
                                portfolio_breakdown_card(),
                                realized_performance_card(),
                                portfolio_curve_card(),
                                section("Daily Performance", "daily-performance-table", "daily-performance-badge"),
                                section("Sync History", "sync-history-table", "sync-history-badge"),
                            ],
                        ),
                        html.Div(
                            className="dashboard-col",
                            children=[
                                section("Target Trade Feed", "copied-trades-table", "copied-trades-badge"),
                                section("Sell Match Audit", "sell-match-table", "sell-match-badge"),
                                trade_views(),
                                section("Live Analysis", "analysis-panel"),
                                section("Engine Log", "engine-log-table", "engine-log-badge"),
                            ],
                        ),
                    ],
                ),
            ],
        ),
        settings_modal(),
    ]
)


def render_table(headers, rows):
    return html.Table(
        [
            html.Thead(html.Tr([html.Th(col) for col in headers])),
            html.Tbody(
                [
                    html.Tr([html.Td(cell, className="cell-primary" if index == 0 else "") for index, cell in enumerate(row)])
                    for row in rows
                ]
            ),
        ]
    )


def portfolio_chart(range_key: str):
    snapshots = database.list_portfolio_snapshots(DB_PATH, limit=720)
    if not snapshots:
        snapshots = [{"ts": datetime.now(timezone.utc).isoformat(), "net_value": 0.0}]

    end_time = parse_utc(snapshots[-1]["ts"]) or datetime.now(timezone.utc)
    range_windows = {
        "1D": timedelta(days=1),
        "1W": timedelta(weeks=1),
        "1M": timedelta(days=30),
    }
    if range_key in range_windows:
        start_time = end_time - range_windows[range_key]
        filtered = [row for row in snapshots if (parse_utc(row["ts"]) or end_time) >= start_time]
        if filtered:
            snapshots = filtered

    baseline = float(snapshots[0]["net_value"] or 0.0)
    current_value = float(snapshots[-1]["net_value"] or 0.0)
    change_value = current_value - baseline
    positive = change_value >= 0
    line_color = "#1fa2ff" if positive else "#ff7d7d"
    fill_color = "rgba(31,162,255,0.18)" if positive else "rgba(255,125,125,0.14)"
    x_values = [to_pacific(row["ts"]).strftime("%b %d %I:%M %p") if to_pacific(row["ts"]) else row["ts"] for row in snapshots]
    y_values = [float(row["net_value"] or 0.0) for row in snapshots]

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=y_values,
            mode="lines",
            line={"color": line_color, "width": 4, "shape": "spline", "smoothing": 1.05},
            fill="tozeroy",
            fillcolor=fill_color,
            hovertemplate="%{x}<br>%{y:$,.2f}<extra></extra>",
            showlegend=False,
        )
    )
    figure.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin={"l": 0, "r": 0, "t": 12, "b": 0},
        font={"color": "#98a8bb", "family": "Manrope"},
        hovermode="x unified",
        hoverlabel={"bgcolor": "#0d1825", "bordercolor": "rgba(57,167,255,0.18)", "font": {"color": "#edf4fb"}},
        xaxis={
            "showgrid": False,
            "zeroline": False,
            "showline": False,
            "showticklabels": False,
            "fixedrange": True,
        },
        yaxis={
            "showgrid": False,
            "zeroline": False,
            "showline": False,
            "showticklabels": False,
            "fixedrange": True,
        },
    )
    return figure


def portfolio_range_meta(range_key: str) -> tuple[str, str]:
    labels = {
        "1D": "Past Day",
        "1W": "Past Week",
        "1M": "Past Month",
        "ALL": "All Time",
    }
    button_map = {
        "1D": "portfolio-range-btn portfolio-range-btn-active",
        "1W": "portfolio-range-btn portfolio-range-btn-active",
        "1M": "portfolio-range-btn portfolio-range-btn-active",
        "ALL": "portfolio-range-btn portfolio-range-btn-active",
    }
    return labels.get(range_key, "Past Day"), button_map.get(range_key, "portfolio-range-btn portfolio-range-btn-active")


@app.callback(
    Output("portfolio-range-store", "data"),
    Input("portfolio-range-1d", "n_clicks"),
    Input("portfolio-range-1w", "n_clicks"),
    Input("portfolio-range-1m", "n_clicks"),
    Input("portfolio-range-all", "n_clicks"),
    State("portfolio-range-store", "data"),
    prevent_initial_call=True,
)
def set_portfolio_range(_, __, ___, ____, current_range):
    triggered = callback_context.triggered_id
    mapping = {
        "portfolio-range-1d": "1D",
        "portfolio-range-1w": "1W",
        "portfolio-range-1m": "1M",
        "portfolio-range-all": "ALL",
    }
    return mapping.get(triggered, current_range)


@app.callback(
    Output("portfolio-chart", "figure"),
    Output("portfolio-chart-amount", "children"),
    Output("portfolio-chart-subtitle", "children"),
    Output("portfolio-range-1d", "className"),
    Output("portfolio-range-1w", "className"),
    Output("portfolio-range-1m", "className"),
    Output("portfolio-range-all", "className"),
    Input("refresh-interval", "n_intervals"),
    Input("portfolio-range-store", "data"),
)
def refresh_portfolio_chart(_, range_key):
    selected_range = range_key or "1D"
    snapshots = database.list_portfolio_snapshots(DB_PATH, limit=720)
    if not snapshots:
        amount_text = fmt_signed_currency(0.0)
    else:
        end_time = parse_utc(snapshots[-1]["ts"]) or datetime.now(timezone.utc)
        range_windows = {
            "1D": timedelta(days=1),
            "1W": timedelta(weeks=1),
            "1M": timedelta(days=30),
        }
        filtered = snapshots
        if selected_range in range_windows:
            start_time = end_time - range_windows[selected_range]
            scoped = [row for row in snapshots if (parse_utc(row["ts"]) or end_time) >= start_time]
            if scoped:
                filtered = scoped
        baseline = float(filtered[0]["net_value"] or 0.0)
        current_value = float(filtered[-1]["net_value"] or 0.0)
        amount_text = fmt_signed_currency(current_value - baseline)

    subtitle_map = {
        "1D": "Past Day",
        "1W": "Past Week",
        "1M": "Past Month",
        "ALL": "All Time",
    }
    button_classes = []
    for key in ("1D", "1W", "1M", "ALL"):
        button_classes.append("portfolio-range-btn portfolio-range-btn-active" if key == selected_range else "portfolio-range-btn")
    return (
        portfolio_chart(selected_range),
        amount_text,
        subtitle_map.get(selected_range, "Past Day"),
        *button_classes,
    )


@app.callback(
    Output("status-pill", "children"),
    Output("status-pill", "className"),
    Output("execution-pill", "children"),
    Output("execution-pill", "className"),
    Output("toggle-engine-btn", "children"),
    Output("refresh-text", "children"),
    Output("clock", "children"),
    Output("market-lead", "children"),
    Output("market-lead-sub", "children"),
    Output("market-heartbeat", "children"),
    Output("market-execution", "children"),
    Output("market-cadence", "children"),
    Output("market-exposure", "children"),
    Output("market-leader-nav", "children"),
    Output("stat-target", "children"),
    Output("stat-target-sub", "children"),
    Output("stat-pending", "children"),
    Output("stat-pending-sub", "children"),
    Output("stat-copied-notional", "children"),
    Output("stat-copied-sub", "children"),
    Output("stat-net-value", "children"),
    Output("stat-net-chip", "children"),
    Output("stat-net-chip", "className"),
    Output("stat-net-sub", "children"),
    Output("portfolio-breakdown-badge", "children"),
    Output("portfolio-cash-allocation", "children"),
    Output("portfolio-holdings-allocation", "children"),
    Output("portfolio-top-holding", "children"),
    Output("portfolio-breakdown-list", "children"),
    Output("realized-performance-badge", "children"),
    Output("realized-performance-panel", "children"),
    Output("daily-performance-badge", "children"),
    Output("daily-performance-table", "children"),
    Output("sync-history-badge", "children"),
    Output("sync-history-table", "children"),
    Output("copied-trades-badge", "children"),
    Output("copied-trades-table", "children"),
    Output("sell-match-badge", "children"),
    Output("sell-match-table", "children"),
    Output("trade-book-badge", "children"),
    Output("trade-book-table", "children"),
    Output("analysis-panel", "children"),
    Output("engine-log-badge", "children"),
    Output("engine-log-table", "children"),
    Input("refresh-interval", "n_intervals"),
    Input("trade-tabs", "value"),
)
def refresh_dashboard(_, trade_tab):
    settings = database.get_settings(DB_PATH)
    app_state = database.get_app_state(DB_PATH)
    runtime_status, runtime_class, stale_age_seconds = engine_runtime_status(app_state, settings)
    live_positions = database.fetch_live_source_positions(DB_PATH)
    source_positions = live_positions[:10]
    source_trades = database.list_source_trades(18, DB_PATH)
    copy_orders = database.list_copy_orders(12, DB_PATH)
    shadow_summary = database.shadow_order_summary(DB_PATH)
    sell_match_rows = database.list_sell_match_audit(12, DB_PATH)
    pending = database.list_pending_source_trades(DB_PATH)
    sync_runs = database.list_sync_runs(12, DB_PATH)
    logs = database.list_logs(14, DB_PATH)
    portfolio = database.portfolio_totals(DB_PATH, live_positions)
    analytics = database.trade_analytics(DB_PATH, live_positions)
    daily_performance = database.daily_portfolio_performance(DB_PATH)[:12]
    daily_realized_map = {row["date"]: row["realized_pnl"] for row in analytics["daily_realized"]}
    effective_exposure_cap = _effective_exposure_cap(settings, portfolio)

    active_positions_value = float(portfolio["gross_exposure"])
    copied_trade_rows = [
        [
            fmt_pacific_time(row["created_at"]),
            short_text(row["market_title"], 34),
            row["outcome"],
            row["side"],
            fmt_currency(row["amount_usd"]),
            fmt_number(row["price"], 3),
            row["copy_status"],
        ]
        for row in source_trades
    ] or [["No copied trades yet", "-", "-", "-", "-", "-", "-"]]
    sell_audit_rows = [
        [
            fmt_pacific_time(row["created_at"]),
            short_text(row["market_title"], 22),
            row["outcome"],
            row["match_strategy"],
            short_text(row["position_key"], 30) or "-",
            short_text(row["source_trade_id"], 26) or "-",
        ]
        for row in sell_match_rows
    ] or [["No sell matches yet", "-", "-", "-", "-", "-"]]
    daily_rows = [
        [
            row["date"],
            fmt_signed_currency(row["day_change"]),
            fmt_signed_currency(daily_realized_map.get(row["date"], 0.0)),
            fmt_currency(row["net_value"]),
        ]
        for row in daily_performance
    ] or [["No daily performance yet", "-", "-", "-"]]

    open_trade_rows = [
        [
            fmt_pacific_time(row["entry_time"]),
            market_link(row["market_title"], row["market_slug"], 30),
            row["outcome"],
            fmt_number(row["shares"], 2),
            fmt_number(row["entry_price"], 3),
            fmt_currency(row["cost_basis"]),
            fmt_number(row["current_price"], 3),
            fmt_currency(row["market_value"]),
            fmt_signed_currency(row["unrealized_pnl"]),
        ]
        for row in analytics["open_trades"][:40]
    ] or [["No open trades", "-", "-", "-", "-", "-", "-", "-", "-"]]
    closed_trade_rows = [
        [
            fmt_pacific_time(row["entry_time"]),
            fmt_pacific_time(row["exit_time"]),
            market_link(row["market_title"], row["market_slug"], 26),
            row["outcome"],
            fmt_number(row["shares"], 2),
            fmt_number(row["entry_price"], 3),
            fmt_number(row["exit_price"], 3),
            fmt_currency(row["cost_basis"]),
            fmt_currency(row["proceeds"]),
            fmt_signed_currency(row["pnl"]),
        ]
        for row in analytics["closed_trades"][:40]
    ] or [["No closed trades", "-", "-", "-", "-", "-", "-", "-", "-", "-"]]
    trade_book_table = (
        render_table(["Bought At (PT)", "Market", "Outcome", "Shares", "Buy Px", "Cost", "Mark Px", "Value", "P/L"], open_trade_rows)
        if trade_tab == "open-trades"
        else render_table(["Bought At (PT)", "Sold At (PT)", "Market", "Outcome", "Shares", "Buy Px", "Sell Px", "Cost", "Proceeds", "P/L"], closed_trade_rows)
    )
    trade_book_count = len(analytics["open_trades"]) if trade_tab == "open-trades" else len(analytics["closed_trades"])
    net_value = max(float(portfolio["net_value"]), 0.01)
    cash_pct = round((float(portfolio["cash_balance"]) / net_value) * 100, 1) if net_value else 0.0
    holdings_pct = round((float(portfolio["gross_exposure"]) / net_value) * 100, 1) if net_value else 0.0
    total_realized_pnl = round(sum(float(row.get("pnl") or 0.0) for row in analytics["closed_trades"]), 2)
    total_closed_cost = round(sum(float(row.get("cost_basis") or 0.0) for row in analytics["closed_trades"]), 2)
    win_count = sum(1 for row in analytics["closed_trades"] if float(row.get("pnl") or 0.0) > 0)
    loss_count = sum(1 for row in analytics["closed_trades"] if float(row.get("pnl") or 0.0) < 0)
    closed_trade_count = len(analytics["closed_trades"])
    realized_win_rate = round((win_count / closed_trade_count) * 100, 1) if closed_trade_count else 0.0
    realized_return_pct = round((total_realized_pnl / total_closed_cost) * 100, 2) if total_closed_cost else 0.0
    all_open_positions = analytics["open_positions"]
    open_positions = all_open_positions[:8]
    top_holding = analytics["open_positions"][0] if analytics["open_positions"] else None
    holdings_list = []
    holdings_list.append(
        html.Div(
            className="portfolio-holding-row portfolio-holding-row-cash",
            children=[
                html.Div(
                    className="portfolio-holding-main",
                    children=[
                        html.Div("Cash", className="portfolio-holding-title"),
                        html.Div("Available buying power", className="portfolio-holding-subtitle"),
                    ],
                ),
                html.Div(
                    className="portfolio-holding-stats",
                    children=[
                        html.Div(f"{cash_pct:.1f}%", className="portfolio-holding-weight"),
                        html.Div(fmt_currency(portfolio["cash_balance"]), className="portfolio-holding-amount"),
                    ],
                ),
            ],
        )
    )
    shown_holdings_value = 0.0
    for row in open_positions:
        shown_holdings_value += float(row["market_value"])
        allocation_pct = round((float(row["market_value"]) / net_value) * 100, 1) if net_value else 0.0
        holdings_list.append(
            html.Div(
                className="portfolio-holding-row",
                children=[
                    html.Div(
                        className="portfolio-holding-main",
                        children=[
                            market_link(row["market_title"], row["market_slug"], 42),
                            html.Div(
                                f"{row['outcome']} | {fmt_number(row['shares'], 2)} shares @ {fmt_number(row['current_price'], 3)}",
                                className="portfolio-holding-subtitle",
                            ),
                        ],
                    ),
                    html.Div(
                        className="portfolio-holding-stats",
                        children=[
                            html.Div(f"{allocation_pct:.1f}%", className="portfolio-holding-weight"),
                            html.Div(fmt_currency(row["market_value"]), className="portfolio-holding-amount"),
                            html.Div(fmt_signed_currency(row["unrealized_pnl"]), className=f"portfolio-holding-pnl {signed_class(row['unrealized_pnl'])}"),
                        ],
                    ),
                ],
            )
        )
    hidden_holdings = max(len(all_open_positions) - len(open_positions), 0)
    other_holdings_value = round(max(float(portfolio["gross_exposure"]) - shown_holdings_value, 0.0), 2)
    other_holdings_pct = round((other_holdings_value / net_value) * 100, 1) if net_value else 0.0
    if hidden_holdings and other_holdings_value > 0:
        holdings_list.append(
            html.Div(
                className="portfolio-holding-row portfolio-holding-row-other",
                children=[
                    html.Div(
                        className="portfolio-holding-main",
                        children=[
                            html.Div("Other Holdings", className="portfolio-holding-title"),
                            html.Div(f"{hidden_holdings} smaller positions not shown individually", className="portfolio-holding-subtitle"),
                        ],
                    ),
                    html.Div(
                        className="portfolio-holding-stats",
                        children=[
                            html.Div(f"{other_holdings_pct:.1f}%", className="portfolio-holding-weight"),
                            html.Div(fmt_currency(other_holdings_value), className="portfolio-holding-amount"),
                        ],
                    ),
                ],
            )
        )
    if not analytics["open_positions"]:
        holdings_list.append(html.Div("No active holdings", className="portfolio-empty"))

    realized_groups = {}
    for row in analytics["closed_trades"]:
        position_key = row["position_key"]
        aggregate = realized_groups.setdefault(
            position_key,
            {
                "market_slug": row["market_slug"],
                "market_title": row["market_title"],
                "outcome": row["outcome"],
                "entry_time": row["entry_time"],
                "exit_time": row["exit_time"],
                "shares": 0.0,
                "cost_basis": 0.0,
                "proceeds": 0.0,
                "pnl": 0.0,
                "trades": 0,
            },
        )
        aggregate["entry_time"] = min(aggregate["entry_time"], row["entry_time"]) if aggregate["entry_time"] else row["entry_time"]
        aggregate["exit_time"] = max(aggregate["exit_time"], row["exit_time"]) if aggregate["exit_time"] else row["exit_time"]
        aggregate["shares"] = round(float(aggregate["shares"]) + float(row["shares"]), 6)
        aggregate["cost_basis"] = round(float(aggregate["cost_basis"]) + float(row["cost_basis"]), 2)
        aggregate["proceeds"] = round(float(aggregate["proceeds"]) + float(row["proceeds"]), 2)
        aggregate["pnl"] = round(float(aggregate["pnl"]) + float(row["pnl"]), 2)
        aggregate["trades"] += 1
    realized_summary_rows = sorted(realized_groups.values(), key=lambda row: row["exit_time"], reverse=True)
    best_closed = max(realized_summary_rows, key=lambda row: float(row["pnl"]), default=None)
    worst_closed = min(realized_summary_rows, key=lambda row: float(row["pnl"]), default=None)
    realized_summary_table_rows = [
        [
            fmt_pacific_time(row["exit_time"]),
            market_link(row["market_title"], row["market_slug"], 28),
            row["outcome"],
            str(row["trades"]),
            fmt_currency(row["cost_basis"]),
            fmt_currency(row["proceeds"]),
            fmt_signed_currency(row["pnl"]),
            f"{((float(row['pnl']) / float(row['cost_basis'])) * 100):+.1f}%" if float(row["cost_basis"]) else "-",
        ]
        for row in realized_summary_rows[:12]
    ] or [["No closed positions yet", "-", "-", "-", "-", "-", "-", "-"]]
    realized_performance_panel = html.Div(
        className="realized-performance-panel",
        children=[
            html.Div(
                className="realized-performance-summary",
                children=[
                    html.Div(
                        className="realized-metric",
                        children=[
                            html.Div("Realized P/L", className="realized-metric-label"),
                            html.Div(fmt_signed_currency(total_realized_pnl), className=f"realized-metric-value {signed_class(total_realized_pnl)}"),
                            html.Div(f"{realized_return_pct:+.2f}% on {fmt_currency(total_closed_cost)} closed cost", className="realized-metric-sub"),
                        ],
                    ),
                    html.Div(
                        className="realized-metric",
                        children=[
                            html.Div("Win Rate", className="realized-metric-label"),
                            html.Div(f"{realized_win_rate:.1f}%", className="realized-metric-value"),
                            html.Div(f"{win_count} wins | {loss_count} losses", className="realized-metric-sub"),
                        ],
                    ),
                    html.Div(
                        className="realized-metric",
                        children=[
                            html.Div("Best Closed Position", className="realized-metric-label"),
                            html.Div(fmt_signed_currency(best_closed["pnl"]) if best_closed else "-", className=f"realized-metric-value {signed_class(best_closed['pnl']) if best_closed else 'text-muted'}"),
                            html.Div(short_text(best_closed["market_title"], 28) if best_closed else "No closed positions yet", className="realized-metric-sub"),
                        ],
                    ),
                    html.Div(
                        className="realized-metric",
                        children=[
                            html.Div("Worst Closed Position", className="realized-metric-label"),
                            html.Div(fmt_signed_currency(worst_closed["pnl"]) if worst_closed else "-", className=f"realized-metric-value {signed_class(worst_closed['pnl']) if worst_closed else 'text-muted'}"),
                            html.Div(short_text(worst_closed["market_title"], 28) if worst_closed else "No closed positions yet", className="realized-metric-sub"),
                        ],
                    ),
                ],
            ),
            html.Div(
                className="realized-performance-table",
                children=render_table(
                    ["Closed At (PT)", "Market", "Outcome", "Fills", "Cost", "Proceeds", "Realized P/L", "Return"],
                    realized_summary_table_rows,
                ),
            ),
        ],
    )
    sync_rows = [
        [
            fmt_pacific_time(row["started_at"]),
            row["status"],
            str(row["trades_seen"]),
            str(row["new_trades"]),
            str(row["copied"]),
            f"{row['latency_ms']}ms",
        ]
        for row in sync_runs
    ] or [["-", "-", "-", "-", "-", "-"]]
    log_rows = [
        [
            fmt_pacific_time(row["ts"]),
            row["level"],
            row["component"],
            row["message"][:48],
        ]
        for row in logs
    ] or [["-", "-", "-", "-"]]

    target_label = f"@{settings['target_handle']}" if settings["target_handle"] else "Wallet target"
    target_wallet = app_state.get("resolved_target_wallet") or settings["target_wallet"] or "Not resolved yet"
    execution_mode = (settings.get("execution_mode") or "paper").strip().lower()
    shadow_mode_active = execution_mode == "shadow"
    execution_pill_text = "SHADOW MODE" if shadow_mode_active else "PAPER MODE"
    execution_pill_class = "mode-pill mode-shadow" if shadow_mode_active else "mode-pill mode-paper"
    market_execution = (
        f"Shadow +{int(settings.get('shadow_extra_slippage_bps') or 0)}bps"
        if shadow_mode_active
        else "Paper simulation"
    )
    execution_card_value = "Shadow On" if shadow_mode_active else "Paper Only"
    execution_card_sub = (
        f"Parallel live-like portfolio vs paper | {target_label}"
        if shadow_mode_active
        else f"No shadow portfolio active | {target_label}"
    )
    shadow_has_history = shadow_mode_active and shadow_summary["total"] > 0
    analysis = html.Div(
        className="analysis-stack",
        children=[
            html.Div(
                className="analysis-block",
                children=[
                    html.Div("Sync Status", className="analysis-label"),
                    html.Div(
                        app_state["last_sync_message"]
                        if runtime_status != "STALE"
                        else f"No sync heartbeat for {stale_age_seconds}s while engine is marked RUNNING.",
                        className="analysis-text",
                    ),
                ],
            ),
            html.Div(
                className="analysis-block",
                children=[
                    html.Div("Target Wallet", className="analysis-label"),
                    html.Div(target_wallet, className="analysis-text mono"),
                ],
            ),
            html.Div(
                className="analysis-block",
                children=[
                    html.Div("Execution Mode", className="analysis-label"),
                    html.Div(
                        (
                            f"Shadow mode: paper remains authoritative while estimated live fills are logged with +{int(settings.get('shadow_extra_slippage_bps') or 0)}bps extra slippage."
                            if shadow_mode_active
                            else "Paper mode only."
                        ),
                        className="analysis-text",
                    ),
                ],
            ),
            html.Div(
                className="analysis-block",
                children=[
                    html.Div("Fast Copy Model", className="analysis-label"),
                    html.Div(
                        f"Polling every {settings['sync_interval_ms']}ms with {settings['trade_fetch_limit']} trade lookback. "
                        "Paper execution sizes buys from available cash, caps each new bet at 20% of current cash, "
                        "and refreshes dashboard marks independently every 5 seconds.",
                        className="analysis-text",
                    ),
                ],
            ),
            html.Div(
                className="analysis-block",
                children=[
                    html.Div("Shadow Drift", className="analysis-label"),
                    html.Div(
                        (
                            f"{shadow_summary['total']} previews logged | avg abs delta {shadow_summary['avg_abs_price_delta_bps']:.2f}bps | max abs delta {shadow_summary['max_abs_price_delta_bps']:.2f}bps"
                            if shadow_mode_active and shadow_summary["total"]
                            else ("No shadow previews logged yet." if shadow_mode_active else "Shadow mode disabled.")
                        ),
                        className="analysis-text mono",
                    ),
                ],
            ),
            html.Div(
                className="analysis-block",
                children=[
                    html.Div("Leader Wallet Value", className="analysis-label"),
                    html.Div(
                        fmt_currency(float(app_state.get("leader_wallet_value") or 0.0)),
                        className="analysis-text mono",
                    ),
                ],
            ),
            html.Div(
                className="analysis-block",
                children=[
                    html.Div("Copy Start Time", className="analysis-label"),
                    html.Div(fmt_pacific_time(app_state.get("copy_start_at", "")) if app_state.get("copy_start_at") else "Immediate", className="analysis-text mono"),
                ],
            ),
            html.Div(
                className="analysis-block",
                children=[
                    html.Div("Last Error", className="analysis-label"),
                    html.Div(app_state["last_error"] or "None", className="analysis-text"),
                ],
            ),
        ],
    )

    refresh_text = f"Last sync: {fmt_pacific_time(app_state['last_sync_at']) if app_state['last_sync_at'] else 'never'}"
    net_gain = float(portfolio["total_gain"])
    net_chip_text = f"{portfolio['total_gain_pct']:+.2f}%"

    return (
        heartbeat_label(app_state, runtime_status, stale_age_seconds),
        runtime_class,
        execution_pill_text,
        execution_pill_class,
        "Pause" if app_state["engine_status"] == "RUNNING" else "Resume",
        refresh_text,
        fmt_pacific_clock(),
        target_label,
        target_wallet,
        heartbeat_label(app_state, runtime_status, stale_age_seconds),
        market_execution,
        f"{settings['sync_interval_ms']} ms",
        fmt_currency(effective_exposure_cap),
        fmt_currency(float(app_state.get("leader_wallet_value") or 0.0)),
        execution_card_value,
        execution_card_sub,
        str(len(pending)),
        f"{len(copy_orders)} total copied orders",
        fmt_currency(active_positions_value),
        f"{portfolio['positions_count']} active positions | updated every 5s",
        fmt_currency(portfolio["net_value"]),
        net_chip_text,
        f"stat-chip {signed_class(net_gain)}",
        f"Cash {fmt_currency(portfolio['cash_balance'])} + Marked Positions {fmt_currency(portfolio['gross_exposure'])} | {fmt_signed_currency(portfolio['total_gain'])} ({portfolio['total_gain_pct']:+.2f}%) vs start",
        str(portfolio["positions_count"]),
        f"{cash_pct:.1f}% | {fmt_currency(portfolio['cash_balance'])}",
        f"{holdings_pct:.1f}% | {fmt_currency(portfolio['gross_exposure'])}",
        f"{short_text(top_holding['market_title'], 24)} | {fmt_currency(top_holding['market_value'])}" if top_holding else "No holdings yet",
        holdings_list,
        str(len(realized_summary_rows)),
        realized_performance_panel,
        str(len(daily_performance)),
        render_table(["Date", "Day Change", "Closed P/L", "Portfolio"], daily_rows),
        str(len(sync_runs)),
        render_table(["Time (PT)", "Status", "Seen", "New", "Copied", "Latency"], sync_rows),
        str(len(source_trades)),
        render_table(["Trade Time (PT)", "Market", "Outcome", "Side", "USD", "Px", "Copy Status"], copied_trade_rows),
        str(len(sell_match_rows)),
        render_table(["Sold At (PT)", "Market", "Outcome", "Match", "Local Position", "Source Trade"], sell_audit_rows),
        str(trade_book_count),
        trade_book_table,
        analysis,
        str(len(logs)),
        render_table(["Time (PT)", "Level", "Comp", "Message"], log_rows),
    )


@app.callback(
    Output("settings-modal-store", "data"),
    Input("open-settings-btn", "n_clicks"),
    Input("close-settings-btn", "n_clicks"),
    State("settings-modal-store", "data"),
    prevent_initial_call=True,
)
def toggle_modal(open_clicks, close_clicks, store):
    triggered = callback_context.triggered_id
    if triggered == "open-settings-btn":
        return {"open": True}
    if triggered == "close-settings-btn":
        return {"open": False}
    return store


@app.callback(
    Output("settings-modal", "className"),
    Output("settings-target-handle", "value"),
    Output("settings-target-wallet", "value"),
    Output("settings-leader-wallet", "value"),
    Output("settings-execution-mode", "value"),
    Output("settings-shadow-extra-slippage-bps", "value"),
    Output("settings-max-exposure", "value"),
    Output("settings-start-balance", "value"),
    Output("settings-cash-balance", "value"),
    Output("settings-slippage-bps", "value"),
    Output("settings-sync-interval", "value"),
    Output("settings-trade-limit", "value"),
    Output("settings-copy-sells", "value"),
    Input("settings-modal-store", "data"),
    Input("refresh-interval", "n_intervals"),
)
def sync_modal(store, _):
    settings = database.get_settings(DB_PATH)
    if store["open"] and callback_context.triggered_id == "refresh-interval":
        return (
            "modal-overlay",
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
        )
    return (
        "modal-overlay" if store["open"] else "modal-overlay hidden",
        settings["target_handle"],
        settings["target_wallet"],
        settings["leader_wallet_address"],
        settings["execution_mode"],
        settings["shadow_extra_slippage_bps"],
        settings["max_total_exposure_usd"],
        settings["paper_starting_balance"],
        settings["paper_cash_balance"],
        settings["slippage_bps"],
        settings["sync_interval_ms"],
        settings["trade_fetch_limit"],
        settings["copy_sells"],
    )


@app.callback(
    Output("settings-modal-store", "data", allow_duplicate=True),
    Input("save-settings-btn", "n_clicks"),
    State("settings-target-handle", "value"),
    State("settings-target-wallet", "value"),
    State("settings-leader-wallet", "value"),
    State("settings-execution-mode", "value"),
    State("settings-shadow-extra-slippage-bps", "value"),
    State("settings-max-exposure", "value"),
    State("settings-start-balance", "value"),
    State("settings-cash-balance", "value"),
    State("settings-slippage-bps", "value"),
    State("settings-sync-interval", "value"),
    State("settings-trade-limit", "value"),
    State("settings-copy-sells", "value"),
    prevent_initial_call=True,
)
def save_settings(_, target_handle, target_wallet, leader_wallet, execution_mode, shadow_extra_slippage_bps, max_exposure, start_balance, cash_balance, slippage_bps, sync_interval, trade_limit, copy_sells):
    normalized_mode = (execution_mode or "paper").strip().lower()
    normalized_mode = "shadow" if normalized_mode == "shadow" else "paper"
    updates = {
        "target_handle": (target_handle or "").strip().lstrip("@"),
        "target_wallet": (target_wallet or "").strip(),
        "leader_wallet_address": (leader_wallet or "").strip(),
        "execution_mode": normalized_mode,
        "shadow_extra_slippage_bps": int(float(shadow_extra_slippage_bps or 0)),
        "max_total_exposure_usd": float(max_exposure or 0),
        "paper_starting_balance": float(start_balance or 0),
        "paper_cash_balance": float(cash_balance or 0),
        "slippage_bps": int(float(slippage_bps or 0)),
        "sync_interval_ms": int(float(sync_interval or 1200)),
        "trade_fetch_limit": min(10, max(1, int(float(trade_limit or 10)))),
        "copy_sells": int(float(copy_sells or 0)),
    }
    database.update_settings(updates, DB_PATH)
    database.log("INFO", "settings", "Settings updated from dashboard.", updates, DB_PATH)
    return {"open": False}


@app.callback(
    Output("toggle-engine-btn", "n_clicks"),
    Input("toggle-engine-btn", "n_clicks"),
    prevent_initial_call=True,
)
def toggle_engine(_):
    current = database.get_app_state(DB_PATH)["engine_status"]
    next_status = "RUNNING" if current != "RUNNING" else "PAUSED"
    database.set_app_state("engine_status", next_status, DB_PATH)
    database.log("INFO", "engine", f"Engine set to {next_status}.", db_path=DB_PATH)
    return 0


@app.callback(
    Output("force-sync-btn", "n_clicks"),
    Input("force-sync-btn", "n_clicks"),
    prevent_initial_call=True,
)
def force_sync(_):
    CopyTradingEngine(DB_PATH).tick(force=True)
    return 0


@app.callback(
    Output("fresh-start-btn", "n_clicks"),
    Input("fresh-start-btn", "n_clicks"),
    prevent_initial_call=True,
)
def fresh_start(_):
    settings = database.get_settings(DB_PATH)
    copy_start_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    database.reset_runtime_state(
        starting_balance=float(settings["paper_starting_balance"]),
        copy_start_at=copy_start_at,
        leader_wallet_address=(settings.get("leader_wallet_address") or "").strip(),
        db_path=DB_PATH,
    )
    # A fresh start should only copy trades that happen after this timestamp.
    database.set_app_state("bootstrap_positions_done_at", copy_start_at, DB_PATH)
    database.log("INFO", "reset", "Fresh start requested from dashboard.", {"copy_start_at": copy_start_at}, DB_PATH)
    CopyTradingEngine(DB_PATH).tick(force=True)
    return 0


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8060"))
    debug = os.environ.get("DEBUG", "").lower() in {"1", "true", "yes"}
    app.run(debug=debug, host=host, port=port)
