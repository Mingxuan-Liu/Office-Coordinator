# Floor plan images

Drop the floor plan image files referenced by `config/rooms.json` here.

Currently expected:

| file | referenced by | status |
|---|---|---|
| `main_office.png` | `rooms.json` → `rooms[0].image` | **MISSING — you need to add this** |

## What to add

The 2D floor plan of the main graduate office (the plan marked `REV. 4.29.15`
with desks numbered 1–31). Save it as `main_office.png`.

The desk coordinates already in `rooms.json` were derived from that image at
**1212 × 706** and are stored in *normalized* (0–1) space, so any resolution
will work as long as the **aspect ratio and framing match** — i.e. use the same
crop, just scaled. If you use a differently-cropped image, re-run the
calibration tool (`tools/calibrate/index.html`) to regenerate the coordinates.

## What happens if it is missing

- `deskmatch validate` emits a warning naming this file.
- The solver and the report still run; the floor-plan heatmap falls back to
  drawing the desk outlines on a blank canvas with a visible note.
- **The web form is the real problem**: `tools/sync_config.py` will warn and
  emit a null image, and students would be asked to rank desks with no plan to
  look at. Add the image before deploying the form.

After adding it, re-run:

```bash
python -m deskmatch validate --config config/
python tools/sync_config.py --config-dir config/ --out frontend/ConfigData.gs
```
