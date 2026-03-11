from __future__ import annotations

from datetime import datetime
import os

import dash
import plotly.graph_objects as go
from flask import jsonify
from dash import Input, Output, State, callback_context, dcc, html

from copytrader import database
from copytrader.config import DB_PATH
from copytrader.engine import CopyTradingEngine


database.init_db(DB_PATH)
engine = CopyTradingEngine(DB_PATH)
engine.start()

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


@server.get("/healthz")
def healthz():
    return jsonify(engine.health()), 200


def fmt_currency(value) -> str:
    return f"${float(value):,.2f}"


def fmt_number(value, digits=2) -> str:
    return f"{float(value):,.{digits}f}"


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
                        className="topbar-title-wrap",
                        children=[
                            html.Span("Polymarket ", className="topbar-title"),
                            html.Span("Copy Trader", className="topbar-title accent"),
                        ],
                    ),
                    html.Span(id="status-pill", className="status-running", children="RUNNING"),
                ],
            ),
            html.Div(
                className="topbar-controls",
                children=[
                    html.Button("Force Sync", id="force-sync-btn", className="btn-outline", n_clicks=0),
                    html.Button("Pause", id="toggle-engine-btn", className="btn-outline", n_clicks=0),
                    html.Button("Settings", id="open-settings-btn", className="btn-accent", n_clicks=0),
                    html.Div(id="refresh-text", className="topbar-meta"),
                    html.Div(id="clock", className="topbar-meta"),
                ],
            ),
        ],
    )


def stat_card(label: str, value_id: str, sub_id: str | None = None):
    children = [
        html.Div(label, className="stat-label"),
        html.Div(id=value_id, className="stat-value"),
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
                                className="modal-row",
                                children=[
                                    field("Target Handle", "settings-target-handle", "@GamblingIsAllYouNeed"),
                                    field("Target Wallet (optional)", "settings-target-wallet", "0x..."),
                                    field("Leader Wallet Address", "settings-leader-wallet", "0xBEbe..."),
                                ],
                            ),
                            html.Div("Execution", className="modal-section-label"),
                            html.Div(
                                className="modal-row",
                                children=[
                                    field("Copy Ratio", "settings-copy-ratio", "1.0"),
                                    field("Max Copy Trade USD", "settings-max-copy-trade", "150"),
                                    field("Max Total Exposure USD", "settings-max-exposure", "2500"),
                                ],
                            ),
                            html.Div(
                                className="modal-row",
                                children=[
                                    field("Paper Starting Balance", "settings-start-balance", "5000"),
                                    field("Paper Cash Balance", "settings-cash-balance", "5000"),
                                    field("Slippage Bps", "settings-slippage-bps", "30"),
                                ],
                            ),
                            html.Div("Sync", className="modal-section-label"),
                            html.Div(
                                className="modal-row",
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
                html.Div(
                    className="stats-row",
                    children=[
                        stat_card("Tracked Trader", "stat-target", "stat-target-sub"),
                        stat_card("Pending Copies", "stat-pending", "stat-pending-sub"),
                        stat_card("Copied Notional", "stat-copied-notional", "stat-copied-sub"),
                        stat_card("Paper Portfolio", "stat-net-value", "stat-net-sub"),
                    ],
                ),
                html.Div(
                    className="dashboard-grid",
                    children=[
                        html.Div(
                            className="dashboard-col",
                            children=[
                                section("Source Positions", "source-positions-table", "source-positions-badge"),
                                section("Paper Portfolio Curve", "portfolio-chart", graph=True),
                                section("Sync History", "sync-history-table", "sync-history-badge"),
                            ],
                        ),
                        html.Div(
                            className="dashboard-col",
                            children=[
                                section("Copied Trades", "copied-trades-table", "copied-trades-badge"),
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


def portfolio_chart():
    snapshots = database.list_portfolio_snapshots(DB_PATH, limit=80)
    x_values = [row["ts"][-8:] for row in snapshots]
    y_values = [row["net_value"] for row in snapshots]
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=y_values,
            mode="lines",
            line={"color": "#a78bfa", "width": 3},
            fill="tozeroy",
            fillcolor="rgba(167,139,250,0.10)",
            hovertemplate="%{y:$,.2f}<extra></extra>",
        )
    )
    figure.update_layout(
        paper_bgcolor="#111114",
        plot_bgcolor="#111114",
        margin={"l": 18, "r": 18, "t": 8, "b": 24},
        font={"color": "#a1a1aa", "family": "Inter"},
        xaxis={"showgrid": False, "zeroline": False},
        yaxis={"gridcolor": "rgba(255,255,255,0.06)", "zeroline": False},
    )
    return figure


@app.callback(
    Output("status-pill", "children"),
    Output("status-pill", "className"),
    Output("toggle-engine-btn", "children"),
    Output("refresh-text", "children"),
    Output("clock", "children"),
    Output("stat-target", "children"),
    Output("stat-target-sub", "children"),
    Output("stat-pending", "children"),
    Output("stat-pending-sub", "children"),
    Output("stat-copied-notional", "children"),
    Output("stat-copied-sub", "children"),
    Output("stat-net-value", "children"),
    Output("stat-net-sub", "children"),
    Output("source-positions-badge", "children"),
    Output("source-positions-table", "children"),
    Output("portfolio-chart", "figure"),
    Output("sync-history-badge", "children"),
    Output("sync-history-table", "children"),
    Output("copied-trades-badge", "children"),
    Output("copied-trades-table", "children"),
    Output("analysis-panel", "children"),
    Output("engine-log-badge", "children"),
    Output("engine-log-table", "children"),
    Input("refresh-interval", "n_intervals"),
)
def refresh_dashboard(_):
    settings = database.get_settings(DB_PATH)
    app_state = database.get_app_state(DB_PATH)
    source_positions = database.list_source_positions(10, DB_PATH)
    copy_orders = database.list_copy_orders(12, DB_PATH)
    pending = database.list_pending_source_trades(DB_PATH)
    sync_runs = database.list_sync_runs(12, DB_PATH)
    logs = database.list_logs(14, DB_PATH)
    portfolio = database.portfolio_totals(DB_PATH)

    copied_notional = sum(row["requested_amount_usd"] for row in copy_orders if row["status"] == "FILLED")
    source_position_rows = [
        [
            row["market_title"][:36],
            row["outcome"],
            fmt_number(row["shares"], 2),
            fmt_currency(row["notional_usd"]),
            fmt_number(row["price"], 3),
        ]
        for row in source_positions
    ] or [["No source positions yet", "-", "-", "-", "-"]]
    copied_trade_rows = [
        [
            row["market_title"][:34],
            row["outcome"],
            row["side"],
            fmt_currency(row["requested_amount_usd"]),
            fmt_number(row["executed_price"], 3),
            row["status"],
        ]
        for row in copy_orders
    ] or [["No copied trades yet", "-", "-", "-", "-", "-"]]
    sync_rows = [
        [
            row["started_at"][11:19] if row["started_at"] else "-",
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
            row["ts"][11:19] if row["ts"] else "-",
            row["level"],
            row["component"],
            row["message"][:48],
        ]
        for row in logs
    ] or [["-", "-", "-", "-"]]

    target_label = f"@{settings['target_handle']}" if settings["target_handle"] else "Wallet target"
    target_wallet = app_state.get("resolved_target_wallet") or settings["target_wallet"] or "Not resolved yet"
    analysis = html.Div(
        className="analysis-stack",
        children=[
            html.Div(
                className="analysis-block",
                children=[
                    html.Div("Sync Status", className="analysis-label"),
                    html.Div(app_state["last_sync_message"], className="analysis-text"),
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
                    html.Div("Fast Copy Model", className="analysis-label"),
                    html.Div(
                        f"Polling every {settings['sync_interval_ms']}ms with {settings['trade_fetch_limit']} trade lookback. "
                        f"Paper execution sizes as proportional wallet slices against {settings['leader_wallet_address']}, "
                        f"scaled by {settings['copy_ratio']}x and capped at {fmt_currency(settings['max_copy_trade_usd'])}. "
                        f"The dashboard refreshes separately and never gates copy execution.",
                        className="analysis-text",
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
                    html.Div(app_state.get("copy_start_at") or "Immediate", className="analysis-text mono"),
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

    return (
        app_state["engine_status"],
        status_class(app_state["engine_status"]),
        "Pause" if app_state["engine_status"] == "RUNNING" else "Resume",
        f"Last sync: {app_state['last_sync_at'][11:19] if app_state['last_sync_at'] else 'never'}",
        datetime.now().strftime("%b %d, %Y %H:%M:%S"),
        target_label,
        target_wallet,
        str(len(pending)),
        f"{len(copy_orders)} total copied orders",
        fmt_currency(copied_notional),
        f"Cash {fmt_currency(portfolio['cash_balance'])} / Exposure {fmt_currency(portfolio['gross_exposure'])}",
        fmt_currency(portfolio["net_value"]),
        f"{portfolio['positions_count']} open local positions",
        str(len(source_positions)),
        render_table(["Market", "Outcome", "Shares", "Notional", "Price"], source_position_rows),
        portfolio_chart(),
        str(len(sync_runs)),
        render_table(["Time", "Status", "Seen", "New", "Copied", "Latency"], sync_rows),
        str(len(copy_orders)),
        render_table(["Market", "Outcome", "Side", "USD", "Px", "Status"], copied_trade_rows),
        analysis,
        str(len(logs)),
        render_table(["Time", "Level", "Comp", "Message"], log_rows),
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
    Output("settings-copy-ratio", "value"),
    Output("settings-max-copy-trade", "value"),
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
        settings["copy_ratio"],
        settings["max_copy_trade_usd"],
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
    State("settings-copy-ratio", "value"),
    State("settings-max-copy-trade", "value"),
    State("settings-max-exposure", "value"),
    State("settings-start-balance", "value"),
    State("settings-cash-balance", "value"),
    State("settings-slippage-bps", "value"),
    State("settings-sync-interval", "value"),
    State("settings-trade-limit", "value"),
    State("settings-copy-sells", "value"),
    prevent_initial_call=True,
)
def save_settings(_, target_handle, target_wallet, leader_wallet, copy_ratio, max_copy_trade, max_exposure, start_balance, cash_balance, slippage_bps, sync_interval, trade_limit, copy_sells):
    updates = {
        "target_handle": (target_handle or "").strip().lstrip("@"),
        "target_wallet": (target_wallet or "").strip(),
        "leader_wallet_address": (leader_wallet or "").strip(),
        "copy_ratio": float(copy_ratio or 1.0),
        "max_copy_trade_usd": float(max_copy_trade or 0),
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
    engine.set_running(current != "RUNNING")
    return 0


@app.callback(
    Output("force-sync-btn", "n_clicks"),
    Input("force-sync-btn", "n_clicks"),
    prevent_initial_call=True,
)
def force_sync(_):
    engine.tick(force=True)
    return 0


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8060"))
    debug = os.environ.get("DEBUG", "").lower() in {"1", "true", "yes"}
    app.run(debug=debug, host=host, port=port)
