# Hover Select

This FreeCAD addon adds a Blender-style hover-selection shortcut for Part
Design bodies:

- Hover the mouse over a visible Body and press **L** to select it.
- Hover another Body and press **L** again to append it to the selection.
- The addon resolves hits on a Body's visible tip or features back to the
  owning `PartDesign::Body`.
- The FreeCAD selection is kept Body-only, so a previously clicked Tip or
  feature is converted to its owning Body before another Body is appended.
- If the current selection contains no Body, the first `L` replaces it. Once a
  Body is selected, later `L` presses append additional Bodies.
- After `L`, the document/group path containing each selected Body is expanded
  and the tree scrolls to the newly selected Body.

The addon is enabled automatically when FreeCAD starts. It uses the active
3D view's public hit-testing and event-callback APIs. It does not change model
geometry or placements.

## Installation

Copy the `HoverSelect` folder into FreeCAD's user `Mod` directory, then restart
FreeCAD. For the current Linux profile this is:

```text
~/.local/share/FreeCAD/v26-3/Mod/HoverSelect
```
