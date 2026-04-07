"""
WiLoc Dashboard — FastAPI + HTMX + Chart.js
Real-time CSI visualization. Lightweight, no Dash/Plotly overhead.

Usage:
    python dashboard/server.py --db wiloc_ble.db --port 8050
    just dashboard
"""

import argparse
import json
import sqlite3
import numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import uvicorn

PROJECT_ROOT = Path(__file__).parent.parent
TEMPLATES_DIR = Path(__file__).parent / "templates"

# ── Config ──
TARGET_MAC = "d0:cf:13:e0:00:c4"
LIVENESS_WINDOW = 5.0  # seconds

DEFAULT_ANCHORS = {
    "anc_00": {"x": 0.1, "y": 0.1, "z": 1.2},
    "anc_01": {"x": 3.9, "y": 0.1, "z": 1.2},
    "anc_02": {"x": 3.9, "y": 3.9, "z": 1.2},
    "anc_03": {"x": 0.1, "y": 3.9, "z": 1.2},
}

SETTINGS_FILE = Path(__file__).parent / "settings.json"


def load_settings():
    if SETTINGS_FILE.exists():
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    return {"room": {"width": 4.0, "depth": 4.0, "height": 3.0},
            "anchors": DEFAULT_ANCHORS}


# ── DB helpers ──
def get_db(db_path: str):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def query_liveness(conn, target_mac=None, window=LIVENESS_WINDOW):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = (now - timedelta(seconds=window)).isoformat()

    if target_mac:
        recent = conn.execute(
            "SELECT anchor_id, COUNT(*) as cnt, MAX(collected_at) as last "
            "FROM csi_readings WHERE collected_at > ? AND target_mac = ? "
            "GROUP BY anchor_id", (cutoff, target_mac)).fetchall()
        all_last = conn.execute(
            "SELECT anchor_id, MAX(collected_at) as last "
            "FROM csi_readings WHERE target_mac = ? GROUP BY anchor_id",
            (target_mac,)).fetchall()
    else:
        recent = conn.execute(
            "SELECT anchor_id, COUNT(*) as cnt, MAX(collected_at) as last "
            "FROM csi_readings WHERE collected_at > ? GROUP BY anchor_id",
            (cutoff,)).fetchall()
        all_last = conn.execute(
            "SELECT anchor_id, MAX(collected_at) as last "
            "FROM csi_readings GROUP BY anchor_id").fetchall()

    result = {}
    for row in all_last:
        try:
            age = (now - datetime.fromisoformat(row["last"])).total_seconds()
        except Exception:
            age = 9999.0
        result[row["anchor_id"]] = {"rate": 0.0, "age": age, "count": 0}
    for row in recent:
        aid = row["anchor_id"]
        if aid in result:
            result[aid]["rate"] = round(row["cnt"] / window, 1)
            result[aid]["count"] = row["cnt"]
            try:
                result[aid]["age"] = (now - datetime.fromisoformat(row["last"])).total_seconds()
            except Exception:
                pass
    return result


def query_rssi_history(conn, target_mac=None, n=100):
    """Per-anchor RSSI for last n packets."""
    out = {}
    for aid in DEFAULT_ANCHORS:
        if target_mac:
            rows = conn.execute(
                "SELECT rssi FROM csi_readings WHERE anchor_id=? AND target_mac=? "
                "ORDER BY id DESC LIMIT ?", (aid, target_mac, n)).fetchall()
        else:
            rows = conn.execute(
                "SELECT rssi FROM csi_readings WHERE anchor_id=? "
                "ORDER BY id DESC LIMIT ?", (aid, n)).fetchall()
        out[aid] = [r["rssi"] for r in reversed(rows) if r["rssi"] is not None]
    return out


def query_csi_amplitudes(conn, anchor_id, target_mac=None, n=30):
    """CSI amplitude matrix for waterfall."""
    if target_mac:
        rows = conn.execute(
            "SELECT csi_raw FROM csi_readings WHERE anchor_id=? AND target_mac=? "
            "AND csi_raw IS NOT NULL ORDER BY id DESC LIMIT ?",
            (anchor_id, target_mac, n)).fetchall()
    else:
        rows = conn.execute(
            "SELECT csi_raw FROM csi_readings WHERE anchor_id=? "
            "AND csi_raw IS NOT NULL ORDER BY id DESC LIMIT ?",
            (anchor_id, n)).fetchall()

    matrix = []
    for row in reversed(rows):
        try:
            vals = json.loads(row["csi_raw"])
            imag = np.array(vals[0::2], dtype=float)
            real = np.array(vals[1::2], dtype=float)
            amp = np.sqrt(real**2 + imag**2).tolist()
            matrix.append(amp)
        except Exception:
            pass
    return matrix


def query_top_macs(conn, window=30.0, limit=15):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = (now - timedelta(seconds=window)).isoformat()
    rows = conn.execute("""
        SELECT target_mac, COUNT(*) as cnt, ROUND(AVG(rssi),1) as avg_rssi,
               COUNT(DISTINCT anchor_id) as n_anchors
        FROM csi_readings WHERE collected_at > ?
        GROUP BY target_mac ORDER BY cnt DESC LIMIT ?
    """, (cutoff, limit)).fetchall()
    return [dict(r) for r in rows]


def query_stats(conn, target_mac=None):
    if target_mac:
        total = conn.execute("SELECT COUNT(*) FROM csi_readings WHERE target_mac=?",
                             (target_mac,)).fetchone()[0]
    else:
        total = conn.execute("SELECT COUNT(*) FROM csi_readings").fetchone()[0]
    return total


def estimate_position(conn, target_mac, anchors):
    """RSSI trilateration."""
    rssi_by_anchor = {}
    for aid, pos in anchors.items():
        rows = conn.execute(
            "SELECT rssi FROM csi_readings WHERE anchor_id=? AND target_mac=? "
            "ORDER BY id DESC LIMIT 20", (aid, target_mac)).fetchall()
        rssis = [r["rssi"] for r in rows if r["rssi"] is not None]
        if rssis:
            rssi_by_anchor[aid] = float(np.mean(rssis))

    if len(rssi_by_anchor) < 3:
        return None

    from scipy.optimize import least_squares
    anc_pos = []
    dists = []
    for aid, avg_rssi in rssi_by_anchor.items():
        p = anchors[aid]
        anc_pos.append([p["x"], p["y"]])
        d = 10 ** ((-45.0 - avg_rssi) / (10 * 2.5))
        dists.append(d)

    anc_pos = np.array(anc_pos)
    dists = np.array(dists)

    def residuals(pos):
        return np.linalg.norm(anc_pos - pos, axis=1) - dists

    result = least_squares(residuals, anc_pos.mean(axis=0), method='lm')
    return {"x": round(float(result.x[0]), 2), "y": round(float(result.x[1]), 2),
            "rssi": rssi_by_anchor}


# ── App ──
def create_app(db_path: str):
    app = FastAPI(title="WiLoc Dashboard")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.auto_reload = True

    settings = load_settings()

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        conn = get_db(db_path)
        macs = query_top_macs(conn)
        conn.close()
        return templates.TemplateResponse(request, "index.html", {
            "macs": macs, "target_mac": TARGET_MAC,
        })

    @app.get("/api/status")
    async def api_status(mac: str = "__all__"):
        conn = get_db(db_path)
        target = None if mac == "__all__" else mac

        liveness = query_liveness(conn, target)
        tgt_liveness = query_liveness(conn, TARGET_MAC)
        total = query_stats(conn, target)
        cur_anchors = settings.get("anchors", DEFAULT_ANCHORS)
        pos = estimate_position(conn, TARGET_MAC, cur_anchors) if target == TARGET_MAC or target is None else None

        # Target health
        tgt_seeing = sum(1 for v in tgt_liveness.values() if v["rate"] > 0)
        tgt_rate = sum(v["rate"] for v in tgt_liveness.values())

        conn.close()
        return {
            "anchors": {aid: {
                "rate": v["rate"],
                "age": round(v["age"], 1),
                "status": "live" if v["rate"] > 0 and v["age"] < 10 else
                          "stale" if v["age"] < 60 else "dead"
            } for aid, v in liveness.items()},
            "target": {
                "mac": TARGET_MAC,
                "anchors_seeing": tgt_seeing,
                "total_rate": round(tgt_rate, 1),
                "status": "live" if tgt_seeing >= 3 else
                          "partial" if tgt_seeing >= 1 else "dead",
                "per_anchor": {aid: v["rate"] for aid, v in tgt_liveness.items()},
            },
            "total_packets": total,
            "position": pos,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
        }

    @app.get("/api/rssi")
    async def api_rssi(mac: str = "__all__", n: int = 100):
        conn = get_db(db_path)
        target = None if mac == "__all__" else mac
        data = query_rssi_history(conn, target, n)
        conn.close()
        return data

    @app.get("/api/csi")
    async def api_csi(anchor: str = "anc_00", mac: str = "__all__", n: int = 30):
        conn = get_db(db_path)
        target = None if mac == "__all__" else mac
        matrix = query_csi_amplitudes(conn, anchor, target, n)
        conn.close()
        return {"anchor": anchor, "matrix": matrix}

    @app.get("/api/macs")
    async def api_macs():
        conn = get_db(db_path)
        macs = query_top_macs(conn)
        conn.close()
        return macs

    @app.get("/api/settings")
    async def api_get_settings():
        return settings

    @app.post("/api/settings")
    async def api_save_settings(request: Request):
        data = await request.json()
        settings.update(data)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
        return {"message": f"Saved at {datetime.now().strftime('%H:%M:%S')}"}

    return app


def main():
    parser = argparse.ArgumentParser(description="WiLoc Dashboard")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "wiloc_ble.db"))
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    print(f"WiLoc Dashboard")
    print(f"  Database: {args.db}")
    print(f"  http://localhost:{args.port}")

    app = create_app(args.db)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
