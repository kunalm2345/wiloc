"""
WiLoc Dashboard — Real-time room visualization from CSI data.

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
from datetime import datetime
from pathlib import Path

import numpy as np
import plotly.graph_objects as go
from dash import Dash, html, dcc, Input, Output, State, callback, no_update

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "processing"))
sys.path.insert(0, str(Path(__file__).parent.parent / "models"))
sys.path.insert(0, str(Path(__file__).parent.parent / "simulator"))

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

SETTINGS_FILE = Path(__file__).parent / "settings.json"
KNOWN_DEVICES_FILE = Path(__file__).parent / "known_devices.json"

DEFAULT_ROOM = {"width": 4.0, "depth": 4.0, "height": 3.0}
DEFAULT_ANCHORS = {
    "anc_00": {"x": 0.1, "y": 0.1, "z": 1.2},
    "anc_01": {"x": 3.9, "y": 0.1, "z": 1.2},
    "anc_02": {"x": 3.9, "y": 3.9, "z": 1.2},
    "anc_03": {"x": 0.1, "y": 3.9, "z": 1.2},
}

WINDOW_SIZE = 100
UPDATE_INTERVAL_MS = 1000
LIVENESS_WINDOW_SEC = 5.0


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    return {"room": dict(DEFAULT_ROOM), "anchors": {k: dict(v) for k, v in DEFAULT_ANCHORS.items()}}


def save_settings(settings: dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)


def _load_known_devices() -> dict[str, str]:
    """Load MAC -> friendly name mapping from known_devices.json."""
    if KNOWN_DEVICES_FILE.exists():
        with open(KNOWN_DEVICES_FILE) as f:
            return json.load(f)
    return {}


# ──────────────────────────────────────────────
# Database access
# ──────────────────────────────────────────────

class CSIStore:
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
                target_mac TEXT, rssi INTEGER, channel INTEGER,
                bandwidth INTEGER, csi_len INTEGER, csi_raw TEXT,
                label_x REAL, label_y REAL, label_z REAL,
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
                (target_mac, n)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM csi_readings ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_recent_by_anchor(self, anchor_id: str, n: int = 50,
                              target_mac: str = None) -> list[dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        if target_mac:
            rows = conn.execute(
                "SELECT * FROM csi_readings WHERE anchor_id = ? AND target_mac = ? ORDER BY id DESC LIMIT ?",
                (anchor_id, target_mac, n)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM csi_readings WHERE anchor_id = ? ORDER BY id DESC LIMIT ?",
                (anchor_id, n)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_stats(self, target_mac: str = None) -> dict:
        conn = sqlite3.connect(self.db_path)
        if target_mac:
            total = conn.execute(
                "SELECT COUNT(*) FROM csi_readings WHERE target_mac = ?", (target_mac,)).fetchone()[0]
            cursor = conn.execute(
                "SELECT anchor_id, COUNT(*), AVG(rssi), MAX(id) FROM csi_readings WHERE target_mac = ? GROUP BY anchor_id",
                (target_mac,))
        else:
            total = conn.execute("SELECT COUNT(*) FROM csi_readings").fetchone()[0]
            cursor = conn.execute(
                "SELECT anchor_id, COUNT(*), AVG(rssi), MAX(id) FROM csi_readings GROUP BY anchor_id")
        per_anchor = {}
        for row in cursor.fetchall():
            per_anchor[row[0]] = {"count": row[1], "avg_rssi": round(row[2], 1) if row[2] else None, "last_id": row[3]}
        conn.close()
        return {"total": total, "per_anchor": per_anchor}

    def get_anchor_liveness(self, target_mac: str = None, window_sec: float = 5.0) -> dict:
        """Per-anchor: pkt/sec over window + seconds since last packet."""
        conn = sqlite3.connect(self.db_path)
        from datetime import timedelta, timezone
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC to match DB
        cutoff_iso = (now_utc - timedelta(seconds=window_sec)).isoformat()
        if target_mac:
            rows = conn.execute(
                """SELECT anchor_id, COUNT(*), MAX(collected_at)
                   FROM csi_readings WHERE collected_at > ? AND target_mac = ?
                   GROUP BY anchor_id""",
                (cutoff_iso, target_mac)).fetchall()
            # Also get last_seen for anchors with no recent data
            all_last = conn.execute(
                """SELECT anchor_id, MAX(collected_at)
                   FROM csi_readings WHERE target_mac = ? GROUP BY anchor_id""",
                (target_mac,)).fetchall()
        else:
            rows = conn.execute(
                """SELECT anchor_id, COUNT(*), MAX(collected_at)
                   FROM csi_readings WHERE collected_at > ?
                   GROUP BY anchor_id""", (cutoff_iso,)).fetchall()
            all_last = conn.execute(
                "SELECT anchor_id, MAX(collected_at) FROM csi_readings GROUP BY anchor_id").fetchall()
        conn.close()

        result = {}
        # Fill in last_seen for all known anchors
        for aid, last_at in all_last:
            try:
                last_dt = datetime.fromisoformat(last_at)
                age = (now_utc - last_dt).total_seconds()
            except Exception:
                age = 9999.0
            result[aid] = {"rate": 0.0, "age_sec": age}
        # Overlay rate for anchors with recent data
        for aid, count, last_at in rows:
            result[aid]["rate"] = round(count / window_sec, 1)
            try:
                last_dt = datetime.fromisoformat(last_at)
                result[aid]["age_sec"] = (now_utc - last_dt).total_seconds()
            except Exception:
                pass
        return result

    def get_top_macs(self, limit: int = 10) -> list[dict]:
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("""
            SELECT target_mac, COUNT(*) as cnt, AVG(rssi) as avg_rssi,
                   COUNT(DISTINCT anchor_id) as n_anchors
            FROM csi_readings GROUP BY target_mac
            HAVING n_anchors >= 3 ORDER BY avg_rssi DESC LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
        return [{"mac": r[0], "count": r[1], "avg_rssi": round(r[2], 1), "n_anchors": r[3]} for r in rows]


# ──────────────────────────────────────────────
# Signal processing
# ──────────────────────────────────────────────

def csi_raw_to_amplitude(csi_raw_json: str) -> np.ndarray:
    try:
        values = json.loads(csi_raw_json)
        imag = np.array(values[0::2], dtype=np.float64)
        real = np.array(values[1::2], dtype=np.float64)
        return np.abs(real + 1j * imag)
    except Exception:
        return np.array([])


def estimate_distance_from_rssi(rssi: float, rssi_at_1m: float = -45.0,
                                path_loss_exp: float = 2.5) -> float:
    if rssi is None or rssi >= 0:
        return 0.0
    return 10 ** ((rssi_at_1m - rssi) / (10 * path_loss_exp))


def trilaterate_2d(anchors: np.ndarray, distances: np.ndarray) -> np.ndarray:
    from scipy.optimize import least_squares
    def residuals(pos):
        return np.linalg.norm(anchors - pos, axis=1) - distances
    x0 = anchors.mean(axis=0)
    result = least_squares(residuals, x0, method='lm')
    return result.x


def estimate_ap_position(data_by_anchor: dict, anchor_positions: dict) -> dict | None:
    """Estimate AP position from per-anchor CSI data (not a mixed recent window)."""
    rssi_by_anchor = {}
    for aid, rows in data_by_anchor.items():
        if aid not in anchor_positions:
            continue
        rssis = [r["rssi"] for r in rows if r.get("rssi") is not None]
        if rssis:
            rssi_by_anchor[aid] = rssis
    if len(rssi_by_anchor) < 3:
        return None
    anchors, distances, rssi_avgs = [], [], {}
    for aid, rssi_list in rssi_by_anchor.items():
        avg_rssi = np.mean(rssi_list)
        rssi_avgs[aid] = avg_rssi
        pos = anchor_positions[aid]
        anchors.append([pos["x"], pos["y"]])
        distances.append(estimate_distance_from_rssi(avg_rssi))
    try:
        pos = trilaterate_2d(np.array(anchors), np.array(distances))
        return {"x": float(pos[0]), "y": float(pos[1]), "rssi": rssi_avgs}
    except Exception:
        return None


# ──────────────────────────────────────────────
# Plotly figure builders
# ──────────────────────────────────────────────

def build_room_figure(room, anchors, ap_pos=None):
    fig = go.Figure()
    w, d, h = room["width"], room["depth"], room["height"]

    for x in np.arange(0, w + 0.01, 0.5):
        fig.add_trace(go.Scatter3d(x=[x, x], y=[0, d], z=[0, 0],
            mode='lines', line=dict(color='lightgray', width=1), showlegend=False, hoverinfo='skip'))
    for y in np.arange(0, d + 0.01, 0.5):
        fig.add_trace(go.Scatter3d(x=[0, w], y=[y, y], z=[0, 0],
            mode='lines', line=dict(color='lightgray', width=1), showlegend=False, hoverinfo='skip'))

    edges = [
        ([0,0,0],[w,0,0]), ([w,0,0],[w,d,0]), ([w,d,0],[0,d,0]), ([0,d,0],[0,0,0]),
        ([0,0,h],[w,0,h]), ([w,0,h],[w,d,h]), ([w,d,h],[0,d,h]), ([0,d,h],[0,0,h]),
        ([0,0,0],[0,0,h]), ([w,0,0],[w,0,h]), ([w,d,0],[w,d,h]), ([0,d,0],[0,d,h]),
    ]
    for a, b in edges:
        fig.add_trace(go.Scatter3d(x=[a[0],b[0]], y=[a[1],b[1]], z=[a[2],b[2]],
            mode='lines', line=dict(color='gray', width=2), showlegend=False, hoverinfo='skip'))

    ax = [v["x"] for v in anchors.values()]
    ay = [v["y"] for v in anchors.values()]
    az = [v["z"] for v in anchors.values()]
    fig.add_trace(go.Scatter3d(x=ax, y=ay, z=az, mode='markers+text',
        marker=dict(size=6, color='red', symbol='diamond'),
        text=list(anchors.keys()), textposition='top center',
        textfont=dict(size=9, color='red'), name='Anchors',
        hovertext=[f"{k}: ({v['x']}, {v['y']}, {v['z']})" for k, v in anchors.items()], hoverinfo='text'))

    if ap_pos:
        fig.add_trace(go.Scatter3d(x=[ap_pos["x"]], y=[ap_pos["y"]], z=[1.0],
            mode='markers+text', marker=dict(size=10, color='blue', symbol='circle'),
            text=['AP'], textposition='top center', textfont=dict(size=11, color='blue'),
            name='Estimated AP', hovertext=f"AP: ({ap_pos['x']:.2f}, {ap_pos['y']:.2f})", hoverinfo='text'))
        for aid, apos in anchors.items():
            fig.add_trace(go.Scatter3d(x=[ap_pos["x"], apos["x"]], y=[ap_pos["y"], apos["y"]], z=[1.0, apos["z"]],
                mode='lines', line=dict(color='deepskyblue', width=2, dash='dash'), showlegend=False, hoverinfo='skip'))

    fig.update_layout(
        scene=dict(xaxis=dict(title='X (m)', range=[-0.5, w+0.5]),
                   yaxis=dict(title='Y (m)', range=[-0.5, d+0.5]),
                   zaxis=dict(title='Z (m)', range=[-0.1, h+0.3]),
                   aspectmode='data', camera=dict(eye=dict(x=1.3, y=-1.3, z=0.8))),
        margin=dict(l=0, r=0, t=30, b=0), height=500)
    return fig


def build_csi_waterfall(data_by_anchor):
    fig = go.Figure()
    for i, (aid, rows) in enumerate(sorted(data_by_anchor.items())):
        if not rows: continue
        amplitudes = []
        for row in reversed(rows):
            csi_raw = row.get("csi_raw")
            if csi_raw:
                amp = csi_raw_to_amplitude(csi_raw)
                if len(amp) > 0: amplitudes.append(amp)
        if not amplitudes: continue
        max_len = max(len(a) for a in amplitudes)
        matrix = np.zeros((len(amplitudes), max_len))
        for j, a in enumerate(amplitudes): matrix[j, :len(a)] = a
        fig.add_trace(go.Heatmap(z=matrix.T, colorscale='Viridis', name=aid,
            visible=(i == 0), colorbar=dict(title='Amplitude')))

    anchor_ids = sorted(data_by_anchor.keys())
    buttons = []
    for i, aid in enumerate(anchor_ids):
        vis = [False] * len(anchor_ids); vis[i] = True
        buttons.append(dict(label=aid, method='update', args=[{'visible': vis}]))
    fig.update_layout(
        updatemenus=[dict(buttons=buttons, direction='down', x=0.0, xanchor='left', y=1.15, yanchor='top')] if buttons else [],
        xaxis_title='Packet index', yaxis_title='Subcarrier', height=300, margin=dict(l=50, r=20, t=40, b=40))
    return fig


def build_rssi_timeline(data_by_anchor):
    fig = go.Figure()
    colors = {'anc_00': '#e41a1c', 'anc_01': '#377eb8', 'anc_02': '#4daf4a', 'anc_03': '#984ea3'}
    for aid, rows in sorted(data_by_anchor.items()):
        if not rows: continue
        rssis = [r["rssi"] for r in reversed(rows) if r.get("rssi") is not None]
        if rssis:
            fig.add_trace(go.Scatter(y=rssis, mode='lines', name=aid,
                line=dict(color=colors.get(aid, 'gray'), width=2)))
    fig.update_layout(xaxis_title='Packet index', yaxis_title='RSSI (dBm)',
        height=250, margin=dict(l=50, r=20, t=30, b=40), legend=dict(orientation='h', y=1.12))
    return fig


# ──────────────────────────────────────────────
# Dash app
# ──────────────────────────────────────────────

def create_app(db_path: str, room: dict, anchors: dict) -> Dash:
    store = CSIStore(db_path)

    app = Dash(__name__, suppress_callback_exceptions=True)
    app.title = "WiLoc Dashboard"

    # Known device name lookup — add your devices here
    known_devices = _load_known_devices()

    def _mac_label(m: dict) -> str:
        mac = m["mac"]
        name = known_devices.get(mac, "")
        prefix = f"[{name}] " if name else ""
        return f"{prefix}{mac}  ({m['avg_rssi']} dBm, {m['count']} pkts, {m['n_anchors']} anchors)"

    top_macs = store.get_top_macs(15)
    mac_options = [{"label": "All MACs (mixed)", "value": "__all__"}]
    for m in top_macs:
        mac_options.append({"label": _mac_label(m), "value": m["mac"]})
    default_mac = "__all__"
    for m in top_macs:
        if m["n_anchors"] >= 4:
            default_mac = m["mac"]
            break

    # ── Shared state via dcc.Store ──
    app.layout = html.Div([
        dcc.Store(id='settings-store', data={"room": room, "anchors": anchors}),
        dcc.Location(id='url', refresh=False),
        html.Div(id='page-content'),
    ])

    # ── Page router ──
    @app.callback(Output('page-content', 'children'), [Input('url', 'pathname')])
    def render_page(pathname):
        if pathname == '/settings':
            return settings_layout(room, anchors)
        return main_layout(mac_options, default_mac)

    # ── Main dashboard layout ──
    def main_layout(mac_opts, default_m):
        return html.Div([
            # Header
            html.Div([
                html.H2("WiLoc — Room Visualizer", style={'margin': '0', 'color': '#333'}),
                html.Div([
                    html.Span(id='status-text', style={'color': '#666', 'fontSize': '14px', 'marginRight': '15px'}),
                    dcc.Link('Settings', href='/settings',
                             style={'color': '#007bff', 'textDecoration': 'none', 'fontSize': '14px'}),
                ]),
            ], style={'display': 'flex', 'justifyContent': 'space-between',
                      'alignItems': 'center', 'padding': '10px 20px', 'borderBottom': '2px solid #eee'}),

            # MAC selector
            html.Div([
                html.Label("Target device (AP):", style={'fontWeight': 'bold', 'marginRight': '10px'}),
                dcc.Dropdown(id='mac-selector', options=mac_opts, value=default_m,
                    style={'width': '600px', 'fontFamily': 'monospace', 'fontSize': '13px'}, clearable=False),
                html.Button("Refresh MACs", id='refresh-macs-btn', n_clicks=0,
                    style={'marginLeft': '10px', 'padding': '5px 15px'}),
            ], style={'display': 'flex', 'alignItems': 'center', 'padding': '10px 20px',
                      'background': '#f8f9fa', 'borderBottom': '1px solid #eee'}),

            # Main content
            html.Div([
                html.Div([
                    html.H4("Room View", style={'margin': '10px 0 5px 0'}),
                    dcc.Graph(id='room-3d', config={'displayModeBar': True}),
                ], style={'flex': '3', 'minWidth': '500px'}),
                html.Div([
                    html.H4("Anchor Status", style={'margin': '10px 0 5px 0'}),
                    html.Div(id='anchor-status-cards'),
                    html.H4("Estimated Position", style={'margin': '15px 0 5px 0'}),
                    html.Div(id='position-estimate', style={
                        'padding': '10px', 'background': '#f8f9fa',
                        'borderRadius': '8px', 'fontFamily': 'monospace'}),
                ], style={'flex': '1', 'minWidth': '280px', 'padding': '0 15px'}),
            ], style={'display': 'flex', 'padding': '0 10px', 'gap': '10px'}),

            # Bottom row
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

            dcc.Interval(id='interval', interval=UPDATE_INTERVAL_MS, n_intervals=0),
        ], style={'fontFamily': 'system-ui, -apple-system, sans-serif', 'maxWidth': '1400px', 'margin': '0 auto'})

    # ── Settings page layout ──
    def settings_layout(cur_room, cur_anchors):
        anchor_inputs = []
        for aid in sorted(cur_anchors.keys()):
            a = cur_anchors[aid]
            anchor_inputs.append(html.Div([
                html.Label(aid, style={'fontWeight': 'bold', 'width': '60px', 'display': 'inline-block'}),
                html.Label("X:", style={'marginLeft': '10px'}),
                dcc.Input(id=f'{aid}-x', type='number', value=a['x'], step=0.1,
                          style={'width': '70px', 'marginLeft': '4px'}),
                html.Label("Y:", style={'marginLeft': '10px'}),
                dcc.Input(id=f'{aid}-y', type='number', value=a['y'], step=0.1,
                          style={'width': '70px', 'marginLeft': '4px'}),
                html.Label("Z:", style={'marginLeft': '10px'}),
                dcc.Input(id=f'{aid}-z', type='number', value=a['z'], step=0.1,
                          style={'width': '70px', 'marginLeft': '4px'}),
            ], style={'padding': '6px 0'}))

        return html.Div([
            html.Div([
                dcc.Link('< Back to Dashboard', href='/',
                         style={'color': '#007bff', 'textDecoration': 'none', 'fontSize': '14px'}),
                html.H2("Settings", style={'margin': '10px 0'}),
            ], style={'padding': '10px 20px', 'borderBottom': '2px solid #eee'}),

            html.Div([
                # Room dimensions
                html.Div([
                    html.H4("Room Dimensions"),
                    html.Div([
                        html.Label("Width (m):"),
                        dcc.Input(id='room-width', type='number', value=cur_room['width'],
                                  step=0.1, style={'width': '80px', 'marginLeft': '8px'}),
                        html.Label("Depth (m):", style={'marginLeft': '20px'}),
                        dcc.Input(id='room-depth', type='number', value=cur_room['depth'],
                                  step=0.1, style={'width': '80px', 'marginLeft': '8px'}),
                        html.Label("Height (m):", style={'marginLeft': '20px'}),
                        dcc.Input(id='room-height', type='number', value=cur_room['height'],
                                  step=0.1, style={'width': '80px', 'marginLeft': '8px'}),
                    ], style={'padding': '8px 0'}),
                ], style={'marginBottom': '20px'}),

                # Anchor positions
                html.Div([
                    html.H4("Anchor Positions (meters from room origin corner)"),
                    *anchor_inputs,
                ], style={'marginBottom': '20px'}),

                html.Button("Save Settings", id='save-settings-btn', n_clicks=0,
                            style={'padding': '8px 24px', 'background': '#007bff', 'color': 'white',
                                   'border': 'none', 'borderRadius': '4px', 'cursor': 'pointer',
                                   'fontSize': '14px'}),
                html.Div(id='save-status', style={'marginTop': '10px', 'color': '#28a745'}),
            ], style={'padding': '20px', 'maxWidth': '700px'}),
        ], style={'fontFamily': 'system-ui, -apple-system, sans-serif', 'maxWidth': '1400px', 'margin': '0 auto'})

    # ── Settings save callback ──
    anchor_ids = sorted(anchors.keys())
    settings_inputs = [Input('save-settings-btn', 'n_clicks')]
    settings_states = [
        State('room-width', 'value'), State('room-depth', 'value'), State('room-height', 'value'),
    ]
    for aid in anchor_ids:
        settings_states.extend([
            State(f'{aid}-x', 'value'), State(f'{aid}-y', 'value'), State(f'{aid}-z', 'value'),
        ])

    @app.callback(
        [Output('save-status', 'children'), Output('settings-store', 'data')],
        settings_inputs, settings_states, prevent_initial_call=True,
    )
    def save_settings_cb(n_clicks, room_w, room_d, room_h, *anchor_vals):
        new_room = {"width": float(room_w), "depth": float(room_d), "height": float(room_h)}
        new_anchors = {}
        for i, aid in enumerate(anchor_ids):
            new_anchors[aid] = {
                "x": float(anchor_vals[i*3]),
                "y": float(anchor_vals[i*3 + 1]),
                "z": float(anchor_vals[i*3 + 2]),
            }
        settings = {"room": new_room, "anchors": new_anchors}
        save_settings(settings)
        # Update the live references
        room.update(new_room)
        anchors.clear()
        anchors.update(new_anchors)
        return f"Saved at {datetime.now().strftime('%H:%M:%S')}", settings

    # ── Refresh MACs ──
    @app.callback(Output('mac-selector', 'options'),
                  [Input('refresh-macs-btn', 'n_clicks')], prevent_initial_call=True)
    def refresh_mac_list(n_clicks):
        refreshed_names = _load_known_devices()
        macs = store.get_top_macs(15)
        options = [{"label": "All MACs (mixed)", "value": "__all__"}]
        for m in macs:
            name = refreshed_names.get(m["mac"], "")
            prefix = f"[{name}] " if name else ""
            label = f"{prefix}{m['mac']}  ({m['avg_rssi']} dBm, {m['count']} pkts, {m['n_anchors']} anchors)"
            options.append({"label": label, "value": m["mac"]})
        return options

    # ── Main dashboard update ──
    @app.callback(
        [Output('room-3d', 'figure'), Output('csi-waterfall', 'figure'),
         Output('rssi-timeline', 'figure'), Output('status-text', 'children'),
         Output('anchor-status-cards', 'children'), Output('position-estimate', 'children')],
        [Input('interval', 'n_intervals')],
        [State('mac-selector', 'value'), State('settings-store', 'data')],
    )
    def update_dashboard(n, selected_mac, settings_data):
        cur_room = settings_data["room"] if settings_data else room
        cur_anchors = settings_data["anchors"] if settings_data else anchors
        target_mac = None if selected_mac == "__all__" else selected_mac

        stats = store.get_stats(target_mac)
        recent = store.get_recent(WINDOW_SIZE, target_mac)
        liveness = store.get_anchor_liveness(target_mac, window_sec=LIVENESS_WINDOW_SEC)

        data_by_anchor = {}
        for aid in cur_anchors:
            data_by_anchor[aid] = store.get_recent_by_anchor(aid, 50, target_mac)

        ap_pos = estimate_ap_position(data_by_anchor, cur_anchors)

        room_fig = build_room_figure(cur_room, cur_anchors, ap_pos)
        waterfall_fig = build_csi_waterfall(data_by_anchor)
        rssi_fig = build_rssi_timeline(data_by_anchor)

        mac_label = f" | MAC: {target_mac}" if target_mac else " | All MACs"
        status = f"{stats['total']} packets{mac_label} | {datetime.now().strftime('%H:%M:%S')}"
        if stats['total'] == 0:
            status = "Waiting for CSI data..."

        stale_threshold = LIVENESS_WINDOW_SEC * 2  # 2x liveness window = stale

        # Anchor status cards with accurate liveness
        cards = []
        for aid in sorted(cur_anchors.keys()):
            info = stats["per_anchor"].get(aid, {})
            count = info.get("count", 0)
            avg_rssi = info.get("avg_rssi", "—")
            live = liveness.get(aid, {"rate": 0.0, "age_sec": 9999})
            rate = live["rate"]
            age = live["age_sec"]

            if rate > 0 and age < stale_threshold:
                color = '#4daf4a'  # green — live data flowing
                age_text = f"{rate} pkt/s"
            elif count > 0 and age < 30:
                color = '#ffc107'  # yellow — recent but slowed
                age_text = f"last {age:.0f}s ago"
            elif count > 0:
                color = '#dc3545'  # red — stale
                age_text = f"stale ({age:.0f}s ago)"
            else:
                color = '#ccc'     # grey — never seen
                age_text = "no data"

            cards.append(html.Div([
                html.Div(style={
                    'width': '10px', 'height': '10px', 'borderRadius': '50%',
                    'background': color, 'display': 'inline-block', 'marginRight': '8px'}),
                html.Span(f"{aid}", style={'fontWeight': 'bold'}),
                html.Span(f"  {age_text}",
                          style={'color': color, 'fontSize': '12px', 'fontWeight': 'bold', 'marginLeft': '6px'}),
                html.Br(),
                html.Span(f"  {count} total, RSSI: {avg_rssi} dBm",
                          style={'color': '#666', 'fontSize': '11px', 'marginLeft': '18px'}),
            ], style={'padding': '5px 0', 'lineHeight': '1.4'}))

        if ap_pos:
            pos_text = [
                html.Div(f"X: {ap_pos['x']:.2f} m"),
                html.Div(f"Y: {ap_pos['y']:.2f} m"),
                html.Br(),
                html.Div("Per-anchor RSSI:", style={'fontWeight': 'bold', 'fontSize': '12px'}),
            ]
            for aid, rssi_val in sorted(ap_pos.get("rssi", {}).items()):
                pos_text.append(html.Div(f"  {aid}: {rssi_val:.1f} dBm", style={'fontSize': '12px'}))
        else:
            pos_text = html.Div("Need data from 3+ anchors", style={'color': '#999'})

        return room_fig, waterfall_fig, rssi_fig, status, cards, pos_text

    return app


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="WiLoc Real-time Dashboard")
    parser.add_argument("--db", default="wiloc_ble.db")
    parser.add_argument("--room", nargs=3, type=float, default=None, metavar=("W", "D", "H"))
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    settings = load_settings()
    room = settings["room"]
    anchors = settings["anchors"]

    if args.room:
        room = {"width": args.room[0], "depth": args.room[1], "height": args.room[2]}

    print(f"WiLoc Dashboard")
    print(f"  Room: {room['width']}m x {room['depth']}m x {room['height']}m")
    print(f"  Anchors: {list(anchors.keys())}")
    print(f"  Database: {args.db}")
    print(f"  Dashboard: http://localhost:{args.port}")
    print(f"  Settings:  http://localhost:{args.port}/settings")
    print()

    app = create_app(args.db, room, anchors)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
