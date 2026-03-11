from __future__ import annotations

from datetime import datetime, timezone
import os
from zoneinfo import ZoneInfo

import dash
import plotly.graph_objects as go
from flask import jsonify
from dash import Input, Output, State, callback_context, dcc, html

from copytrader import database
from copytrader.config import DB_PATH
from copytrader.engine import CopyTradingEngine


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
    portfolio = database.portfolio_totals(DB_PATH)
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
                            html.Div("CopyPelosi", className="topbar-title"),
                            html.Div("Polymarket copy monitor", className="topbar-subtitle"),
                        ],
                    ),
                    html.Span(id="status-pill", className="status-running", children="RUNNING"),
                    html.Span(id="sync-latency-chip", className="latency-chip", children="—"),
                ],
            ),
            html.Div(
                className="topbar-controls",
                children=[
                    html.Button("Force Sync", id="force-sync-btn", className="btn-outline", n_clicks=0),
                    html.Button("Pause", id="toggle-engine-btn", className="btn-outline", n_clicks=0),
                ],
            ),
        ],
    )


def stats_strip():
    def cell(label, value_id, extra_class=""):
        return html.Div(
            className="stats-strip-cell",
            children=[
                html.Div(label, className="stats-strip-label"),
                html.Div(id=value_id, className=f"stats-strip-value {extra_class}".strip()),
            ],
        )
    return html.Div(
        className="stats-strip",
        children=[
            cell("Portfolio NAV", "stat-nav"),
            cell("Total Invested", "stat-invested"),
            cell("Unrealized P/L", "stat-unrealized"),
            cell("Fill Rate", "stat-fill-rate"),
            cell("Open Positions", "stat-open-count"),
        ],
    )


def overview_metric(label: str, value_id: str, sub_id: str | None = None, tone_id: str | None = None):
    children = [
        html.Div(label, className="overview-metric-label"),
        html.Div(id=value_id, className="overview-metric-value"),
    ]
    if tone_id:
        children.append(html.Div(id=tone_id, className="stat-chip"))
    if sub_id:
        children.append(html.Div(id=sub_id, className="overview-metric-sub"))
    return html.Div(
        className="overview-metric-card",
        children=children,
    )


def overview_shell():
    kv = lambda label, value_id: html.Div(
        className="signal-item",
        children=[
            html.Div(label, className="signal-label"),
            html.Div(id=value_id, className="signal-value"),
        ],
    )
    return html.Div(
        className="overview-grid",
        children=[
            html.Div(
                className="hero-panel",
                children=[
                    html.Div(
                        className="hero-header",
                        children=[
                            html.Div(
                                children=[
                                    html.Div("Portfolio NAV", className="hero-eyebrow"),
                                    html.Div(id="hero-nav", className="hero-value"),
                                ]
                            ),
                            html.Div(id="hero-return-chip", className="hero-return-chip"),
                        ],
                    ),
                    html.Div(id="hero-subtitle", className="hero-subtitle"),
                    html.Div(
                        className="hero-metrics",
                        children=[
                            overview_metric("Cash Available", "hero-cash", "hero-cash-sub"),
                            overview_metric("Gross Exposure", "hero-exposure", "hero-exposure-sub", "hero-exposure-chip"),
                            overview_metric("Copy Fill Rate", "hero-fill-rate", "hero-fill-sub"),
                            overview_metric("Open Positions", "hero-open-positions", "hero-open-sub"),
                        ],
                    ),
                ],
            ),
            html.Div(
                className="signal-panel",
                children=[
                    html.Div(
                        className="signal-header",
                        children=[
                            html.Div("Execution Status", className="section-title"),
                            html.Div(id="signal-runtime", className="badge"),
                        ],
                    ),
                    html.Div(id="signal-summary", className="signal-summary"),
                    html.Div(
                        className="signal-grid",
                        children=[
                            kv("Lead Trader", "signal-trader"),
                            kv("Target Wallet", "signal-wallet"),
                            kv("Heartbeat", "signal-heartbeat"),
                            kv("Sync Cadence", "signal-cadence"),
                            kv("Leader NAV", "signal-leader-nav"),
                            kv("Pending Queue", "signal-pending"),
                        ],
                    ),
                ],
            ),
        ],
    )


def pulse_card(title: str, value_id: str, sub_id: str | None = None, tone_id: str | None = None):
    children = [html.Div(title, className="pulse-label"), html.Div(id=value_id, className="pulse-value")]
    if tone_id:
        children.append(html.Div(id=tone_id, className="stat-chip"))
    if sub_id:
        children.append(html.Div(id=sub_id, className="pulse-sub"))
    return html.Div(className="pulse-card", children=children)


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
                    dcc.Tab(label="Open Trades", value="open-trades", className="trade-tab", selected_className="trade-tab-selected"),
                    dcc.Tab(label="Closed Trades", value="closed-trades", className="trade-tab", selected_className="trade-tab-selected"),
                ],
            ),
            html.Div(id="trade-book-table", className="trade-book-table"),
        ],
    )


def live_tab():
    def section_header(title_id, meta_id):
        return html.Div(
            className="live-section-header",
            children=[
                html.Div(id=title_id, className="live-section-title"),
                html.Div(id=meta_id, className="live-section-meta"),
            ],
        )

    return html.Div(
        className="tab-content",
        children=[
            html.Div(
                className="live-grid",
                children=[
                    html.Div(
                        className="live-col",
                        children=[
                            section_header("live-leader-title", "live-leader-meta"),
                            html.Div(className="live-table-wrap", children=[html.Div(id="live-source-positions-table")]),
                            section_header("live-trades-title", "live-trades-meta"),
                            html.Div(className="live-table-wrap", children=[html.Div(id="live-source-trades-table")]),
                        ],
                    ),
                    html.Div(
                        className="live-col",
                        children=[
                            section_header("live-copies-title", "live-copies-meta"),
                            html.Div(className="live-table-wrap", children=[html.Div(id="live-open-positions-table")]),
                            section_header("live-orders-title", "live-orders-meta"),
                            html.Div(className="live-table-wrap", children=[html.Div(id="live-copy-orders-table")]),
                        ],
                    ),
                ],
            ),
        ],
    )


def portfolio_tab():
    return html.Div(
        className="tab-content",
        children=[
            html.Div(
                className="section-card",
                children=[
                    html.Div(className="section-header", children=[html.Span("Paper Portfolio Curve", className="section-title")]),
                    dcc.Graph(id="portfolio-chart", config={"displayModeBar": False}, className="chart"),
                ],
            ),
            html.Div(
                className="section-card",
                children=[
                    html.Div(
                        className="section-header",
                        children=[
                            html.Span("Daily Performance", className="section-title"),
                            html.Span(id="daily-performance-badge", className="badge"),
                        ],
                    ),
                    html.Div(id="daily-performance-table"),
                ],
            ),
        ],
    )


def history_tab():
    return html.Div(
        className="tab-content",
        children=[
            html.Div(
                className="section-card",
                children=[
                    html.Div(
                        className="section-header",
                        children=[
                            html.Span("Closed Trades", className="section-title"),
                            html.Span(id="closed-trades-badge", className="badge"),
                        ],
                    ),
                    html.Div(id="closed-trades-table"),
                ],
            ),
            html.Div(
                className="section-card",
                children=[
                    html.Div(
                        className="section-header",
                        children=[
                            html.Span("Sync History", className="section-title"),
                            html.Span(id="sync-history-badge", className="badge"),
                        ],
                    ),
                    html.Div(id="sync-history-table"),
                ],
            ),
        ],
    )


def settings_tab_content():
    field = lambda label, fid, placeholder: html.Div(
        className="settings-field",
        children=[
            html.Div(label, className="settings-label"),
            dcc.Input(id=fid, className="settings-input", placeholder=placeholder, debounce=False),
        ],
    )
    return html.Div(
        className="tab-content",
        children=[
            html.Div(
                className="settings-inline",
                children=[
                    html.Div("Target", className="settings-section-label"),
                    html.Div(
                        className="settings-grid-3",
                        children=[
                            field("Target Handle", "settings-target-handle", "@GamblingIsAllYouNeed"),
                            field("Target Wallet (optional)", "settings-target-wallet", "0x..."),
                            field("Reference Wallet", "settings-leader-wallet", "0xBEbe..."),
                        ],
                    ),
                    html.Div("Capital", className="settings-section-label"),
                    html.Div(
                        className="settings-grid-2",
                        children=[
                            field("Paper Starting Balance", "settings-start-balance", "100"),
                            field("Paper Cash Balance", "settings-cash-balance", "100"),
                        ],
                    ),
                    html.Div(
                        className="add-money-row",
                        children=[
                            html.Div(
                                className="settings-field",
                                style={"flex": 1},
                                children=[
                                    html.Div("Add Paper Money", className="settings-label"),
                                    dcc.Input(
                                        id="add-paper-money-input",
                                        className="settings-input",
                                        placeholder="50.00",
                                        type="number",
                                        min=0,
                                        debounce=False,
                                    ),
                                ],
                            ),
                            html.Button("+ Add", id="add-paper-money-btn", className="btn-accent", n_clicks=0),
                        ],
                    ),
                    html.Div(id="add-money-status", className="add-money-status"),
                    html.Div("Execution", className="settings-section-label"),
                    html.Div(
                        className="settings-grid-2",
                        children=[
                            field("Max Total Exposure USD", "settings-max-exposure", "100"),
                            field("Slippage Bps", "settings-slippage-bps", "30"),
                        ],
                    ),
                    html.Div(
                        className="settings-grid-3",
                        children=[
                            field("Sync Interval ms", "settings-sync-interval", "1200"),
                            field("Trade Fetch Limit", "settings-trade-limit", "100"),
                            field("Copy Sells (1/0)", "settings-copy-sells", "1"),
                        ],
                    ),
                    html.Div(
                        className="settings-save-row",
                        children=[html.Button("Save Settings", id="save-settings-btn", className="btn-accent", n_clicks=0)],
                    ),
                ],
            ),
        ],
    )


def settings_modal():
    field = lambda label, field_id, placeholder, value=None: html.Div(
        className="settings-field",
        children=[
            html.Div(label, className="settings-label"),
            dcc.Input(id=field_id, className="settings-input", value=value, placeholder=placeholder, debounce=False),
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
                                    field("Target Handle", "settings-target-handle", "@GamblingIsAllYouNeed"),
                                    field("Target Wallet (optional)", "settings-target-wallet", "0x..."),
                                    field("Reference Wallet", "settings-leader-wallet", "0xBEbe..."),
                                ],
                            ),
                            html.Div("Execution", className="modal-section-label"),
                            html.Div(
                                className="modal-row modal-row-2",
                                children=[
                                    field("Paper Starting Balance", "settings-start-balance", "5000"),
                                    field("Paper Cash Balance", "settings-cash-balance", "5000"),
                                ],
                            ),
                            html.Div(
                                className="modal-row modal-row-2",
                                children=[
                                    field("Max Total Exposure USD", "settings-max-exposure", "2500"),
                                    field("Slippage Bps", "settings-slippage-bps", "30"),
                                ],
                            ),
                            html.Div("Sync", className="modal-section-label"),
                            html.Div(
                                className="modal-row modal-row-3",
                                children=[
                                    field("Sync Interval ms", "settings-sync-interval", "1200"),
                                    field("Trade Fetch Limit", "settings-trade-limit", "100"),
                                    field("Copy Sells (1/0)", "settings-copy-sells", "1"),
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


app.layout = html.Div(
    [
        dcc.Interval(id="refresh-interval", interval=5000, n_intervals=0),
        dcc.Store(id="settings-modal-store", data={"open": False}),
        topbar(),
        html.Div(
            className="page",
            children=[
                overview_shell(),
                html.Div(
                    className="pulse-grid",
                    children=[
                        pulse_card("Realized Today", "pulse-daily-realized", "pulse-daily-realized-sub", "pulse-daily-realized-chip"),
                        pulse_card("Unrealized P/L", "pulse-unrealized", "pulse-unrealized-sub", "pulse-unrealized-chip"),
                        pulse_card("Copied Volume", "pulse-copied-notional", "pulse-copied-sub"),
                        pulse_card("Recent Syncs", "pulse-sync-health", "pulse-sync-sub", "pulse-sync-chip"),
                    ],
                ),
                html.Div(
                    className="dashboard-grid",
                    children=[
                        html.Div(
                            className="dashboard-col dashboard-col-wide",
                            children=[
                                section("Paper Portfolio Curve", "portfolio-chart", graph=True),
                                trade_views(),
                                section("Daily Performance", "daily-performance-table", "daily-performance-badge"),
                            ],
                        ),
                        html.Div(
                            className="dashboard-col",
                            children=[
                                section("Execution Brief", "analysis-panel"),
                                section("Source Positions", "source-positions-table", "source-positions-badge"),
                                section("Copied Trades", "copied-trades-table", "copied-trades-badge"),
                                section("Sync History", "sync-history-table", "sync-history-badge"),
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


def portfolio_chart():
    snapshots = database.list_portfolio_snapshots(DB_PATH, limit=80)
    if not snapshots:
        snapshots = [{"ts": "-", "net_value": 0.0}]
    baseline = float(snapshots[0]["net_value"] or 0.0)
    x_values = [fmt_pacific_time(row["ts"]) for row in snapshots]
    y_values = [float(row["net_value"] or 0.0) for row in snapshots]
    current_value = y_values[-1]
    is_up = current_value >= baseline
    line_color = "#00c805" if is_up else "#ff5a63"
    fill_color = "rgba(0, 200, 5, 0.14)" if is_up else "rgba(255, 90, 99, 0.14)"
    pct_deltas = [
        round((v - baseline) / baseline * 100.0, 2) if baseline else 0.0
        for v in y_values
    ]
    figure = go.Figure()
    # Baseline reference trace (drawn first so main trace can fill "tonexty")
    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=[baseline] * len(y_values),
            mode="lines",
            line={"color": "rgba(255,255,255,0.07)", "width": 1, "dash": "dot"},
            hoverinfo="skip",
            showlegend=False,
        )
    )
    # Main portfolio trace — fills to baseline, sharp line
    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=y_values,
            mode="lines",
            line={"color": line_color, "width": 2, "shape": "linear"},
            fill="tonexty",
            fillcolor=fill_color,
            customdata=pct_deltas,
            hovertemplate="<b>%{y:$,.2f}</b>  %{customdata:+.2f}%<extra></extra>",
        )
    )
    figure.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin={"l": 0, "r": 70, "t": 8, "b": 24},
        font={"color": "#6f7d8b", "family": "Manrope"},
        hovermode="x unified",
        hoverlabel={"bgcolor": "#0f1419", "bordercolor": "rgba(255,255,255,0.06)", "font": {"color": "#f5f8fb"}},
        xaxis={
            "showgrid": False,
            "zeroline": False,
            "showline": False,
            "tickfont": {"size": 10, "color": "#7b8794"},
            "ticklen": 0,
        },
        yaxis={
            "showgrid": True,
            "gridcolor": "rgba(255,255,255,0.04)",
            "zeroline": False,
            "showticklabels": True,
            "tickprefix": "$",
            "tickformat": ",.0f",
            "tickfont": {"size": 10, "color": "#7b8794", "family": "IBM Plex Mono"},
            "side": "right",
            "fixedrange": True,
        },
    )
    return figure


@app.callback(
    Output("status-pill", "children"),
    Output("status-pill", "className"),
    Output("toggle-engine-btn", "children"),
    Output("refresh-text", "children"),
    Output("clock", "children"),
    Output("hero-nav", "children"),
    Output("hero-return-chip", "children"),
    Output("hero-return-chip", "className"),
    Output("hero-subtitle", "children"),
    Output("hero-cash", "children"),
    Output("hero-cash-sub", "children"),
    Output("hero-exposure", "children"),
    Output("hero-exposure-chip", "children"),
    Output("hero-exposure-chip", "className"),
    Output("hero-exposure-sub", "children"),
    Output("hero-fill-rate", "children"),
    Output("hero-fill-sub", "children"),
    Output("hero-open-positions", "children"),
    Output("hero-open-sub", "children"),
    Output("signal-runtime", "children"),
    Output("signal-summary", "children"),
    Output("signal-trader", "children"),
    Output("signal-wallet", "children"),
    Output("signal-heartbeat", "children"),
    Output("signal-cadence", "children"),
    Output("signal-leader-nav", "children"),
    Output("signal-pending", "children"),
    Output("pulse-daily-realized", "children"),
    Output("pulse-daily-realized-sub", "children"),
    Output("pulse-daily-realized-chip", "children"),
    Output("pulse-daily-realized-chip", "className"),
    Output("pulse-unrealized", "children"),
    Output("pulse-unrealized-sub", "children"),
    Output("pulse-unrealized-chip", "children"),
    Output("pulse-unrealized-chip", "className"),
    Output("pulse-copied-notional", "children"),
    Output("pulse-copied-sub", "children"),
    Output("pulse-sync-health", "children"),
    Output("pulse-sync-sub", "children"),
    Output("pulse-sync-chip", "children"),
    Output("pulse-sync-chip", "className"),
    Output("source-positions-badge", "children"),
    Output("source-positions-table", "children"),
    Output("portfolio-chart", "figure"),
    Output("daily-performance-badge", "children"),
    Output("daily-performance-table", "children"),
    Output("sync-history-badge", "children"),
    Output("sync-history-table", "children"),
    Output("copied-trades-badge", "children"),
    Output("copied-trades-table", "children"),
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
    source_positions = database.list_source_positions(10, DB_PATH)
    copy_orders = database.list_copy_orders(12, DB_PATH)
    pending = database.list_pending_source_trades(DB_PATH)
    sync_runs = database.list_sync_runs(12, DB_PATH)
    logs = database.list_logs(14, DB_PATH)
    portfolio = database.portfolio_totals(DB_PATH)
    analytics = database.trade_analytics(DB_PATH)
    daily_performance = database.daily_portfolio_performance(DB_PATH)[:12]
    daily_realized_map = {row["date"]: row["realized_pnl"] for row in analytics["daily_realized"]}
    open_trades = analytics["open_trades"]
    closed_trades = analytics["closed_trades"]

    copied_notional = sum(row["requested_amount_usd"] for row in copy_orders if row["status"] == "FILLED")
    filled_orders = [row for row in copy_orders if row["status"] == "FILLED"]
    latest_daily = daily_performance[0] if daily_performance else None
    daily_realized = float(daily_realized_map.get(latest_daily["date"], 0.0)) if latest_daily else 0.0
    unrealized_total = sum(float(row["unrealized_pnl"]) for row in open_trades)
    exposure_cap = float(settings["max_total_exposure_usd"] or 0.0)
    gross_exposure = float(portfolio["gross_exposure"] or 0.0)
    exposure_usage = (gross_exposure / exposure_cap * 100.0) if exposure_cap > 0 else 0.0
    fill_rate = (len(filled_orders) / len(copy_orders) * 100.0) if copy_orders else 0.0
    sync_success_count = len([row for row in sync_runs if row["status"] == "SUCCESS"])
    sync_success_rate = (sync_success_count / len(sync_runs) * 100.0) if sync_runs else 0.0
    latest_sync = sync_runs[0] if sync_runs else None
    latest_latency = f"{latest_sync['latency_ms']}ms" if latest_sync and latest_sync.get("latency_ms") is not None else "-"

    source_position_rows = [
        [
            short_text(row["market_title"], 36),
            row["outcome"],
            fmt_number(row["shares"], 2),
            fmt_currency(row["notional_usd"]),
            fmt_number(row["price"], 3),
        ]
        for row in source_positions
    ] or [["No source positions yet", "-", "-", "-", "-"]]
    copied_trade_rows = [
        [
            fmt_pacific_time(row["created_at"]),
            short_text(row["market_title"], 34),
            row["outcome"],
            row["side"],
            fmt_currency(row["requested_amount_usd"]),
            fmt_number(row["executed_price"], 3),
            row["status"],
        ]
        for row in copy_orders
    ] or [["No copied trades yet", "-", "-", "-", "-", "-", "-"]]
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
            short_text(row["market_title"], 30),
            row["outcome"],
            fmt_number(row["shares"], 2),
            fmt_currency(row["cost_basis"]),
            fmt_currency(row["market_value"]),
            fmt_signed_currency(row["unrealized_pnl"]),
        ]
        for row in open_trades[:20]
    ] or [["No open trades", "-", "-", "-", "-", "-"]]
    closed_trade_rows = [
        [
            fmt_pacific_time(row["exit_time"]),
            short_text(row["market_title"], 26),
            row["outcome"],
            fmt_number(row["shares"], 2),
            fmt_currency(row["cost_basis"]),
            fmt_currency(row["proceeds"]),
            fmt_signed_currency(row["pnl"]),
        ]
        for row in closed_trades[:20]
    ] or [["No closed trades", "-", "-", "-", "-", "-", "-"]]
    trade_book_table = (
        render_table(["Market", "Outcome", "Shares", "Cost", "Value", "P/L"], open_trade_rows)
        if trade_tab == "open-trades"
        else render_table(["Sold At (PT)", "Market", "Outcome", "Shares", "Cost", "Proceeds", "P/L"], closed_trade_rows)
    )
    trade_book_count = len(open_trades) if trade_tab == "open-trades" else len(closed_trades)
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
    heartbeat = heartbeat_label(app_state, runtime_status, stale_age_seconds)
    analysis = html.Div(
        className="analysis-stack",
        children=[
            html.Div(
                className="analysis-block",
                children=[
                    html.Div("Execution Posture", className="analysis-label"),
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
                    html.Div("Account Mirror", className="analysis-label"),
                    html.Div(f"{target_label} resolved to {target_wallet}.", className="analysis-text"),
                ],
            ),
            html.Div(
                className="analysis-block",
                children=[
                    html.Div("Copy Configuration", className="analysis-label"),
                    html.Div(
                        f"Polling every {settings['sync_interval_ms']}ms, fetch depth {settings['trade_fetch_limit']}, "
                        f"slippage cap {settings['slippage_bps']} bps, sells {'on' if int(settings['copy_sells']) else 'off'}.",
                        className="analysis-text",
                    ),
                ],
            ),
            html.Div(
                className="analysis-block",
                children=[
                    html.Div("Risk Budget", className="analysis-label"),
                    html.Div(
                        f"{fmt_currency(gross_exposure)} deployed against a {fmt_currency(exposure_cap)} cap "
                        f"({exposure_usage:.1f}% used). Cash available is {fmt_currency(portfolio['cash_balance'])}.",
                        className="analysis-text",
                    ),
                ],
            ),
            html.Div(
                className="analysis-block",
                children=[
                    html.Div("Operational Risk", className="analysis-label"),
                    html.Div(
                        app_state["last_error"] or f"Recent sync success rate is {sync_success_rate:.0f}% with latest latency {latest_latency}.",
                        className="analysis-text",
                    ),
                ],
            ),
        ],
    )

    refresh_text = f"Last sync: {fmt_pacific_time(app_state['last_sync_at']) if app_state['last_sync_at'] else 'never'}"
    net_gain = float(portfolio["total_gain"])
    net_chip_text = f"{portfolio['total_gain_pct']:+.2f}%"

    return (
        heartbeat,
        runtime_class,
        "Pause" if app_state["engine_status"] == "RUNNING" else "Resume",
        refresh_text,
        fmt_pacific_clock(),
        fmt_currency(portfolio["net_value"]),
        net_chip_text,
        f"hero-return-chip {signed_class(net_gain)}",
        f"{fmt_signed_currency(net_gain)} since inception. Tracking {target_label} with {len(open_trades)} open positions.",
        fmt_currency(portfolio["cash_balance"]),
        f"{(float(portfolio['cash_balance']) / float(portfolio['net_value']) * 100.0):.1f}% of NAV is unallocated" if float(portfolio["net_value"]) else "No capital available",
        fmt_currency(gross_exposure),
        f"{exposure_usage:.1f}%",
        f"stat-chip {signed_class(exposure_cap - gross_exposure if exposure_cap else 0)}",
        f"Cap {fmt_currency(exposure_cap)}",
        f"{fill_rate:.0f}%",
        f"{len(filled_orders)} filled from {len(copy_orders)} copy attempts",
        str(len(open_trades)),
        f"{len(closed_trades)} trades have been closed",
        runtime_status,
        app_state["last_sync_message"] if runtime_status != "STALE" else f"Engine marked RUNNING but heartbeat is stale by {stale_age_seconds}s.",
        target_label,
        target_wallet,
        heartbeat,
        f"{settings['sync_interval_ms']} ms",
        fmt_currency(float(app_state.get("leader_wallet_value") or 0.0)),
        str(len(pending)),
        fmt_signed_currency(daily_realized),
        latest_daily["date"] if latest_daily else "No realized activity yet",
        fmt_signed_currency(daily_realized),
        f"stat-chip {signed_class(daily_realized)}",
        fmt_signed_currency(unrealized_total),
        f"Across {len(open_trades)} active positions",
        fmt_signed_currency(unrealized_total),
        f"stat-chip {signed_class(unrealized_total)}",
        fmt_currency(copied_notional),
        f"{len(filled_orders)} fills from the latest {len(copy_orders)} copy orders",
        f"{sync_success_rate:.0f}%",
        f"{len(sync_runs)} recent sync runs. Latest latency {latest_latency}.",
        f"{sync_success_rate:.0f}%",
        f"stat-chip {signed_class(sync_success_rate - 90)}",
        str(len(source_positions)),
        render_table(["Market", "Outcome", "Shares", "Value", "Price"], source_position_rows),
        portfolio_chart(),
        str(len(daily_performance)),
        render_table(["Date", "Day Change", "Closed P/L", "Portfolio"], daily_rows),
        str(len(sync_runs)),
        render_table(["Time (PT)", "Status", "Seen", "New", "Copied", "Latency"], sync_rows),
        str(len(copy_orders)),
        render_table(["Completed At (PT)", "Market", "Outcome", "Side", "USD", "Px", "Status"], copied_trade_rows),
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
    return (
        "modal-overlay" if store["open"] else "modal-overlay hidden",
        settings["target_handle"],
        settings["target_wallet"],
        settings["leader_wallet_address"],
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
    State("settings-max-exposure", "value"),
    State("settings-start-balance", "value"),
    State("settings-cash-balance", "value"),
    State("settings-slippage-bps", "value"),
    State("settings-sync-interval", "value"),
    State("settings-trade-limit", "value"),
    State("settings-copy-sells", "value"),
    prevent_initial_call=True,
)
def save_settings(_, target_handle, target_wallet, leader_wallet, max_exposure, start_balance, cash_balance, slippage_bps, sync_interval, trade_limit, copy_sells):
    updates = {
        "target_handle": (target_handle or "").strip().lstrip("@"),
        "target_wallet": (target_wallet or "").strip(),
        "leader_wallet_address": (leader_wallet or "").strip(),
        "max_total_exposure_usd": float(max_exposure or 0),
        "paper_starting_balance": float(start_balance or 0),
        "paper_cash_balance": float(cash_balance or 0),
        "slippage_bps": int(float(slippage_bps or 0)),
        "sync_interval_ms": int(float(sync_interval or 1200)),
        "trade_fetch_limit": int(float(trade_limit or 100)),
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


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8060"))
    debug = os.environ.get("DEBUG", "").lower() in {"1", "true", "yes"}
    app.run(debug=debug, host=host, port=port)
