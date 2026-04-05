"""
WiLoc Dashboard — Real-time room visualization from CSI data.

Lightweight web app that shows:
- 3D room view with anchor positions
- Live AP position estimates as CSI data arrives
- Detected wall positions from reflection analysis
- Signal strength heatmap overlay
- CSI amplitude waterfall per anchor

Runs in any browser. No Blender, no Sionna — just plotly + dash.

Usage:
    python app.py                              # default, reads wiloc_ble.db
    python app.py --db /path/to/data.db        # custom database
    python app.py --room 4.0 4.0 3.0           # custom room dimensions
    python app.py --port 8050                   # custom port
"""

import argparse
import json
import sqlite3
import time
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
import plotly.graph_objects as go
from dash import Dash, html, dcc, Input, Output, State, callback

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "processing"))
sys.path.insert(0, str(Path(__file__).parent.parent / "models"))
sys.path.insert(0, str(Path(__file__).parent.parent / "simulator"))

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

DEFAULT_ROOM = {"width": 4.0, "depth": 4.0, "height": 3.0}
DEFAULT_ANCHORS = {
    "anc_00": {"x": 0.1, "y": 0.1, "z": 1.2},
    "anc_01": {"x": 3.9, "y": 0.1, "z": 1.2},
    "anc_02": {"x": 3.9, "y": 3.9, "z": 1.2},
    "anc_03": {"x": 0.1, "y": 3.9, "z": 1.2},
}

# How many recent CSI readings to consider per update
WINDOW_SIZE = 100
UPDATE_INTERVAL_MS = 2000

# ──────────────────────────────────────────────
# Database access
# ──────────────────────────────────────────────

class CSIStore:
    """Thread-safe read access to the CSI SQLite database."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_db()

    def _ensure_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS csi_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_ms INTEGER NOT NULL,
                anchor_id TEXT NOT NULL,
                target_mac TEXT,
                rssi INTEGER,
                channel INTEGER,
                bandwidth INTEGER,
                csi_len INTEGER,
                csi_raw TEXT,
                label_x REAL,
                label_y REAL,
                label_z REAL,
                collected_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def get_recent(self, n: int = WINDOW_SIZE, target_mac: str = None) -> list[dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        if target_mac:
            rows = conn.execute(
                "SELECT * FROM csi_readings WHERE target_mac = ? ORDER BY id DESC LIMIT ?",
                (target_mac, n),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM csi_readings ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_recent_by_anchor(self, anchor_id: str, n: int = 50,
                              target_mac: str = None) -> list[dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        if target_mac:
            rows = conn.execute(
                "SELECT * FROM csi_readings WHERE anchor_id = ? AND target_mac = ? ORDER BY id DESC LIMIT ?",
                (anchor_id, target_mac, n),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM csi_readings WHERE anchor_id = ? ORDER BY id DESC LIMIT ?",
                (anchor_id, n),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_stats(self, target_mac: str = None) -> dict:
        conn = sqlite3.connect(self.db_path)
        if target_mac:
            total = conn.execute(
                "SELECT COUNT(*) FROM csi_readings WHERE target_mac = ?", (target_mac,)
            ).fetchone()[0]
            query = """SELECT anchor_id, COUNT(*), AVG(rssi), MAX(id)
                       FROM csi_readings WHERE target_mac = ? GROUP BY anchor_id"""
            cursor = conn.execute(query, (target_mac,))
        else:
            total = conn.execute("SELECT COUNT(*) FROM csi_readings").fetchone()[0]
            cursor = conn.execute(
                "SELECT anchor_id, COUNT(*), AVG(rssi), MAX(id) FROM csi_readings GROUP BY anchor_id"
            )
        per_anchor = {}
        for row in cursor.fetchall():
            per_anchor[row[0]] = {
                "count": row[1],
                "avg_rssi": round(row[2], 1) if row[2] else None,
                "last_id": row[3],
            }
        conn.close()
        return {"total": total, "per_anchor": per_anchor}

    def get_top_macs(self, limit: int = 10) -> list[dict]:
        """Get the most-seen target MACs, ranked by strongest average RSSI."""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("""
            SELECT target_mac, COUNT(*) as cnt, AVG(rssi) as avg_rssi,
                   COUNT(DISTINCT anchor_id) as n_anchors
            FROM csi_readings
            GROUP BY target_mac
            HAVING n_anchors >= 3
            ORDER BY avg_rssi DESC
            LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
        return [{"mac": r[0], "count": r[1], "avg_rssi": round(r[2], 1),
                 "n_anchors": r[3]} for r in rows]


# ──────────────────────────────────────────────
# Signal processing (lightweight, for real-time)
# ──────────────────────────────────────────────

def csi_raw_to_amplitude(csi_raw_json: str) -> np.ndarray:
    """Convert raw CSI JSON string to amplitude array."""
    try:
        values = json.loads(csi_raw_json)
        imag = np.array(values[0::2], dtype=np.float64)
        real = np.array(values[1::2], dtype=np.float64)
        return np.abs(real + 1j * imag)
    except Exception:
        return np.array([])


def estimate_distance_from_rssi(rssi: float, rssi_at_1m: float = -45.0,
                                path_loss_exp: float = 2.5) -> float:
    """Quick RSSI-to-distance estimate using log-distance path loss model.

    rssi_at_1m: measured or estimated RSSI at 1 meter from the transmitter.
                Typical ESP32 in a room: -40 to -50 dBm.
    path_loss_exp: 2.0 = free space, 2.5-3.5 = indoor with obstacles.
    """
    if rssi is None or rssi >= 0:
        return 0.0
    return 10 ** ((rssi_at_1m - rssi) / (10 * path_loss_exp))


def trilaterate_2d(anchors: np.ndarray, distances: np.ndarray) -> np.ndarray:
    """Least-squares 2D trilateration."""
    from scipy.optimize import least_squares

    def residuals(pos):
        return np.linalg.norm(anchors - pos, axis=1) - distances

    x0 = anchors.mean(axis=0)
    result = least_squares(residuals, x0, method='lm')
    return result.x


def estimate_ap_position(recent_data: list[dict], anchor_positions: dict) -> dict | None:
    """Estimate AP position from recent CSI readings."""
    rssi_by_anchor = {}
    for row in recent_data:
        aid = row.get("anchor_id")
        rssi = row.get("rssi")
        if aid and rssi is not None and aid in anchor_positions:
            rssi_by_anchor.setdefault(aid, []).append(rssi)

    if len(rssi_by_anchor) < 3:
        return None

    anchors = []
    distances = []
    rssi_avgs = {}
    for aid, rssi_list in rssi_by_anchor.items():
        avg_rssi = np.mean(rssi_list)
        rssi_avgs[aid] = avg_rssi
        pos = anchor_positions[aid]
        anchors.append([pos["x"], pos["y"]])
        distances.append(estimate_distance_from_rssi(avg_rssi))

    anchors = np.array(anchors)
    distances = np.array(distances)

    try:
        pos = trilaterate_2d(anchors, distances)
        return {"x": float(pos[0]), "y": float(pos[1]), "rssi": rssi_avgs}
    except Exception:
        return None


# ──────────────────────────────────────────────
# Plotly figure builders
# ──────────────────────────────────────────────

def build_room_figure(room: dict, anchors: dict, ap_pos: dict = None,
                      wall_estimates: list = None) -> go.Figure:
    """Build the 3D room visualization."""
    fig = go.Figure()
    w, d, h = room["width"], room["depth"], room["height"]

    # Floor grid
    for x in np.arange(0, w + 0.01, 0.5):
        fig.add_trace(go.Scatter3d(
            x=[x, x], y=[0, d], z=[0, 0],
            mode='lines', line=dict(color='lightgray', width=1),
            showlegend=False, hoverinfo='skip',
        ))
    for y in np.arange(0, d + 0.01, 0.5):
        fig.add_trace(go.Scatter3d(
            x=[0, w], y=[y, y], z=[0, 0],
            mode='lines', line=dict(color='lightgray', width=1),
            showlegend=False, hoverinfo='skip',
        ))

    # Wall edges
    edges = [
        ([0,0,0],[w,0,0]), ([w,0,0],[w,d,0]), ([w,d,0],[0,d,0]), ([0,d,0],[0,0,0]),
        ([0,0,h],[w,0,h]), ([w,0,h],[w,d,h]), ([w,d,h],[0,d,h]), ([0,d,h],[0,0,h]),
        ([0,0,0],[0,0,h]), ([w,0,0],[w,0,h]), ([w,d,0],[w,d,h]), ([0,d,0],[0,d,h]),
    ]
    for a, b in edges:
        fig.add_trace(go.Scatter3d(
            x=[a[0],b[0]], y=[a[1],b[1]], z=[a[2],b[2]],
            mode='lines', line=dict(color='gray', width=2),
            showlegend=False, hoverinfo='skip',
        ))

    # Anchors
    ax = [v["x"] for v in anchors.values()]
    ay = [v["y"] for v in anchors.values()]
    az = [v["z"] for v in anchors.values()]
    labels = list(anchors.keys())
    fig.add_trace(go.Scatter3d(
        x=ax, y=ay, z=az,
        mode='markers+text',
        marker=dict(size=6, color='red', symbol='diamond'),
        text=labels, textposition='top center',
        textfont=dict(size=9, color='red'),
        name='Anchors',
        hovertext=[f"{k}: ({v['x']}, {v['y']}, {v['z']})" for k, v in anchors.items()],
        hoverinfo='text',
    ))

    # AP position estimate
    if ap_pos:
        fig.add_trace(go.Scatter3d(
            x=[ap_pos["x"]], y=[ap_pos["y"]], z=[1.0],
            mode='markers+text',
            marker=dict(size=10, color='blue', symbol='circle'),
            text=['AP'], textposition='top center',
            textfont=dict(size=11, color='blue'),
            name='Estimated AP',
            hovertext=f"AP: ({ap_pos['x']:.2f}, {ap_pos['y']:.2f})",
            hoverinfo='text',
        ))

        # Signal lines from AP to anchors
        for aid, apos in anchors.items():
            fig.add_trace(go.Scatter3d(
                x=[ap_pos["x"], apos["x"]], y=[ap_pos["y"], apos["y"]], z=[1.0, apos["z"]],
                mode='lines',
                line=dict(color='deepskyblue', width=2, dash='dash'),
                showlegend=False, hoverinfo='skip',
            ))

    # Detected wall estimates (if available)
    if wall_estimates:
        for wall in wall_estimates:
            if wall["axis"] == "x":
                fig.add_trace(go.Scatter3d(
                    x=[wall["position"]]*2, y=[0, d], z=[0, 0],
                    mode='lines', line=dict(color='green', width=4, dash='dot'),
                    showlegend=False,
                    hovertext=f"Detected wall X={wall['position']:.2f}m",
                    hoverinfo='text',
                ))
            elif wall["axis"] == "y":
                fig.add_trace(go.Scatter3d(
                    x=[0, w], y=[wall["position"]]*2, z=[0, 0],
                    mode='lines', line=dict(color='green', width=4, dash='dot'),
                    showlegend=False,
                    hovertext=f"Detected wall Y={wall['position']:.2f}m",
                    hoverinfo='text',
                ))

    fig.update_layout(
        scene=dict(
            xaxis=dict(title='X (m)', range=[-0.5, w + 0.5]),
            yaxis=dict(title='Y (m)', range=[-0.5, d + 0.5]),
            zaxis=dict(title='Z (m)', range=[-0.1, h + 0.3]),
            aspectmode='data',
            camera=dict(eye=dict(x=1.3, y=-1.3, z=0.8)),
        ),
        margin=dict(l=0, r=0, t=30, b=0),
        height=500,
    )
    return fig


def build_csi_waterfall(data_by_anchor: dict) -> go.Figure:
    """Build CSI amplitude waterfall (subcarrier x time) for each anchor."""
    fig = go.Figure()

    for i, (aid, rows) in enumerate(sorted(data_by_anchor.items())):
        if not rows:
            continue

        amplitudes = []
        for row in reversed(rows):  # oldest first
            csi_raw = row.get("csi_raw")
            if csi_raw:
                amp = csi_raw_to_amplitude(csi_raw)
                if len(amp) > 0:
                    amplitudes.append(amp)

        if not amplitudes:
            continue

        # Pad to same length
        max_len = max(len(a) for a in amplitudes)
        matrix = np.zeros((len(amplitudes), max_len))
        for j, a in enumerate(amplitudes):
            matrix[j, :len(a)] = a

        fig.add_trace(go.Heatmap(
            z=matrix.T,
            colorscale='Viridis',
            name=aid,
            visible=(i == 0),
            colorbar=dict(title='Amplitude'),
        ))

    # Dropdown to switch between anchors
    anchor_ids = sorted(data_by_anchor.keys())
    buttons = []
    for i, aid in enumerate(anchor_ids):
        vis = [False] * len(anchor_ids)
        vis[i] = True
        buttons.append(dict(
            label=aid,
            method='update',
            args=[{'visible': vis}],
        ))

    fig.update_layout(
        updatemenus=[dict(
            buttons=buttons,
            direction='down',
            x=0.0, xanchor='left',
            y=1.15, yanchor='top',
        )] if buttons else [],
        xaxis_title='Packet index',
        yaxis_title='Subcarrier',
        height=300,
        margin=dict(l=50, r=20, t=40, b=40),
    )
    return fig


def build_rssi_timeline(data_by_anchor: dict) -> go.Figure:
    """Build RSSI over time for each anchor."""
    fig = go.Figure()

    colors = {'anc_00': '#e41a1c', 'anc_01': '#377eb8',
              'anc_02': '#4daf4a', 'anc_03': '#984ea3'}

    for aid, rows in sorted(data_by_anchor.items()):
        if not rows:
            continue
        rssis = [r["rssi"] for r in reversed(rows) if r.get("rssi") is not None]
        if rssis:
            fig.add_trace(go.Scatter(
                y=rssis,
                mode='lines',
                name=aid,
                line=dict(color=colors.get(aid, 'gray'), width=2),
            ))

    fig.update_layout(
        xaxis_title='Packet index',
        yaxis_title='RSSI (dBm)',
        height=250,
        margin=dict(l=50, r=20, t=30, b=40),
        legend=dict(orientation='h', y=1.12),
    )
    return fig


# ──────────────────────────────────────────────
# Dash app
# ──────────────────────────────────────────────

def create_app(db_path: str, room: dict, anchors: dict) -> Dash:
    store = CSIStore(db_path)

    app = Dash(__name__)
    app.title = "WiLoc Dashboard"

    # Build initial MAC dropdown options
    top_macs = store.get_top_macs(15)
    mac_options = [{"label": "All MACs (mixed)", "value": "__all__"}]
    mac_options.append({"label": "── Strongest signal (best for in-room device) ──", "value": "__all__", "disabled": True})
    for m in top_macs:
        label = f"{m['mac']}  ({m['avg_rssi']} dBm, {m['count']} pkts, {m['n_anchors']} anchors)"
        mac_options.append({"label": label, "value": m["mac"]})
    # Auto-select strongest MAC seen by all 4 anchors
    default_mac = "__all__"
    for m in top_macs:
        if m["n_anchors"] >= 4:
            default_mac = m["mac"]
            break

    app.layout = html.Div([
        # Header
        html.Div([
            html.H2("WiLoc — Room Visualizer", style={'margin': '0', 'color': '#333'}),
            html.Span(id='status-text', style={'color': '#666', 'fontSize': '14px'}),
        ], style={'display': 'flex', 'justifyContent': 'space-between',
                  'alignItems': 'center', 'padding': '10px 20px',
                  'borderBottom': '2px solid #eee'}),

        # Target MAC selector
        html.Div([
            html.Label("Target device (AP):", style={'fontWeight': 'bold', 'marginRight': '10px'}),
            dcc.Dropdown(
                id='mac-selector',
                options=mac_options,
                value=default_mac,
                style={'width': '600px', 'fontFamily': 'monospace', 'fontSize': '13px'},
                clearable=False,
            ),
            html.Button("Refresh MACs", id='refresh-macs-btn', n_clicks=0,
                         style={'marginLeft': '10px', 'padding': '5px 15px'}),
        ], style={'display': 'flex', 'alignItems': 'center', 'padding': '10px 20px',
                  'background': '#f8f9fa', 'borderBottom': '1px solid #eee'}),

        # Main content
        html.Div([
            # Left: 3D room
            html.Div([
                html.H4("Room View", style={'margin': '10px 0 5px 0'}),
                dcc.Graph(id='room-3d', config={'displayModeBar': True}),
            ], style={'flex': '3', 'minWidth': '500px'}),

            # Right: stats panel
            html.Div([
                html.H4("Anchor Status", style={'margin': '10px 0 5px 0'}),
                html.Div(id='anchor-status-cards'),

                html.H4("Estimated Position", style={'margin': '15px 0 5px 0'}),
                html.Div(id='position-estimate', style={
                    'padding': '10px', 'background': '#f8f9fa',
                    'borderRadius': '8px', 'fontFamily': 'monospace',
                }),
            ], style={'flex': '1', 'minWidth': '250px', 'padding': '0 15px'}),
        ], style={'display': 'flex', 'padding': '0 10px', 'gap': '10px'}),

        # Bottom row: CSI waterfall + RSSI timeline
        html.Div([
            html.Div([
                html.H4("CSI Amplitude Waterfall", style={'margin': '10px 0 5px 0'}),
                dcc.Graph(id='csi-waterfall'),
            ], style={'flex': '1'}),
            html.Div([
                html.H4("RSSI Timeline", style={'margin': '10px 0 5px 0'}),
                dcc.Graph(id='rssi-timeline'),
            ], style={'flex': '1'}),
        ], style={'display': 'flex', 'padding': '0 10px', 'gap': '10px'}),

        # Auto-refresh interval
        dcc.Interval(id='interval', interval=UPDATE_INTERVAL_MS, n_intervals=0),

    ], style={'fontFamily': 'system-ui, -apple-system, sans-serif',
              'maxWidth': '1400px', 'margin': '0 auto'})

    @app.callback(
        Output('mac-selector', 'options'),
        [Input('refresh-macs-btn', 'n_clicks')],
        prevent_initial_call=True,
    )
    def refresh_mac_list(n_clicks):
        macs = store.get_top_macs(15)
        options = [{"label": "All MACs (mixed)", "value": "__all__"}]
        for m in macs:
            label = f"{m['mac']}  ({m['avg_rssi']} dBm, {m['count']} pkts, {m['n_anchors']} anchors)"
            options.append({"label": label, "value": m["mac"]})
        return options

    @app.callback(
        [Output('room-3d', 'figure'),
         Output('csi-waterfall', 'figure'),
         Output('rssi-timeline', 'figure'),
         Output('status-text', 'children'),
         Output('anchor-status-cards', 'children'),
         Output('position-estimate', 'children')],
        [Input('interval', 'n_intervals')],
        [State('mac-selector', 'value')],
    )
    def update_dashboard(n, selected_mac):
        target_mac = None if selected_mac == "__all__" else selected_mac

        stats = store.get_stats(target_mac)
        recent = store.get_recent(WINDOW_SIZE, target_mac)

        # Per-anchor data
        data_by_anchor = {}
        for aid in anchors:
            data_by_anchor[aid] = store.get_recent_by_anchor(aid, 50, target_mac)

        # Estimate AP position
        ap_pos = estimate_ap_position(recent, anchors)

        # Build figures
        room_fig = build_room_figure(room, anchors, ap_pos)
        waterfall_fig = build_csi_waterfall(data_by_anchor)
        rssi_fig = build_rssi_timeline(data_by_anchor)

        # Status text
        mac_label = f" | MAC: {target_mac}" if target_mac else " | All MACs"
        status = f"{stats['total']} packets{mac_label} | {datetime.now().strftime('%H:%M:%S')}"
        if stats['total'] == 0:
            status = "Waiting for CSI data... Connect ESP32 anchors via BLE receiver"

        # Anchor status cards
        cards = []
        for aid in sorted(anchors.keys()):
            info = stats["per_anchor"].get(aid, {})
            count = info.get("count", 0)
            avg_rssi = info.get("avg_rssi", "—")
            color = '#4daf4a' if count > 0 else '#ccc'
            cards.append(html.Div([
                html.Div(style={
                    'width': '10px', 'height': '10px', 'borderRadius': '50%',
                    'background': color, 'display': 'inline-block', 'marginRight': '8px',
                }),
                html.Span(f"{aid}", style={'fontWeight': 'bold'}),
                html.Span(f"  {count} pkts, RSSI: {avg_rssi}",
                          style={'color': '#666', 'fontSize': '12px'}),
            ], style={'padding': '5px 0'}))

        # Position estimate text
        if ap_pos:
            pos_text = [
                html.Div(f"X: {ap_pos['x']:.2f} m"),
                html.Div(f"Y: {ap_pos['y']:.2f} m"),
                html.Br(),
                html.Div("Per-anchor RSSI:", style={'fontWeight': 'bold', 'fontSize': '12px'}),
            ]
            for aid, rssi_val in sorted(ap_pos.get("rssi", {}).items()):
                pos_text.append(html.Div(f"  {aid}: {rssi_val:.1f} dBm",
                                         style={'fontSize': '12px'}))
        else:
            pos_text = html.Div("Need data from 3+ anchors", style={'color': '#999'})

        return room_fig, waterfall_fig, rssi_fig, status, cards, pos_text

    return app


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="WiLoc Real-time Dashboard")
    parser.add_argument("--db", default="wiloc_ble.db",
                        help="Path to CSI SQLite database")
    parser.add_argument("--room", nargs=3, type=float, default=[4.0, 4.0, 3.0],
                        metavar=("W", "D", "H"),
                        help="Room dimensions in meters (width depth height)")
    parser.add_argument("--port", type=int, default=8050,
                        help="Dashboard web server port")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Host to bind to")
    parser.add_argument("--anchors", help="Path to setup.json with anchor positions")
    args = parser.parse_args()

    room = {"width": args.room[0], "depth": args.room[1], "height": args.room[2]}

    # Load anchors from setup.json if provided
    anchors = dict(DEFAULT_ANCHORS)
    if args.anchors:
        with open(args.anchors) as f:
            setup = json.load(f)
        # If setup.json has position info, use it
        # For now, use defaults (user needs to set anchor positions after placement)
        for dev in setup.get("devices", []):
            did = dev["device_id"]
            if did in anchors:
                anchors[did]["ble_name"] = dev.get("ble_name", "")
                anchors[did]["mac"] = dev.get("base_mac", "")

    print(f"WiLoc Dashboard")
    print(f"  Room: {room['width']}m x {room['depth']}m x {room['height']}m")
    print(f"  Anchors: {list(anchors.keys())}")
    print(f"  Database: {args.db}")
    print(f"  Open http://localhost:{args.port} in your browser")
    print()

    app = create_app(args.db, room, anchors)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
