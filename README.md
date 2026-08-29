# Grab Move

This FreeCAD addon gives PartDesign bodies and ShapeBinders a Blender-style
modal move command:

- Select one Body, ShapeBinder, or SubShapeBinder and press `G`.
- Move the mouse, then press `X`, `Y`, or `Z` for a global-axis constraint.
- Press `B`, click a source point (the selected object's geometry is preferred),
  hover a point on another object, and click again to align the two points.
- Snapping searches a small magnetic radius around the cursor, prioritizes
  nearby vertices and edges, and briefly holds a target once acquired.
- Straight edges expose a midpoint snap when the cursor is near the center.
- After the source click in `B` mode, a yellow marker stays on the selected
  source point while the target is chosen.
- Left mouse or Enter confirms. Right mouse or Escape cancels.
- A selected Body moves during the grab. On confirmation, its internal
  ShapeBinders receive the same final translation before recompute, preventing
  synchronized PartDesign features from snapping back.

The addon is installed in the active FreeCAD user Mod directory. It uses
FreeCAD's public view callbacks, `getObjectsInfo()`/`getObjectInfo()` hit
testing, and `Placement` updates, so the document remains a normal FreeCAD
document and the move is one undoable transaction.
