# Horizon feature: panorama → horizon line → Stellarium / NINA

Builds an accurate local horizon for the pier the SeeStar stands on: trees, buildings, observatory walls and leafless branches. Uses the wide-angle camera, on clear nights or in daylight (daylight capture is untested).

## Pipeline

1. **Plan:** `tools/plan_grid.py` (altitude rows, generous overlap) or `tools/plan_optimal.py` (HA/Dec strips, minimal overlap, about half the time; see [grid-optimization.md](grid-optimization.md)). Both respect the cable window.
2. **Capture:** `tools/capture.py --exp 20 --gain 200` at night. It's resumable and writes `data/pano/status.js`, so you can watch `web/monitor.html` live. `tools/render_allsky.py --watch` adds a reprojected low-res preview layer.
3. **Solve:**
   - `tools/solve_frames.py` blind-solves frames (needs a wide-field solver).
   - `tools/mount_model.py --no-guided` fits a mount model (camera mounting, polar/zero-point rotation) to the solved frames. It places every frame, including ones with no usable stars; about 0.25° RMS in the first run.
4. **Stitch:** `tools/stitch.py` applies bias and a radial flat-field, then reprojects to equirectangular (4096 px wide, alt −10…90) with feathered blending.
5. **Horizon line:** `tools/horizon2.py` does ground-connected segmentation. Stars are removed by grey opening, and a local sky level is estimated. Foreground is anything darker (trees, branches) or brighter (lit structures) than the local sky and connected to the ground. The line is the top of that foreground: dense, plus a simplified edit set.
6. **Review:**
   - `tools/editor_points.py`, then open `web/horizon_editor.html`. Drag, add and delete points over the panorama, then download `.hrz` / `horizon.txt`.
   - An earlier threshold-only approach (`tools/horizon.py`) is kept for reference; it struggles with frame seams.
7. **Package:** `tools/package.py --id my_pier --name "…" --author "…"` writes a Stellarium **spherical** landscape zip (sky transparent above the line, polygonal horizon) and a NINA `.hrz`.

## Stellarium gotchas (25.3)

- **Az 0.0 vertex:** a `polygonal_horizon_list` with a vertex at exactly azimuth 0.0 silently fails, and no horizon line is drawn. The packager samples at bin centres (0.25°, 0.75°, …).
- **Layout:** with `angle_rotatez = 0`, the texture's left edge is due east and azimuth increases to the right. Verified against the polygon.
- **Line hidden by texture:** keep the texture transparent slightly *below* the line, or the opaque texture hides the red horizon line.

## Roadmap

Run the whole workflow from the browser: start capture, watch progress, review and edit the line, download the packages.
