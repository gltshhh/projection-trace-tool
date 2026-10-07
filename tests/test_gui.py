# SPDX-License-Identifier: GPL-3.0-or-later
"""Interactive tests with simulated input. Run from the repository root:

    blender --factory-startup --enable-event-simulate --python tests/test_gui.py

Opens its own Blender window and a projector window (placed on the primary monitor
for the test), draws lines with simulated clicks, runs the light test, then quits.
"""

import importlib.util
import os
import sys
import traceback

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "projection_trace_tool", os.path.join(ROOT, "__init__.py"), submodule_search_locations=[ROOT])
m = importlib.util.module_from_spec(spec)
sys.modules["projection_trace_tool"] = m
spec.loader.exec_module(m)
m.register()

results = []
found_hwnd = []

# keep the test window on the primary monitor, whatever monitors exist
m._projector_monitor = lambda _i: (80, 80, 1000, 700, False)
_orig_new_window = m._new_blender_window


def _rec_new_window(before):
    h = _orig_new_window(before)
    found_hwnd.append((h, m._process_windows().get(h)) if h else (None, None))
    return h


m._new_blender_window = _rec_new_window


def ok(name, cond, info=""):
    results.append((name, bool(cond), "" if cond else str(info)))


def proj():
    return m._projector_area()


def click(win, region, fx, fy, drag_to=None):
    x, y = region.x + int(region.width * fx), region.y + int(region.height * fy)
    win.event_simulate(type="MOUSEMOVE", value="NOTHING", x=x, y=y)
    win.event_simulate(type="LEFTMOUSE", value="PRESS", x=x, y=y)
    if drag_to:
        tx, ty = region.x + int(region.width * drag_to[0]), region.y + int(region.height * drag_to[1])
        for i in range(1, 21):
            win.event_simulate(type="MOUSEMOVE", value="NOTHING",
                               x=x + (tx - x) * i // 20, y=y + (ty - y) * i // 20)
        x, y = tx, ty
    win.event_simulate(type="LEFTMOUSE", value="RELEASE", x=x, y=y)


def key(win, k, **mods):
    win.event_simulate(type=k, value="PRESS", **mods)
    win.event_simulate(type=k, value="RELEASE", **mods)


def traces():
    col = bpy.data.collections.get(m.TRACE_COLLECTION)
    return [o for o in col.objects if o.type == "CURVE"] if col else []


def steps():
    ctx = bpy.context
    main = ctx.window_manager.windows[0]
    scene = ctx.scene
    p = scene.ptrace
    p.proj_shading = "SOLID"

    # --- projector window
    with ctx.temp_override(window=main):
        r = bpy.ops.ptrace.projector_open()
    ok("projector_open FINISHED", r == {"FINISHED"}, r)
    yield 1.5
    win, area = proj()
    ok("projector window tagged, one VIEW_3D area",
       area is not None and len(win.screen.areas) == 1, win and len(win.screen.areas))
    ok("new OS window found by class", found_hwnd and found_hwnd[0][0], found_hwnd)
    print("PTRACE new window:", found_hwnd)
    ok("camera view + clean view",
       area.spaces.active.region_3d.view_perspective == "CAMERA" and not area.spaces.active.overlay.show_overlays)
    region = m._window_region(area)

    # --- reopen replaces the old window
    n = len(ctx.window_manager.windows)
    with ctx.temp_override(window=main):
        bpy.ops.ptrace.projector_open()
    yield 1.5
    ok("reopen keeps a single projector window", len(ctx.window_manager.windows) == n,
       len(ctx.window_manager.windows))
    win, area = proj()
    region = m._window_region(area)

    # --- trace: three clicks + Enter, then a freehand drag + C + Enter
    with ctx.temp_override(window=main):
        bpy.ops.ptrace.start()
    yield 0.3
    ok("trace active in projector area", m._trace["active"] and m._trace["area_ptr"] == area.as_pointer())
    for fx, fy in ((0.4, 0.4), (0.6, 0.4), (0.6, 0.6)):
        click(win, region, fx, fy)
    yield 0.5
    ok("3 points recorded", len(m._trace["points"]) == 3, len(m._trace["points"]))
    key(win, "RET")
    yield 0.5
    t = traces()
    ok("Enter saves a curve", len(t) == 1 and len(t[0].data.splines[0].points) == 3,
       [(o.name, len(o.data.splines[0].points)) for o in t])
    if t:
        zs = [round(pt.co[2], 3) for pt in t[0].data.splines[0].points]
        print("PTRACE point z:", zs)
        ok("points land on the default cube or the Z plane", all(-1.001 <= z <= 1.001 for z in zs), zs)

    click(win, region, 0.3, 0.3, drag_to=(0.7, 0.7))
    yield 0.5
    ok("freehand drag adds several points", len(m._trace["points"]) >= 3, len(m._trace["points"]))
    key(win, "BACK_SPACE")
    key(win, "C")
    yield 0.3
    ok("C closes the loop", m._trace["closed"])
    key(win, "SPACE")
    yield 0.5
    t = traces()
    ok("second curve is cyclic", len(t) == 2 and t[-1].data.splines[0].use_cyclic_u, len(t))
    ok("draw handler without errors", m._draw_error is None, m._draw_error)

    # --- blocked keys do nothing (X would delete in the 3D View)
    n_obj = len(bpy.data.objects)
    key(win, "X")
    key(win, "DEL")
    yield 0.3
    ok("other keys blocked while tracing", len(bpy.data.objects) == n_obj)

    key(win, "ESC")
    yield 0.3
    ok("Esc with no points exits", not m._trace["active"])

    # --- closing the projector window while tracing (bypassing the operator) -> cancel()
    with ctx.temp_override(window=main):
        bpy.ops.ptrace.start()
    yield 0.3
    ok("restart trace", m._trace["active"])
    m._close_projector()
    yield 0.5
    win2, _a = proj()
    ok("projector closed", win2 is None)
    ok("trace state reset when its window closed", not m._trace["active"])
    ok("no draw errors after close", m._draw_error is None, m._draw_error)

    # --- dark background restores previous shading
    with ctx.temp_override(window=main):
        bpy.ops.ptrace.projector_open()
    yield 1.5
    win, area = proj()
    sh = area.spaces.active.shading
    before = (sh.type, sh.light, sh.background_type, tuple(sh.background_color))
    with ctx.temp_override(window=main):
        bpy.ops.ptrace.dark_background()
    dark = (sh.type, sh.background_type, tuple(sh.background_color))
    with ctx.temp_override(window=main):
        bpy.ops.ptrace.dark_background()
    after = (sh.type, sh.light, sh.background_type, tuple(sh.background_color))
    ok("dark background on", dark == ("SOLID", "VIEWPORT", (0.0, 0.0, 0.0)), dark)
    ok("dark background off restores shading", before == after, (before, after))

    # --- light test SOLID
    ld = tuple(scene.display.light_direction)
    p.lt_mode, p.lt_interval = "SOLID", 0.2
    with ctx.temp_override(window=main):
        bpy.ops.ptrace.light_test()
    yield 1.5
    ok("light test SOLID running + auto steps", m._light["active"] and m._light["step"] >= 2, m._light["step"])
    with ctx.temp_override(window=main):
        bpy.ops.ptrace.light_step()
        bpy.ops.ptrace.light_test()
    ok("light test SOLID stop restores",
       not m._light["active"] and tuple(scene.display.light_direction) == ld and sh.type == before[0]
       and not bpy.app.timers.is_registered(m._light_tick))

    # --- light test EEVEE (perspective camera)
    p.lt_mode = "EEVEE"
    with ctx.temp_override(window=main):
        bpy.ops.ptrace.light_test()
    yield 1.0
    sun = bpy.data.objects.get(m._light["sun"])
    ok("light test EEVEE creates a sun", sun is not None and sun.type == "LIGHT")
    p.lt_strength = 7.0
    ok("strength updates the sun", sun is not None and abs(sun.data.energy - 7.0) < 1e-6)
    with ctx.temp_override(window=main):
        bpy.ops.ptrace.light_test()
    yield 0.3
    ok("light test EEVEE stop removes the sun", not any(o.name.startswith(m.SUN_NAME) for o in bpy.data.objects))

    # --- light test running while the projector window closes
    p.lt_mode = "SOLID"
    with ctx.temp_override(window=main):
        bpy.ops.ptrace.light_test()
    yield 0.3
    with ctx.temp_override(window=main):
        bpy.ops.ptrace.projector_close()
    yield 0.5
    ok("closing projector stops the light test", not m._light["active"] and proj()[0] is None)
    ok("no draw errors at the end", m._draw_error is None, m._draw_error)


def run():
    gen = steps()

    def tick():
        try:
            return next(gen)
        except StopIteration:
            pass
        except Exception:
            results.append(("unexpected exception", False, traceback.format_exc()))
        finish()
        return None

    bpy.app.timers.register(tick, first_interval=1.0)


def finish():
    print("\n=== Projection Trace Tool GUI tests (Blender %s) ===" % bpy.app.version_string)
    for name, good, err in results:
        print("%s  %s%s" % ("PASS" if good else "FAIL", name, ("  -> " + err) if err else ""))
    failed = sum(not g for _n, g, _e in results)
    print("=== %d passed, %d failed ===" % (len(results) - failed, failed))
    sys.stdout.flush()
    m.unregister()
    os._exit(1 if failed else 0)


run()
