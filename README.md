# Grab Move

This FreeCAD addon gives PartDesign bodies and ShapeBinders a Blender-style
modal move command:

- Select one Body, ShapeBinder, or SubShapeBinder and press `G`.
- Move the mouse, then press `X`, `Y`, or `Z` for a global-axis constraint.
- Press `B`, click a point on the selected object, hover a point on another
  object, and click again to align the two points.
- Left mouse or Enter confirms. Right mouse or Escape cancels.

The addon is installed in the active FreeCAD user Mod directory. It uses
FreeCAD's public view callbacks, `getObjectsInfo()` hit testing, and
`Placement` updates, so the document remains a normal FreeCAD document and
the move is one undoable transaction.

