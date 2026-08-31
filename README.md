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

When a PartDesign Linear Pattern task dialog is open, the addon also adds
`Grab preview with mouse`. Check it to display orange preview occurrence
handles. Click an occurrence handle and drag it along the pattern direction;
the Linear Pattern Length (Extent mode) or Offset (Spacing mode) is updated
live. Right-click cancels the current preview drag, and the task's normal
Cancel button cancels the whole edit.

The preview-drag control is intentionally limited to uniform first-direction
linear patterns. Non-uniform spacing patterns are left to FreeCAD's normal
controls. If Length or Offset is driven by an expression, the first successful
preview move replaces that field with the numeric value, just like an explicit
manual edit; cancelling the task restores the expression.

The addon is installed in the active FreeCAD user Mod directory. It uses
FreeCAD's public view callbacks, `getObjectsInfo()`/`getObjectInfo()` hit
testing, and `Placement` updates, so the document remains a normal FreeCAD
document and the move is one undoable transaction.

The repository also includes the companion `HoverSelect` addon. It selects the
Body under the cursor with `L`, appends additional Bodies on later `L` presses,
and expands/scrolls the tree to show the selected Bodies.
