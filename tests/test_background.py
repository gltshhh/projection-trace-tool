# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless tests. Run from the repository root:

    blender --background --factory-startup --python tests/test_background.py

Optional: set PTRACE_LEGACY to the path of version 1.0 (projector_trace.py) to test
that loading this version cleanly replaces it.
"""

import ctypes
import importlib.util
import os
import sys
import tempfile
import traceback

import bpy
from mathutils import Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
results = []


def load_addon():
    spec = importlib.util.spec_from_file_location(
        "projection_trace_tool", os.path.join(ROOT, "__init__.py"), submodule_search_locations=[ROOT])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["projection_trace_tool"] = mod
    spec.loader.exec_module(mod)
    return mod


def check(name, fn):
    try:
        fn()
        results.append((name, True, ""))
    except Exception:
        results.append((name, False, traceback.format_exc(limit=3)))


def plane(name, z, size=4.0):
    me = bpy.data.meshes.new(name)
    s = size / 2
    me.from_pydata([(-s, -s, z), (s, -s, z), (s, s, z), (-s, s, z)], [], [(0, 1, 2, 3)])
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    return ob


def dpi_awareness():
    if sys.platform != "win32":
        return None
    v = ctypes.c_int(-1)
    ctypes.WinDLL("shcore").GetProcessDpiAwareness(None, ctypes.byref(v))
    return v.value


DPI_BEFORE = dpi_awareness()

legacy = os.environ.get("PTRACE_LEGACY")
if legacy:
    def t_legacy_loaded():
        src = open(legacy, encoding="utf-8").read()
        exec(compile(src, legacy, "exec"), {"__name__": "__main__"})
        assert hasattr(bpy.types, "PTRACE_OT_projektor")
    check("legacy 1.0 loads (setup)", t_legacy_loaded)

m = load_addon()


def t_register():
    m.register()
    assert hasattr(bpy.types.Scene, "ptrace")
    assert hasattr(bpy.types, "PTRACE_OT_projector_open")
    if legacy:
        assert not hasattr(bpy.types, "PTRACE_OT_projektor"), "legacy operator still registered"


def t_reregister():
    m.unregister()
    assert not hasattr(bpy.types.Scene, "ptrace")
    assert m._draw_handle is None and not m._keymaps
    assert m._on_load_pre not in bpy.app.handlers.load_pre
    m.register()
    assert bpy.app.handlers.load_pre.count(m._on_load_pre) == 1


def t_run_script_reload():
    # a second copy (Run Script again) must replace the first one
    src = open(os.path.join(ROOT, "__init__.py"), encoding="utf-8").read()
    g = {"__name__": "__main__"}
    exec(compile(src, "projection_trace_tool.py", "exec"), g)
    assert bpy.types.PTRACE_OT_trace.modal.__globals__ is g
    assert m._draw_handle is None, "first copy not unregistered"
    g["unregister"]()
    m.register()


def t_plane_hit():
    o, d = Vector((0, 0, 10)), Vector((0, 0, -1))
    assert (m._plane_hit(o, d, 2.0) - Vector((0, 0, 2))).length < 1e-6
    assert m._plane_hit(o, Vector((0, 0, 1)), 2.0) is None, "plane behind viewer must be None"
    assert m._plane_hit(o, Vector((1, 0, 0)), 2.0) is None
    # vertical Y plane: projector on -Y looking along +Y at a facade
    o = Vector((0, -5, 0.5))
    assert (m._plane_hit(o, Vector((0, 1, 0)), 0.0, 1) - Vector((0, 0, 0.5))).length < 1e-6
    assert m._plane_hit(o, Vector((0, -1, 0)), 0.0, 1) is None, "Y plane behind viewer must be None"
    assert m._plane_hit(o, Vector((0, 0, 1)), 0.0, 1) is None


def t_surface_hit_skips_trace():
    scan = plane("scan", 1.0)
    tr = plane("trace_001_mesh", 2.0)
    col = m._trace_collection(bpy.context.scene)
    for c in list(tr.users_collection):
        c.objects.unlink(tr)
    col.objects.link(tr)
    bpy.context.view_layer.update()
    p = bpy.context.scene.ptrace
    p.target = None
    hit = m._surface_hit(bpy.context, Vector((0.3, 0.2, 10)), Vector((0, 0, -1)))
    assert hit is not None and abs(hit.z - 1.0) < 1e-4, hit
    p.target = scan
    hit = m._surface_hit(bpy.context, Vector((0.3, 0.2, 10)), Vector((0, 0, -1)))
    assert hit is not None and abs(hit.z - 1.0) < 1e-4, hit
    p.target = None
    miss = m._surface_hit(bpy.context, Vector((50, 50, 10)), Vector((0, 0, -1)))
    assert miss is None


def t_curve_and_mesh():
    pts = [Vector(v) for v in ((0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1))]
    ob = m._create_curve(bpy.context, pts, True)
    assert ob.name.startswith("trace_") and ob.name in m._trace_collection(bpy.context.scene).objects
    assert len(ob.data.splines[0].points) == 4 and ob.data.splines[0].use_cyclic_u
    with bpy.context.temp_override(selected_objects=[ob]):
        assert bpy.ops.ptrace.to_mesh() == {"FINISHED"}
    me = bpy.data.objects[ob.name + "_mesh"].data
    assert (len(me.vertices), len(me.polygons)) == (4, 1)
    op = m._create_curve(bpy.context, pts[:3], False)
    with bpy.context.temp_override(selected_objects=[op]):
        bpy.ops.ptrace.to_mesh()
    me = bpy.data.objects[op.name + "_mesh"].data
    assert (len(me.vertices), len(me.edges), len(me.polygons)) == (3, 2, 0)


def t_polls_without_viewport():
    assert not bpy.ops.ptrace.dark_background.poll()
    assert not bpy.ops.ptrace.trace.poll()
    try:
        bpy.ops.ptrace.light_test()
    except RuntimeError as ex:
        assert "viewport" in str(ex).lower()
    assert not m._light["active"]


def t_load_pre_resets_state():
    m._trace.update(active=True, points=[Vector()], area_ptr=123)
    m._dark_backup[1] = {}
    path = os.path.join(tempfile.mkdtemp(), "t.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    assert not m._trace["active"] and not m._trace["points"] and not m._dark_backup


def t_translations():
    view = bpy.context.preferences.view
    view.language = "pl_PL"
    view.use_translate_interface = True
    view.use_translate_tooltips = True
    try:
        tr = bpy.app.translations
        assert tr.pgettext_iface("Line width") == "Grubość linii", tr.pgettext_iface("Line width")
        assert tr.pgettext_iface("Open on projector", "Operator") == "Otwórz na projektorze"
        assert tr.pgettext_tip("Next sun angle") == "Następny kąt słońca"
        assert m.rpt_("Saved %s") % "x" == "Zapisano x"
        # every format string keeps its placeholders
        for msgid, msgstr in m._PL:
            for ph in ("%d", "%s", "%3.0f", "%2.0f"):
                assert msgid.count(ph) == msgstr.count(ph), msgid
    finally:
        view.language = "en_US"


def t_win32():
    if sys.platform != "win32":
        return
    mons = m._monitors()
    assert mons and sum(1 for x in mons if x[4]) == 1, mons
    assert isinstance(m._process_windows(), dict)
    assert m._projector_monitor(0) == mons[0]
    try:
        m._projector_monitor(len(mons))
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass
    # shared ctypes.windll must stay untouched, and so must process DPI awareness
    assert ctypes.windll.user32.SetWindowPos.argtypes is None
    assert dpi_awareness() == DPI_BEFORE


def t_unregister_clean():
    m.unregister()
    assert not hasattr(bpy.types, "PTRACE_OT_trace")
    assert not any(k.idname.startswith("ptrace.")
                   for km in bpy.context.window_manager.keyconfigs.addon.keymaps
                   for k in km.keymap_items)


for name, fn in [
    ("register (replaces legacy 1.0)" if legacy else "register", t_register),
    ("unregister + register", t_reregister),
    ("Run Script reload replaces copy", t_run_script_reload),
    ("plane hit Z / Y, behind = None", t_plane_hit),
    ("raycast skips TRACE, target mesh", t_surface_hit_skips_trace),
    ("curve + to_mesh (closed, open)", t_curve_and_mesh),
    ("polls without a viewport", t_polls_without_viewport),
    ("load_pre resets state", t_load_pre_resets_state),
    ("Polish translation", t_translations),
    ("win32 API, no global side effects", t_win32),
    ("unregister leaves nothing", t_unregister_clean),
]:
    check(name, fn)

print("\n=== Projection Trace Tool background tests (Blender %s) ===" % bpy.app.version_string)
for name, ok, err in results:
    print("%s  %s" % ("PASS" if ok else "FAIL", name))
    if err:
        print(err)
failed = sum(not ok for _n, ok, _e in results)
print("=== %d passed, %d failed ===" % (len(results) - failed, failed))
sys.exit(1 if failed else 0)
