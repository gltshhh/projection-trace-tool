# SPDX-FileCopyrightText: 2026 precyzja.org
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Projection Trace Tool - video mapping: draw lines live in a viewport shown on a projector.

Install as an extension (Preferences > Get Extensions > Install from Disk) or run
this file from the Text Editor (Run Script); running it again reloads the add-on.

Panel: 3D View > Sidebar (N) > "Trace".
Projector: "Open on projector" opens a new window with a viewport, fullscreen on the
second monitor (Windows only). Clicking again closes the old window and opens a new one.
Ctrl+Shift+Q (from any window) closes the projector window.
Home (mouse over the projector window) scales the camera frame back to the full screen
after any zoom or pan, so the projection lines up with the object again.
Start: "Start trace (projector)" button or Ctrl+Shift+T with the mouse over a viewport.

While tracing:
  LMB click           - add a point
  LMB drag            - freehand (one point every "Freehand step" pixels)
  Backspace / Ctrl+Z  - remove the last point
  C                   - close / open the loop
  Enter / Space       - finish the line -> curve in the TRACE collection, start the next one
  Esc                 - discard the current line; a second Esc exits
  Home                - back to the full camera frame
  Scroll / MMB / Numpad - navigate the view as usual
Points are stored in 3D (projected onto the scan surface or onto a Z plane),
so zooming while drawing does not break anything.
"""

import ctypes
import math
import os
import sys

import bpy
import gpu
from bpy.app.handlers import persistent
from bpy.app.translations import pgettext_iface as iface_
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

rpt_ = getattr(bpy.app.translations, "pgettext_rpt", bpy.app.translations.pgettext_tip)

TRACE_COLLECTION = "TRACE"
PROJECTOR_TAG = "ptrace_projector"              # custom property on the projector window's screen
LEGACY_PROJECTOR_TAGS = ("_viewport_drugi_ekran",)  # tag used by version 1.0
SUN_NAME = "PTRACE_sun_test"
BLENDER_WINDOW_CLASS = "GHOST_WindowClass"
ORTHO_RAY_BACKOFF = 10000.0
RAYCAST_MAX_SKIPS = 32
NAV_EVENTS = {
    "MIDDLEMOUSE", "WHEELUPMOUSE", "WHEELDOWNMOUSE", "TRACKPADPAN", "TRACKPADZOOM",
    "HOME", "NDOF_MOTION", "NUMPAD_0", "NUMPAD_1", "NUMPAD_2", "NUMPAD_3", "NUMPAD_4",
    "NUMPAD_5", "NUMPAD_6", "NUMPAD_7", "NUMPAD_8", "NUMPAD_9", "NUMPAD_PERIOD",
    "NUMPAD_PLUS", "NUMPAD_MINUS",
}

# Areas are remembered by as_pointer() and looked up again on every use, so a closed
# window never leaves a dangling Python reference behind.
_trace = {"points": [], "mouse": None, "area_ptr": 0, "closed": False, "active": False, "id": 0}
_light = {"active": False, "step": 0, "backup": None, "area_ptr": 0, "mode": None,
          "scene": "", "sun": ""}
_dark_backup = {}
_keymaps = []
_draw_handle = None
_draw_error = None
_win32_api = None


# --- helpers ------------------------------------------------------------------

def _redraw():
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _status(text):
    for win in bpy.context.window_manager.windows:
        try:
            win.workspace.status_text_set(text)
        except (AttributeError, TypeError):
            pass


def _window_region(area):
    return next((r for r in area.regions if r.type == "WINDOW"), None)


def _find_area(ptr):
    if ptr:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                if area.as_pointer() == ptr:
                    return win, area
    return None, None


def _is_projector_screen(screen):
    return any(screen.get(tag) for tag in (PROJECTOR_TAG,) + LEGACY_PROJECTOR_TAGS)


def _projector_area():
    for win in bpy.context.window_manager.windows:
        if _is_projector_screen(win.screen):
            for area in win.screen.areas:
                if area.type == "VIEW_3D":
                    return win, area
    return None, None


def _in_trace_collection(ob):
    return any(c.name == TRACE_COLLECTION for c in ob.users_collection)


def _plane_hit(origin, direction, z):
    if abs(direction.z) < 1e-9:
        return None
    t = (z - origin.z) / direction.z
    if t < 0.0:
        return None  # plane is behind the viewer
    return origin + direction * t


def _surface_hit(context, origin, direction):
    """First hit on the target mesh, or on the scene while skipping trace objects."""
    p = context.scene.ptrace
    dg = context.evaluated_depsgraph_get()
    if p.target is not None:
        ob = p.target.evaluated_get(dg)
        inv = ob.matrix_world.inverted()
        ok, loc, _n, _i = ob.ray_cast(inv @ origin, (inv.to_3x3() @ direction).normalized())
        return ob.matrix_world @ loc if ok else None
    start = origin
    for _ in range(RAYCAST_MAX_SKIPS):
        ok, loc, _n, _i, ob, _m = context.scene.ray_cast(dg, start, direction)
        if not ok:
            return None
        if not _in_trace_collection(getattr(ob, "original", ob)):
            return loc
        start = loc + direction * 1e-4  # continue through finished traces
    return None


def _point_3d(context, region, rv3d, co2d):
    """Project a screen point onto the scan (raycast) or onto the Z plane."""
    p = context.scene.ptrace
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, co2d)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, co2d)
    if not rv3d.is_perspective:
        # in ortho the origin can land past the scene (negative clip) - move the ray start back
        origin = origin - direction * ORTHO_RAY_BACKOFF
    if p.projection == "SURFACE":
        hit = _surface_hit(context, origin, direction)
        if hit is not None:
            return hit
    return _plane_hit(origin, direction, p.plane_z)


def _trace_collection(scene):
    col = bpy.data.collections.get(TRACE_COLLECTION)
    if col is None:
        col = bpy.data.collections.new(TRACE_COLLECTION)
    if col.name not in scene.collection.children:
        try:
            scene.collection.children.link(col)
        except RuntimeError:
            pass
    return col


def _unique_name(prefix):
    i = 1
    while bpy.data.objects.get("%s_%03d" % (prefix, i)):
        i += 1
    return "%s_%03d" % (prefix, i)


def _create_curve(context, points, closed):
    col = _trace_collection(context.scene)
    name = _unique_name("trace")
    cu = bpy.data.curves.new(name, "CURVE")
    cu.dimensions = "3D"
    sp = cu.splines.new("POLY")
    sp.points.add(len(points) - 1)
    for pt, co in zip(sp.points, points):
        pt.co = (co.x, co.y, co.z, 1.0)
    sp.use_cyclic_u = closed
    ob = bpy.data.objects.new(name, cu)
    col.objects.link(ob)
    return ob


def _curve_points(ob):
    """List of (world_points, cyclic) for every spline."""
    out = []
    mw = ob.matrix_world
    for sp in ob.data.splines:
        if sp.type == "BEZIER":
            pts = [mw @ b.co for b in sp.bezier_points]
        else:
            pts = [mw @ Vector(p.co[:3]) for p in sp.points]
        out.append((pts, sp.use_cyclic_u))
    return out


# --- projector window (Windows API) ---------------------------------------------

class _Win32:
    """user32 with explicit signatures, on a private WinDLL so ctypes.windll stays untouched."""

    def __init__(self):
        import ctypes.wintypes as wt

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                        ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

        self.MONITORINFO = MONITORINFO
        self.MonitorEnumProc = ctypes.WINFUNCTYPE(
            wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
        self.WndEnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        u = ctypes.WinDLL("user32", use_last_error=True)
        for fn, res, args in (
            (u.EnumDisplayMonitors, wt.BOOL,
             (wt.HDC, ctypes.POINTER(wt.RECT), self.MonitorEnumProc, wt.LPARAM)),
            (u.GetMonitorInfoW, wt.BOOL, (wt.HMONITOR, ctypes.POINTER(MONITORINFO))),
            (u.EnumWindows, wt.BOOL, (self.WndEnumProc, wt.LPARAM)),
            (u.GetWindowThreadProcessId, wt.DWORD, (wt.HWND, ctypes.POINTER(wt.DWORD))),
            (u.IsWindowVisible, wt.BOOL, (wt.HWND,)),
            (u.GetClassNameW, ctypes.c_int, (wt.HWND, wt.LPWSTR, ctypes.c_int)),
            (u.ShowWindow, wt.BOOL, (wt.HWND, ctypes.c_int)),
            (u.SetWindowPos, wt.BOOL, (wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wt.UINT)),
        ):
            fn.restype = res
            fn.argtypes = args
        self.user32 = u
        self.wt = wt


def _win32():
    global _win32_api
    if sys.platform != "win32":
        raise RuntimeError(rpt_("The projector window is only supported on Windows"))
    if _win32_api is None:
        _win32_api = _Win32()
    return _win32_api


def _monitors():
    """List of (x, y, width, height, primary) in Windows pixels."""
    w = _win32()
    out = []

    def cb(hmon, _hdc, _rect, _lparam):
        mi = w.MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        if w.user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            out.append((r.left, r.top, r.right - r.left, r.bottom - r.top, bool(mi.dwFlags & 1)))
        return True

    w.user32.EnumDisplayMonitors(None, None, w.MonitorEnumProc(cb), 0)
    return out


def _process_windows():
    """Visible top-level windows of this process: {hwnd: class name}."""
    w = _win32()
    pid = os.getpid()
    out = {}

    def cb(hwnd, _lparam):
        owner = w.wt.DWORD()
        w.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and w.user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            w.user32.GetClassNameW(hwnd, buf, 256)
            out[hwnd] = buf.value
        return True

    w.user32.EnumWindows(w.WndEnumProc(cb), 0)
    return out


def _new_blender_window(before):
    new = {h: c for h, c in _process_windows().items() if h not in before}
    ghost = [h for h, c in new.items() if c == BLENDER_WINDOW_CLASS]
    if len(ghost) == 1:
        return ghost[0]
    if len(new) == 1:
        return next(iter(new))
    return None


def _projector_monitor(index):
    mons = _monitors()
    if index >= 0:
        if index >= len(mons):
            raise RuntimeError(rpt_("Monitor %d does not exist (found %d)") % (index, len(mons)))
        return mons[index]
    others = [m for m in mons if not m[4]]
    if not others:
        raise RuntimeError(rpt_("No second monitor found"))
    return others[0]


def _close_projector():
    wm = bpy.context.window_manager
    for win in list(wm.windows):
        if _is_projector_screen(win.screen) and len(wm.windows) > 1:
            ptrs = {a.as_pointer() for a in win.screen.areas}
            if _light["active"] and _light["area_ptr"] in ptrs:
                _light_stop()
            for ptr in ptrs:
                _dark_backup.pop(ptr, None)
            with bpy.context.temp_override(window=win):
                bpy.ops.wm.window_close()


def _setup_projector_viewport(context, win, p):
    screen = win.screen
    screen[PROJECTOR_TAG] = True
    area = max(screen.areas, key=lambda a: a.width * a.height)
    area.type = "VIEW_3D"
    for a in list(screen.areas):
        if a != area:
            with context.temp_override(window=win, screen=screen, area=a):
                try:
                    bpy.ops.screen.area_close()
                except RuntimeError:
                    pass
    space = area.spaces.active
    space.shading.type = p.proj_shading
    if p.proj_clean:
        space.show_region_header = False
        space.show_region_tool_header = False
        space.show_region_toolbar = False
        space.show_region_ui = False
        space.overlay.show_overlays = False
        space.show_gizmo = False
    _setup_projector_camera(context, win, area, p)


def _setup_projector_camera(context, win, area, p):
    """Selected camera as the viewport's local camera - the scene camera is left alone."""
    space = area.spaces.active
    if p.proj_camera is not None:
        space.use_local_camera = True
        space.camera = p.proj_camera
    else:
        space.use_local_camera = False
    if p.proj_use_camera and (p.proj_camera or context.scene.camera):
        space.region_3d.view_perspective = "CAMERA"
        space.lock_camera = False
        with context.temp_override(window=win, area=area, region=_window_region(area)):
            bpy.ops.view3d.view_center_camera()
    area.tag_redraw()


def _on_camera_change(self, context):
    win, area = _projector_area()
    if area is not None:
        _setup_projector_camera(context, win, area, self)


# --- drawing --------------------------------------------------------------------

def _lines(shader, coords, color, width, region):
    if len(coords) < 2:
        return
    batch = batch_for_shader(shader, "LINES", {"pos": coords})
    shader.bind()
    shader.uniform_float("viewportSize", (region.width, region.height))
    shader.uniform_float("lineWidth", width)
    shader.uniform_float("color", color)
    batch.draw(shader)


def _segments(pts2d, closed):
    pairs = []
    for a, b in zip(pts2d, pts2d[1:]):
        if a is not None and b is not None:
            pairs += [a, b]
    if closed and len(pts2d) > 2 and pts2d[-1] is not None and pts2d[0] is not None:
        pairs += [pts2d[-1], pts2d[0]]
    return pairs


def _point_shader():
    try:
        return gpu.shader.from_builtin("POINT_UNIFORM_COLOR")
    except ValueError:  # added in Blender 4.5; before that UNIFORM_COLOR draws points
        return gpu.shader.from_builtin("UNIFORM_COLOR")


def _draw():
    global _draw_error
    try:
        _draw_impl()
    except Exception as ex:  # an error in a draw handler must not break the viewport
        if repr(ex) != _draw_error:
            print("Projection Trace Tool: draw error:", repr(ex))
        _draw_error = repr(ex)
        gpu.state.point_size_set(1.0)
        gpu.state.blend_set("NONE")


def _draw_impl():
    ctx = bpy.context
    p = getattr(ctx.scene, "ptrace", None)
    region, rv3d, area = ctx.region, ctx.region_data, ctx.area
    if p is None or rv3d is None or not p.show_lines:
        return

    def project(co):
        return view3d_utils.location_3d_to_region_2d(region, rv3d, co)

    poly = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")

    # finished traces
    col = bpy.data.collections.get(TRACE_COLLECTION)
    if col is not None and p.show_done:
        pairs = []
        for ob in col.all_objects:
            if ob.type == "CURVE" and ob.visible_get():
                for pts, cyclic in _curve_points(ob):
                    pairs += _segments([project(c) for c in pts], cyclic)
        _lines(poly, pairs, p.color_done, p.line_width, region)

    # current line
    if _trace["active"]:
        pts2d = [project(c) for c in _trace["points"]]
        pairs = _segments(pts2d, _trace["closed"])
        here = area is not None and area.as_pointer() == _trace["area_ptr"]
        mouse = _trace["mouse"] if here else None
        if mouse is not None and pts2d and pts2d[-1] is not None:
            pairs += [pts2d[-1], mouse]
        _lines(poly, pairs, p.color, p.line_width, region)

        points = [c for c in pts2d if c is not None]
        if points:
            pt_sh = _point_shader()
            gpu.state.point_size_set(p.line_width * 2.5)
            batch = batch_for_shader(pt_sh, "POINTS", {"pos": points})
            pt_sh.bind()
            pt_sh.uniform_float("color", p.color)
            batch.draw(pt_sh)
            gpu.state.point_size_set(1.0)

        if mouse is not None and p.crosshair:
            x, y = mouse
            cross = [(0, y), (region.width, y), (x, 0), (x, region.height)]
            _lines(poly, cross, p.color_crosshair, max(1.0, p.line_width * 0.4), region)

    gpu.state.blend_set("NONE")


# --- operators ------------------------------------------------------------------

def _reset_trace():
    _trace["id"] += 1  # a running modal sees a stale id and ends on its next event
    _trace.update(points=[], mouse=None, area_ptr=0, closed=False, active=False)


class PTRACE_OT_trace(bpy.types.Operator):
    """Draw lines in the viewport (LMB points / drag, Enter finishes, Esc exits)"""
    bl_idname = "ptrace.trace"
    bl_label = "Trace"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == "VIEW_3D"

    def invoke(self, context, event):
        # a new start takes over drawing; the previous modal ends by itself
        _trace["id"] += 1
        self.run_id = _trace["id"]
        self.area_ptr = context.area.as_pointer()
        self.dragging = False
        _trace.update(points=[], mouse=None, area_ptr=self.area_ptr, closed=False, active=True)
        context.window_manager.modal_handler_add(self)
        self._info()
        _redraw()
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        # called by Blender when the window closes or a file is loaded
        if _trace["id"] == self.run_id:
            _reset_trace()
            _status(None)

    def _info(self):
        loop = iface_("  [LOOP]") if _trace["closed"] else ""
        _status(iface_("TRACE  points: %d%s  |  LMB point/drag  Backspace undo  C loop  "
                       "Enter finish  Esc exit") % (len(_trace["points"]), loop))

    def _view(self):
        _win, area = _find_area(self.area_ptr)
        if area is None or area.type != "VIEW_3D":
            return None, None
        return _window_region(area), area.spaces.active.region_3d

    @staticmethod
    def _mouse_in_region(region, event):
        x = event.mouse_x - region.x
        y = event.mouse_y - region.y
        if 0 <= x < region.width and 0 <= y < region.height:
            return Vector((x, y))
        return None

    def _add(self, context, region, rv3d, co2d):
        co = _point_3d(context, region, rv3d, co2d)
        if co is not None:
            _trace["points"].append(co.copy())

    def _finish_line(self, context):
        if len(_trace["points"]) >= 2:
            ob = _create_curve(context, _trace["points"], _trace["closed"])
            bpy.ops.ed.undo_push(message="Trace " + ob.name)
            self.report({"INFO"}, rpt_("Saved %s") % ob.name)
        _trace["points"] = []
        _trace["closed"] = False

    def _exit(self):
        _reset_trace()
        _status(None)
        _redraw()
        return {"FINISHED"}

    def modal(self, context, event):
        if _trace["id"] != self.run_id:
            return {"CANCELLED"}  # taken over by a newer start / reset
        region, rv3d = self._view()
        if region is None:
            return self._exit()  # the area is gone

        t, v = event.type, event.value

        if t in NAV_EVENTS or (event.ctrl and event.alt and t == "F"):
            _redraw()
            return {"PASS_THROUGH"}

        if t == "MOUSEMOVE":
            m = self._mouse_in_region(region, event)
            _trace["mouse"] = m
            if self.dragging and m is not None and _trace["points"]:
                last = view3d_utils.location_3d_to_region_2d(region, rv3d, _trace["points"][-1])
                if last is None or (m - last).length >= context.scene.ptrace.step_px:
                    self._add(context, region, rv3d, m)
                    self._info()
            _redraw()
            return {"RUNNING_MODAL"}

        if t == "LEFTMOUSE":
            if v == "PRESS":
                m = self._mouse_in_region(region, event)
                if m is None:
                    return {"PASS_THROUGH"}
                self._add(context, region, rv3d, m)
                self.dragging = True
            elif v == "RELEASE":
                self.dragging = False
            self._info()
            _redraw()
            return {"RUNNING_MODAL"}

        if v != "PRESS":
            return {"RUNNING_MODAL"}

        if t == "BACK_SPACE" or (t == "Z" and event.ctrl):
            if _trace["points"]:
                _trace["points"].pop()
        elif t == "C":
            _trace["closed"] = not _trace["closed"]
        elif t in {"RET", "NUMPAD_ENTER", "SPACE"}:
            self._finish_line(context)
        elif t == "ESC":
            if _trace["points"]:
                _trace["points"] = []
                _trace["closed"] = False
            else:
                return self._exit()
        self._info()
        _redraw()
        return {"RUNNING_MODAL"}


class PTRACE_OT_projector_open(bpy.types.Operator):
    """Open the viewport in a new window, fullscreen on the second monitor (projector)"""
    bl_idname = "ptrace.projector_open"
    bl_label = "Open on projector"

    def execute(self, context):
        p = context.scene.ptrace
        try:
            x, y, w, h, _ = _projector_monitor(p.proj_monitor)
            api = _win32()
        except (RuntimeError, OSError) as ex:
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}

        _close_projector()
        wm = context.window_manager
        main = wm.windows[0]
        hwnds_before = set(_process_windows())
        wins_before = set(wm.windows[:])
        with context.temp_override(window=main):
            bpy.ops.wm.window_new()
        new = [win for win in wm.windows if win not in wins_before]
        if not new:
            self.report({"ERROR"}, rpt_("Blender did not create a new window"))
            return {"CANCELLED"}
        win = new[0]
        _setup_projector_viewport(context, win, p)

        hwnd = _new_blender_window(hwnds_before)
        if hwnd is None:
            self.report({"WARNING"}, rpt_("Projector window not found, move it to the projector manually"))
            return {"FINISHED"}
        api.user32.ShowWindow(hwnd, 9)                          # SW_RESTORE
        api.user32.SetWindowPos(hwnd, None, x, y, w, h, 0x0040)  # SWP_SHOWWINDOW

        def fullscreen():
            if win in bpy.context.window_manager.windows[:]:
                with bpy.context.temp_override(window=win):
                    bpy.ops.wm.window_fullscreen_toggle()
            return None

        # go fullscreen only once the window has actually moved to the other monitor
        bpy.app.timers.register(fullscreen, first_interval=0.3)
        self.report({"INFO"}, rpt_("Projector %dx%d at (%d, %d)") % (w, h, x, y))
        return {"FINISHED"}


class PTRACE_OT_projector_close(bpy.types.Operator):
    """Close the projector window (Ctrl+Shift+Q)"""
    bl_idname = "ptrace.projector_close"
    bl_label = "Close projector"

    def execute(self, context):
        if _trace["active"]:
            bpy.ops.ptrace.reset()

        # close from a timer - the shortcut may come from inside the window being closed
        def close():
            _close_projector()
            return None

        bpy.app.timers.register(close, first_interval=0.05)
        return {"FINISHED"}


class PTRACE_OT_start(bpy.types.Operator):
    """Start tracing in the projector window (or the current viewport)"""
    bl_idname = "ptrace.start"
    bl_label = "Start trace (projector)"

    def execute(self, context):
        win, area = _projector_area()
        if area is None:
            win, area = context.window, context.area
            if area is None or area.type != "VIEW_3D":
                self.report({"ERROR"}, rpt_("No projector window, click 'Open on projector'"))
                return {"CANCELLED"}
        with context.temp_override(window=win, screen=win.screen, area=area,
                                   region=_window_region(area)):
            bpy.ops.ptrace.trace("INVOKE_DEFAULT")
        return {"FINISHED"}


class PTRACE_OT_reset(bpy.types.Operator):
    """Stop tracing and discard the unsaved line (finished traces stay)"""
    bl_idname = "ptrace.reset"
    bl_label = "Stop / clear line"

    def execute(self, context):
        _reset_trace()
        _status(None)
        _redraw()
        return {"FINISHED"}


_DARK_KEYS = ("light", "color_type", "single_color", "background_type", "background_color",
              "show_cavity", "show_shadows", "type")


def _dark_target(context):
    _win, area = _projector_area()
    if area is None and context.area is not None and context.area.type == "VIEW_3D":
        area = context.area
    return area


class PTRACE_OT_dark_background(bpy.types.Operator):
    """Toggle the projector between a black background (lines only) and the normal view"""
    bl_idname = "ptrace.dark_background"
    bl_label = "Black background on projector"

    @classmethod
    def poll(cls, context):
        return _dark_target(context) is not None

    def execute(self, context):
        area = _dark_target(context)
        sh = area.spaces.active.shading
        key = area.as_pointer()
        is_dark = sh.type == "SOLID" and sh.background_type == "VIEWPORT" \
            and tuple(sh.background_color) == (0.0, 0.0, 0.0) and sh.color_type == "SINGLE"
        if is_dark:
            backup = _dark_backup.pop(key, None)
            if backup:
                for k in _DARK_KEYS:
                    setattr(sh, k, backup[k])
            else:
                sh.type = "MATERIAL"
                sh.background_type = "THEME"
        else:
            _dark_backup[key] = {k: (tuple(getattr(sh, k)) if k.endswith("color") else getattr(sh, k))
                                 for k in _DARK_KEYS}
            sh.type = "SOLID"
            sh.light = "FLAT"
            sh.color_type = "SINGLE"
            sh.single_color = (0.0, 0.0, 0.0)
            sh.background_type = "VIEWPORT"
            sh.background_color = (0.0, 0.0, 0.0)
            sh.show_cavity = False
            sh.show_shadows = False
        area.tag_redraw()
        return {"FINISHED"}


class PTRACE_OT_to_mesh(bpy.types.Operator):
    """Convert selected trace curves to meshes (closed loop -> filled face)"""
    bl_idname = "ptrace.to_mesh"
    bl_label = "Selected traces to mesh"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        curves = [o for o in context.selected_objects if o.type == "CURVE"]
        if not curves:
            self.report({"WARNING"}, rpt_("Select trace curves"))
            return {"CANCELLED"}
        col = _trace_collection(context.scene)
        for ob in curves:
            verts, edges, faces = [], [], []
            for pts, cyclic in _curve_points(ob):
                if not pts:
                    continue
                s = len(verts)
                verts += [tuple(c) for c in pts]
                idx = list(range(s, len(verts)))
                edges += list(zip(idx, idx[1:]))
                if cyclic and len(idx) > 2:
                    faces.append(idx)
                elif cyclic and len(idx) == 2:
                    edges.append((idx[-1], idx[0]))
            me = bpy.data.meshes.new(ob.name + "_mesh")
            me.from_pydata(verts, edges, faces)
            me.update()
            mo = bpy.data.objects.new(ob.name + "_mesh", me)
            col.objects.link(mo)
        self.report({"INFO"}, rpt_("Created %d mesh(es)") % len(curves))
        return {"FINISHED"}


# --- light test -------------------------------------------------------------------
# The sun jumps between azimuths around the model - a preview of how shadows fall.
# SOLID: Workbench shadows (scene.display.light_direction), no EEVEE.
# EEVEE: temporary sun in the scene + Material Preview with scene lights.

def _sun_direction(az_deg, el_deg):
    """Vector from the model towards the sun."""
    az, el = math.radians(az_deg), math.radians(el_deg)
    return Vector((math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)))


def _apply_light(scene):
    p = scene.ptrace
    n = max(1, p.lt_steps)
    az = 360.0 * (_light["step"] % n) / n
    direction = _sun_direction(az, p.lt_elevation)
    if _light["mode"] == "SOLID":
        scene.display.light_direction = direction
    else:
        ob = bpy.data.objects.get(_light["sun"])
        if ob is not None:
            ob.rotation_euler = direction.to_track_quat("Z", "Y").to_euler()  # sun shines along -Z
    _status(iface_("LIGHT TEST  azimuth %3.0f°  elevation %2.0f°  (%d/%d)")
            % (az, p.lt_elevation, _light["step"] % n + 1, n))
    _redraw()


def _light_tick():
    if not _light["active"]:
        return None
    scene = bpy.data.scenes.get(_light["scene"])
    if scene is None:
        _light_stop()
        return None
    p = scene.ptrace
    if p.lt_auto:
        _light["step"] += 1
        _apply_light(scene)
    return max(0.2, p.lt_interval)


def _light_start(context):
    p = context.scene.ptrace
    _win, area = _projector_area()
    if area is None:
        area = context.area if context.area and context.area.type == "VIEW_3D" else None
    if area is None:
        raise RuntimeError(rpt_("No viewport, open the projector first"))
    sh = area.spaces.active.shading
    _light.update(active=True, step=0, area_ptr=area.as_pointer(), mode=p.lt_mode,
                  scene=context.scene.name, sun="", backup={
                      "type": sh.type, "show_shadows": sh.show_shadows,
                      "shadow_intensity": sh.shadow_intensity,
                      "use_scene_lights": sh.use_scene_lights,
                      "use_scene_world": sh.use_scene_world,
                      "light_direction": tuple(context.scene.display.light_direction)})
    if p.lt_mode == "SOLID":
        sh.type = "SOLID"
        sh.show_shadows = True
        sh.shadow_intensity = p.lt_shadow
    else:
        data = bpy.data.lights.new(SUN_NAME, "SUN")
        data.energy = p.lt_strength
        ob = bpy.data.objects.new(SUN_NAME, data)
        context.scene.collection.objects.link(ob)
        _light["sun"] = ob.name
        sh.type = "MATERIAL"
        sh.use_scene_lights = True
        sh.use_scene_world = False
    _apply_light(context.scene)
    bpy.app.timers.register(_light_tick, first_interval=max(0.2, p.lt_interval))


def _light_stop():
    if bpy.app.timers.is_registered(_light_tick):
        bpy.app.timers.unregister(_light_tick)
    b = _light["backup"]
    if b:
        _win, area = _find_area(_light["area_ptr"])
        if area is not None and area.type == "VIEW_3D":  # the projector window may be closed
            sh = area.spaces.active.shading
            for k in ("type", "show_shadows", "shadow_intensity", "use_scene_lights", "use_scene_world"):
                setattr(sh, k, b[k])
        scene = bpy.data.scenes.get(_light["scene"])
        if scene is not None:
            scene.display.light_direction = b["light_direction"]
    ob = bpy.data.objects.get(_light["sun"]) if _light["sun"] else None
    if ob is not None and ob.type == "LIGHT":
        data = ob.data
        bpy.data.objects.remove(ob)
        if data is not None and data.users == 0:
            bpy.data.lights.remove(data)
    _light.update(active=False, backup=None, area_ptr=0, mode=None, scene="", sun="")
    _status(None)
    _redraw()


def _on_light_change(self, context):
    if not _light["active"]:
        return
    if _light["mode"] == "SOLID":
        _win, area = _find_area(_light["area_ptr"])
        if area is not None and area.type == "VIEW_3D":
            area.spaces.active.shading.shadow_intensity = self.lt_shadow
    else:
        ob = bpy.data.objects.get(_light["sun"])
        if ob is not None:
            ob.data.energy = self.lt_strength
    _apply_light(context.scene)


class PTRACE_OT_light_test(bpy.types.Operator):
    """Toggle the light test: the sun jumps between angles around the model"""
    bl_idname = "ptrace.light_test"
    bl_label = "Light test"

    def execute(self, context):
        if _light["active"]:
            _light_stop()
            return {"FINISHED"}
        try:
            _light_start(context)
        except RuntimeError as ex:
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        return {"FINISHED"}


class PTRACE_OT_light_step(bpy.types.Operator):
    """Next sun angle"""
    bl_idname = "ptrace.light_step"
    bl_label = "Next angle"

    def execute(self, context):
        if not _light["active"]:
            self.report({"WARNING"}, rpt_("Light test is not running"))
            return {"CANCELLED"}
        _light["step"] += 1
        _apply_light(context.scene)
        return {"FINISHED"}


@persistent
def _on_load_pre(*_args):
    """A loaded file drops modal operators and timers - drop our state with them."""
    if _light["active"]:
        _light_stop()
    if _trace["active"]:
        _reset_trace()
        _status(None)
    _dark_backup.clear()


# --- settings and panel -----------------------------------------------------------

def _poll_mesh(_self, ob):
    return ob.type == "MESH"


def _poll_camera(_self, ob):
    return ob.type == "CAMERA"


class PTRACE_Props(bpy.types.PropertyGroup):
    projection: bpy.props.EnumProperty(
        name="Projection", default="SURFACE",
        items=[("SURFACE", "On scan", "Points land on mesh surfaces (raycast)"),
               ("PLANE", "On Z plane", "Points land on a horizontal plane at height Z")])
    target: bpy.props.PointerProperty(
        name="Only object", type=bpy.types.Object, poll=_poll_mesh,
        description="Raycast only against this mesh (faster). Empty = whole scene")
    plane_z: bpy.props.FloatProperty(name="Plane Z", default=0.0, unit="LENGTH")
    step_px: bpy.props.FloatProperty(name="Freehand step (px)", default=12.0, min=2.0, max=200.0)
    line_width: bpy.props.FloatProperty(name="Line width", default=4.0, min=1.0, max=30.0)
    color: bpy.props.FloatVectorProperty(
        name="Line color", subtype="COLOR_GAMMA", size=4, min=0, max=1,
        default=(0.0, 1.0, 0.2, 1.0))
    color_done: bpy.props.FloatVectorProperty(
        name="Finished color", subtype="COLOR_GAMMA", size=4, min=0, max=1,
        default=(1.0, 0.1, 0.8, 1.0))
    color_crosshair: bpy.props.FloatVectorProperty(
        name="Crosshair color", subtype="COLOR_GAMMA", size=4, min=0, max=1,
        default=(1.0, 1.0, 1.0, 0.6))
    crosshair: bpy.props.BoolProperty(name="Crosshair", default=True)
    show_lines: bpy.props.BoolProperty(name="Show lines", default=True)
    show_done: bpy.props.BoolProperty(name="Show finished traces", default=True)
    proj_monitor: bpy.props.IntProperty(
        name="Monitor", default=-1, min=-1, max=16,
        description="-1 = first non-primary monitor, 0, 1, ... = specific monitor")
    proj_use_camera: bpy.props.BoolProperty(
        name="Camera view", default=True, update=_on_camera_change,
        description="Look through the projector camera")
    proj_camera: bpy.props.PointerProperty(
        name="Camera", type=bpy.types.Object, update=_on_camera_change, poll=_poll_camera,
        description="Projector camera (projector window only). Empty = active scene camera")
    proj_shading: bpy.props.EnumProperty(
        name="Shading", default="MATERIAL",
        items=[("SOLID", "Solid", ""), ("MATERIAL", "Material", ""), ("RENDERED", "Rendered", "")])
    proj_clean: bpy.props.BoolProperty(
        name="Clean view", default=True,
        description="No headers, panels, overlays or gizmos")
    lt_mode: bpy.props.EnumProperty(
        name="Mode", default="SOLID",
        items=[("SOLID", "Solid", "Workbench shadows, lightweight, no EEVEE"),
               ("EEVEE", "EEVEE", "Temporary sun + Material Preview (heavier)")])
    lt_elevation: bpy.props.FloatProperty(
        name="Elevation", default=35.0, min=5.0, max=90.0, update=_on_light_change,
        description="Sun height above the horizon (degrees)")
    lt_steps: bpy.props.IntProperty(
        name="Angles around", default=8, min=2, max=36, update=_on_light_change,
        description="How many azimuths a full turn is split into")
    lt_interval: bpy.props.FloatProperty(
        name="Interval (s)", default=2.0, min=0.2, max=30.0)
    lt_auto: bpy.props.BoolProperty(
        name="Auto", default=True, description="Advance automatically; off = button only")
    lt_shadow: bpy.props.FloatProperty(
        name="Shadow strength", default=0.8, min=0.0, max=1.0, update=_on_light_change)
    lt_strength: bpy.props.FloatProperty(
        name="Sun strength", default=4.0, min=0.0, max=50.0, update=_on_light_change)


class PTRACE_PT_panel(bpy.types.Panel):
    bl_label = "Projection Trace Tool"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Trace"

    def draw(self, context):
        p = context.scene.ptrace
        lay = self.layout
        box = lay.box()
        row = box.row(align=True)
        row.operator("ptrace.projector_open", icon="WINDOW")
        row.operator("ptrace.projector_close", text="", icon="X")
        box.prop(p, "proj_monitor")
        box.prop(p, "proj_camera", icon="CAMERA_DATA")
        row = box.row()
        row.prop(p, "proj_use_camera")
        row.prop(p, "proj_clean")
        box.prop(p, "proj_shading")
        lay.operator("ptrace.start", icon="GREASEPENCIL")
        lay.operator("ptrace.reset", icon="X")
        lay.operator("ptrace.dark_background", icon="SHADING_SOLID")
        box = lay.box()
        box.prop(p, "projection")
        if p.projection == "SURFACE":
            box.prop(p, "target")
        box.prop(p, "plane_z")
        box.prop(p, "step_px")
        box = lay.box()
        box.prop(p, "line_width")
        box.prop(p, "color")
        box.prop(p, "color_done")
        row = box.row()
        row.prop(p, "crosshair")
        row.prop(p, "color_crosshair", text="")
        box.prop(p, "show_lines")
        box.prop(p, "show_done")
        lay.operator("ptrace.to_mesh", icon="MESH_DATA")
        box = lay.box()
        row = box.row(align=True)
        row.operator("ptrace.light_test", depress=_light["active"],
                     text=iface_("Stop light test") if _light["active"] else iface_("Light test"),
                     icon="LIGHT_SUN")
        row.operator("ptrace.light_step", text="", icon="FRAME_NEXT")
        sub = box.row()
        sub.enabled = not _light["active"]
        sub.prop(p, "lt_mode", expand=True)
        box.prop(p, "lt_elevation")
        box.prop(p, "lt_steps")
        row = box.row()
        row.prop(p, "lt_auto")
        row.prop(p, "lt_interval")
        box.prop(p, "lt_shadow" if p.lt_mode == "SOLID" else "lt_strength")


# --- translations -----------------------------------------------------------------

_PL = (
    ("Open on projector", "Otwórz na projektorze"),
    ("Close projector", "Zamknij projektor"),
    ("Start trace (projector)", "Start trace (projektor)"),
    ("Stop / clear line", "Przerwij / wyczyść linię"),
    ("Black background on projector", "Czarne tło na projektorze"),
    ("Selected traces to mesh", "Zaznaczone trace -> mesh"),
    ("Stop light test", "Zatrzymaj light test"),
    ("Next angle", "Następny kąt"),
    ("Draw lines in the viewport (LMB points / drag, Enter finishes, Esc exits)",
     "Rysuj linie w viewporcie (LPM punkty / przeciąganie, Enter kończy, Esc wychodzi)"),
    ("Open the viewport in a new window, fullscreen on the second monitor (projector)",
     "Otwórz viewport w nowym oknie, fullscreen na drugim monitorze (projektorze)"),
    ("Close the projector window (Ctrl+Shift+Q)", "Zamknij okno projektora (Ctrl+Shift+Q)"),
    ("Start tracing in the projector window (or the current viewport)",
     "Uruchom trace w oknie projektora (albo w bieżącym viewporcie)"),
    ("Stop tracing and discard the unsaved line (finished traces stay)",
     "Przerwij trace i skasuj niezapisaną linię (gotowe trace zostają)"),
    ("Toggle the projector between a black background (lines only) and the normal view",
     "Przełącz projektor: czarne tło (widać tylko linie) / normalny podgląd"),
    ("Convert selected trace curves to meshes (closed loop -> filled face)",
     "Zamień zaznaczone krzywe trace na mesh (pętla zamknięta -> wypełniona ściana)"),
    ("Toggle the light test: the sun jumps between angles around the model",
     "Włącz/wyłącz light test: słońce przeskakuje po kątach dookoła makiety"),
    ("Next sun angle", "Następny kąt słońca"),
    ("Projection", "Rzut"),
    ("On scan", "Na skan"),
    ("On Z plane", "Na płaszczyznę Z"),
    ("Points land on mesh surfaces (raycast)", "Punkt ląduje na powierzchni meshy (raycast)"),
    ("Points land on a horizontal plane at height Z", "Punkt ląduje na płaszczyźnie o wysokości Z"),
    ("Only object", "Tylko obiekt"),
    ("Raycast only against this mesh (faster). Empty = whole scene",
     "Raycast tylko w ten mesh (szybciej). Puste = cała scena"),
    ("Plane Z", "Z płaszczyzny"),
    ("Freehand step (px)", "Krok odręcznie (px)"),
    ("Line width", "Grubość linii"),
    ("Line color", "Kolor linii"),
    ("Finished color", "Kolor gotowych"),
    ("Crosshair color", "Kolor celownika"),
    ("Crosshair", "Celownik"),
    ("Show lines", "Pokazuj linie"),
    ("Show finished traces", "Pokazuj gotowe trace"),
    ("-1 = first non-primary monitor, 0, 1, ... = specific monitor",
     "-1 = pierwszy monitor niebędący głównym, 0, 1, ... = konkretny monitor"),
    ("Camera view", "Widok z kamery"),
    ("Look through the projector camera", "Patrz przez kamerę projektora"),
    ("Projector camera (projector window only). Empty = active scene camera",
     "Kamera projektora (tylko w oknie projektora). Puste = aktywna kamera sceny"),
    ("Clean view", "Czysty widok"),
    ("No headers, panels, overlays or gizmos", "Bez nagłówków, paneli, overlayów i gizmo"),
    ("Mode", "Tryb"),
    ("Workbench shadows, lightweight, no EEVEE", "Cienie Workbench – lekkie, bez EEVEE"),
    ("Temporary sun + Material Preview (heavier)", "Tymczasowe słońce + Material Preview (cięższe)"),
    ("Elevation", "Elewacja"),
    ("Sun height above the horizon (degrees)", "Wysokość słońca nad horyzontem (stopnie)"),
    ("Angles around", "Kąty dookoła"),
    ("How many azimuths a full turn is split into", "Na ile azymutów dzielić pełny obrót"),
    ("Interval (s)", "Co ile s"),
    ("Advance automatically; off = button only", "Przeskakuj samo; wyłączone = tylko przycisk"),
    ("Shadow strength", "Siła cienia"),
    ("Sun strength", "Moc słońca"),
    ("Saved %s", "Zapisano %s"),
    ("Blender did not create a new window", "Blender nie utworzył nowego okna"),
    ("Projector window not found, move it to the projector manually",
     "Nie znaleziono okna – przesuń je na projektor ręcznie"),
    ("Projector %dx%d at (%d, %d)", "Projektor %dx%d @ (%d, %d)"),
    ("No projector window, click 'Open on projector'",
     "Brak okna projektora – kliknij „Otwórz na projektorze”"),
    ("Select trace curves", "Zaznacz krzywe trace"),
    ("Created %d mesh(es)", "Utworzono %d mesh"),
    ("Light test is not running", "Light test nie działa"),
    ("No viewport, open the projector first", "Brak viewportu – otwórz projektor"),
    ("The projector window is only supported on Windows", "Okno projektora działa tylko na Windows"),
    ("Monitor %d does not exist (found %d)", "Nie ma monitora %d (są %d)"),
    ("No second monitor found", "Nie znaleziono drugiego monitora"),
    ("TRACE  points: %d%s  |  LMB point/drag  Backspace undo  C loop  Enter finish  Esc exit",
     "TRACE  punkty: %d%s  |  LPM punkt/przeciągnij  Backspace cofnij  C pętla  "
     "Enter zakończ  Esc wyjdź"),
    ("  [LOOP]", "  [PĘTLA]"),
    ("LIGHT TEST  azimuth %3.0f°  elevation %2.0f°  (%d/%d)",
     "LIGHT TEST  azymut %3.0f°  elewacja %2.0f°  (%d/%d)"),
)


def _translations():
    pl = {}
    for msgid, msgstr in _PL:
        for ctx in ("*", "Operator"):
            pl[(ctx, msgid)] = msgstr
    return {"pl_PL": pl}


# --- registration -----------------------------------------------------------------

CLASSES = (PTRACE_Props, PTRACE_OT_trace, PTRACE_OT_projector_open, PTRACE_OT_projector_close,
           PTRACE_OT_start, PTRACE_OT_reset, PTRACE_OT_dark_background, PTRACE_OT_to_mesh,
           PTRACE_OT_light_test, PTRACE_OT_light_step, PTRACE_PT_panel)


def _unload_other_copy():
    """Unregister another loaded copy (an older version or a previous Run Script)."""
    old = getattr(bpy.types, "PTRACE_OT_trace", None)
    if old is None or old.modal.__globals__ is globals():
        return
    g = old.modal.__globals__
    state = g.get("_trace") or g.get("_stan") or {}
    if state.get("active") or state.get("aktywny"):
        # unregistering a running modal operator can crash Blender
        raise RuntimeError("Projection Trace Tool: stop tracing (Esc twice) before reloading")
    g["unregister"]()


def register():
    global _draw_handle
    _unload_other_copy()
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.ptrace = bpy.props.PointerProperty(type=PTRACE_Props)
    _draw_handle = bpy.types.SpaceView3D.draw_handler_add(_draw, (), "WINDOW", "POST_PIXEL")
    bpy.app.handlers.load_pre.append(_on_load_pre)
    bpy.app.translations.register(__name__, _translations())
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="3D View", space_type="VIEW_3D")
        kmi = km.keymap_items.new("ptrace.trace", "T", "PRESS", ctrl=True, shift=True)
        _keymaps.append((km, kmi))
        km = kc.keymaps.new(name="Window", space_type="EMPTY", region_type="WINDOW")
        kmi = km.keymap_items.new("ptrace.projector_close", "Q", "PRESS", ctrl=True, shift=True)
        _keymaps.append((km, kmi))


def unregister():
    global _draw_handle
    if _light["active"]:
        _light_stop()
    if bpy.app.timers.is_registered(_light_tick):
        bpy.app.timers.unregister(_light_tick)
    if _trace["active"]:
        _reset_trace()
        _status(None)
    for km, kmi in _keymaps:
        try:
            km.keymap_items.remove(kmi)
        except (ReferenceError, RuntimeError):
            pass
    _keymaps.clear()
    try:
        bpy.app.translations.unregister(__name__)
    except (ValueError, RuntimeError):
        pass
    if _on_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_on_load_pre)
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, "WINDOW")
        _draw_handle = None
    del bpy.types.Scene.ptrace
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
