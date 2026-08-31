# Grab Move

This FreeCAD addon gives PartDesign bodies and ShapeBinders a Blender-style
modal move command:

- Select one or more Bodies, ShapeBinders, or SubShapeBinders and press `G`.
- All selected objects move together by the same world-space translation.
- Move the mouse, then press `X`, `Y`, or `Z` for a global-axis constraint.
- After choosing an axis, type a distance such as `10` or `-2.5` for an exact
  displacement. Backspace edits the value; without a value, the mouse remains
  in control.
- Numeric keypad view shortcuts are overridden while Grab Move is active, so
  keypad digits enter the distance instead of changing the camera view.
- Press `B`, click a source point (the selected object's geometry is preferred),
  hover a point on another object, and click again to align the two points.
- Snapping searches a small magnetic radius around the cursor, prioritizes
  nearby vertices and edges, and briefly holds a target once acquired.
- Straight edges expose a midpoint snap when the cursor is near the center.
- After the source click in `B` mode, a yellow marker stays on the selected
  source point while the target is chosen.
- A temporary on-screen readout shows the X, Y, and Z displacement together.
- Left mouse or Enter confirms. Right mouse or Escape cancels.
- Selected Bodies move during the grab. On confirmation, each Body's internal
  ShapeBinders receive the same final translation before recompute, preventing
  synchronized PartDesign features from snapping back.

The addon is installed in the active FreeCAD user Mod directory. It uses
FreeCAD's public view callbacks, `getObjectsInfo()`/`getObjectInfo()` hit
testing, and `Placement` updates, so the document remains a normal FreeCAD
document and the move is one undoable transaction.
