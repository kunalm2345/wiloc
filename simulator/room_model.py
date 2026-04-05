"""
Room Simulator — generates a 3D room model with obstacles for WiFi ray-tracing.

Two modes:
1. Export a Blender-compatible scene (.obj + .mtl) for visualization
2. Generate a Sionna-compatible scene (.xml) for RF ray-tracing

Your 4x4m room with 4 ESP32 anchors at corners, 1 AP target in the middle.
"""

import json
import math
import numpy as np
from dataclasses import dataclass, field
from pathlib import Path


# --- Material RF properties (at 2.4 GHz) ---
# relative_permittivity, conductivity (S/m)
RF_MATERIALS = {
    "concrete":    {"permittivity": 5.31, "conductivity": 0.0326, "thickness": 0.15},
    "brick":       {"permittivity": 3.75, "conductivity": 0.038,  "thickness": 0.12},
    "drywall":     {"permittivity": 2.94, "conductivity": 0.0116, "thickness": 0.013},
    "wood":        {"permittivity": 1.99, "conductivity": 0.0047, "thickness": 0.03},
    "glass":       {"permittivity": 6.27, "conductivity": 0.0043, "thickness": 0.006},
    "metal":       {"permittivity": 1.0,  "conductivity": 1e7,    "thickness": 0.002},
    "floor_tile":  {"permittivity": 3.0,  "conductivity": 0.01,   "thickness": 0.01},
    "air":         {"permittivity": 1.0,  "conductivity": 0.0,    "thickness": 0.0},
}


@dataclass
class Box:
    """Axis-aligned box obstacle."""
    name: str
    x: float          # center x
    y: float          # center y
    z: float          # center z (height center)
    width: float      # along x
    depth: float      # along y
    height: float     # along z
    material: str = "wood"

    @property
    def min_corner(self):
        return (self.x - self.width/2, self.y - self.depth/2, self.z - self.height/2)

    @property
    def max_corner(self):
        return (self.x + self.width/2, self.y + self.depth/2, self.z + self.height/2)


@dataclass
class Anchor:
    """Fixed ESP32 station at known position."""
    id: str
    x: float
    y: float
    z: float


@dataclass
class Room:
    """Room model with walls, floor, ceiling, and obstacles."""
    width: float = 4.0          # x dimension (meters)
    depth: float = 4.0          # y dimension
    height: float = 3.0         # z dimension (floor to ceiling)
    wall_material: str = "concrete"
    floor_material: str = "floor_tile"
    ceiling_material: str = "concrete"
    obstacles: list[Box] = field(default_factory=list)
    anchors: list[Anchor] = field(default_factory=list)

    def add_obstacle(self, obs: Box):
        self.obstacles.append(obs)

    def add_anchor(self, anchor: Anchor):
        self.anchors.append(anchor)


def create_default_room() -> Room:
    """
    Your 4x4m room setup:
    - 4 ESP32 anchors at corners (at ~1.2m height, mounted on walls)
    - Table in the center-ish area
    - Almirah (wardrobe) against one wall
    """
    room = Room(width=4.0, depth=4.0, height=3.0)

    # Corner offset — anchors slightly inset from walls
    offset = 0.1
    h = 1.2  # mounting height

    room.add_anchor(Anchor("anchor_00", offset,              offset,              h))
    room.add_anchor(Anchor("anchor_01", room.width - offset, offset,              h))
    room.add_anchor(Anchor("anchor_02", room.width - offset, room.depth - offset, h))
    room.add_anchor(Anchor("anchor_03", offset,              room.depth - offset, h))

    # Table: ~1.2m x 0.6m, 0.75m tall, roughly center of room
    room.add_obstacle(Box(
        name="table",
        x=2.0, y=2.0, z=0.375,
        width=1.2, depth=0.6, height=0.75,
        material="wood",
    ))

    # Almirah (wardrobe): ~1.0m x 0.5m, 1.8m tall, against back wall
    room.add_obstacle(Box(
        name="almirah",
        x=3.2, y=3.75, z=0.9,
        width=1.0, depth=0.5, height=1.8,
        material="wood",
    ))

    return room


# ──────────────────────────────────────────────
# OBJ Export (for Blender visualization)
# ──────────────────────────────────────────────

def _box_to_obj_faces(box: Box, vertex_offset: int) -> tuple[list[str], list[str]]:
    """Generate OBJ vertices and faces for a box."""
    mn = box.min_corner
    mx = box.max_corner

    # 8 vertices of the box
    verts = [
        (mn[0], mn[1], mn[2]),  # 0: front-bottom-left
        (mx[0], mn[1], mn[2]),  # 1: front-bottom-right
        (mx[0], mx[1], mn[2]),  # 2: back-bottom-right
        (mn[0], mx[1], mn[2]),  # 3: back-bottom-left
        (mn[0], mn[1], mx[2]),  # 4: front-top-left
        (mx[0], mn[1], mx[2]),  # 5: front-top-right
        (mx[0], mx[1], mx[2]),  # 6: back-top-right
        (mn[0], mx[1], mx[2]),  # 7: back-top-left
    ]

    v_lines = [f"v {v[0]:.4f} {v[2]:.4f} {v[1]:.4f}" for v in verts]  # swap y/z for Blender

    o = vertex_offset  # 1-based
    faces = [
        f"f {o+0} {o+1} {o+5} {o+4}",  # front
        f"f {o+2} {o+3} {o+7} {o+6}",  # back
        f"f {o+0} {o+3} {o+7} {o+4}",  # left
        f"f {o+1} {o+2} {o+6} {o+5}",  # right
        f"f {o+4} {o+5} {o+6} {o+7}",  # top
        f"f {o+0} {o+1} {o+2} {o+3}",  # bottom
    ]

    return v_lines, faces


def export_obj(room: Room, path: str):
    """Export room as Wavefront OBJ file for Blender import."""
    lines = ["# WiLoc Room Model", f"# Room: {room.width}x{room.depth}x{room.height}m", ""]

    vertex_count = 0

    # Floor
    lines.append("o Floor")
    lines.append(f"v 0 0 0")
    lines.append(f"v {room.width} 0 0")
    lines.append(f"v {room.width} 0 {room.depth}")
    lines.append(f"v 0 0 {room.depth}")
    vertex_count += 4
    lines.append(f"f 1 2 3 4")
    lines.append("")

    # Ceiling
    lines.append("o Ceiling")
    h = room.height
    for v in [(0,h,0), (room.width,h,0), (room.width,h,room.depth), (0,h,room.depth)]:
        lines.append(f"v {v[0]} {v[1]} {v[2]}")
    vertex_count += 4
    o = vertex_count - 3
    lines.append(f"f {o} {o+1} {o+2} {o+3}")
    lines.append("")

    # 4 Walls
    wall_verts = [
        # Front wall (y=0)
        [(0,0,0), (room.width,0,0), (room.width,h,0), (0,h,0)],
        # Back wall (y=depth)
        [(0,0,room.depth), (room.width,0,room.depth), (room.width,h,room.depth), (0,h,room.depth)],
        # Left wall (x=0)
        [(0,0,0), (0,0,room.depth), (0,h,room.depth), (0,h,0)],
        # Right wall (x=width)
        [(room.width,0,0), (room.width,0,room.depth), (room.width,h,room.depth), (room.width,h,0)],
    ]
    for i, wv in enumerate(wall_verts):
        lines.append(f"o Wall_{i}")
        for v in wv:
            lines.append(f"v {v[0]} {v[1]} {v[2]}")
        vertex_count += 4
        o = vertex_count - 3
        lines.append(f"f {o} {o+1} {o+2} {o+3}")
        lines.append("")

    # Obstacles
    for obs in room.obstacles:
        lines.append(f"o {obs.name}")
        v_lines, f_lines = _box_to_obj_faces(obs, vertex_count + 1)
        lines.extend(v_lines)
        vertex_count += 8
        lines.extend(f_lines)
        lines.append("")

    # Anchors as small cubes (for visualization)
    for anchor in room.anchors:
        lines.append(f"o {anchor.id}")
        a_box = Box(anchor.id, anchor.x, anchor.y, anchor.z, 0.05, 0.05, 0.05, "metal")
        v_lines, f_lines = _box_to_obj_faces(a_box, vertex_count + 1)
        lines.extend(v_lines)
        vertex_count += 8
        lines.extend(f_lines)
        lines.append("")

    Path(path).write_text("\n".join(lines))
    print(f"Exported OBJ to {path} ({vertex_count} vertices)")


# ──────────────────────────────────────────────
# Sionna XML Export (for RF ray-tracing)
# ──────────────────────────────────────────────

def export_sionna_xml(room: Room, path: str):
    """
    Export room as Mitsuba-format XML scene that Sionna can load.
    Sionna uses Mitsuba 3 scene format for its ray tracer.
    """
    indent = "  "

    def mat_xml(name: str, mat_key: str) -> str:
        m = RF_MATERIALS[mat_key]
        return (
            f'{indent}<bsdf type="principled" id="mat-{name}">\n'
            f'{indent}{indent}<!-- RF: permittivity={m["permittivity"]}, '
            f'conductivity={m["conductivity"]}, thickness={m["thickness"]}m -->\n'
            f'{indent}{indent}<string name="rf_material" value="{mat_key}"/>\n'
            f'{indent}</bsdf>\n'
        )

    def rect_xml(name: str, transform: str, mat_name: str) -> str:
        return (
            f'{indent}<shape type="rectangle" id="{name}">\n'
            f'{indent}{indent}<ref id="mat-{mat_name}"/>\n'
            f'{indent}{indent}<transform name="to_world">\n'
            f'{transform}'
            f'{indent}{indent}</transform>\n'
            f'{indent}</shape>\n'
        )

    lines = ['<?xml version="1.0" encoding="utf-8"?>\n']
    lines.append('<scene version="2.1.0">\n')

    # Materials
    used_mats = {room.wall_material, room.floor_material, room.ceiling_material}
    for obs in room.obstacles:
        used_mats.add(obs.material)
    for mat_key in used_mats:
        lines.append(mat_xml(mat_key, mat_key))

    lines.append("\n")

    # Floor — rectangle at z=0, scaled to room size
    w2, d2 = room.width / 2, room.depth / 2
    lines.append(
        f'{indent}<shape type="rectangle" id="floor">\n'
        f'{indent}{indent}<ref id="mat-{room.floor_material}"/>\n'
        f'{indent}{indent}<transform name="to_world">\n'
        f'{indent}{indent}{indent}<scale x="{w2}" y="{d2}" z="1"/>\n'
        f'{indent}{indent}{indent}<translate x="{w2}" y="{d2}" z="0"/>\n'
        f'{indent}{indent}</transform>\n'
        f'{indent}</shape>\n'
    )

    # Ceiling
    lines.append(
        f'{indent}<shape type="rectangle" id="ceiling">\n'
        f'{indent}{indent}<ref id="mat-{room.ceiling_material}"/>\n'
        f'{indent}{indent}<transform name="to_world">\n'
        f'{indent}{indent}{indent}<scale x="{w2}" y="{d2}" z="1"/>\n'
        f'{indent}{indent}{indent}<rotate x="1" angle="180"/>\n'
        f'{indent}{indent}{indent}<translate x="{w2}" y="{d2}" z="{room.height}"/>\n'
        f'{indent}{indent}</transform>\n'
        f'{indent}</shape>\n'
    )

    # Walls
    h2 = room.height / 2
    wall_specs = [
        ("wall_front", w2, h2, f'<scale x="{w2}" y="{h2}" z="1"/>\n'
         f'{indent}{indent}{indent}<rotate x="1" angle="90"/>\n'
         f'{indent}{indent}{indent}<translate x="{w2}" y="0" z="{h2}"/>\n'),
        ("wall_back", w2, h2, f'<scale x="{w2}" y="{h2}" z="1"/>\n'
         f'{indent}{indent}{indent}<rotate x="1" angle="-90"/>\n'
         f'{indent}{indent}{indent}<translate x="{w2}" y="{room.depth}" z="{h2}"/>\n'),
        ("wall_left", d2, h2, f'<scale x="{d2}" y="{h2}" z="1"/>\n'
         f'{indent}{indent}{indent}<rotate y="1" angle="-90"/>\n'
         f'{indent}{indent}{indent}<rotate x="1" angle="90"/>\n'
         f'{indent}{indent}{indent}<translate x="0" y="{d2}" z="{h2}"/>\n'),
        ("wall_right", d2, h2, f'<scale x="{d2}" y="{h2}" z="1"/>\n'
         f'{indent}{indent}{indent}<rotate y="1" angle="90"/>\n'
         f'{indent}{indent}{indent}<rotate x="1" angle="90"/>\n'
         f'{indent}{indent}{indent}<translate x="{room.width}" y="{d2}" z="{h2}"/>\n'),
    ]
    for name, _, _, transform_inner in wall_specs:
        lines.append(
            f'{indent}<shape type="rectangle" id="{name}">\n'
            f'{indent}{indent}<ref id="mat-{room.wall_material}"/>\n'
            f'{indent}{indent}<transform name="to_world">\n'
            f'{indent}{indent}{indent}{transform_inner}'
            f'{indent}{indent}</transform>\n'
            f'{indent}</shape>\n'
        )

    # Obstacles as boxes
    for obs in room.obstacles:
        cx, cy, cz = obs.x, obs.y, obs.z
        sx, sy, sz = obs.width/2, obs.depth/2, obs.height/2
        lines.append(
            f'{indent}<shape type="cube" id="{obs.name}">\n'
            f'{indent}{indent}<ref id="mat-{obs.material}"/>\n'
            f'{indent}{indent}<transform name="to_world">\n'
            f'{indent}{indent}{indent}<scale x="{sx}" y="{sy}" z="{sz}"/>\n'
            f'{indent}{indent}{indent}<translate x="{cx}" y="{cy}" z="{cz}"/>\n'
            f'{indent}{indent}</transform>\n'
            f'{indent}</shape>\n'
        )

    lines.append('</scene>\n')

    Path(path).write_text("".join(lines))
    print(f"Exported Sionna XML to {path}")


# ──────────────────────────────────────────────
# JSON scene export (our internal format)
# ──────────────────────────────────────────────

def export_json(room: Room, path: str):
    """Export room as JSON for our own tools."""
    data = {
        "room": {
            "width": room.width,
            "depth": room.depth,
            "height": room.height,
            "wall_material": room.wall_material,
            "floor_material": room.floor_material,
            "ceiling_material": room.ceiling_material,
        },
        "obstacles": [
            {
                "name": o.name, "x": o.x, "y": o.y, "z": o.z,
                "width": o.width, "depth": o.depth, "height": o.height,
                "material": o.material,
            }
            for o in room.obstacles
        ],
        "anchors": [
            {"id": a.id, "x": a.x, "y": a.y, "z": a.z}
            for a in room.anchors
        ],
        "rf_materials": RF_MATERIALS,
    }
    Path(path).write_text(json.dumps(data, indent=2))
    print(f"Exported JSON to {path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Room simulator / scene exporter")
    parser.add_argument("--format", choices=["obj", "xml", "json", "all"], default="all")
    parser.add_argument("--out_dir", default=".")
    args = parser.parse_args()

    room = create_default_room()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    if args.format in ("obj", "all"):
        export_obj(room, str(out / "room.obj"))
    if args.format in ("xml", "all"):
        export_sionna_xml(room, str(out / "room.xml"))
    if args.format in ("json", "all"):
        export_json(room, str(out / "room.json"))
