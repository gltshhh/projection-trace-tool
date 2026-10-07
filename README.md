# Projection Trace Tool

**Projection mapping and video mapping add-on for Blender.** Trace lines live through a
projector onto a physical model, see the result in 3D, and preview shadows before the show.

Built for projection mapping on physical objects: architectural and scale models, museum
installations, stage sets and building facades. You light the real object with the
projector straight from a Blender viewport, draw on it, and the lines are saved in 3D on
your scan or model, ready for animation and content work.

- **Projector window**: opens a viewport in a new window, fullscreen on the second
  monitor, looking through a chosen camera (the scene camera is left alone).
- **Trace**: draw lines in that window. Points land in 3D on the scanned surface (or on a
  horizontal plane), so zooming while drawing breaks nothing. Lines are saved as curves
  in the `TRACE` collection and can be converted to meshes.
- **Light test**: the sun jumps around the model in steps (Workbench shadows or a
  temporary EEVEE sun).

The UI is in English, with a Polish translation (Preferences > Interface > Translation).

Made by [precyzja.org](https://precyzja.org), a 3D video mapping and immersive
projection studio, for the [r/video_mapping](https://www.reddit.com/r/video_mapping/)
community. Questions, ideas and projects made with it are welcome there.

## Requirements

- Blender 4.2 or newer (tested on 4.3, 4.5 and 5.1).
- The projector window (moving it to a monitor) uses the Windows API and works on
  Windows only. Tracing and the light test work on every platform; elsewhere, move the
  window to the projector by hand.

## Installation

Download `projection_trace_tool-<version>.zip` from [Releases](https://github.com/gltshhh/projection-trace-tool/releases/latest), then in Blender:
Edit > Preferences > Get Extensions > ⌄ > **Install from Disk**.

If you used version 1.0 (named Projector Trace, `projector_trace.py` run as a
script or a legacy add-on), disable it first. Saved settings from 1.0 are not carried
over, because properties were renamed to English; traces in the `TRACE` collection
are kept.

## Usage

Panel: 3D View > Sidebar (N) > **Trace**.

| Action | How |
|---|---|
| Open the projector window | **Open on projector** (Monitor −1 = first non-primary) |
| Close the projector window | ✕ next to it, or **Ctrl+Shift+Q** from any window |
| Start tracing | **Start trace (projector)**, or **Ctrl+Shift+T** over a viewport |
| Black background (lines only) | **Black background on projector** (toggle; restores the previous shading) |
| Curves to meshes | Select traces > **Selected traces to mesh** (closed loop gives a filled face) |
| Light test | **Light test** (toggle), ⏭ for the next angle |

While tracing:

| Key | Action |
|---|---|
| LMB click / drag | add a point / draw freehand (a point every *Freehand step* px) |
| Backspace, Ctrl+Z | remove the last point |
| C | close / open the loop |
| Enter, Space | save the line as a curve and start the next one |
| Esc | discard the current line; a second Esc exits |
| MMB, scroll, Home, numpad, Ctrl+Alt+F | navigate as usual |

Other keys are blocked while tracing, so you cannot delete an object by accident.

## Notes

- With *Projection: On scan* and no *Only object* set, the first click may take a moment
  on very heavy scenes while Blender builds the raycast structure. Set *Only object* to
  the scan to avoid it.
- The camera frame aspect comes from the scene render resolution; set it to the
  projector's native resolution.
- Do not reload or disable the add-on while tracing is running (press Esc twice first).

## FAQ

**How do I do projection mapping on a physical model in Blender?**
Model or scan the object, put a camera where the real projector is, open that camera on
the projector with *Open on projector*, then align the camera until the image matches the
object. Render your content from the same camera.

**How do I trace the outline of a real object through a projector?**
Open the projector window, press *Start trace (projector)* and click or drag on the
projected image. Each line lands on your 3D scan (raycast), so it fits the real surface.

**Does it replace a media server (Resolume, MadMapper, PIXERA, TouchDesigner)?**
No. It helps you prepare and check content in Blender. The final playback and fine warping
stay in your media server.

**Can I check shadows on the model before the show?**
Yes. *Light test* rotates a sun around the model in steps, using Workbench shadows or EEVEE.

## Tests

Run from the repository root (they start their own Blender, never an open session):

```
blender --background --factory-startup --python tests/test_background.py
blender --factory-startup --enable-event-simulate --python tests/test_gui.py
```

## License

Copyright © 2026 [precyzja.org](https://precyzja.org). GPL-3.0-or-later, see [LICENSE](LICENSE).
