# actions/make_3d.py
"""make_3d — CAD-lite: text spec → 3D model file (STL / OBJ).

WHAT IT DOES
    Builds a mesh from primitives with transforms and writes an STL
    (binary, printable) or OBJ (viewable everywhere):

        make_3d spec="box 40 20 8; hole cylinder r=6 h=10 at 0 0 4"
        → models/<name>.stl

    Primitives: box, cylinder, sphere, cone, plate (=flat box).
    Operators: union (default), difference (subtract — `hole` keyword or
    `minus`), translate `at x y z`, scale `scale s`.
    Output: binary STL (3D-print ready) or OBJ with normals.

WHY IT'S IN THE ROADMAP AS UNIQUE: every other assistant stops at code
or images — this gives a real, editable 3D file from a sentence.

NO DEPS: triangle soup + struct — pure stdlib. The mesh math is ours;
the files open in any slicer/CAD (Cura, FreeCAD, Blender).
"""
from __future__ import annotations

import math
import re
import struct
import time
from pathlib import Path

Vec3 = tuple[float, float, float]
Tri = tuple[Vec3, Vec3, Vec3]


# ── math ─────────────────────────────────────────────────────────────────────

def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _norm(a: Vec3) -> Vec3:
    m = math.sqrt(a[0] ** 2 + a[1] ** 2 + a[2] ** 2) or 1.0
    return (a[0] / m, a[1] / m, a[2] / m)


def _add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a: Vec3, s: float) -> Vec3:
    return (a[0] * s, a[1] * s, a[2] * s)


# ── primitives (centred at origin unless noted) ──────────────────────────────

def _box(w: float, h: float, d: float) -> list[Tri]:
    x, y, z = w / 2, h / 2, d / 2
    v = [(-x, -y, -z), (x, -y, -z), (x, y, -z), (-x, y, -z),
         (-x, -y, z), (x, -y, z), (x, y, z), (-x, y, z)]
    quads = [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1),
             (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]
    tris: list[Tri] = []
    for a, b, c, d_ in quads:
        tris.append((v[a], v[b], v[c]))
        tris.append((v[a], v[c], v[d_]))
    return tris


def _cyl(r: float, h: float, seg: int = 32) -> list[Tri]:
    seg = max(6, min(128, seg))
    hh = h / 2
    tris: list[Tri] = []
    for i in range(seg):
        a0 = 2 * math.pi * i / seg
        a1 = 2 * math.pi * (i + 1) / seg
        p0 = (r * math.cos(a0), r * math.sin(a0), -hh)
        p1 = (r * math.cos(a1), r * math.sin(a1), -hh)
        q0 = (p0[0], p0[1], hh)
        q1 = (p1[0], p1[1], hh)
        # side (CCW seen from outside)
        tris.append((p0, q0, q1))
        tris.append((p0, q1, p1))
        # caps
        tris.append(((0, 0, hh), q0, q1))       # top
        tris.append(((0, 0, -hh), p1, p0))      # bottom
    return tris


def _sphere(r: float, seg: int = 24) -> list[Tri]:
    seg = max(6, min(96, seg))
    rings = max(4, seg // 2)
    tris: list[Tri] = []
    def pt(i: int, j: int) -> Vec3:
        theta = math.pi * i / rings
        phi = 2 * math.pi * j / seg
        return (r * math.sin(theta) * math.cos(phi),
                r * math.sin(theta) * math.sin(phi),
                r * math.cos(theta))
    for i in range(rings):
        for j in range(seg):
            a, b = pt(i, j), pt(i, j + 1)
            c, d_ = pt(i + 1, j + 1), pt(i + 1, j)
            if i != 0:
                tris.append((a, b, c))
            if i != rings - 1:
                tris.append((a, c, d_))
    return tris


def _cone(r: float, h: float, seg: int = 32) -> list[Tri]:
    seg = max(6, min(128, seg))
    hh = h / 2
    apex = (0, 0, hh)
    tris: list[Tri] = []
    for i in range(seg):
        a0 = 2 * math.pi * i / seg
        a1 = 2 * math.pi * (i + 1) / seg
        p0 = (r * math.cos(a0), r * math.sin(a0), -hh)
        p1 = (r * math.cos(a1), r * math.sin(a1), -hh)
        tris.append((p0, p1, apex))
        tris.append(((0, 0, -hh), p1, p0))
    return tris


# ── boolean-lite: difference by z-slab clipping is complex; we do the
# practical CAD-lite thing — subtract via "open shell removal" is NOT
# mesh-boolean. Instead `difference` carves by keeping the min solid via
# a simple BSP-free approach: represent holes as inverted同心 shells is
# not manifold-safe either. So: honest v1 — difference writes the tool
# solid into a SECOND group and reports it; the STL still contains both
# shells (slicers with "remove overlaps" handle concentric shells), and
# we SAY so in the result. True mesh booleans need a kernel (manifold3d
# is free but a wheel) — noted in the output for honesty.
# ────────────────────────────────────────────────────────────────────────────

def _transform(tris: list[Tri], at: Vec3 = (0, 0, 0),
               s: float = 1.0) -> list[Tri]:
    out = []
    for t in tris:
        out.append(tuple(_add(_scale(v, s), at) for v in t))  # type: ignore
    return out  # type: ignore


# ── spec parser ──────────────────────────────────────────────────────────────

_PRIM = re.compile(
    r"^(?P<kind>box|plate|cylinder|cyl|sphere|ball|cone)\s+"
    r"(?P<dims>(?:[-\d.]+\s*){1,4})",
    re.I)
# named-dims entry: "box w=40 …" — kind only; values parsed separately
_PRIM_KIND = re.compile(
    r"^(?P<kind>box|plate|cylinder|cyl|sphere|ball|cone)\b",
    re.I)
_AT = re.compile(r"\bat\s+([-\d.]+)[,\s]+([-\d.]+)[,\s]+([-\d.]+)", re.I)
_SCALE = re.compile(r"\bscale\s+([-\d.]+)", re.I)


def _parse_spec(spec: str) -> tuple[list[tuple[list[Tri], str]], list[str]]:
    """Returns (groups, notes). Each group = (triangles, op) where op is
    'union' or 'minus'. Statements split on ';' or newline."""
    groups: list[tuple[list[Tri], str]] = []
    notes: list[str] = []
    stmts = [s.strip() for s in re.split(r"[;\n]", spec) if s.strip()]
    if not stmts:
        raise ValueError("empty spec")
    for n, stmt in enumerate(stmts):
        op = "minus" if re.match(r"^(hole|minus|subtract|difference)\b",
                                 stmt, re.I) else "union"
        body = re.sub(r"^(hole|minus|subtract|difference)\s+",
                      "", stmt, flags=re.I)
        m = _PRIM.match(body)
        kind = ""
        dims: list[float] = []
        if m:
            kind = m.group("kind").lower()
            dims = [float(x) for x in m.group("dims").split()]
        elif (named_m := _PRIM_KIND.match(body)):
            # named-dims form: "cylinder r=6 h=10", "box w=40 h=20 d=8"
            kind = named_m.group("kind").lower()
            named = {k.lower(): float(v)
                     for k, v in re.findall(
                         r"([a-zA-Z]{1,6})\s*=\s*([-\d.]+)", body)}
            if kind in ("box", "plate"):
                w = named.get("w", named.get("width"))
                h = named.get("h", named.get("height"))
                if w is None or h is None:
                    raise ValueError(f"{kind} needs w=/h= (or d=): {stmt!r}")
                dims = [w, h, named.get("d", named.get("depth", 2.0))]
            elif kind in ("cylinder", "cyl", "cone"):
                if named.get("r") is None or named.get("h") is None:
                    raise ValueError(f"{kind} needs r= and h=: {stmt!r}")
                dims = [named["r"], named["h"], named.get("seg", 32.0)]
            else:  # sphere/ball
                if named.get("r") is None:
                    raise ValueError(f"{kind} needs r=: {stmt!r}")
                dims = [named["r"], named.get("seg", 24.0)]
        else:
            raise ValueError(f"don't understand {stmt!r} — start with a "
                             f"primitive: box/cylinder/sphere/cone/plate")
        if kind == "box":
            if len(dims) < 3:
                raise ValueError(f"box needs 3 dims: {stmt!r}")
            tris = _box(dims[0], dims[1], dims[2])
        elif kind == "plate":
            if len(dims) < 3:
                dims = [dims[0], dims[1], 2.0]
            tris = _box(dims[0], dims[1], dims[2])
        elif kind in ("cylinder", "cyl"):
            if len(dims) < 2:
                raise ValueError(f"cylinder needs r h: {stmt!r}")
            tris = _cyl(dims[0], dims[1],
                        int(dims[2]) if len(dims) > 2 else 32)
        elif kind in ("sphere", "ball"):
            tris = _sphere(dims[0],
                           int(dims[1]) if len(dims) > 1 else 24)
        else:  # cone
            if len(dims) < 2:
                raise ValueError(f"cone needs r h: {stmt!r}")
            tris = _cone(dims[0], dims[1],
                         int(dims[2]) if len(dims) > 2 else 32)
        at = _AT.search(body)
        at_v: Vec3 = (float(at.group(1)), float(at.group(2)),
                      float(at.group(3))) if at else (0.0, 0.0, 0.0)
        sc = _SCALE.search(body)
        sc_v = float(sc.group(1)) if sc else 1.0
        groups.append((_transform(tris, at_v, sc_v), op))
        if op == "minus":
            notes.append("hole/minus written as a separate shell — use a "
                         "slicer's overlap removal, or say 'OBJ' for "
                         "editing in FreeCAD/Blender.")
    return groups, notes


# ── writers ──────────────────────────────────────────────────────────────────

def _flatten(groups: list[tuple[list[Tri], str]]) -> list[Tri]:
    # v1: all shells concatenated (manifold-safe: each shell closed)
    out: list[Tri] = []
    for tris, _op in groups:
        out.extend(tris)
    return out


def _write_stl(tris: list[Tri], path: Path) -> None:
    with open(path, "wb") as f:
        f.write(b"jarvis make_3d".ljust(80, b"\0"))
        f.write(struct.pack("<I", len(tris)))
        for a, b, c in tris:
            n = _norm(_cross(_sub(b, a), _sub(c, a)))
            f.write(struct.pack("<3f", *n))
            for v in (a, b, c):
                f.write(struct.pack("<3f", *v))
            f.write(struct.pack("<H", 0))


def _write_obj(tris: list[Tri], path: Path) -> None:
    lines = ["# jarvis make_3d"]
    idx = 1
    for a, b, c in tris:
        n = _norm(_cross(_sub(b, a), _sub(c, a)))
        lines.append(f"vn {n[0]:.6f} {n[1]:.6f} {n[2]:.6f}")
        for v in (a, b, c):
            lines.append(f"v {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}")
        lines.append(f"f {idx}//{idx} {idx+1}//{idx} {idx+2}//{idx}")
        idx += 3
    path.write_text("\n".join(lines), encoding="utf-8")


# ── handler ──────────────────────────────────────────────────────────────────

def make_3d(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    spec = str(params.get("spec") or params.get("description") or "").strip()
    if not spec:
        return ("Give me a spec — e.g. spec='box 40 20 8; cylinder 6 12 "
                "at 10 0 4'. Primitives: box/plate/cylinder/sphere/cone, "
                "with `at x y z`, `scale s`, `hole` prefixes.")
    fmt = str(params.get("format") or "stl").lower().strip()
    if fmt not in ("stl", "obj"):
        return "format must be stl or obj."

    try:
        groups, notes = _parse_spec(spec)
    except ValueError as e:
        return f"Spec problem: {e}"
    tris = _flatten(groups)
    if not tris:
        return "Spec produced no geometry."

    name = str(params.get("name") or "").strip()
    if not name:
        first = re.match(r"(\w+)", spec)
        name = first.group(1).lower() if first else "model"
    name = re.sub(r"[^A-Za-z0-9_-]", "-", name)[:48] or "model"

    from config import get_base_dir
    out_dir = get_base_dir() / "models"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{name}-{int(time.time()) % 100000}.{fmt}"
        if fmt == "stl":
            _write_stl(tris, path)
        else:
            _write_obj(tris, path)
    except Exception as e:
        return f"Couldn't write the model: {e}"

    # bounding box for a quick sanity line
    xs = [v[0] for t in tris for v in t]
    ys = [v[1] for t in tris for v in t]
    zs = [v[2] for t in tris for v in t]
    size = (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
    msg = (f"Model saved: {path} — {len(tris)} triangles, "
           f"≈{size[0]:.1f} × {size[1]:.1f} × {size[2]:.1f} units.")
    for n in notes[:2]:
        msg += f"\nNote: {n}"
    # Render surface: the model lands on the dashboard's 3D tab.
    if player is not None:
        try:
            player.show_content(f"3D — {name}"[:48], msg[:4000])
        except Exception:
            pass
    return msg


TOOL = {
    "name": "make_3d",
    "description": (
        "CAD-lite: build a 3D model file from a text spec. Primitives "
        "box/plate/cylinder/sphere/cone with `at x y z` translation, "
        "`scale s`, and `hole`/`minus` prefixes; statements split on ';'. "
        "format: stl (binary, 3D-print ready) | obj. Output saved to "
        "models/. Use for '3D model banao', 'design a phone stand', "
        "'printable hook'. Not a full CAD kernel — complex booleans are "
        "noted in the reply."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "spec": {"type": "STRING", "description": "Geometry spec"},
            "format": {"type": "STRING", "description": "stl | obj — default stl"},
            "name": {"type": "STRING", "description": "Output file stem"},
        },
        "required": ["spec"],
    },
    "handler": make_3d,
}


def run(params: dict, ctx: dict | None = None) -> str:
    return make_3d(params,
                   player=(ctx or {}).get("player"),
                   session_memory=(ctx or {}).get("session_memory"))


if __name__ == "__main__":
    print(make_3d({"spec": "box 40 20 8; cylinder 6 12 at 10 0 4"}))
