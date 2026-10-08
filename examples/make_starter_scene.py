# SPDX-License-Identifier: GPL-3.0-or-later
"""Builds examples/projection_trace_starter.blend. Run from the repository root
with the oldest Blender you support, so the file opens everywhere:

    blender --background --factory-startup --python examples/make_starter_scene.py

The scene: a scale model of a small building facade (about 1.9 x 0.5 x 0.9 m) on a
floor, and a "Projector" camera in front of it on the -Y axis, level, looking along +Y,
the way projectors usually face a wall or a facade.
"""

import importlib.util
import math
import os
import sys

import bmesh
import bpy
from mathutils import Matrix, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "examples", "projection_trace_starter.blend")

THROW_RATIO = 1.5      # distance / image width, a typical standard-throw projector
IMAGE_WIDTH = 2.4      # m, the model plus some margin
LENS_HEIGHT = 0.45     # m, about the middle of the facade


def load_addon():
    spec = importlib.util.spec_from_file_location(
        "projection_trace_tool", os.path.join(ROOT, "__init__.py"), submodule_search_locations=[ROOT])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["projection_trace_tool"] = mod
    spec.loader.exec_module(mod)
    mod.register()
    return mod


def clear_scene():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob)
    for coll in (bpy.data.meshes, bpy.data.cameras, bpy.data.lights, bpy.data.materials):
        for block in list(coll):
            coll.remove(block)


def box(bm, x0, x1, y0, y1, z0, z1):
    """Axis-aligned box from min/max corners (metres)."""
    size = Vector((x1 - x0, y1 - y0, z1 - z0))
    center = Vector(((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2))
    mat = Matrix.Translation(center) @ Matrix.Diagonal(size.to_4d())
    bmesh.ops.create_cube(bm, size=1.0, matrix=mat)


def prism(bm, x0, x1, y0, y1, z0, z1):
    """Triangular pediment: ridge along Y, apex at the middle of X."""
    xm = (x0 + x1) / 2
    verts = [bm.verts.new(v) for v in (
        (x0, y0, z0), (x1, y0, z0), (xm, y0, z1),
        (x0, y1, z0), (x1, y1, z0), (xm, y1, z1))]
    f, b = verts[:3], verts[3:]
    bm.faces.new((f[0], f[1], f[2]))
    bm.faces.new((b[2], b[1], b[0]))
    bm.faces.new((f[0], b[0], b[1], f[1]))
    bm.faces.new((f[1], b[1], b[2], f[2]))
    bm.faces.new((f[2], b[2], b[0], f[0]))


def mesh_object(name, bm):
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    return ob


def build_model():
    # Solid volumes, all overlapping into one closed-looking shape.
    bm = bmesh.new()
    box(bm, -0.62, 0.62, -0.02, 0.47, 0.00, 0.08)      # plinth
    box(bm, -0.60, 0.60, 0.00, 0.45, 0.00, 0.60)       # main block
    box(bm, -0.63, 0.63, -0.03, 0.48, 0.60, 0.64)      # cornice
    box(bm, -0.18, 0.18, -0.06, 0.45, 0.00, 0.72)      # central risalit
    box(bm, -0.20, 0.20, -0.08, 0.47, 0.72, 0.75)      # risalit cornice
    prism(bm, -0.20, 0.20, -0.08, 0.47, 0.75, 0.88)    # pediment
    for x in (-0.60, -0.40, 0.40, 0.60):               # pilasters
        box(bm, x - 0.02, x + 0.02, -0.02, 0.01, 0.08, 0.60)
    for i in range(3):                                 # steps to the door
        box(bm, -0.14, 0.14, -0.06 - 0.06 * (3 - i), -0.06, 0.0, 0.03 * (i + 1))
    box(bm, 0.60, 0.95, 0.05, 0.40, 0.00, 0.40)        # lower side wing
    box(bm, 0.60, 0.97, 0.03, 0.42, 0.40, 0.43)        # wing roof edge
    bmesh.ops.create_cone(                     # round tower on the left
        bm, cap_ends=True, segments=32, radius1=0.13, radius2=0.13, depth=0.70,
        matrix=Matrix.Translation((-0.80, 0.22, 0.35)))
    bmesh.ops.create_cone(                             # conical roof
        bm, cap_ends=True, segments=32, radius1=0.15, radius2=0.0, depth=0.22,
        matrix=Matrix.Translation((-0.80, 0.22, 0.81)))
    model = mesh_object("Model", bm)

    # Window and door recesses, cut with one boolean so the facade has real depth.
    bm = bmesh.new()
    for x in (-0.50, -0.29, 0.29, 0.50):               # two floors of windows
        for z0, z1 in ((0.14, 0.30), (0.38, 0.54)):
            box(bm, x - 0.06, x + 0.06, -0.10, 0.03, z0, z1)
    box(bm, -0.07, 0.07, -0.15, -0.02, 0.09, 0.36)     # door
    box(bm, -0.10, 0.10, -0.15, -0.03, 0.44, 0.62)     # risalit window
    box(bm, 0.70, 0.86, -0.10, 0.08, 0.14, 0.30)       # wing window
    cutter = mesh_object("Cutter", bm)
    mod = model.modifiers.new("Recesses", "BOOLEAN")
    mod.operation = "DIFFERENCE"
    mod.solver = "EXACT"
    mod.use_self = True        # the volumes above overlap inside one mesh
    mod.object = cutter
    deps = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(model.evaluated_get(deps))
    old = model.data
    model.modifiers.clear()
    model.data = me
    me.name = "Model"
    bpy.data.meshes.remove(old)
    bpy.data.objects.remove(cutter)
    bpy.data.meshes.remove(bpy.data.meshes["Cutter"])

    mat = bpy.data.materials.new("Model white")
    mat.diffuse_color = (0.85, 0.85, 0.85, 1.0)
    mat.use_nodes = True
    bsdf = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (0.85, 0.85, 0.85, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.8
    model.data.materials.append(mat)
    return model


def build_floor():
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=2.0,
                          matrix=Matrix.Translation((0.0, 0.5, 0.0)))
    floor = mesh_object("Floor", bm)
    mat = bpy.data.materials.new("Floor grey")
    mat.diffuse_color = (0.25, 0.25, 0.25, 1.0)
    mat.use_nodes = True
    bsdf = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (0.25, 0.25, 0.25, 1.0)
    floor.data.materials.append(mat)
    return floor


def build_camera():
    cam = bpy.data.cameras.new("Projector")
    cam.sensor_fit = "HORIZONTAL"
    cam.sensor_width = 36.0
    cam.lens = THROW_RATIO * cam.sensor_width
    cam.clip_start = 0.05
    cam.clip_end = 100.0
    ob = bpy.data.objects.new("Projector", cam)
    bpy.context.scene.collection.objects.link(ob)
    ob.location = (0.0, -THROW_RATIO * IMAGE_WIDTH, LENS_HEIGHT)
    ob.rotation_euler = (math.radians(90.0), 0.0, 0.0)    # level, looking along +Y
    return ob


def setup_ui():
    """Open the sidebar and frame the model in every 3D view of the saved screens."""
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            space.show_region_ui = True
            space.shading.type = "SOLID"
            space.shading.show_cavity = True
            space.shading.show_object_outline = True
            r3d = space.region_3d
            r3d.view_perspective = "PERSP"
            r3d.view_location = (0.0, 0.2, 0.4)
            r3d.view_distance = 4.5
            r3d.view_rotation = (Matrix.Rotation(math.radians(-25.0), 4, "Z")
                                 @ Matrix.Rotation(math.radians(75.0), 4, "X")).to_quaternion()


def main():
    load_addon()
    clear_scene()
    scene = bpy.context.scene
    scene.name = "Projection Trace starter"
    build_model()
    build_floor()
    cam = build_camera()
    scene.camera = cam
    scene.render.resolution_x = 1920
    scene.render.resolution_y = 1080
    scene.render.resolution_percentage = 100
    scene.unit_settings.system = "METRIC"
    scene.display.shading.show_cavity = True
    if scene.world is None:
        scene.world = bpy.data.worlds.new("World")
    scene.world.color = (0.05, 0.05, 0.05)

    pt = scene.ptrace
    pt.proj_camera = cam
    pt.projection = "SURFACE"

    setup_ui()
    bpy.ops.wm.save_as_mainfile(filepath=OUT, compress=True)
    print("Saved", OUT)


main()
