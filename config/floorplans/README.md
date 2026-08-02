# Floor plan images

**Nothing in the running system reads this directory any more.**

`config/rooms.json` is a schematic: the desk rectangles are the map, and their
spacing encodes the layout (narrow gap = two columns facing each other, wide gap
= an aisle, widest = the wall between the two sides). Neither the web form nor
the report loads a bitmap, so there is no image to keep in sync and no
missing-image warning to ignore.

`main_office.png` is kept here as **reference only** — it is the architect's plan
the schematic was derived from, and it is the thing to check the schematic
against if the room is ever rearranged. Delete it if you would rather not carry
the megabyte; nothing will break.

## If you want the bitmap back

Add an `image` key to a room in `rooms.json`:

```json
"image": "floorplans/main_office.png",
```

The validator, `tools/sync_config.py` and the report all handle it again from
that point — a configured-but-missing file warns, an absent key is silent.

**But note:** the desk coordinates are now schematic and will *not* line up with
the architect's drawing. Re-deriving image-aligned coordinates means re-doing
them in `tools/calibrate/index.html` against that image.
