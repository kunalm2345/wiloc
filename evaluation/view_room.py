"""
Room Visualizer — interactive 3D view in your browser. No Blender needed.

Uses plotly for interactive 3D (rotate, zoom, pan) that opens in any browser.
Falls back to matplotlib if plotly isn't installed.

Usage:
    python view_room.py                          # default room
    python view_room.py --config room.json       # from exported JSON
    python view_room.py --ap 2.0 2.0 1.0         # show AP at position
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "simulator"))
from room_model import Room, Box, Anchor, create_default_room, export_json


# Color scheme
COLORS = {
    "wall": "rgba(180, 180, 190, 0.15)",
    "wall_edge": "rgba(100, 100, 110, 0.6)",
    "floor": "rgba(210, 200, 180, 0.3)",
    "ceiling": "rgba(220, 220, 230, 0.1)",
    "wood": "rgba(160, 120, 60, 0.6)",
    "metal": "rgba(150, 150, 160, 0.7)",
    "anchor": "rgba(220, 50, 50, 1.0)",
    "ap": "rgba(50, 150, 220, 1.0)",
    "grid": "rgba(200, 200, 200, 0.3)",
}


def _box_mesh(mn, mx):
    """Generate vertices and triangle indices for a box."""
    x = [mn[0], mx[0]]
    y = [mn[1], mx[1]]
    z = [mn[2], mx[2]]

    # 8 vertices
    verts = np.array([
        [x[0], y[0], z[0]], [x[1], y[0], z[0]], [x[1], y[1], z[0]], [x[0], y[1], z[0]],
        [x[0], y[0], z[1]], [x[1], y[0], z[1]], [x[1], y[1], z[1]], [x[0], y[1], z[1]],
    ])

    # 12 triangles (2 per face)
    faces = np.array([
        [0,1,5], [0,5,4],  # front
        [2,3,7], [2,7,6],  # back
        [0,3,7], [0,7,4],  # left
        [1,2,6], [1,6,5],  # right
        [4,5,6], [4,6,7],  # top
        [0,1,2], [0,2,3],  # bottom
    ])

    return verts, faces


def _box_edges(mn, mx):
    """Generate edge lines for a box (12 edges)."""
    x0, y0, z0 = mn
    x1, y1, z1 = mx

    corners = [
        [x0,y0,z0], [x1,y0,z0], [x1,y1,z0], [x0,y1,z0],
        [x0,y0,z1], [x1,y0,z1], [x1,y1,z1], [x0,y1,z1],
    ]

    edges = [
        (0,1),(1,2),(2,3),(3,0),  # bottom
        (4,5),(5,6),(6,7),(7,4),  # top
        (0,4),(1,5),(2,6),(3,7),  # verticals
    ]

    xs, ys, zs = [], [], []
    for a, b in edges:
        xs += [corners[a][0], corners[b][0], None]
        ys += [corners[a][1], corners[b][1], None]
        zs += [corners[a][2], corners[b][2], None]

    return xs, ys, zs


def view_plotly(room: Room, ap_pos: list = None, title: str = "WiLoc Room View"):
    """Interactive 3D visualization using plotly (opens in browser)."""
    import plotly.graph_objects as go

    fig = go.Figure()

    # ── Floor grid ──
    grid_step = 0.5
    for x in np.arange(0, room.width + 0.01, grid_step):
        fig.add_trace(go.Scatter3d(
            x=[x, x], y=[0, room.depth], z=[0, 0],
            mode='lines', line=dict(color='lightgray', width=1),
            showlegend=False, hoverinfo='skip',
        ))
    for y in np.arange(0, room.depth + 0.01, grid_step):
        fig.add_trace(go.Scatter3d(
            x=[0, room.width], y=[y, y], z=[0, 0],
            mode='lines', line=dict(color='lightgray', width=1),
            showlegend=False, hoverinfo='skip',
        ))

    # ── Walls (transparent meshes + edge lines) ──
    wall_defs = [
        # (name, 4 corners)
        ("Front wall",  [[0,0,0],[room.width,0,0],[room.width,0,room.height],[0,0,room.height]]),
        ("Back wall",   [[0,room.depth,0],[room.width,room.depth,0],[room.width,room.depth,room.height],[0,room.depth,room.height]]),
        ("Left wall",   [[0,0,0],[0,room.depth,0],[0,room.depth,room.height],[0,0,room.height]]),
        ("Right wall",  [[room.width,0,0],[room.width,room.depth,0],[room.width,room.depth,room.height],[room.width,0,room.height]]),
    ]

    for name, corners in wall_defs:
        c = np.array(corners)
        fig.add_trace(go.Mesh3d(
            x=c[:,0], y=c[:,1], z=c[:,2],
            i=[0, 0], j=[1, 2], k=[2, 3],
            color=COLORS["wall"], opacity=0.12,
            name=name, showlegend=False, hoverinfo='name',
        ))
        # wall edges
        xs = list(c[:,0]) + [c[0,0]]
        ys = list(c[:,1]) + [c[0,1]]
        zs = list(c[:,2]) + [c[0,2]]
        fig.add_trace(go.Scatter3d(
            x=xs, y=ys, z=zs,
            mode='lines', line=dict(color='gray', width=2),
            showlegend=False, hoverinfo='skip',
        ))

    # ── Floor ──
    fc = np.array([[0,0,0],[room.width,0,0],[room.width,room.depth,0],[0,room.depth,0]])
    fig.add_trace(go.Mesh3d(
        x=fc[:,0], y=fc[:,1], z=fc[:,2],
        i=[0, 0], j=[1, 2], k=[2, 3],
        color=COLORS["floor"], opacity=0.25,
        name="Floor", showlegend=False, hoverinfo='name',
    ))

    # ── Obstacles ──
    for obs in room.obstacles:
        mn = obs.min_corner
        mx = obs.max_corner
        verts, faces = _box_mesh(mn, mx)
        color = COLORS.get(obs.material, "rgba(150, 150, 150, 0.5)")

        fig.add_trace(go.Mesh3d(
            x=verts[:,0], y=verts[:,1], z=verts[:,2],
            i=faces[:,0], j=faces[:,1], k=faces[:,2],
            color=color, opacity=0.6,
            name=obs.name, showlegend=True,
            hovertext=f"{obs.name}<br>{obs.width:.1f}×{obs.depth:.1f}×{obs.height:.1f}m<br>Material: {obs.material}",
            hoverinfo='text',
        ))

        # obstacle edges
        ex, ey, ez = _box_edges(mn, mx)
        fig.add_trace(go.Scatter3d(
            x=ex, y=ey, z=ez,
            mode='lines', line=dict(color='saddlebrown', width=3),
            showlegend=False, hoverinfo='skip',
        ))

    # ── Anchors ──
    ax = [a.x for a in room.anchors]
    ay = [a.y for a in room.anchors]
    az = [a.z for a in room.anchors]
    labels = [f"{a.id}<br>({a.x:.1f}, {a.y:.1f}, {a.z:.1f})" for a in room.anchors]

    fig.add_trace(go.Scatter3d(
        x=ax, y=ay, z=az,
        mode='markers+text',
        marker=dict(size=8, color='red', symbol='diamond'),
        text=[a.id for a in room.anchors],
        textposition='top center',
        textfont=dict(size=10, color='red'),
        name='ESP32 Anchors',
        hovertext=labels, hoverinfo='text',
    ))

    # ── AP target ──
    if ap_pos:
        fig.add_trace(go.Scatter3d(
            x=[ap_pos[0]], y=[ap_pos[1]], z=[ap_pos[2]],
            mode='markers+text',
            marker=dict(size=12, color='blue', symbol='circle'),
            text=['AP'],
            textposition='top center',
            textfont=dict(size=12, color='blue'),
            name='Target AP',
            hovertext=f"AP<br>({ap_pos[0]:.2f}, {ap_pos[1]:.2f}, {ap_pos[2]:.2f})",
            hoverinfo='text',
        ))

        # Draw lines from AP to each anchor (signal paths)
        for a in room.anchors:
            dist = np.sqrt((a.x-ap_pos[0])**2 + (a.y-ap_pos[1])**2 + (a.z-ap_pos[2])**2)
            fig.add_trace(go.Scatter3d(
                x=[ap_pos[0], a.x], y=[ap_pos[1], a.y], z=[ap_pos[2], a.z],
                mode='lines',
                line=dict(color='deepskyblue', width=2, dash='dash'),
                showlegend=False,
                hovertext=f"→ {a.id}: {dist:.2f}m",
                hoverinfo='text',
            ))

    # ── Dimension annotations ──
    # Width arrow along x
    fig.add_trace(go.Scatter3d(
        x=[0, room.width], y=[-0.3, -0.3], z=[0, 0],
        mode='lines+text',
        line=dict(color='black', width=2),
        text=[f'{room.width:.1f}m', ''],
        textposition='top center',
        showlegend=False, hoverinfo='skip',
    ))
    # Depth arrow along y
    fig.add_trace(go.Scatter3d(
        x=[-0.3, -0.3], y=[0, room.depth], z=[0, 0],
        mode='lines+text',
        line=dict(color='black', width=2),
        text=[f'{room.depth:.1f}m', ''],
        textposition='middle right',
        showlegend=False, hoverinfo='skip',
    ))

    # ── Layout ──
    fig.update_layout(
        title=dict(text=title, font=dict(size=18)),
        scene=dict(
            xaxis=dict(title='X (m)', range=[-0.5, room.width + 0.5]),
            yaxis=dict(title='Y (m)', range=[-0.5, room.depth + 0.5]),
            zaxis=dict(title='Z (m)', range=[-0.1, room.height + 0.3]),
            aspectmode='data',
            camera=dict(
                eye=dict(x=1.5, y=-1.5, z=1.0),
                up=dict(x=0, y=0, z=1),
            ),
        ),
        legend=dict(x=0.02, y=0.98),
        margin=dict(l=0, r=0, t=40, b=0),
        width=1000,
        height=700,
    )

    fig.show()
    print("Room visualization opened in browser.")


def view_matplotlib(room: Room, ap_pos: list = None, title: str = "WiLoc Room View"):
    """Fallback 3D visualization using matplotlib."""
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    fig = plt.figure(figsize=(12, 8))
    ax = fig.add_subplot(111, projection='3d')

    # Floor
    floor_verts = [[
        [0, 0, 0], [room.width, 0, 0],
        [room.width, room.depth, 0], [0, room.depth, 0]
    ]]
    ax.add_collection3d(Poly3DCollection(floor_verts, alpha=0.15, facecolor='tan', edgecolor='gray'))

    # Walls (transparent)
    wall_sets = [
        [[0,0,0],[room.width,0,0],[room.width,0,room.height],[0,0,room.height]],
        [[0,room.depth,0],[room.width,room.depth,0],[room.width,room.depth,room.height],[0,room.depth,room.height]],
        [[0,0,0],[0,room.depth,0],[0,room.depth,room.height],[0,0,room.height]],
        [[room.width,0,0],[room.width,room.depth,0],[room.width,room.depth,room.height],[room.width,0,room.height]],
    ]
    ax.add_collection3d(Poly3DCollection(wall_sets, alpha=0.05, facecolor='lightblue', edgecolor='gray', linewidth=0.5))

    # Obstacles
    for obs in room.obstacles:
        mn = obs.min_corner
        mx = obs.max_corner
        # 6 faces
        faces = [
            [[mn[0],mn[1],mn[2]],[mx[0],mn[1],mn[2]],[mx[0],mn[1],mx[2]],[mn[0],mn[1],mx[2]]],
            [[mn[0],mx[1],mn[2]],[mx[0],mx[1],mn[2]],[mx[0],mx[1],mx[2]],[mn[0],mx[1],mx[2]]],
            [[mn[0],mn[1],mn[2]],[mn[0],mx[1],mn[2]],[mn[0],mx[1],mx[2]],[mn[0],mn[1],mx[2]]],
            [[mx[0],mn[1],mn[2]],[mx[0],mx[1],mn[2]],[mx[0],mx[1],mx[2]],[mx[0],mn[1],mx[2]]],
            [[mn[0],mn[1],mx[2]],[mx[0],mn[1],mx[2]],[mx[0],mx[1],mx[2]],[mn[0],mx[1],mx[2]]],
            [[mn[0],mn[1],mn[2]],[mx[0],mn[1],mn[2]],[mx[0],mx[1],mn[2]],[mn[0],mx[1],mn[2]]],
        ]
        color = 'peru' if obs.material == 'wood' else 'silver'
        ax.add_collection3d(Poly3DCollection(faces, alpha=0.4, facecolor=color, edgecolor='saddlebrown', linewidth=1))
        ax.text(obs.x, obs.y, mx[2] + 0.1, obs.name, fontsize=8, ha='center')

    # Anchors
    for a in room.anchors:
        ax.scatter(a.x, a.y, a.z, c='red', s=80, marker='^', zorder=5)
        ax.text(a.x, a.y, a.z + 0.15, a.id, fontsize=7, ha='center', color='red')

    # AP
    if ap_pos:
        ax.scatter(ap_pos[0], ap_pos[1], ap_pos[2], c='blue', s=120, marker='o', zorder=5)
        ax.text(ap_pos[0], ap_pos[1], ap_pos[2] + 0.15, 'AP', fontsize=10, ha='center', color='blue', weight='bold')
        for a in room.anchors:
            ax.plot([ap_pos[0], a.x], [ap_pos[1], a.y], [ap_pos[2], a.z],
                    'b--', alpha=0.3, linewidth=1)

    ax.set_xlabel('X (m)')
    ax.set_ylabel('Y (m)')
    ax.set_zlabel('Z (m)')
    ax.set_title(title)
    ax.set_xlim(-0.3, room.width + 0.3)
    ax.set_ylim(-0.3, room.depth + 0.3)
    ax.set_zlim(-0.1, room.height + 0.2)

    plt.tight_layout()
    plt.savefig("room_view.png", dpi=150)
    print("Saved to room_view.png")
    plt.show()


def load_room_from_json(path: str) -> Room:
    """Load room from our JSON export format."""
    with open(path) as f:
        data = json.load(f)

    r = data["room"]
    room = Room(
        width=r["width"], depth=r["depth"], height=r["height"],
        wall_material=r["wall_material"],
        floor_material=r["floor_material"],
        ceiling_material=r["ceiling_material"],
    )
    for o in data["obstacles"]:
        room.add_obstacle(Box(
            name=o["name"], x=o["x"], y=o["y"], z=o["z"],
            width=o["width"], depth=o["depth"], height=o["height"],
            material=o["material"],
        ))
    for a in data["anchors"]:
        room.add_anchor(Anchor(id=a["id"], x=a["x"], y=a["y"], z=a["z"]))

    return room


def main():
    parser = argparse.ArgumentParser(description="View room in browser (no Blender)")
    parser.add_argument("--config", help="Room JSON file (from room_model.py export)")
    parser.add_argument("--ap", nargs=3, type=float, metavar=("X", "Y", "Z"),
                        help="Show AP at position (x y z)")
    parser.add_argument("--backend", choices=["plotly", "matplotlib", "auto"], default="auto",
                        help="Rendering backend")
    args = parser.parse_args()

    # Load room
    if args.config:
        room = load_room_from_json(args.config)
        print(f"Loaded room from {args.config}")
    else:
        room = create_default_room()
        print("Using default 4x4m room")

    ap_pos = args.ap
    print(f"Room: {room.width}×{room.depth}×{room.height}m")
    print(f"Anchors: {len(room.anchors)}, Obstacles: {len(room.obstacles)}")
    if ap_pos:
        print(f"AP at: ({ap_pos[0]}, {ap_pos[1]}, {ap_pos[2]})")

    # Pick backend
    backend = args.backend
    if backend == "auto":
        try:
            import plotly
            backend = "plotly"
        except ImportError:
            backend = "matplotlib"

    if backend == "plotly":
        view_plotly(room, ap_pos)
    else:
        view_matplotlib(room, ap_pos)


if __name__ == "__main__":
    main()
