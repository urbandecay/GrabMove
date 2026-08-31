"""Blender-style modal grab/move for PartDesign bodies and shape binders.

The command deliberately lives in Python rather than changing FreeCAD's
core.  It uses the public view event and placement APIs:

* ``G`` starts a modal translation for every selected moveable object.
* ``X``, ``Y`` or ``Z`` constrains the translation to a global axis.
* Type a numeric distance after an axis to enter an exact displacement;
  Backspace edits it and the live X/Y/Z displacement HUD stays visible.
  Numeric keypad view shortcuts are captured while the modal move is active.
* ``B`` enters snap-source mode.  Click a point on the selected object, then
  hover and click a point on another object to align the two points.
* Left mouse/Enter confirms; right mouse/Escape cancels.

This is intentionally a small, self-contained first implementation.  FreeCAD
does not currently expose Blender's complete transform/snap modal operator to
Python, so the modal state machine is implemented here and Placement remains
the source of truth for the actual move.  Selected objects receive one shared
world-space translation.  When a Body contains ShapeBinders, the Body moves
during the grab and the Binder placements are synchronized with the Body's
final translation immediately before recompute.
"""

from __future__ import print_function

import ctypes
import math
import os
import time
import traceback

import FreeCAD as App
import FreeCADGui as Gui

try:
    from pivy import coin
except ImportError:  # pragma: no cover - FreeCAD normally ships pivy
    coin = None


COMMAND_NAME = "GrabMove_Move"
SESSION_ATTRIBUTE = "_GrabMoveSession"
COMMAND_ATTRIBUTE = "_GrabMoveCommand"
PARAMETER_PATH = "User parameter:BaseApp/Preferences/Mod/GrabMove"
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "GrabMove-debug.log")
DEFAULT_SNAP_RADIUS_PIXELS = 12

def _debug_enabled():
    """Debug is on while the modal command is being validated."""

    try:
        return App.ParamGet(PARAMETER_PATH).GetBool("Debug", True)
    except Exception:
        return True


def _debug(message):
    if not _debug_enabled():
        return
    line = "[%s] [GrabMove][DEBUG] %s\n" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), _text(message)
    )
    try:
        with open(LOG_PATH, "a") as log_file:
            log_file.write(line)
    except Exception:
        pass
    try:
        App.Console.PrintMessage(line)
    except Exception:
        pass


def _snap_radius_pixels():
    """Read the magnetic snap search radius from preferences."""

    try:
        radius = int(
            App.ParamGet(PARAMETER_PATH).GetInt(
                "SnapRadiusPixels", DEFAULT_SNAP_RADIUS_PIXELS
            )
        )
    except Exception:
        radius = DEFAULT_SNAP_RADIUS_PIXELS
    return max(0, min(radius, 64))


def _qt_enum(container, name, nested_name=None):
    value = getattr(container, name, None)
    if value is not None:
        return value
    nested = getattr(container, nested_name, None) if nested_name else None
    return getattr(nested, name, None) if nested is not None else None


def _qt_modules():
    try:
        from PySide import QtCore, QtGui
        try:
            from PySide import QtWidgets
        except ImportError:
            QtWidgets = QtGui
        return QtCore, QtGui, QtWidgets
    except ImportError:
        from PySide6 import QtCore, QtGui, QtWidgets
        return QtCore, QtGui, QtWidgets


class _XcbKeyPressEvent(ctypes.Structure):
    """Small prefix of xcb_key_press_event_t used by the native filter."""

    _fields_ = (
        ("response_type", ctypes.c_ubyte),
        ("detail", ctypes.c_ubyte),
        ("sequence", ctypes.c_ushort),
        ("time", ctypes.c_uint32),
        ("root", ctypes.c_uint32),
        ("event", ctypes.c_uint32),
        ("child", ctypes.c_uint32),
        ("root_x", ctypes.c_int16),
        ("root_y", ctypes.c_int16),
        ("event_x", ctypes.c_int16),
        ("event_y", ctypes.c_int16),
        ("state", ctypes.c_ushort),
        ("same_screen", ctypes.c_ubyte),
        ("pad0", ctypes.c_ubyte),
    )


def _text(value):
    try:
        return str(value)
    except Exception:
        return ""


def _copy_placement(value):
    """Return a detached App.Placement copy across FreeCAD versions."""

    try:
        return App.Placement(value)
    except Exception:
        result = App.Placement()
        result.Base = App.Vector(value.Base)
        result.Rotation = App.Rotation(value.Rotation)
        return result


def _copy_vector(value):
    try:
        return App.Vector(value)
    except Exception:
        return App.Vector(float(value[0]), float(value[1]), float(value[2]))


def _vector_from_value(value):
    """Convert common FreeCAD/Coin/Python point representations."""

    if value is None:
        return None

    try:
        return App.Vector(value)
    except Exception:
        pass

    try:
        if hasattr(value, "getValue"):
            return _vector_from_value(value.getValue())
    except Exception:
        pass

    try:
        return App.Vector(float(value[0]), float(value[1]), float(value[2]))
    except Exception:
        return None


def _active_view():
    try:
        gui_document = Gui.activeDocument()
        if gui_document is not None:
            return gui_document.ActiveView
    except Exception:
        pass

    try:
        return Gui.ActiveDocument.ActiveView
    except Exception:
        return None


def _linear_pattern_preview_controller():
    return getattr(
        App, "_GrabMoveLinearPatternPreviewController", None
    )


def _linear_pattern_preview_available():
    controller = _linear_pattern_preview_controller()
    method = getattr(controller, "can_start_grab", None)
    if not callable(method):
        return False
    try:
        return bool(method())
    except Exception:
        return False


def _linear_pattern_task_active():
    """Return whether FreeCAD is currently editing a Linear Pattern.

    A pattern task can still have a selected Body or feature behind its
    temporary preview.  That selection must not make the ordinary G command
    available while the native task is open.
    """

    controller = _linear_pattern_preview_controller()
    pattern = getattr(controller, "_pattern", None)
    try:
        if pattern is not None and _text(pattern.TypeId) == (
            "PartDesign::LinearPattern"
        ):
            return True
    except Exception:
        pass

    documents = []
    active_document = getattr(Gui, "ActiveDocument", None)
    if active_document is not None:
        documents.append(active_document)
    try:
        active_document = Gui.activeDocument()
        if active_document is not None:
            documents.append(active_document)
    except Exception:
        pass

    seen = set()
    for document in documents:
        marker = id(document)
        if marker in seen:
            continue
        seen.add(marker)
        getter = getattr(document, "getInEdit", None)
        if not callable(getter):
            continue
        try:
            value = getter()
            if isinstance(value, (list, tuple)):
                value = value[0] if value else None
            obj = getattr(value, "Object", None) or getattr(value, "object", None)
            if obj is None and hasattr(value, "TypeId"):
                obj = value
            if obj is not None and _text(obj.TypeId) == (
                "PartDesign::LinearPattern"
            ):
                return True
        except Exception:
            continue
    return False


def _document_for_object(obj):
    try:
        return obj.Document
    except Exception:
        return App.ActiveDocument


def _object_key(obj):
    try:
        document = obj.Document
        return (getattr(document, "Name", ""), getattr(obj, "Name", ""))
    except Exception:
        return ("", "")


def _same_object(left, right):
    if left is right:
        return True
    if left is None or right is None:
        return False
    return _object_key(left) == _object_key(right)


def _is_moveable_object(obj):
    """Limit the command to Body, ShapeBinder and SubShapeBinder objects."""

    if obj is None:
        return False

    try:
        type_id = _text(obj.TypeId)
    except Exception:
        type_id = ""

    if "Body" in type_id or "ShapeBinder" in type_id:
        return hasattr(obj, "Placement")

    # Older ShapeBinder implementations were Python features whose TypeId
    # did not contain ShapeBinder.  Their stable identifying traits are the
    # PartDesign feature type plus a binder name and a Support property.
    name = (_text(getattr(obj, "Name", "")) + " "
            + _text(getattr(obj, "Label", ""))).lower()
    if "binder" in name and hasattr(obj, "Placement"):
        try:
            if obj.isDerivedFrom("PartDesign::Feature"):
                return True
        except Exception:
            return hasattr(obj, "Shape")

    return False


def _parent_geo_feature_group(obj):
    getter = getattr(obj, "getParentGeoFeatureGroup", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:
        return None


def _is_binder_object(obj):
    if obj is None:
        return False
    return "ShapeBinder" in _text(getattr(obj, "TypeId", ""))


def _binder_tracks_support(binder):
    """Return whether a binder follows placement changes of its support."""

    type_id = _text(getattr(binder, "TypeId", ""))
    if "SubShapeBinder" in type_id:
        # SubShapeBinders always track the relative placement of their
        # referenced geometry.
        return True

    try:
        return bool(getattr(binder, "TraceSupport"))
    except Exception:
        return False


def _linked_objects(value):
    """Yield document objects from Link, LinkSub, and LinkSubList values."""

    if value is None:
        return
    if hasattr(value, "Name") and hasattr(value, "Document"):
        yield value
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            for linked in _linked_objects(item):
                yield linked


def _group_members(root):
    """Return a Body and all of the features contained by its Group tree."""

    members = []
    pending = [root]
    visited = set()
    while pending:
        current = pending.pop()
        key = _object_key(current)
        if key in visited:
            continue
        visited.add(key)
        members.append(current)
        try:
            pending.extend(list(getattr(current, "Group", []) or []))
        except Exception:
            pass
    return members


def _binder_supports_body(binder, body, body_keys):
    try:
        support = binder.Support
    except Exception:
        return False

    for linked in _linked_objects(support):
        if _object_key(linked) in body_keys:
            return True

        # A support may itself be a nested PartDesign feature whose parent
        # group is the selected Body even when it is not exposed in Group.
        current = linked
        visited = set()
        for _index in range(32):
            key = _object_key(current)
            if key in visited:
                break
            visited.add(key)
            parent = _parent_geo_feature_group(current)
            if parent is None:
                break
            if _same_object(parent, body):
                return True
            current = parent
    return False


def _binders_for_body(body):
    """Return ShapeBinders whose placements drive a Body's visible result."""

    if body is None or "Body" not in _text(getattr(body, "TypeId", "")):
        return []

    body_members = _group_members(body)
    body_keys = set(_object_key(member) for member in body_members)
    try:
        document_objects = list(body.Document.Objects)
    except Exception:
        document_objects = []

    binders = []
    for member in body_members:
        if _is_binder_object(member):
            binders.append(member)

    # Group normally contains all features in a Body.  The parent check also
    # covers versions/documents where a ShapeBinder is owned by the Body but
    # is not exposed through the Group property.
    for candidate in document_objects:
        if not _is_binder_object(candidate):
            continue
        if _object_key(candidate) in body_keys:
            continue
        if _same_object(_parent_geo_feature_group(candidate), body):
            binders.append(candidate)

    if not binders:
        return []

    # Keep stable document order while removing any duplicate references.
    unique = []
    seen = set()
    for binder in binders:
        key = _object_key(binder)
        if key in seen:
            continue
        seen.add(key)
        unique.append(binder)
    _debug(
        "body follow target=%s binders=%s"
        % (
            _object_key(body),
            [_object_key(item) for item in unique],
        )
    )
    return unique


def _resolve_moveable_object(obj):
    """Resolve a selected PartDesign feature to its owning Body.

    In the 3D view FreeCAD often reports the visible tip feature (for example
    a LinearPattern) rather than the Body containing it. Grab Move operates
    on the Body in that case, while preserving direct ShapeBinder selection.
    """

    if _is_moveable_object(obj):
        return obj

    current = obj
    visited = set()
    for _index in range(32):
        key = _object_key(current)
        if key in visited:
            break
        visited.add(key)
        current = _parent_geo_feature_group(current)
        if current is None:
            break
        if _is_moveable_object(current):
            return current
    return None


def _selected_objects():
    """Return the raw objects currently selected in FreeCAD."""

    try:
        return list(Gui.Selection.getSelection())
    except Exception:
        return []


def _selected_object():
    """Return the one raw object selected in the tree or 3D view."""

    selection = _selected_objects()
    if len(selection) != 1:
        return None
    return selection[0]


def _body_for_object(obj):
    """Return the Body that visually owns a selected feature, when known."""

    if obj is None:
        return None
    if "Body" in _text(getattr(obj, "TypeId", "")):
        return obj

    current = obj
    visited = set()
    for _index in range(32):
        key = _object_key(current)
        if key in visited:
            break
        visited.add(key)
        current = _parent_geo_feature_group(current)
        if current is None:
            break
        if "Body" in _text(getattr(current, "TypeId", "")):
            return current
    return None


def _selected_moveable_object():
    objects = _selected_moveable_objects()
    if len(objects) != 1:
        return None
    return objects[0]


def _selected_moveable_items():
    """Return unique move targets and their visual snap proxies.

    HoverSelect keeps the normal selection Body-only, but resolving every raw
    selection here also makes Grab Move work when a Body's visible Tip or a
    ShapeBinder was selected through the tree or another FreeCAD command.
    """

    items = []
    seen = set()
    for selected_obj in _selected_objects():
        obj = _resolve_moveable_object(selected_obj)
        if obj is None:
            continue
        key = _object_key(obj)
        if key in seen:
            continue
        seen.add(key)
        visual_obj = _body_for_object(selected_obj) or selected_obj
        items.append((obj, visual_obj))
    return items


def _selected_moveable_objects():
    """Return all unique moveable objects in the current FreeCAD selection."""

    return [item[0] for item in _selected_moveable_items()]


def _global_placement(obj):
    getter = getattr(obj, "getGlobalPlacement", None)
    if callable(getter):
        try:
            return _copy_placement(getter())
        except Exception:
            pass
    return _copy_placement(obj.Placement)


def _parent_global_placement(obj):
    getter = getattr(obj, "getParentGeoFeatureGroup", None)
    if not callable(getter):
        return App.Placement()

    try:
        parent = getter()
    except Exception:
        parent = None

    if parent is None or _same_object(parent, obj):
        return App.Placement()
    return _global_placement(parent)


def _global_to_local(obj, placement):
    """Convert a desired global placement to the object's local placement."""

    parent_placement = _parent_global_placement(obj)
    try:
        return parent_placement.inverse() * placement
    except Exception:
        # Objects without a geometric parent use their own local placement as
        # global placement.  This fallback also helps older FreeCAD builds.
        return _copy_placement(placement)


def _component_index(component, prefix):
    """Return the zero-based subshape index from a hit component name."""

    value = _text(component).strip()
    marker = _text(prefix).lower()
    start = value.lower().find(marker)
    if start < 0:
        return None

    suffix = value[start + len(marker):]
    digits = []
    for character in suffix:
        if character.isdigit():
            digits.append(character)
        elif digits:
            break
        else:
            return None

    if not digits:
        return None
    index = int("".join(digits)) - 1
    return index if index >= 0 else None


def _shape_element(obj, collection_name, index):
    """Get a shape subelement without making hit-picking depend on topology."""

    if index is None:
        return None
    try:
        shape = getattr(obj, "Shape", None)
        if shape is None:
            return None
        elements = list(getattr(shape, collection_name))
        if index >= len(elements):
            return None
        return elements[index]
    except Exception:
        return None


def _shape_point_to_global(obj, point):
    """Convert a Shape point into world space without double-transforming it."""

    try:
        # Shape.Vertexes/Face.CenterOfMass already include the object's own
        # Placement.  Only a containing PartDesign group still needs to be
        # applied here (for example, a feature inside a moved Body).
        return _parent_global_placement(obj).multVec(point)
    except Exception:
        return _copy_vector(point)


def _straight_edge_midpoint(obj, edge_index):
    """Return the world midpoint for a straight EdgeN hit."""

    edge = _shape_element(obj, "Edges", edge_index)
    if edge is None:
        return None

    try:
        curve = edge.Curve
        curve_name = "%s %s" % (
            type(curve).__name__,
            _text(getattr(curve, "TypeId", "")),
        )
        if "line" not in curve_name.lower():
            return None

        vertices = list(edge.Vertexes)
        if len(vertices) < 2:
            return None
        first = _copy_vector(vertices[0].Point)
        last = _copy_vector(vertices[-1].Point)
        midpoint = App.Vector(
            (first.x + last.x) * 0.5,
            (first.y + last.y) * 0.5,
            (first.z + last.z) * 0.5,
        )
        return _shape_point_to_global(obj, midpoint)
    except Exception:
        return None


def _axis_vector(axis):
    if axis == "X":
        return App.Vector(1.0, 0.0, 0.0)
    if axis == "Y":
        return App.Vector(0.0, 1.0, 0.0)
    if axis == "Z":
        return App.Vector(0.0, 0.0, 1.0)
    return None


def _constrain(delta, axis):
    vector = _axis_vector(axis)
    if vector is None:
        return _copy_vector(delta)
    return vector * delta.dot(vector)


def _same_translation(left, right, tolerance=1.0e-7):
    """Compare the placement translation, which is what Grab Move changes."""

    try:
        left_base = left.Base
        right_base = right.Base
        return all(
            abs(float(getattr(left_base, axis)) - float(getattr(right_base, axis)))
            <= tolerance
            for axis in ("x", "y", "z")
        )
    except Exception:
        return False


def _event_position(event):
    position = event.get("Position") if isinstance(event, dict) else None
    if position is None:
        return None

    try:
        if hasattr(position, "getValue"):
            position = position.getValue()
    except Exception:
        pass

    try:
        return int(position[0]), int(position[1])
    except Exception:
        return None


def _event_type(event):
    if not isinstance(event, dict):
        return ""
    return _text(event.get("Type", event.get("type", "")))


def _event_state(event):
    if not isinstance(event, dict):
        return ""
    return _text(event.get("State", event.get("state", ""))).upper()


def _event_key(event):
    if not isinstance(event, dict):
        return ""
    key = event.get("Key", event.get("key", ""))
    return _text(key).upper()


def _event_button(event):
    if not isinstance(event, dict):
        return ""
    return _text(event.get("Button", event.get("button", ""))).upper()


def _numeric_key_value(key):
    """Return the typed numeric character represented by a Coin key name."""
    value = _text(key).upper()
    if len(value) == 1 and value.isdigit():
        return value

    for prefix in ("KP_", "KP", "NUM_"):
        if value.startswith(prefix):
            suffix = value[len(prefix):]
            if len(suffix) == 1 and suffix.isdigit():
                return suffix

    if value in (".", "PERIOD", "DECIMAL", "KP_PERIOD", "KP_DECIMAL"):
        return "."
    if value in ("-", "MINUS", "SUBTRACT", "KP_MINUS", "KP_SUBTRACT"):
        return "-"
    if value in ("+", "PLUS", "ADD", "KP_PLUS", "KP_ADD"):
        return "+"
    return None


def _component_priority(component):
    """Prefer precise topological snap elements over broad surfaces."""

    name = _text(component).lower()
    if "vertex" in name:
        return 0
    if "edge" in name:
        return 1
    if "wire" in name:
        return 2
    if "face" in name:
        return 3
    if "solid" in name:
        return 4
    return 5


def _snap_sample_positions(screen_position, radius):
    """Return a small screen-space grid used for magnetic snap searching."""

    x = int(screen_position[0])
    y = int(screen_position[1])
    if radius <= 0:
        return [(x, y)]

    # A 3x3 grid gives coverage across the full radius without requiring a
    # large number of ray-pick calls.  The center is first so exact hits keep
    # their normal behavior when no nearby precise component is present.
    step = max(1, int(round(float(radius) * 2.0 / 3.0)))
    offsets = (
        (0, 0),
        (-step, 0), (step, 0), (0, -step), (0, step),
        (-step, -step), (-step, step), (step, -step), (step, step),
    )
    return [(x + dx, y + dy) for dx, dy in offsets]


class _Marker(object):
    """A lightweight point marker added temporarily to the active scene."""

    def __init__(self, color):
        self.separator = None
        self.coordinates = None
        self.points = None

        if coin is None:
            return

        try:
            self.separator = coin.SoSeparator()
            material = coin.SoMaterial()
            material.diffuseColor.setValue(*color)
            material.emissiveColor.setValue(*color)

            draw_style = coin.SoDrawStyle()
            draw_style.pointSize = 9.0

            self.coordinates = coin.SoCoordinate3()
            self.points = coin.SoPointSet()
            self.points.numPoints = 0

            self.separator.addChild(material)
            self.separator.addChild(draw_style)
            self.separator.addChild(self.coordinates)
            self.separator.addChild(self.points)
        except Exception:
            self.separator = None
            self.coordinates = None
            self.points = None

    def set_point(self, point):
        if self.coordinates is None or self.points is None:
            return
        try:
            self.coordinates.point.set1Value(
                0, coin.SbVec3f(float(point.x), float(point.y), float(point.z))
            )
            self.points.numPoints = 1
        except Exception:
            pass

    def hide(self):
        if self.points is not None:
            try:
                self.points.numPoints = 0
            except Exception:
                pass


class GrabMoveSession(object):
    """One running modal move for one or more selected objects."""

    def __init__(self, obj, visual_obj=None, visual_objects=None):
        if isinstance(obj, (list, tuple)):
            moveable_objects = list(obj)
        else:
            moveable_objects = [obj]
        if not moveable_objects:
            raise ValueError("Grab Move requires at least one moveable object")

        if visual_objects is not None:
            snap_proxies = list(visual_objects)
        elif isinstance(visual_obj, (list, tuple)):
            snap_proxies = list(visual_obj)
        elif visual_obj is not None:
            snap_proxies = [visual_obj]
        else:
            snap_proxies = list(moveable_objects)

        self.target_states = []
        seen_targets = set()
        for index, target in enumerate(moveable_objects):
            key = _object_key(target)
            if key in seen_targets:
                continue
            seen_targets.add(key)
            proxy = (
                snap_proxies[index]
                if index < len(snap_proxies) and snap_proxies[index] is not None
                else target
            )
            original_local = _copy_placement(target.Placement)
            original_global = _global_placement(target)
            follow_binder_states = []
            for binder in _binders_for_body(target):
                try:
                    follow_binder_states.append(
                        (binder, _copy_placement(binder.Placement))
                    )
                except Exception:
                    _debug(
                        "could not snapshot body-follow Binder=%s"
                        % (_object_key(binder),)
                    )
            self.target_states.append(
                {
                    "obj": target,
                    "visual_obj": proxy,
                    "original_local": original_local,
                    "original_global": original_global,
                    "preview_global": _copy_placement(original_global),
                    "follow_binder_states": follow_binder_states,
                }
            )

        if not self.target_states:
            raise ValueError("Grab Move requires at least one moveable object")

        # Keep the primary-object attributes for the HUD and existing snap
        # calculations.  The target_states list is the source of truth for
        # applying and restoring the shared translation.
        primary = self.target_states[0]
        self.obj = primary["obj"]
        self.visual_obj = primary["visual_obj"]
        self.move_objects = [state["obj"] for state in self.target_states]
        self.visual_objects = [
            state["visual_obj"] for state in self.target_states
        ]
        self.document = _document_for_object(self.obj)
        self.view = _active_view()
        self.phase = "move"
        self.axis = None
        self.done = False
        self.finish_pending = False
        self.pending_commit = None
        self.cleanup_done = False

        self.original_local = primary["original_local"]
        self.original_global = primary["original_global"]
        self.original_globals = [
            state["original_global"] for state in self.target_states
        ]
        self.preview_global = _copy_placement(self.original_global)
        self.follow_binder_states = primary["follow_binder_states"]

        self.initial_cursor_world = None
        self.last_screen_position = None

        # Keyboard distance entry is an optional override of the live mouse
        # displacement on the selected axis.  An empty buffer means that the
        # mouse controls the distance again.
        self.numeric_buffer = ""
        self.numeric_value = None

        # A Qt label is used for the live displacement readout.  Keeping this
        # out of the Coin scene graph avoids changing the scene graph while a
        # Coin event callback is active.
        self.hud_label = None
        self.hud_viewport = None

        self.snap_baseline_global = None
        self.snap_baseline_placements = None
        self.source_world = None
        self.source_local = None
        self.source_hit = None
        self.target_hit = None

        self.callback_id = None
        self.transaction_open = False
        self.marker_root = None
        self.debug_event_count = 0
        self.debug_move_count = 0
        self.debug_snap_query_count = 0
        self.snap_radius_pixels = _snap_radius_pixels()
        self.snap_release_pixels = (
            self.snap_radius_pixels + 6 if self.snap_radius_pixels else 0
        )
        self.source_marker = _Marker((1.0, 0.75, 0.1))
        self.target_marker = _Marker((0.1, 0.9, 1.0))
        _debug(
            "session created for targets=%s view=%s"
            % (
                [
                    (_object_key(state["obj"]),
                     _text(getattr(state["obj"], "TypeId", "")))
                    for state in self.target_states
                ],
                type(self.view).__name__ if self.view is not None else "None",
            )
        )
        if not _same_object(self.visual_obj, self.obj):
            _debug(
                "visual grab proxy=%s actual move target=%s"
                % (_object_key(self.visual_obj), _object_key(self.obj))
            )

    def start(self):
        if self.view is None:
            _debug("start failed: no active 3D view")
            raise RuntimeError("No active 3D view")

        try:
            self.document.openTransaction("Grab Move")
            self.transaction_open = True
            _debug("document transaction opened")
        except Exception:
            # The move remains usable without an undo transaction, although
            # this should not happen in a normal FreeCAD document.
            self.transaction_open = False
            _debug("document transaction could not be opened")

        self._install_scene_markers()
        _debug("scene markers installed=%s" % (self.marker_root is not None))
        self._install_hud()
        self._update_hud()

        try:
            self.callback_id = self.view.addEventCallback(
                "SoEvent", self._handle_event
            )
            _debug("SoEvent callback installed id=%s" % self.callback_id)
        except Exception:
            self._remove_scene_markers()
            self._remove_hud()
            if self.transaction_open:
                try:
                    self.document.abortTransaction()
                except Exception:
                    pass
                self.transaction_open = False
            raise

        self._focus_view()
        _debug("view focus requested")
        self._status(
            "Grab Move: moving %d object%s | move mouse | X/Y/Z constrain | "
            "B pick snap source | "
            "LMB/Enter accept | RMB/Esc cancel"
            % (
                len(self.target_states),
                "" if len(self.target_states) == 1 else "s",
            )
        )

    def _focus_view(self):
        """Give the active view keyboard focus when its widget is exposed."""

        try:
            main = Gui.getMainWindow()
            if main is not None:
                main.activateWindow()
        except Exception:
            pass

        try:
            viewer = self.view.getViewer()
            widget = viewer.getWidget()
            widget.setFocus()
            _debug("focus set through ActiveView.getViewer().getWidget()")
            return
        except Exception:
            _debug("ActiveView viewer widget focus path unavailable")

        # v26.3 does not expose getWidget() on the Python viewer wrapper, but
        # the same QOpenGLWidget can be found through the Qt view hierarchy.
        # Focusing it is important: otherwise mouse motion reaches Coin while
        # typed distances can remain focused on the tree or property editor.
        try:
            widget = self.hud_viewport
            if widget is None:
                _QtCore, _QtGui, QtWidgets = _qt_modules()
                widget = self._find_viewport_widget(QtWidgets)
            if widget is not None:
                try:
                    QtCore, _QtGui, _QtWidgets = _qt_modules()
                    strong_focus = _qt_enum(
                        QtCore.Qt, "StrongFocus", "FocusPolicy"
                    )
                    if strong_focus is not None:
                        widget.setFocusPolicy(strong_focus)
                except Exception:
                    pass
                widget.setFocus()
                _debug(
                    "focus set through Qt viewport widget class=%s"
                    % (
                        _text(widget.metaObject().className())
                        if hasattr(widget, "metaObject")
                        else type(widget).__name__
                    )
                )
                return
        except Exception:
            _debug("Qt viewport focus path unavailable")

        try:
            self.view.setFocus()
            _debug("focus set through ActiveView.setFocus()")
        except Exception:
            _debug("ActiveView.setFocus() unavailable")

    def _install_scene_markers(self):
        self.marker_root = None
        if coin is None:
            _debug("scene markers unavailable: pivy.coin is not installed")
            return
        if (
            self.source_marker.separator is None
            or self.target_marker.separator is None
        ):
            _debug("scene markers unavailable: marker nodes could not be created")
            return

        try:
            marker_root = coin.SoSeparator()
            marker_root.addChild(self.source_marker.separator)
            marker_root.addChild(self.target_marker.separator)
            self.view.getSceneGraph().addChild(marker_root)
            self.marker_root = marker_root
            _debug("scene markers installed before modal callback")
        except Exception:
            self.marker_root = None
            _debug("scene marker installation failed:\n%s" % traceback.format_exc())

    def _remove_scene_markers(self):
        if self.marker_root is None:
            return
        try:
            self.view.getSceneGraph().removeChild(self.marker_root)
        except Exception:
            pass
        self.marker_root = None

    def _status(self, message):
        try:
            main = Gui.getMainWindow()
            if main is not None and main.statusBar() is not None:
                main.statusBar().showMessage(message)
        except Exception:
            pass

    def _clear_status(self):
        try:
            main = Gui.getMainWindow()
            if main is not None and main.statusBar() is not None:
                main.statusBar().clearMessage()
        except Exception:
            pass

    def _install_hud(self):
        """Install a small, mouse-transparent displacement readout."""

        self.hud_label = None
        try:
            QtCore, QtGui, QtWidgets = _qt_modules()
            label_class = getattr(QtWidgets, "QLabel", None)
            if label_class is None:
                label_class = getattr(QtGui, "QLabel", None)
            if label_class is None:
                _debug("distance HUD unavailable: QLabel is not exposed")
                return

            parent = None
            parent_source = ""
            try:
                viewer = self.view.getViewer()
                parent = viewer.getWidget()
                parent_source = "active viewer widget"
            except Exception:
                pass
            if parent is None:
                parent = self._find_viewport_widget(QtWidgets)
                parent_source = "Gui::View3DInventor hierarchy"
            if parent is None:
                _debug("distance HUD unavailable: no 3D viewport widget")
                return

            # Never attach the readout to the main window.  That makes it
            # appear in the application corner instead of in the modeling
            # viewport when a viewer wrapper is unavailable.
            try:
                class_name = _text(parent.metaObject().className()).lower()
                if "mainwindow" in class_name:
                    _debug("distance HUD unavailable: viewer returned main window")
                    return
            except Exception:
                pass

            label = label_class(parent)
            try:
                label.setObjectName("GrabMoveDistanceHud")
            except Exception:
                pass
            try:
                transparent_attribute = _qt_enum(
                    QtCore.Qt,
                    "WA_TransparentForMouseEvents",
                    "WidgetAttribute",
                )
                if transparent_attribute is not None:
                    label.setAttribute(transparent_attribute, True)
            except Exception:
                pass
            try:
                label.setStyleSheet(
                    "QLabel { color: #ffffff; "
                    "background-color: rgba(25, 25, 25, 215); "
                    "border: 1px solid #888888; padding: 6px; }"
                )
            except Exception:
                pass
            self.hud_viewport = parent
            self.hud_label = label
            label.show()
            self._position_hud()
            try:
                label.raise_()
            except Exception:
                pass
            try:
                viewport_class = _text(parent.metaObject().className())
            except Exception:
                viewport_class = type(parent).__name__
            try:
                viewport_name = _text(parent.objectName())
            except Exception:
                viewport_name = ""
            _debug(
                "distance HUD installed inside viewport (%s) class=%s object=%s"
                % (parent_source, viewport_class, viewport_name)
            )
        except Exception:
            _debug("distance HUD installation failed:\n%s" % traceback.format_exc())

    def _find_viewport_widget(self, QtWidgets):
        """Find the actual GL viewport when getViewer().getWidget() is hidden."""

        try:
            main = Gui.getMainWindow()
            if main is None:
                return None

            candidates = []
            try:
                active_window = main.activeWindow()
                if active_window is not None:
                    candidates.append(active_window)
            except Exception:
                pass

            try:
                widget_class = getattr(QtWidgets, "QWidget", None)
                if widget_class is not None:
                    candidates.extend(main.findChildren(widget_class))
            except Exception:
                pass

            seen = set()
            for candidate in candidates:
                marker = id(candidate)
                if marker in seen:
                    continue
                seen.add(marker)
                try:
                    class_name = _text(candidate.metaObject().className())
                except Exception:
                    continue
                if class_name != "Gui::View3DInventor":
                    continue

                try:
                    descendants = candidate.findChildren(QtWidgets.QWidget)
                except Exception:
                    descendants = []

                # In newer FreeCAD builds the Coin viewer's Python wrapper
                # does not expose getWidget().  The actual OpenGL widget is
                # still present below Gui::View3DInventor and is the best
                # parent for a readout drawn over the 3D scene.
                for child in descendants:
                    try:
                        child_class = _text(child.metaObject().className())
                    except Exception:
                        continue
                    if child_class in ("QOpenGLWidget", "QGLWidget"):
                        return child

                for child in descendants:
                    try:
                        child_class = _text(child.metaObject().className())
                    except Exception:
                        continue
                    if child_class == "Gui::View3DInventorViewer":
                        return child

                # View3DInventor contains a QStackedWidget whose current
                # child is the real Coin/OpenGL viewport.  Parenting the HUD
                # to that child keeps it inside the modeling area.
                try:
                    central = candidate.centralWidget()
                    current = central.currentWidget()
                    if current is not None:
                        return current
                    if central is not None:
                        return central
                except Exception:
                    pass
                return candidate
        except Exception:
            _debug("viewport widget lookup failed:\n%s" % traceback.format_exc())
        return None

    def _remove_hud(self):
        label = self.hud_label
        self.hud_label = None
        self.hud_viewport = None
        if label is None:
            return
        try:
            label.hide()
        except Exception:
            pass
        try:
            label.deleteLater()
        except Exception:
            pass

    def _position_hud(self):
        """Keep the readout inside the active 3D viewport, near its top center."""

        if self.hud_label is None or self.hud_viewport is None:
            return
        try:
            self.hud_label.adjustSize()
            width = int(self.hud_viewport.width())
            height = int(self.hud_viewport.height())
            label_width = int(self.hud_label.width())
            x = max(12, int((width - label_width) / 2))
            y = 20 if height > 60 else 8
            self.hud_label.move(x, y)
        except Exception:
            pass

    def _preview_delta(self):
        try:
            return _copy_vector(
                self.preview_global.Base - self.original_global.Base
            )
        except Exception:
            return App.Vector(0.0, 0.0, 0.0)

    def _update_hud(self):
        if self.hud_label is None:
            return
        try:
            delta = self._preview_delta()
            axis_text = self.axis if self.axis is not None else "free"
            if self.numeric_buffer:
                input_text = "Input: %s" % self.numeric_buffer
            else:
                input_text = "Input: mouse"
            self.hud_label.setText(
                "Grab Move\n"
                "X: %+.3f mm\n"
                "Y: %+.3f mm\n"
                "Z: %+.3f mm\n"
                "Axis: %s\n"
                "%s"
                % (
                    float(delta.x),
                    float(delta.y),
                    float(delta.z),
                    axis_text,
                    input_text,
                )
            )
            self.hud_label.adjustSize()
            self._position_hud()
            self.hud_label.raise_()
        except Exception:
            # The HUD is optional; never let a widget update interrupt a move.
            pass

    def _reset_numeric_input(self):
        self.numeric_buffer = ""
        self.numeric_value = None

    def _delta_with_numeric(self, delta):
        constrained = _constrain(delta, self.axis)
        if self.numeric_value is not None and self.axis is not None:
            return _axis_vector(self.axis) * float(self.numeric_value)
        return constrained

    def _apply_numeric_value(self):
        """Apply an entered distance immediately when a move baseline exists."""

        if self.numeric_value is None or self.axis is None:
            return False

        delta = _axis_vector(self.axis) * float(self.numeric_value)
        if self.phase == "move":
            self._apply_global_translation(
                self.original_global, delta, self.original_globals
            )
            return True
        if self.phase == "snap_target" and self.snap_baseline_global is not None:
            self._apply_global_translation(
                self.snap_baseline_global,
                delta,
                self.snap_baseline_placements,
            )
            self._update_source_marker()
            return True
        return False

    def _reapply_current_position(self):
        if self.phase == "move" and self.last_screen_position is not None:
            self._update_move(self.last_screen_position)
        elif (
            self.phase == "snap_target"
            and self.last_screen_position is not None
        ):
            self._update_snap_target(self.last_screen_position)
        elif self.phase == "move":
            self._apply_global_translation(
                self.original_global,
                App.Vector(0.0, 0.0, 0.0),
                self.original_globals,
            )
        elif self.phase == "snap_target" and self.snap_baseline_global is not None:
            self._apply_global_translation(
                self.snap_baseline_global,
                App.Vector(0.0, 0.0, 0.0),
                self.snap_baseline_placements,
            )
            self._update_source_marker()
        else:
            self._update_hud()

    def _handle_numeric_input(self, key):
        """Consume a numeric key while an axis-constrained move is active."""

        if self.phase not in ("move", "pick_source", "snap_target"):
            return False

        if self.axis is None:
            if key in (
                "BACKSPACE",
                "BACK",
                "DELETE",
                "DEL",
            ) or _numeric_key_value(key) is not None:
                self._status(
                    "Grab Move: press X, Y, or Z before entering a distance"
                )
                return True
            return False

        if key in ("BACKSPACE", "BACK"):
            self.numeric_buffer = self.numeric_buffer[:-1]
            if self.numeric_buffer:
                try:
                    self.numeric_value = float(self.numeric_buffer)
                except (TypeError, ValueError):
                    self.numeric_value = None
            else:
                self.numeric_value = None
            self._reapply_current_position()
            self._status(
                "Grab Move: %s distance %s | Enter accept | Backspace edit"
                % (self.axis, self.numeric_buffer or "mouse")
            )
            return True

        character = _numeric_key_value(key)
        if character is None:
            return False

        current = self.numeric_buffer
        if character in ("+", "-") and current:
            return True
        if character == "." and "." in current:
            return True

        candidate = current + character
        self.numeric_buffer = candidate
        try:
            self.numeric_value = float(candidate)
        except (TypeError, ValueError):
            # A leading sign or decimal point is valid partial input.
            self.numeric_value = None

        if self.numeric_value is not None:
            self._apply_numeric_value()
        else:
            self._update_hud()
        self._status(
            "Grab Move: %s distance %s | Enter accept | Backspace edit"
            % (self.axis, self.numeric_buffer)
        )
        _debug(
            "numeric input axis=%s buffer=%s value=%s"
            % (self.axis, self.numeric_buffer, self.numeric_value)
        )
        return True

    def _view_point(self, screen_position):
        if screen_position is None:
            return None
        try:
            point = self.view.getPoint(
                int(screen_position[0]), int(screen_position[1])
            )
            return _vector_from_value(point)
        except Exception:
            return None

    def _hit_from_record(self, record):
        """Normalize a FreeCAD view hit into the addon's hit format."""

        if not isinstance(record, dict):
            return None
        obj = self._object_from_record(record)
        point = self._point_from_record(record)
        if obj is None or point is None:
            return None
        return {
            "record": record,
            "object": obj,
            "point": point,
            "component": _text(record.get("Component", "")),
        }

    def _objects_at(self, screen_position):
        if screen_position is None:
            return []

        position = (int(screen_position[0]), int(screen_position[1]))
        try:
            records = self.view.getObjectsInfo(position)
        except Exception:
            records = None

        if records is None:
            records = []
        elif isinstance(records, dict):
            records = [records]

        try:
            records = list(records)
        except Exception:
            return []

        result = []
        for record in records:
            hit = self._hit_from_record(record)
            if hit is not None:
                result.append(hit)

        if result:
            return result

        # Some FreeCAD builds/views return no usable list from
        # getObjectsInfo() even though the single-hit API can identify the
        # point under the cursor.  The two APIs use the same record format.
        single_record = None
        for args in ((position,), position):
            try:
                single_record = self.view.getObjectInfo(*args)
            except Exception:
                continue
            if single_record is not None:
                break

        if single_record is None:
            return []
        if isinstance(single_record, dict):
            single_record = [single_record]
        try:
            single_record = list(single_record)
        except Exception:
            return []

        for record in single_record:
            hit = self._hit_from_record(record)
            if hit is not None:
                result.append(hit)
        return result

    def _object_from_record(self, record):
        obj = record.get("Object")
        if obj is not None and not isinstance(obj, (str, bytes)):
            if hasattr(obj, "Name"):
                return obj

        object_name = _text(obj)
        if not object_name:
            return None

        document_name = _text(record.get("Document", ""))
        document = None
        if document_name:
            try:
                document = App.getDocument(document_name)
            except Exception:
                document = None
        if document is None:
            document = self.document

        try:
            return document.getObject(object_name)
        except Exception:
            return None

    def _point_from_record(self, record):
        values = {}
        lowered = {}
        for key, value in record.items():
            lowered[_text(key).lower()] = value

        for axis in ("x", "y", "z"):
            if axis not in lowered:
                return None
            try:
                values[axis] = float(lowered[axis])
            except Exception:
                return None
        return App.Vector(values["x"], values["y"], values["z"])

    def _screen_position_for_point(self, point):
        """Project a world point into the event coordinate system."""

        try:
            projected = self.view.getPointOnScreen(_copy_vector(point))
            # Both getPointOnScreen() and SoEvent positions use the
            # view's bottom-origin coordinate system.  The Y inversion is
            # only needed when converting to a Qt QMouseEvent.
            return float(projected[0]), float(projected[1])
        except Exception:
            return None

    def _snap_geometry_point(self, hit):
        """Return a synthetic center point for supported hit components."""

        component = hit.get("component", "")
        obj = hit.get("object")
        edge_index = _component_index(component, "Edge")
        if edge_index is not None:
            point = _straight_edge_midpoint(obj, edge_index)
            if point is not None:
                return point, "Edge%d midpoint" % (edge_index + 1)

        return None

    def _enrich_snap_hit(self, hit, sample, center, sample_index):
        """Add search metadata and use a nearby edge midpoint when found."""

        dx = float(sample[0] - center[0])
        dy = float(sample[1] - center[1])
        sample_distance = (dx * dx + dy * dy) ** 0.5
        enriched = dict(hit)
        enriched["snap_screen_position"] = sample
        enriched["snap_screen_distance"] = sample_distance
        enriched["snap_sample_index"] = sample_index
        enriched["snap_geometry_center"] = False

        geometry_point = self._snap_geometry_point(hit)
        if geometry_point is None:
            return enriched

        point, component = geometry_point
        projected = self._screen_position_for_point(point)
        if projected is None:
            return enriched

        center_dx = projected[0] - float(center[0])
        center_dy = projected[1] - float(center[1])
        center_distance = (center_dx * center_dx + center_dy * center_dy) ** 0.5
        if center_distance > self.snap_radius_pixels:
            return enriched

        enriched["point"] = point
        enriched["component"] = component
        enriched["snap_screen_position"] = projected
        enriched["snap_screen_distance"] = center_distance
        enriched["snap_geometry_center"] = True
        return enriched

    def _parent_group(self, obj):
        getter = getattr(obj, "getParentGeoFeatureGroup", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:
            return None

    def _moving_target_for_object(self, obj):
        """Return the selected target that owns or matches ``obj``."""

        if obj is None:
            return None
        owner = _body_for_object(obj)
        for target in self.move_objects:
            if _same_object(obj, target) or (
                owner is not None and _same_object(owner, target)
            ):
                return target
        return None

    def _binder_follows_other_moving_target(self, binder, current_target):
        """Check whether a binder already follows another selected target.

        A traced ShapeBinder follows its support. If that support is another
        selected Body, the support Body's shared translation already moves the
        binder. Applying the Body-follow adjustment a second time would make
        linked geometry drift apart, especially after a second axis move.
        """

        if not _binder_tracks_support(binder):
            return False

        try:
            support = binder.Support
        except Exception:
            return False

        for linked in _linked_objects(support):
            moving_target = self._moving_target_for_object(linked)
            if moving_target is not None and not _same_object(
                moving_target, current_target
            ):
                return True
        return False

    def _belongs_to_moving_object(self, obj):
        if obj is None:
            return False

        # Snapping should recognize both the actual Placement target and the
        # object the user thinks they grabbed.  The latter is normally the
        # selected Body when a binder is being moved on its behalf.
        roots = []
        for root in self.move_objects + self.visual_objects:
            if not any(_same_object(root, existing) for existing in roots):
                roots.append(root)

        for root in roots:
            if _same_object(obj, root):
                return True

            # A view hit may report a visible PartDesign tip or link whose
            # parent chain is not exposed consistently by every FreeCAD
            # version.  Resolve its owning Body before falling back to Group
            # traversal.
            owner = _body_for_object(obj)
            if owner is not None and _same_object(owner, root):
                return True

            current = obj
            visited = set()
            for _index in range(32):
                key = _object_key(current)
                if key in visited:
                    break
                visited.add(key)
                parent = self._parent_group(current)
                if parent is None:
                    break
                if _same_object(parent, root):
                    return True
                current = parent

            # Some older PartDesign objects expose the Body relationship
            # through Group but not getParentGeoFeatureGroup.
            try:
                pending = list(getattr(root, "Group", []) or [])
            except Exception:
                pending = []
            visited = set()
            while pending:
                candidate = pending.pop()
                key = _object_key(candidate)
                if key in visited:
                    continue
                visited.add(key)
                if _same_object(candidate, obj):
                    return True
                try:
                    pending.extend(list(getattr(candidate, "Group", []) or []))
                except Exception:
                    pass
        return False

    def _sticky_target_hit(self, screen_position):
        """Keep a recently acquired target while the cursor is still nearby."""

        if self.snap_release_pixels <= 0:
            return None

        previous = self.target_hit
        if not isinstance(previous, dict):
            return None
        previous_position = previous.get("snap_screen_position")
        if previous_position is None:
            return None

        dx = float(screen_position[0]) - float(previous_position[0])
        dy = float(screen_position[1]) - float(previous_position[1])
        if dx * dx + dy * dy > self.snap_release_pixels ** 2:
            return None

        sticky = dict(previous)
        sticky["snap_screen_distance"] = (dx * dx + dy * dy) ** 0.5
        sticky["snap_sticky"] = True
        return sticky

    def _pick_snap_hit(self, screen_position, source):
        """Pick a nearby snap component using a small screen-space search."""

        if screen_position is None:
            return None

        center = (int(screen_position[0]), int(screen_position[1]))
        samples = _snap_sample_positions(center, self.snap_radius_pixels)
        hits = []
        candidates = []
        for sample_index, sample in enumerate(samples):
            sample_hits = self._objects_at(sample)
            dx = float(sample[0] - center[0])
            dy = float(sample[1] - center[1])
            sample_distance = (dx * dx + dy * dy) ** 0.5
            for hit in sample_hits:
                enriched = self._enrich_snap_hit(
                    hit, sample, center, sample_index
                )
                hits.append(enriched)

                # Off-center face/solid hits describe a broad surface rather
                # than a nearby snap point. Keep them for an explicit B
                # fallback, but only use precise components for magnetic
                # searching away from the exact cursor position.
                if (
                    sample_distance > 0.0
                    and not enriched.get("snap_geometry_center", False)
                    and _component_priority(enriched.get("component", "")) > 2
                ):
                    continue
                belongs = self._belongs_to_moving_object(enriched["object"])
                if belongs == source:
                    candidates.append(enriched)

        self.debug_snap_query_count += 1

        # B is an explicit request to choose a source point. If FreeCAD's hit
        # record does not expose the selected Body's ownership (common for
        # linked/tip display paths), keep the nearest visible point instead of
        # rejecting the click outright. The normal ownership match wins when
        # available.
        if source and not candidates and hits:
            candidates = list(hits)
            _debug(
                "snap source ownership fallback object=%s component=%s"
                % (
                    _object_key(candidates[0]["object"]),
                    candidates[0]["component"] or "point",
                )
            )

        if not candidates and not source:
            sticky = self._sticky_target_hit(center)
            if sticky is not None:
                return sticky

        if not candidates:
            if (
                self.debug_snap_query_count <= 3
                or self.debug_snap_query_count % 25 == 0
            ):
                _debug(
                    "snap %s query at %s: no candidate (raw_hits=%d samples=%d radius=%d)"
                    % (
                        "source" if source else "target",
                        screen_position,
                        len(hits),
                        len(samples),
                        self.snap_radius_pixels,
                    )
                )
            return None

        # Prefer the most precise component in the search area. Distance is
        # the tie-breaker, so a nearby Vertex beats a broad Face under the
        # cursor while two Vertices choose the one closest to the cursor.
        return min(
            candidates,
            key=lambda hit: (
                _component_priority(hit.get("component", "")),
                hit.get("snap_screen_distance", 0.0),
                hit.get("snap_sample_index", 0),
            ),
        )

    def _pick_source(self, screen_position):
        return self._pick_snap_hit(screen_position, True)

    def _pick_target(self, screen_position):
        return self._pick_snap_hit(screen_position, False)

    def _apply_global_translation(self, baseline, delta, baselines=None):
        """Apply one world-space translation to every selected target."""

        if baselines is None:
            baselines = self.original_globals

        desired_primary = None
        for index, state in enumerate(self.target_states):
            target_baseline = (
                baselines[index]
                if index < len(baselines)
                else baseline
            )
            desired = _copy_placement(target_baseline)
            desired.Base = (
                _copy_vector(target_baseline.Base) + _copy_vector(delta)
            )
            target = state["obj"]
            local = _global_to_local(target, desired)
            try:
                target.Placement = local
            except Exception:
                raise RuntimeError(
                    "A selected object has a read-only Placement"
                )

            state["preview_global"] = desired
            if index == 0:
                desired_primary = desired

        if desired_primary is not None:
            self.preview_global = desired_primary
        self._update_hud()
        self.debug_move_count += 1
        if self.debug_move_count <= 3 or self.debug_move_count % 25 == 0:
            _debug(
                "placement update #%d delta=(%.3f, %.3f, %.3f) base=(%.3f, %.3f, %.3f)"
                % (
                    self.debug_move_count,
                    delta.x, delta.y, delta.z,
                    desired.Base.x, desired.Base.y, desired.Base.z,
                )
            )

    def _update_source_marker(self):
        """Keep the yellow source marker attached to the moving object."""

        if self.source_world is None:
            return

        try:
            if self.snap_baseline_global is not None:
                translation = (
                    _copy_vector(self.preview_global.Base)
                    - _copy_vector(self.snap_baseline_global.Base)
                )
                current_source = _copy_vector(self.source_world) + translation
            elif self.source_local is not None:
                current_source = self.preview_global.multVec(self.source_local)
            else:
                return
            self.source_marker.set_point(current_source)
        except Exception:
            _debug("source marker update failed:\n%s" % traceback.format_exc())

    def _sync_body_binders(self, final_local, target_state=None):
        """Make internal ShapeBinders follow the Body's committed translation."""

        if target_state is None:
            target_state = self.target_states[0]
        follow_binder_states = target_state["follow_binder_states"]
        if not follow_binder_states:
            return

        body_delta = (
            _copy_vector(final_local.Base)
            - _copy_vector(target_state["original_local"].Base)
        )
        _debug(
            "body follow delta=(%.3f, %.3f, %.3f) binders=%s"
            % (
                body_delta.x,
                body_delta.y,
                body_delta.z,
                [_object_key(item[0]) for item in follow_binder_states],
            )
        )

        for binder, original in follow_binder_states:
            if any(
                _same_object(binder, target) for target in self.move_objects
            ):
                _debug(
                    "body follow skipped selected binder=%s"
                    % (_object_key(binder),)
                )
                continue
            if self._binder_follows_other_moving_target(
                binder, target_state["obj"]
            ):
                _debug(
                    "body follow skipped traced linked binder=%s target=%s"
                    % (_object_key(binder), _object_key(target_state["obj"]))
                )
                continue
            desired = _copy_placement(original)
            desired.Base = _copy_vector(original.Base) + body_delta
            try:
                binder.Placement = desired
                _debug(
                    "body follow applied binder=%s base=(%.3f, %.3f, %.3f)"
                    % (
                        _object_key(binder),
                        desired.Base.x,
                        desired.Base.y,
                        desired.Base.z,
                    )
                )
            except Exception:
                _debug(
                    "body follow failed binder=%s:\n%s"
                    % (_object_key(binder), traceback.format_exc())
                )

    def _update_move(self, screen_position):
        world = self._view_point(screen_position)
        if world is None:
            return

        self.last_screen_position = screen_position
        if self.initial_cursor_world is None:
            self.initial_cursor_world = world

        delta = world - self.initial_cursor_world
        self._apply_global_translation(
            self.original_global,
            self._delta_with_numeric(delta),
            self.original_globals,
        )

    def _update_source_hover(self, screen_position):
        hit = self._pick_source(screen_position)
        if hit is None:
            self.source_marker.hide()
            self._status(
                "Grab Move: B mode — hover a point on selected geometry"
            )
            return

        self.source_marker.set_point(hit["point"])
        component = hit["component"] or "point"
        self._status(
            "Grab Move: click snap source (%s) | X/Y/Z constrain | Esc cancel"
            % component
        )

    def _begin_snap_source(self, screen_position):
        hit = self._pick_source(screen_position)
        if hit is None:
            _debug("snap source click missed at %s" % (screen_position,))
            self._status(
                "Grab Move: no source point under cursor; hover selected geometry"
            )
            return False

        self.source_hit = hit
        self.source_world = _copy_vector(hit["point"])
        self.snap_baseline_placements = [
            _copy_placement(state["preview_global"])
            for state in self.target_states
        ]
        self.snap_baseline_global = _copy_placement(self.preview_global)
        try:
            self.source_local = self.snap_baseline_global.inverse().multVec(
                self.source_world
            )
        except Exception:
            self.source_local = None

        self.source_marker.set_point(self.source_world)
        self.target_marker.hide()
        self.target_hit = None
        self.phase = "snap_target"
        self._apply_numeric_value()
        _debug("source marker placed at selected snap point")
        _debug(
            "snap source selected object=%s component=%s point=(%.3f, %.3f, %.3f)"
            % (
                _object_key(hit["object"]),
                hit["component"] or "point",
                self.source_world.x, self.source_world.y, self.source_world.z,
            )
        )
        self._status(
            "Grab Move: source set (yellow marker) — hover a target and click | "
            "X/Y/Z constrain | Esc cancel"
        )
        return True

    def _update_snap_target(self, screen_position):
        if self.source_world is None or self.snap_baseline_global is None:
            return

        target = self._pick_target(screen_position)
        if target is not None:
            self.target_hit = target
            target_point = target["point"]
            self.target_marker.set_point(target_point)
            delta = target_point - self.source_world
            component = target["component"] or "point"
            self._status(
                "Grab Move: target %s — click to place | X/Y/Z constrain | Esc cancel"
                % component
            )
        else:
            self.target_hit = None
            self.target_marker.hide()
            cursor_point = self._view_point(screen_position)
            if cursor_point is None:
                return
            delta = cursor_point - self.source_world
            self._status(
                "Grab Move: move source freely or hover target geometry | "
                "X/Y/Z constrain | LMB/Enter accept | Esc cancel"
            )

        self._apply_global_translation(
            self.snap_baseline_global,
            self._delta_with_numeric(delta),
            self.snap_baseline_placements,
        )
        self._update_source_marker()

    def _set_axis(self, key):
        if key not in ("X", "Y", "Z"):
            return
        self.axis = None if self.axis == key else key
        self._reset_numeric_input()
        axis_text = self.axis if self.axis is not None else "free"

        if self.phase == "move" and self.last_screen_position is not None:
            self._update_move(self.last_screen_position)
        elif self.phase == "snap_target" and self.last_screen_position is not None:
            self._update_snap_target(self.last_screen_position)
        else:
            self._status("Grab Move: constraint %s" % axis_text)
            self._update_hud()

    def _handle_keyboard(self, event):
        if _event_state(event) not in ("", "DOWN", "PRESS"):
            return

        key = _event_key(event)
        _debug(
            "keyboard event key=%s state=%s phase=%s"
            % (key, _event_state(event), self.phase)
        )
        if key in ("ESC", "ESCAPE"):
            self.finish(False)
            return
        if key in ("RETURN", "ENTER", "KP_ENTER"):
            self.finish(True)
            return
        if key in ("X", "Y", "Z"):
            self._set_axis(key)
            return
        if self._handle_numeric_input(key):
            return
        if key == "B" and self.phase == "move":
            self.phase = "pick_source"
            self._reset_numeric_input()
            self.snap_baseline_placements = [
                _copy_placement(state["preview_global"])
                for state in self.target_states
            ]
            self.snap_baseline_global = _copy_placement(self.preview_global)
            self.source_marker.hide()
            self.target_marker.hide()
            if self.last_screen_position is not None:
                self._update_source_hover(self.last_screen_position)
            self._status(
                "Grab Move: B mode — hover a point on selected geometry "
                "and click"
            )
            self._update_hud()

    def _handle_qt_key(self, key):
        """Handle a key captured before FreeCAD's view shortcut dispatcher."""

        if self.done:
            return
        self._handle_keyboard(
            {
                "Type": "SoKeyboardEvent",
                "Key": _text(key).upper(),
                "State": "DOWN",
            }
        )

    def _handle_mouse(self, event):
        if _event_state(event) not in ("", "DOWN", "PRESS"):
            return

        position = _event_position(event) or self.last_screen_position
        button = _event_button(event)
        _debug(
            "mouse event button=%s state=%s position=%s phase=%s"
            % (button, _event_state(event), position, self.phase)
        )
        if button in ("BUTTON3", "RIGHT", "RIGHTBUTTON"):
            self.finish(False)
            return
        if button not in ("BUTTON1", "LEFT", "LEFTBUTTON", "1"):
            return

        if self.phase == "move":
            self.finish(True)
        elif self.phase == "pick_source":
            self._begin_snap_source(position)
        elif self.phase == "snap_target":
            self._update_snap_target(position)
            if self.target_hit is not None:
                _debug(
                    "snap target confirmed object=%s component=%s point=(%.3f, %.3f, %.3f)"
                    % (
                        _object_key(self.target_hit["object"]),
                        self.target_hit["component"] or "point",
                        self.target_hit["point"].x,
                        self.target_hit["point"].y,
                        self.target_hit["point"].z,
                    )
                )
                self.finish(True)
                return

            # B selects the source point, but it must not force the user to
            # find a second snap point.  If the cursor is over empty space,
            # _update_snap_target() already applied the free-space view-point
            # translation; the click should commit that placement just like a
            # normal Grab Move click does.  Only ignore the click when there
            # is no usable 3D point at all.
            free_point = (
                self._view_point(position) if position is not None else None
            )
            if free_point is not None:
                _debug(
                    "free-space target confirmed point=(%.3f, %.3f, %.3f)"
                    % (free_point.x, free_point.y, free_point.z)
                )
                self.finish(True)
            else:
                _debug(
                    "free-space target click ignored: no 3D point at %s"
                    % (position,)
                )

    def _handle_event(self, event):
        if self.done or not isinstance(event, dict):
            return

        event_type = _event_type(event)
        self.debug_event_count += 1
        if not event_type.endswith("Location2Event") or (
            self.debug_event_count <= 3 or self.debug_event_count % 25 == 0
        ):
            _debug(
                "event #%d type=%s position=%s"
                % (self.debug_event_count, event_type, _event_position(event))
            )
        if event_type.endswith("KeyboardEvent"):
            self._handle_keyboard(event)
        elif event_type.endswith("MouseButtonEvent"):
            self._handle_mouse(event)
        elif event_type.endswith("Location2Event"):
            position = _event_position(event)
            if position is None:
                return
            self.last_screen_position = position
            if self.phase == "move":
                self._update_move(position)
            elif self.phase == "pick_source":
                self._update_source_hover(position)
            elif self.phase == "snap_target":
                self._update_snap_target(position)

    def finish(self, commit):
        """Request completion after Coin has returned from the event callback."""

        if self.done or self.finish_pending:
            return
        self.done = True
        self.finish_pending = True
        self.pending_commit = bool(commit)
        _debug(
            "finish requested commit=%s phase=%s events=%d moves=%d"
            % (
                self.pending_commit,
                self.phase,
                self.debug_event_count,
                self.debug_move_count,
            )
        )

        try:
            QtCore, _QtGui, _QtWidgets = _qt_modules()
            QtCore.QTimer.singleShot(0, self._finish_deferred)
            _debug("finish cleanup deferred until Qt event loop returns")
        except Exception:
            # This is only a fallback for headless/startup error paths. In the
            # normal GUI path Qt's zero-delay timer keeps cleanup outside the
            # Coin SoHandleEventAction traversal.
            _debug("finish cleanup could not be deferred; running fallback")
            self._finish_deferred()

    def _finish_deferred(self):
        """Remove the modal callback and close the transaction safely."""

        if self.cleanup_done:
            return
        self.cleanup_done = True
        commit = bool(self.pending_commit)
        _debug(
            "finishing commit=%s phase=%s events=%d moves=%d"
            % (commit, self.phase, self.debug_event_count, self.debug_move_count)
        )

        final_locals = [
            _copy_placement(state["obj"].Placement)
            for state in self.target_states
        ]
        final_local = final_locals[0]
        _debug(
            "final placement before transaction targets=%d primary_base=(%.3f, %.3f, %.3f)"
            % (
                len(final_locals),
                final_local.Base.x,
                final_local.Base.y,
                final_local.Base.z,
            )
        )

        if not commit:
            for state in self.target_states:
                try:
                    state["obj"].Placement = _copy_placement(
                        state["original_local"]
                    )
                except Exception:
                    _debug(
                        "could not restore cancelled target=%s:\n%s"
                        % (_object_key(state["obj"]), traceback.format_exc())
                    )

        if self.callback_id is not None:
            try:
                self.view.removeEventCallback("SoEvent", self.callback_id)
            except Exception:
                pass
            self.callback_id = None

        self._remove_scene_markers()
        self._remove_hud()

        if commit:
            # Apply the Body's final translation to its internal synchronized
            # Binders before recompute, so the PartDesign result does not
            # resolve back to the pre-grab location.
            for state, target_final_local in zip(
                self.target_states, final_locals
            ):
                self._sync_body_binders(target_final_local, state)
            # Recompute while the transaction is still open. Some PartDesign
            # documents can refresh a Body during recompute; preserve the
            # final snapped Placement before committing the undo record.
            try:
                self.document.recompute()
            except Exception:
                _debug("document recompute during commit failed:\n%s" % traceback.format_exc())
            for state, target_final_local in zip(
                self.target_states, final_locals
            ):
                target = state["obj"]
                try:
                    if not _same_translation(target.Placement, target_final_local):
                        _debug(
                            "recompute changed final placement; restoring "
                            "snapped placement target=%s"
                            % (_object_key(target),)
                        )
                        target.Placement = _copy_placement(target_final_local)
                except Exception:
                    _debug(
                        "could not restore final placement after recompute "
                        "target=%s:\n%s"
                        % (_object_key(target), traceback.format_exc())
                    )

        if self.transaction_open:
            try:
                if commit:
                    self.document.commitTransaction()
                else:
                    self.document.abortTransaction()
            except Exception:
                _debug(
                    "%s transaction failed:\n%s"
                    % ("commit" if commit else "abort", traceback.format_exc())
                )
            self.transaction_open = False

        try:
            finished_locals = [
                _copy_placement(state["obj"].Placement)
                for state in self.target_states
            ]
            finished_local = finished_locals[0]
            _debug(
                "final placement after transaction targets=%d primary_base=(%.3f, %.3f, %.3f)"
                % (
                    len(finished_locals),
                    finished_local.Base.x,
                    finished_local.Base.y,
                    finished_local.Base.z,
                )
            )
        except Exception:
            pass

        if getattr(App, SESSION_ATTRIBUTE, None) is self:
            setattr(App, SESSION_ATTRIBUTE, None)
        self._clear_status()
        _debug("session finished")


class LinearPatternPreviewGrabSession(GrabMoveSession):
    """Run the normal GrabMove G/B workflow against a pattern preview."""

    def __init__(self, controller, data, index=None):
        pattern = controller._pattern
        if pattern is None or data is None:
            raise ValueError("A Linear Pattern preview is required")
        if index is not None and int(index) <= 0:
            raise ValueError("A preview occurrence is required")

        self.controller = controller
        self.pattern = pattern
        self.data = data
        self.index = int(index) if index is not None else None
        self.view = controller._view or _active_view()
        self.document = _document_for_object(pattern)
        self._QtCore, _QtGui, _QtWidgets = _qt_modules()

        # The checked overlay makes this preview the active GrabMove target.
        # Its only motion is along the pattern direction; B enters the same
        # snap-source step used by a normal object grab.
        self.phase = "move"
        self.axis = None
        self.done = False
        self.finish_pending = False
        self.pending_commit = None
        self.cleanup_done = False
        self.callback_id = None
        self.transaction_open = False
        self.marker_root = None

        self.last_screen_position = controller._last_pointer_position
        self.numeric_buffer = ""
        self.numeric_value = None
        self.hud_label = None
        self.hud_viewport = None

        self.property_name = _text(data["property_name"])
        self.original_value = controller._quantity_value(
            getattr(pattern, self.property_name, 0.0)
        )
        self.expression_snapshot = controller._expression_map(pattern)
        self.current_value = self.original_value

        self.source_world = None
        self.source_scalar = None
        self.source_occurrence_scalar = None
        self.source_local_scalar = 0.0
        self.target_hit = None
        self.source_hit = None
        self.snap_baseline_value = None
        self.source_marker = _Marker((1.0, 0.75, 0.1))
        self.target_marker = _Marker((0.1, 0.9, 1.0))

        self.snap_radius_pixels = _snap_radius_pixels()
        self.snap_release_pixels = (
            self.snap_radius_pixels + 6 if self.snap_radius_pixels else 0
        )
        self.debug_event_count = 0
        self.debug_move_count = 0
        self.debug_snap_query_count = 0

        self._move_anchor_occurrence_scalar = None
        self._move_anchor_cursor_scalar = None

        _debug(
            "preview GrabMove session started pattern=%s"
            % (_object_key(pattern),)
        )

    def _occurrence_scalar(self, data):
        if self.index is None:
            return None
        point = data["points"][self.index]
        return (point - data["start"]).dot(data["direction"])

    def _current_value(self):
        try:
            return self.controller._quantity_value(
                getattr(self.pattern, self.property_name)
            )
        except Exception:
            return self.current_value

    def _current_data(self):
        return self.controller._pattern_data_at_value(
            self.data, self._current_value()
        )

    def _value_for_occurrence_scalar(self, scalar):
        if self.index is None:
            return None
        scalar = max(0.001, float(scalar))
        occurrences = float(max(1, int(self.data["occurrences"]) - 1))
        index = float(max(1, self.index))
        if self.property_name.lower() == "offset":
            return scalar / index
        return scalar * occurrences / index

    def _apply_occurrence_scalar(self, scalar):
        value = self._value_for_occurrence_scalar(scalar)
        if value is None:
            return False
        if not self.controller._set_parameter(self.property_name, value):
            return False
        self.current_value = value
        self.controller._update_overlay(
            self.controller._pattern_data_at_value(self.data, value),
            redraw=False,
        )
        self.controller._schedule_recompute()
        self._update_source_marker()
        return True

    def _update_move(self, screen_position):
        if self.index is None:
            return
        data = self._current_data()
        scalar = self.controller._ray_scalar(screen_position, data)
        if scalar is None:
            return
        self.last_screen_position = screen_position
        delta = float(scalar) - float(self._move_anchor_cursor_scalar)
        if self.axis is not None:
            axis = _axis_vector(self.axis)
            delta *= abs(float(axis.dot(data["direction"])))
        self._apply_occurrence_scalar(
            self._move_anchor_occurrence_scalar + delta
        )

    def _pick_source(self, screen_position):
        if screen_position is None:
            return None
        data = self._current_data()
        index = self.controller._nearest_occurrence(screen_position, data)
        if index is None:
            return None
        point = data["points"][index]
        projected = self.controller._screen_position(point)
        if projected is None:
            projected = screen_position
        dx = float(projected[0]) - float(screen_position[0])
        dy = float(projected[1]) - float(screen_position[1])
        return {
            "record": {},
            "object": self.pattern,
            "index": index,
            "point": point,
            "component": "Preview occurrence %d" % (index + 1),
            "snap_screen_position": projected,
            "snap_screen_distance": (dx * dx + dy * dy) ** 0.5,
        }

    def _occurrence_index_for_point(self, point, data):
        """Identify which repeated preview owns a picked geometry point."""

        try:
            step = float(data["step"])
            if step <= 1.0e-12:
                return None
            scalar = (point - data["start"]).dot(data["direction"])
            index = int(round(float(scalar) / step))
            if 1 <= index < int(data["occurrences"]):
                return index
        except Exception:
            pass
        return None

    def _pick_preview_geometry_source(self, screen_position):
        """Use GrabMove's normal snap picker, restricted to the preview."""

        if screen_position is None:
            return None
        center = (int(screen_position[0]), int(screen_position[1]))
        candidates = []
        for sample_index, sample in enumerate(
            _snap_sample_positions(center, self.snap_radius_pixels)
        ):
            for hit in self._objects_at(sample):
                if not (
                    _same_object(hit.get("object"), self.pattern)
                    or self.controller._record_matches_pattern(
                        hit.get("record", {})
                    )
                ):
                    continue
                enriched = self._enrich_snap_hit(
                    hit, sample, center, sample_index
                )
                index = self._occurrence_index_for_point(
                    enriched["point"], self._current_data()
                )
                if index is None:
                    continue
                enriched["index"] = index
                candidates.append(enriched)

        self.debug_snap_query_count += 1
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda hit: (
                _component_priority(hit.get("component", "")),
                hit.get("snap_screen_distance", 0.0),
                hit.get("snap_sample_index", 0),
            ),
        )

    def _update_source_hover(self, screen_position):
        hit = self._pick_source(screen_position)
        if hit is None:
            self.source_marker.hide()
            self._status(
                "Grab Move: B mode — click a point on preview geometry"
            )
            return
        self.source_marker.set_point(hit["point"])
        self._status(
            "Grab Move: click preview source | hover a target and click | Esc cancel"
        )

    def _begin_snap_source(self, screen_position):
        # Prefer the actual preview vertex/edge/face point, exactly like a
        # normal Body grab. The orange occurrence handle remains a fallback.
        hit = self._pick_preview_geometry_source(screen_position)
        if hit is None:
            hit = self._pick_source(screen_position)
        if hit is None:
            _debug("preview snap source click missed at %s" % (screen_position,))
            self._status(
                "Grab Move: no preview occurrence under cursor"
            )
            return False

        self.source_hit = hit
        self.index = int(hit["index"])
        self.source_world = _copy_vector(hit["point"])
        self._move_anchor_occurrence_scalar = self._occurrence_scalar(
            self._current_data()
        )
        self._move_anchor_cursor_scalar = self._move_anchor_occurrence_scalar
        self.source_occurrence_scalar = self._move_anchor_occurrence_scalar
        self.source_scalar = (
            self.source_world - self.data["start"]
        ).dot(self.data["direction"])
        self.source_local_scalar = (
            self.source_scalar - self.source_occurrence_scalar
        )
        self.snap_baseline_value = self._current_value()
        self.target_hit = None
        self.phase = "snap_target"
        self.source_marker.set_point(self.source_world)
        self.target_marker.hide()
        _debug(
            "preview snap source selected occurrence=%d point=(%.3f, %.3f, %.3f)"
            % (
                self.index,
                self.source_world.x,
                self.source_world.y,
                self.source_world.z,
            )
        )
        self._status(
            "Grab Move: source set — hover any target point and click | Esc cancel"
        )
        return True

    def _update_snap_target(self, screen_position):
        if self.source_world is None or self.source_scalar is None:
            return

        self.last_screen_position = screen_position
        target = self._pick_target(screen_position)
        if target is not None:
            self.target_hit = target
            target_point = target["point"]
            self.target_marker.set_point(target_point)
            self._status(
                "Grab Move: target %s — click to place | Esc cancel"
                % (target["component"] or "point")
            )
        else:
            self.target_hit = None
            self.target_marker.hide()
            target_point = self._view_point(screen_position)
            if target_point is None:
                return
            self._status(
                "Grab Move: move over a target point and click | Esc cancel"
            )

        delta = target_point - self.source_world
        desired_source_scalar = (
            self.source_scalar + delta.dot(self.data["direction"])
        )
        self._apply_occurrence_scalar(
            desired_source_scalar - self.source_local_scalar
        )

    def _update_source_marker(self):
        if self.source_world is None or self.index is None:
            return
        data = self._current_data()
        try:
            current_occurrence_scalar = self._occurrence_scalar(data)
            shift = (
                current_occurrence_scalar - self.source_occurrence_scalar
            )
            self.source_marker.set_point(
                self.source_world + data["direction"] * shift
            )
        except Exception:
            pass

    def _belongs_to_moving_object(self, obj):
        # The temporary PartDesign preview is reported as the pattern object
        # by current FreeCAD builds. Exclude it from target candidates so the
        # snap target must be other geometry.
        return _same_object(obj, self.pattern)

    def _handle_keyboard(self, event):
        if _event_state(event) not in ("", "DOWN", "PRESS"):
            return
        key = _event_key(event)
        _debug(
            "preview keyboard event key=%s state=%s phase=%s"
            % (key, _event_state(event), self.phase)
        )
        if key in ("ESC", "ESCAPE"):
            self.finish(False)
            return
        if key in ("RETURN", "ENTER", "KP_ENTER"):
            self.finish(True)
            return
        if key == "B" and self.phase == "move":
            self.phase = "pick_source"
            self._reset_numeric_input()
            self.source_marker.hide()
            self.target_marker.hide()
            if self.last_screen_position is not None:
                self._update_source_hover(self.last_screen_position)
            self._status(
                "Grab Move: B mode — click a preview occurrence as the source"
            )
            return
        if key in ("X", "Y", "Z"):
            self._status(
                "Preview GrabMove follows the Linear Pattern direction; "
                "X/Y/Z are not used"
            )

    def _handle_mouse(self, event):
        if _event_state(event) not in ("", "DOWN", "PRESS"):
            return
        position = _event_position(event) or self.last_screen_position
        button = _event_button(event)
        if button in ("BUTTON3", "RIGHT", "RIGHTBUTTON"):
            self.finish(False)
            return
        if button not in ("BUTTON1", "LEFT", "LEFTBUTTON", "1"):
            return

        if self.phase == "move":
            self.finish(True)
        elif self.phase == "pick_source":
            self._begin_snap_source(position)
        elif self.phase == "snap_target":
            self._update_snap_target(position)
            if self.target_hit is not None:
                self.finish(True)
                return
            if position is not None and self._view_point(position) is not None:
                self.finish(True)

    def start(self):
        if self.view is None:
            raise RuntimeError("No active 3D view")
        self._install_scene_markers()
        self.callback_id = self.view.addEventCallback(
            "SoEvent", self._handle_event
        )
        self._focus_view()
        self._status(
            "Grab Move: press B, click a preview source, then click a target | "
            "Esc cancel"
        )
        _debug("preview GrabMove event callback installed")

    def finish(self, commit):
        if self.done or self.finish_pending:
            return
        self.done = True
        self.finish_pending = True
        self.pending_commit = bool(commit)
        _debug(
            "preview GrabMove finish requested commit=%s phase=%s events=%d moves=%d"
            % (
                self.pending_commit,
                self.phase,
                self.debug_event_count,
                self.debug_move_count,
            )
        )
        try:
            self._QtCore.QTimer.singleShot(0, self._finish_deferred)
        except Exception:
            self._finish_deferred()

    def _finish_deferred(self):
        if self.cleanup_done:
            return
        self.cleanup_done = True
        commit = bool(self.pending_commit)
        if not commit:
            try:
                self.controller._set_parameter(
                    self.property_name, self.original_value
                )
                for name, expression in self.expression_snapshot.items():
                    setter = getattr(self.pattern, "setExpression", None)
                    if callable(setter):
                        setter(name, expression)
                self.controller._schedule_recompute()
            except Exception:
                _debug(
                    "preview GrabMove cancel restore failed:\n%s"
                    % traceback.format_exc()
                )

        if self.callback_id is not None:
            try:
                self.view.removeEventCallback("SoEvent", self.callback_id)
            except Exception:
                pass
            self.callback_id = None
        self._remove_scene_markers()
        self.controller._update_overlay(
            self.controller._pattern_data_at_value(
                self.data,
                self.original_value if not commit else self._current_value(),
            )
        )
        if getattr(App, SESSION_ATTRIBUTE, None) is self:
            setattr(App, SESSION_ATTRIBUTE, None)
        if getattr(self.controller, "_preview_session", None) is self:
            self.controller._preview_session = None
        self._clear_status()
        _debug(
            "preview GrabMove session finished commit=%s" % commit
        )


class LinearPatternPreviewGrabController(object):
    """Expose native Linear Pattern previews to GrabMove's G/B workflow.

    PartDesign owns a temporary Coin preview for each occurrence rather than
    document objects. This controller draws small orange handles through the
    public 3D-view API, tracks the hovered handle without consuming input,
    and changes the pattern's existing Length or Offset while the native edit
    transaction is still open.
    """

    CHECKBOX_NAME = "GrabMoveLinearPatternPreviewCheckBox"
    # The native PartDesign preview is fairly expensive on a real model.
    # Keep task discovery and geometry recompute out of the high-frequency
    # mouse-event path so a dense model cannot starve FreeCAD's Qt event loop.
    POLL_INTERVAL_MS = 300
    RECOMPUTE_INTERVAL_MS = 120
    HANDLE_RADIUS_PIXELS = 32.0

    def __init__(self, main):
        self.main = main
        self._QtCore, self._QtGui, self._QtWidgets = _qt_modules()
        self._pattern = None
        self._task_root = None
        self._parameter_widget = None
        self._checkbox = None
        self._own_checkbox = False
        self._view = None
        self._callback_view = None
        self._callbacks = []
        self._overlay_root = None
        self._line_coordinates = None
        self._line_set = None
        self._occurrence_coordinates = None
        self._occurrence_set = None
        self._active_coordinates = None
        self._active_set = None
        self._dragging = False
        self._drag_index = None
        self._drag_offset = 0.0
        self._drag_snapshot = None
        self._last_cursor = None
        self._last_scalar = None
        self._drag_data = None
        self._preview_dirty = False
        self._last_pointer_position = None
        self._preview_session = None
        self._closed = False

        self._poll_timer = self._QtCore.QTimer(main)
        self._poll_timer.setInterval(self.POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start()

        self._recompute_timer = self._QtCore.QTimer(main)
        self._recompute_timer.setSingleShot(True)
        self._recompute_timer.setInterval(self.RECOMPUTE_INTERVAL_MS)
        self._recompute_timer.timeout.connect(self._recompute_preview)
        self._poll()

    # ---- task-panel discovery -----------------------------------------

    def _active_dialog(self):
        """Return the native active task dialog when the API is available."""

        control = getattr(Gui, "Control", None)
        for owner in (control, Gui):
            if owner is None:
                continue
            method = getattr(owner, "activeDialog", None)
            if callable(method):
                try:
                    dialog = method()
                    if dialog is not None:
                        return dialog
                except Exception:
                    pass
            dialog = getattr(owner, "ActiveDialog", None)
            if dialog is not None:
                return dialog
        return None

    def _task_panel(self):
        control = getattr(Gui, "Control", None)
        if control is None:
            return None
        method = getattr(control, "taskPanel", None)
        if callable(method):
            try:
                return method()
            except Exception:
                pass
        return None

    def _widget_class_name(self, widget):
        try:
            return _text(widget.metaObject().className())
        except Exception:
            return type(widget).__name__

    def _widget_name(self, widget):
        try:
            return _text(widget.objectName())
        except Exception:
            return ""

    def _widget_descendants(self, root):
        if root is None:
            return []
        widgets = [root]
        try:
            widget_class = getattr(self._QtWidgets, "QWidget", None)
            if widget_class is not None:
                widgets.extend(list(root.findChildren(widget_class)))
            else:
                widgets.extend(list(root.findChildren()))
        except Exception:
            pass

        result = []
        seen = set()
        for widget in widgets:
            marker = id(widget)
            if marker in seen:
                continue
            seen.add(marker)
            result.append(widget)
        return result

    def _find_named_widget(self, root, name):
        wanted = _text(name).lower()
        for widget in self._widget_descendants(root):
            if self._widget_name(widget).lower() == wanted:
                return widget
        return None

    def _find_pattern_task(self, dialog):
        """Find TaskPatternParameters across old and current FreeCAD builds."""

        roots = [dialog, self._task_panel(), self.main]
        for root in roots:
            if root is None:
                continue
            widgets = self._widget_descendants(root)

            # Current FreeCAD calls this TaskPatternParameters.  Older
            # versions used TaskLinearPatternParameters.
            for widget in widgets:
                class_name = self._widget_class_name(widget).lower()
                if (
                    "taskpatternparameters" in class_name
                    or "tasklinearpatternparameters" in class_name
                ):
                    update_box = self._find_named_widget(
                        widget, "checkBoxUpdateView"
                    )
                    return widget, update_box

            # If the C++ task class is hidden from Qt's child search, the
            # stable update-view object name identifies its parameter proxy.
            if root is dialog or dialog is None:
                update_box = self._find_named_widget(
                    root, "checkBoxUpdateView"
                )
                if update_box is not None:
                    try:
                        parent = update_box.parentWidget()
                    except Exception:
                        parent = None
                    return parent or update_box, update_box
        return None, None

    def _edit_object(self):
        """Return the document object currently owned by the edit task."""

        candidates = []
        active_document = getattr(Gui, "ActiveDocument", None)
        if active_document is not None:
            candidates.append(active_document)
        try:
            active_document = Gui.activeDocument()
            if active_document is not None:
                candidates.append(active_document)
        except Exception:
            pass

        for source in candidates:
            method = getattr(source, "getInEdit", None)
            if not callable(method):
                continue
            try:
                value = method()
            except Exception:
                continue
            if isinstance(value, (list, tuple)):
                value = value[0] if value else None
            if value is None:
                continue
            obj = getattr(value, "Object", None)
            if obj is None:
                obj = getattr(value, "object", None)
            if obj is None and hasattr(value, "TypeId"):
                obj = value
            if obj is not None:
                return obj
        return None

    def _is_linear_pattern(self, obj):
        if obj is None:
            return False
        try:
            return "PartDesign::LinearPattern" == _text(obj.TypeId)
        except Exception:
            # FreeCAD invalidates the temporary edit object immediately when
            # the task is accepted/cancelled. The polling timer can see that
            # dead Python wrapper once before it sees getInEdit() clear.
            return False

    def _attach_task_checkbox(self, task_widget, update_box):
        if task_widget is None:
            return False

        existing = self._find_named_widget(task_widget, self.CHECKBOX_NAME)
        if existing is not None:
            self._checkbox = existing
            self._own_checkbox = True
            try:
                existing.setText("Grab preview with mouse")
                existing.setToolTip(
                    "Click and drag an orange Linear Pattern preview occurrence "
                    "along the pattern direction. Right-click cancels the drag."
                )
            except Exception:
                pass
            try:
                self._checkbox.toggled.connect(self._checkbox_toggled)
            except Exception:
                pass
            return True

        checkbox_class = getattr(self._QtWidgets, "QCheckBox", None)
        if checkbox_class is None:
            checkbox_class = getattr(self._QtGui, "QCheckBox", None)
        if checkbox_class is None:
            return False

        parent = task_widget
        layout = None
        if update_box is not None:
            try:
                parent_candidate = update_box.parentWidget()
                if parent_candidate is not None:
                    parent = parent_candidate
            except Exception:
                pass

        # Put the control next to the native Update View control whenever
        # possible, keeping it inside the existing Linear Pattern box.
        probe = parent
        for _index in range(8):
            if probe is None:
                break
            try:
                layout = probe.layout()
            except Exception:
                layout = None
            if layout is not None:
                parent = probe
                break
            try:
                probe = probe.parentWidget()
            except Exception:
                probe = None

        if layout is None:
            try:
                layout_class = getattr(self._QtWidgets, "QVBoxLayout", None)
                if layout_class is None:
                    return False
                layout = layout_class(parent)
                layout.setContentsMargins(0, 0, 0, 0)
            except Exception:
                return False

        try:
            checkbox = checkbox_class(parent)
            checkbox.setObjectName(self.CHECKBOX_NAME)
            checkbox.setText("Grab preview with mouse")
            checkbox.setToolTip(
                "Click and drag an orange Linear Pattern preview occurrence "
                "along the pattern direction. Right-click cancels the drag."
            )
            checkbox.setProperty("GrabMoveOwned", True)
            inserted = False
            if update_box is not None:
                try:
                    index = int(layout.indexOf(update_box))
                    if index >= 0:
                        layout.insertWidget(index + 1, checkbox)
                        inserted = True
                except Exception:
                    pass
            if not inserted:
                layout.addWidget(checkbox)
            checkbox.toggled.connect(self._checkbox_toggled)
            self._checkbox = checkbox
            self._own_checkbox = True
            _debug("Linear Pattern preview grab checkbox added")
            return True
        except Exception:
            _debug(
                "could not add Linear Pattern preview checkbox:\n%s"
                % traceback.format_exc()
            )
            return False

    # ---- pattern geometry and parameters ------------------------------

    def _quantity_value(self, value, fallback=0.0):
        try:
            return float(value)
        except Exception:
            return float(fallback)

    def _shape_center(self, shape):
        try:
            box = shape.BoundBox
            return App.Vector(
                (float(box.XMin) + float(box.XMax)) * 0.5,
                (float(box.YMin) + float(box.YMax)) * 0.5,
                (float(box.ZMin) + float(box.ZMax)) * 0.5,
            )
        except Exception:
            return None

    def _pattern_start_point(self, pattern):
        originals = []
        try:
            originals = list(getattr(pattern, "Originals", []) or [])
        except Exception:
            pass

        # The native task uses the original AddSubShape moved by the feature's
        # location, not the complete patterned Shape.  In Python, Shape is
        # already in the feature's placement while AddSubShape is local, so
        # prefer Shape and transform AddSubShape explicitly when needed.
        for original in originals:
            for property_name in ("Shape", "AddSubShape"):
                try:
                    shape = getattr(original, property_name)
                except Exception:
                    shape = None
                center = self._shape_center(shape)
                if center is not None:
                    if property_name == "AddSubShape":
                        try:
                            return _global_placement(original).multVec(center)
                        except Exception:
                            pass
                    return _shape_point_to_global(original, center)

        base = None
        try:
            base = pattern.getBaseObject()
        except Exception:
            base = getattr(pattern, "BaseFeature", None)
        if base is not None:
            for property_name in ("Shape", "AddSubShape"):
                try:
                    shape = getattr(base, property_name)
                except Exception:
                    shape = None
                center = self._shape_center(shape)
                if center is not None:
                    if property_name == "AddSubShape":
                        try:
                            return _global_placement(base).multVec(center)
                        except Exception:
                            pass
                    return _shape_point_to_global(base, center)
        return None

    def _reference_direction(self, reference, sub_names):
        if reference is None:
            return None

        type_name = _text(getattr(reference, "TypeId", "")).lower()
        if "line" in type_name or "axis" in type_name:
            try:
                placement = _global_placement(reference)
                return placement.Rotation.multVec(App.Vector(1.0, 0.0, 0.0))
            except Exception:
                pass

        component = ""
        try:
            for value in list(sub_names or []):
                if _text(value):
                    component = _text(value)
                    break
        except Exception:
            component = ""

        try:
            shape = reference.Shape
            edge_index = _component_index(component, "Edge")
            if edge_index is not None:
                edges = list(shape.Edges)
                edge = edges[edge_index]
                vertices = list(edge.Vertexes)
                if len(vertices) >= 2:
                    direction = _copy_vector(vertices[-1].Point) - _copy_vector(
                        vertices[0].Point
                    )
                    parent = _parent_global_placement(reference)
                    return parent.Rotation.multVec(direction)

            face_index = _component_index(component, "Face")
            if face_index is not None:
                faces = list(shape.Faces)
                face = faces[face_index]
                normal = face.normalAt(0.0, 0.0)
                parent = _parent_global_placement(reference)
                return parent.Rotation.multVec(normal)
        except Exception:
            pass

        # A few older reference objects expose a direct Direction vector.
        try:
            direct = _vector_from_value(reference.Direction)
            if direct is not None:
                return direct
        except Exception:
            pass
        return None

    def _pattern_direction(self, pattern):
        value = getattr(pattern, "Direction", None)
        reference = None
        sub_names = []
        direct = None
        if isinstance(value, (list, tuple)):
            if value:
                candidate = value[0]
                if hasattr(candidate, "Name"):
                    reference = candidate
                else:
                    direct = _vector_from_value(candidate)
            if len(value) > 1:
                try:
                    sub_names = list(value[1] or [])
                except Exception:
                    sub_names = []
        elif hasattr(value, "Name"):
            reference = value
        else:
            direct = _vector_from_value(value)

        direction = self._reference_direction(reference, sub_names)
        if direction is None:
            direction = direct

        if direction is None:
            try:
                base = pattern.getBaseObject()
                direction = _vector_from_value(base.Direction)
            except Exception:
                direction = None
        if direction is None:
            return None

        try:
            magnitude = math.sqrt(
                float(direction.x) ** 2
                + float(direction.y) ** 2
                + float(direction.z) ** 2
            )
            if magnitude <= 1.0e-12:
                return None
            direction = _copy_vector(direction) * (1.0 / magnitude)
        except Exception:
            return None

        try:
            if bool(pattern.Reversed):
                direction = direction * -1.0
        except Exception:
            pass
        return direction

    def _uniform_spacing_supported(self, pattern):
        try:
            values = getattr(pattern, "SpacingPattern")
        except Exception:
            return True
        if not isinstance(values, (list, tuple)):
            return True
        try:
            return not any(abs(float(value)) > 1.0e-9 for value in values)
        except Exception:
            return True

    def _pattern_data(self, pattern=None):
        if pattern is None:
            pattern = self._pattern
        if pattern is None or not self._is_linear_pattern(pattern):
            return None
        try:
            occurrences = int(getattr(pattern, "Occurrences"))
        except Exception:
            return None
        if occurrences < 2 or not self._uniform_spacing_supported(pattern):
            return None

        try:
            mode = _text(getattr(pattern, "Mode", "Extent")).lower()
        except Exception:
            mode = "extent"
        try:
            length = self._quantity_value(getattr(pattern, "Length", 0.0))
            offset = self._quantity_value(getattr(pattern, "Offset", 0.0))
        except Exception:
            return None
        if "spacing" in mode:
            step = abs(offset)
            if step <= 1.0e-12:
                step = abs(length) / float(max(1, occurrences - 1))
            extent = step * float(occurrences - 1)
            property_name = "Offset"
        else:
            extent = abs(length)
            step = extent / float(max(1, occurrences - 1))
            property_name = "Length"

        start = self._pattern_start_point(pattern)
        direction = self._pattern_direction(pattern)
        if start is None or direction is None:
            return None

        points = [
            start + direction * (step * index)
            for index in range(occurrences)
        ]
        return {
            "start": start,
            "direction": direction,
            "occurrences": occurrences,
            "extent": extent,
            "step": step,
            "mode": mode,
            "property_name": property_name,
            "points": points,
        }

    def _pattern_data_at_value(self, data, value):
        """Rebuild only the lightweight handle geometry after a drag edit."""

        if data is None:
            return None
        try:
            occurrences = int(data["occurrences"])
            property_name = _text(data["property_name"])
            numeric_value = abs(float(value))
            if property_name.lower() == "offset":
                step = numeric_value
                extent = step * float(max(1, occurrences - 1))
            else:
                extent = numeric_value
                step = extent / float(max(1, occurrences - 1))
            points = [
                data["start"] + data["direction"] * (step * index)
                for index in range(occurrences)
            ]
        except Exception:
            return data

        result = dict(data)
        result["step"] = step
        result["extent"] = extent
        result["points"] = points
        return result

    def can_start_grab(self):
        """Return whether the enabled pattern can receive preview GrabMove."""

        if self._closed or self._pattern is None or self._checkbox is None:
            return False
        if getattr(App, SESSION_ATTRIBUTE, None) is not None:
            return False
        try:
            if not self._checkbox.isChecked():
                return False
        except Exception:
            return False
        data = self._pattern_data(self._pattern)
        if data is None or _active_view() is None:
            return False
        return True

    def start_preview_grab(self):
        """Start GrabMove for the preview occurrence under the cursor."""

        if not self.can_start_grab():
            return False
        data = self._pattern_data(self._pattern)
        if data is None:
            return False
        session = None
        try:
            session = LinearPatternPreviewGrabSession(self, data)
            self._preview_session = session
            setattr(App, SESSION_ATTRIBUTE, session)
            session.start()
            return True
        except Exception:
            self._preview_session = None
            if session is not None and getattr(App, SESSION_ATTRIBUTE, None) is session:
                setattr(App, SESSION_ATTRIBUTE, None)
            _debug(
                "preview GrabMove start failed:\n%s"
                % traceback.format_exc()
            )
            if session is not None:
                try:
                    session._finish_deferred()
                except Exception:
                    pass
            return False

    # ---- Coin overlay and view callbacks -------------------------------

    def _new_material(self, color):
        material = coin.SoMaterial()
        material.diffuseColor.setValue(*color)
        material.emissiveColor.setValue(*color)
        return material

    def _create_overlay(self):
        if coin is None or self._view is None or self._overlay_root is not None:
            return
        try:
            root = coin.SoSeparator()

            line_group = coin.SoSeparator()
            line_draw = coin.SoDrawStyle()
            line_draw.lineWidth = 3.0
            line_coordinates = coin.SoCoordinate3()
            line_set = coin.SoLineSet()
            line_set.numVertices.set1Value(0, 2)
            line_group.addChild(self._new_material((0.15, 0.9, 1.0)))
            line_group.addChild(line_draw)
            line_group.addChild(line_coordinates)
            line_group.addChild(line_set)

            occurrence_group = coin.SoSeparator()
            occurrence_draw = coin.SoDrawStyle()
            occurrence_draw.pointSize = 11.0
            occurrence_coordinates = coin.SoCoordinate3()
            occurrence_set = coin.SoPointSet()
            occurrence_set.numPoints = 0
            occurrence_group.addChild(self._new_material((1.0, 0.65, 0.1)))
            occurrence_group.addChild(occurrence_draw)
            occurrence_group.addChild(occurrence_coordinates)
            occurrence_group.addChild(occurrence_set)

            active_group = coin.SoSeparator()
            active_draw = coin.SoDrawStyle()
            active_draw.pointSize = 19.0
            active_coordinates = coin.SoCoordinate3()
            active_set = coin.SoPointSet()
            active_set.numPoints = 0
            active_group.addChild(self._new_material((1.0, 0.15, 0.1)))
            active_group.addChild(active_draw)
            active_group.addChild(active_coordinates)
            active_group.addChild(active_set)

            root.addChild(line_group)
            root.addChild(occurrence_group)
            root.addChild(active_group)
            self._view.getSceneGraph().addChild(root)
            self._overlay_root = root
            self._line_coordinates = line_coordinates
            self._line_set = line_set
            self._occurrence_coordinates = occurrence_coordinates
            self._occurrence_set = occurrence_set
            self._active_coordinates = active_coordinates
            self._active_set = active_set
        except Exception:
            self._overlay_root = None
            _debug(
                "could not create Linear Pattern preview overlay:\n%s"
                % traceback.format_exc()
            )

    def _set_coordinate_points(self, field, points):
        if field is None:
            return
        for index, point in enumerate(points):
            field.point.set1Value(
                index,
                coin.SbVec3f(float(point.x), float(point.y), float(point.z)),
            )

    def _screen_position(self, point):
        if self._view is None or point is None:
            return None
        try:
            projected = self._view.getPointOnScreen(_copy_vector(point))
            if hasattr(projected, "getValue"):
                projected = projected.getValue()
            return float(projected[0]), float(projected[1])
        except Exception:
            return None

    def _update_overlay(self, data=None, redraw=True):
        if self._checkbox is None:
            return
        try:
            checked = self._checkbox.isChecked()
        except Exception:
            return
        if not checked:
            return
        data = data or self._pattern_data()
        if data is None:
            return
        self._create_overlay()
        if self._overlay_root is None:
            return
        try:
            self._set_coordinate_points(
                self._line_coordinates,
                [data["points"][0], data["points"][-1]],
            )
            self._line_set.numVertices.set1Value(0, 2)
            self._set_coordinate_points(
                self._occurrence_coordinates, data["points"]
            )
            self._occurrence_set.numPoints = len(data["points"])
            active_index = self._drag_index
            preview_session = self._preview_session
            if (
                active_index is None
                and preview_session is not None
                and not getattr(preview_session, "done", True)
            ):
                active_index = preview_session.index
            if active_index is None or active_index >= len(data["points"]):
                active_index = len(data["points"]) - 1
            self._set_coordinate_points(
                self._active_coordinates, [data["points"][active_index]]
            )
            self._active_set.numPoints = 1
            # Coin changes are cheap; forcing an immediate OpenGL redraw for
            # every mouse-location event is not.  The native viewer normally
            # repaints as it processes the event, and the throttled preview
            # timer performs an explicit redraw after a native recompute.
            if redraw and self._view is not None:
                self._view.redraw()
        except Exception:
            pass

    def _remove_overlay(self):
        root = self._overlay_root
        self._overlay_root = None
        self._line_coordinates = None
        self._line_set = None
        self._occurrence_coordinates = None
        self._occurrence_set = None
        self._active_coordinates = None
        self._active_set = None
        if root is None:
            return
        try:
            self._view.getSceneGraph().removeChild(root)
        except Exception:
            pass

    def _install_callbacks(self):
        view = _active_view()
        if view is None or coin is None:
            return
        if self._callback_view is view and self._callbacks:
            return
        self._remove_callbacks()
        try:
            location_type = coin.SoLocation2Event.getClassTypeId()
            mouse_type = coin.SoMouseButtonEvent.getClassTypeId()
            self._callbacks = [
                (
                    location_type,
                    view.addEventCallbackPivy(
                        location_type, self._on_location
                    ),
                ),
                (
                    mouse_type,
                    view.addEventCallbackPivy(mouse_type, self._on_mouse),
                ),
            ]
            self._callback_view = view
            self._view = view
            self._create_overlay()
            _debug("Linear Pattern preview callbacks installed")
        except Exception:
            self._callbacks = []
            self._callback_view = None
            _debug(
                "could not install Linear Pattern preview callbacks:\n%s"
                % traceback.format_exc()
            )

    def _remove_callbacks(self):
        view = self._callback_view
        callbacks = list(self._callbacks)
        self._callbacks = []
        self._callback_view = None
        if view is None:
            return
        for event_type, callback_id in callbacks:
            try:
                view.removeEventCallbackPivy(event_type, callback_id)
            except Exception:
                pass

    def _event_position(self, event):
        try:
            position = event.getPosition()
            if hasattr(position, "getValue"):
                position = position.getValue()
            return int(position[0]), int(position[1])
        except Exception:
            return None

    def _nearest_occurrence(self, position, data):
        nearest_index = None
        nearest_distance = self.HANDLE_RADIUS_PIXELS
        for index, point in enumerate(data["points"]):
            if index == 0:
                continue
            screen = self._screen_position(point)
            if screen is None:
                continue
            distance = math.sqrt(
                (float(screen[0]) - float(position[0])) ** 2
                + (float(screen[1]) - float(position[1])) ** 2
            )
            if distance <= nearest_distance:
                nearest_index = index
                nearest_distance = distance
        return nearest_index

    def _point_from_record(self, record):
        if not isinstance(record, dict):
            return None
        values = {}
        lowered = {
            _text(key).lower(): value for key, value in record.items()
        }
        for axis in ("x", "y", "z"):
            if axis not in lowered:
                return None
            try:
                values[axis] = float(lowered[axis])
            except Exception:
                return None
        return App.Vector(values["x"], values["y"], values["z"])

    def _record_matches_pattern(self, record):
        if not isinstance(record, dict) or self._pattern is None:
            return False
        value = record.get("Object")
        if hasattr(value, "Name"):
            return _same_object(value, self._pattern)
        return _text(value) in (
            _text(getattr(self._pattern, "Name", "")),
            _text(getattr(self._pattern, "Label", "")),
        )

    def _hit_scalar(self, position, data):
        """Use a picked preview point when FreeCAD exposes one."""

        if self._view is None:
            return None
        try:
            records = self._view.getObjectsInfo(position)
        except Exception:
            records = None
        if isinstance(records, dict):
            records = [records]
        try:
            records = list(records or [])
        except Exception:
            records = []
        for record in records:
            if not self._record_matches_pattern(record):
                continue
            point = self._point_from_record(record)
            if point is not None:
                return (point - data["start"]).dot(data["direction"])
        return None

    def _ray_scalar(self, position, data):
        if self._view is None or coin is None:
            return None
        x, y = int(position[0]), int(position[1])
        projected = None
        for argument in (coin.SbVec2s(x, y), (x, y)):
            try:
                projected = self._view.projectPointToLine(argument)
                if projected is not None:
                    break
            except Exception:
                continue
        if projected is not None:
            try:
                ray_start = _vector_from_value(projected[0])
                ray_end = _vector_from_value(projected[1])
                ray = ray_end - ray_start
                axis = data["direction"]
                w = data["start"] - ray_start
                a = axis.dot(axis)
                b = axis.dot(ray)
                c = ray.dot(ray)
                denominator = a * c - b * b
                if abs(float(denominator)) > 1.0e-12:
                    return (
                        b * w.dot(ray) - c * w.dot(axis)
                    ) / denominator
            except Exception:
                pass

        # Focal-plane projection is a stable fallback for orthographic views
        # and camera/axis configurations where the two lines are parallel.
        for argument in (coin.SbVec2s(x, y), (x, y)):
            try:
                point = _vector_from_value(
                    self._view.getPointOnFocalPlane(argument)
                )
                if point is not None:
                    return (point - data["start"]).dot(data["direction"])
            except Exception:
                continue
        return None

    def _cursor_scalar(self, position, data, include_scene_hit=True):
        scalar = None
        if include_scene_hit:
            scalar = self._hit_scalar(position, data)
        if scalar is None:
            scalar = self._ray_scalar(position, data)
        if scalar is None:
            scalar = self._last_scalar
        return scalar

    def _hit_occurrence(self, position, data):
        """Return the preview occurrence under the mouse, including geometry.

        The orange handles are a convenience overlay, not the native preview
        itself.  A click on a batten/face can therefore miss the handle even
        though FreeCAD's picking API can identify the temporary Linear Pattern
        preview.  Use that picked point as a fallback and map it to the nearest
        occurrence on the one-dimensional pattern.
        """

        index = self._nearest_occurrence(position, data)
        if index is not None:
            return index

        scalar = self._hit_scalar(position, data)
        if scalar is None:
            return None
        try:
            step = float(data["step"])
            occurrences = int(data["occurrences"])
            if abs(step) <= 1.0e-12 or occurrences <= 1:
                return None
            raw_index = float(scalar) / step
            index = int(round(raw_index))
            if index <= 0 or index >= occurrences:
                return None
            # A point on a preview solid may be some distance from its center;
            # reject a hit that is clearly between two occurrences.
            if abs(raw_index - float(index)) > 0.75:
                return None
            return index
        except Exception:
            return None

    # ---- drag state ----------------------------------------------------

    def _snapshot_parameters(self):
        if self._pattern is None:
            return None
        result = {}
        for name in ("Length", "Offset"):
            try:
                result[name] = self._quantity_value(
                    getattr(self._pattern, name)
                )
            except Exception:
                pass
        result["_expressions"] = self._expression_map(self._pattern)
        return result

    def _expression_map(self, pattern):
        expressions = {}
        try:
            for item in list(getattr(pattern, "ExpressionEngine", []) or []):
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    name = _text(item[0])
                    expression = _text(item[1])
                    if name and expression:
                        expressions[name] = expression
        except Exception:
            pass
        return expressions

    def _set_parameter(self, name, value):
        if self._pattern is None:
            return False
        try:
            # A Length/Offset expression otherwise wins again on the next
            # recompute. A preview GrabMove is the same kind of explicit
            # numeric edit as typing into the native quantity field, so
            # replace the expression for this edit. The task's Cancel or
            # preview GrabMove cancel restores the saved expression.
            expression = self._expression_map(self._pattern).get(name)
            if expression:
                clearer = getattr(self._pattern, "clearExpression", None)
                if callable(clearer):
                    clearer(name)
            setattr(self._pattern, name, float(max(0.001, value)))
            return True
        except Exception:
            return False

    def _apply_drag(self, position):
        # The occurrence geometry and direction do not change during one
        # drag.  Reusing the click-time data avoids rebuilding the source
        # shape/direction information for every mouse-location event.
        data = self._drag_data or self._pattern_data()
        index = self._drag_index
        if data is None or index is None or index <= 0:
            return
        # Scene picking is only needed on mouse-down to identify a handle.
        # Calling getObjectsInfo() for every motion event can traverse a large
        # PartDesign preview and was the main UI-freeze risk.
        scalar = self._cursor_scalar(
            position, data, include_scene_hit=False
        )
        if scalar is None:
            return
        self._last_scalar = scalar
        desired_occurrence = float(scalar) - float(self._drag_offset)
        desired_occurrence = max(0.001, desired_occurrence)
        if _text(data["property_name"]).lower() == "offset":
            property_value = desired_occurrence / float(index)
        else:
            property_value = desired_occurrence * (
                float(data["occurrences"] - 1) / float(index)
            )
        if self._set_parameter(data["property_name"], property_value):
            self._last_cursor = position
            self._schedule_recompute()
            self._update_overlay(
                self._pattern_data_at_value(data, property_value),
                redraw=False,
            )

    def _on_location(self, event_callback):
        try:
            event = event_callback.getEvent()
            position = self._event_position(event)
            if position is None:
                return
            self._last_pointer_position = position
            if self._dragging:
                self._apply_drag(position)
        except Exception:
            _debug(
                "Linear Pattern preview drag motion failed:\n%s"
                % traceback.format_exc()
            )

    def _on_mouse(self, event_callback):
        try:
            event = event_callback.getEvent()
            position = self._event_position(event)
            if position is not None:
                self._last_pointer_position = position

            button = _text(event.getButton()).upper()
            state = _text(event.getState()).upper()
            _debug(
                "Linear Pattern preview mouse button=%s state=%s position=%s"
                % (button, state, position)
            )

            if button in ("BUTTON3", "RIGHT", "RIGHTBUTTON", "3"):
                if self._dragging:
                    self._cancel_drag(restore=True)
                    _debug("Linear Pattern preview drag cancelled")
                return

            if button not in ("BUTTON1", "LEFT", "LEFTBUTTON", "1"):
                return

            # Coin exposes SoButtonEvent.DOWN/UP as the strings "1"/"0"
            # through the Python binding on this FreeCAD build.
            if state in ("0", "UP", "RELEASE", "RELEASED"):
                if self._dragging:
                    self._dragging = False
                    self._drag_index = None
                    self._drag_data = None
                    self._drag_offset = 0.0
                    self._drag_snapshot = None
                    self._update_overlay(self._pattern_data())
                    _debug("Linear Pattern preview drag finished")
                return

            if state not in ("", "1", "DOWN", "PRESS") or position is None:
                return

            if self._dragging:
                self._apply_drag(position)
                return

            data = self._pattern_data()
            index = self._hit_occurrence(position, data) if data else None
            if data is None or index is None or index <= 0:
                return

            scalar = self._cursor_scalar(position, data, include_scene_hit=True)
            if scalar is None:
                return

            occurrence_scalar = (
                data["points"][index] - data["start"]
            ).dot(data["direction"])
            self._dragging = True
            self._drag_index = index
            self._drag_data = data
            # Preserve the point under the cursor as the preview moves.
            self._drag_offset = float(scalar) - float(occurrence_scalar)
            self._drag_snapshot = self._snapshot_parameters()
            self._last_cursor = position
            self._last_scalar = scalar
            _debug(
                "Linear Pattern preview drag started occurrence=%d" % index
            )
            self._apply_drag(position)
        except Exception:
            _debug(
                "Linear Pattern preview mouse handling failed:\n%s"
                % traceback.format_exc()
            )

    def _cancel_drag(self, restore=False):
        snapshot = self._drag_snapshot if restore else None
        self._dragging = False
        self._drag_index = None
        self._drag_data = None
        self._drag_offset = 0.0
        self._drag_snapshot = None
        if restore and snapshot and self._pattern is not None:
            for name, value in snapshot.items():
                if name.startswith("_"):
                    continue
                try:
                    setattr(self._pattern, name, float(value))
                except Exception:
                    pass
            for name, expression in snapshot.get("_expressions", {}).items():
                try:
                    setter = getattr(self._pattern, "setExpression", None)
                    if callable(setter):
                        setter(name, expression)
                except Exception:
                    pass
            self._schedule_recompute()
        data = self._pattern_data()
        if data is not None:
            self._update_overlay(data)

    def _checkbox_toggled(self, checked):
        _debug(
            "Linear Pattern preview grab %s"
            % ("enabled" if checked else "disabled")
        )
        if checked:
            self._view = _active_view()
            self._install_callbacks()
            self._update_overlay(self._pattern_data())
            self._status(
                "Preview drag ready: click an orange occurrence and drag "
                "along the pattern direction"
            )
        else:
            session = self._preview_session
            if session is not None and not getattr(session, "done", True):
                session.finish(False)
            self._cancel_drag(restore=False)
            self._remove_callbacks()
            self._remove_overlay()
            self._status("")

    def _schedule_recompute(self):
        self._preview_dirty = True
        try:
            # Do not restart an already-running single-shot timer.  Restarting
            # it for every mouse event means a fast drag can postpone the
            # preview indefinitely and still leaves all work queued at once.
            if not self._recompute_timer.isActive():
                self._recompute_timer.start()
        except Exception:
            self._recompute_preview()

    def _sync_task_ui(self):
        # The native task's Apply method reads its spin boxes when OK is
        # pressed, so refresh them after a drag if the wrapper exposes the
        # native updateUI() method.
        updated = False
        for widget in (self._parameter_widget, self._task_root):
            method = getattr(widget, "updateUI", None)
            if callable(method):
                try:
                    method()
                    updated = True
                    break
                except Exception:
                    pass

        if updated:
            return

        data = self._pattern_data()
        if data is None:
            return

        # Some FreeCAD builds expose the task as a generic QWidget, so its
        # C++ updateUI() method is not visible through PySide.  Keep the
        # first-direction quantity widget synchronized as a fallback; the
        # native Apply method then reads the value we just displayed.
        name = data["property_name"].lower()
        value = data["extent"] if name == "length" else data["step"]
        for widget in self._widget_descendants(self._parameter_widget):
            widget_name = self._widget_name(widget).lower()
            if name not in widget_name or widget_name.endswith("2"):
                continue
            class_name = self._widget_class_name(widget).lower()
            if "spinbox" not in class_name and "spinbox" not in widget_name:
                continue
            try:
                raw_value = widget.property("rawValue")
                if raw_value is not None:
                    if abs(float(raw_value) - float(value)) <= 1.0e-7:
                        return
                    widget.setProperty("rawValue", float(value))
                    return
            except Exception:
                pass
            try:
                setter = getattr(widget, "setValue", None)
                if callable(setter):
                    setter(float(value))
                    return
            except Exception:
                pass

    def _recompute_preview(self):
        pattern = self._pattern
        if pattern is None or not self._is_linear_pattern(pattern):
            return
        if not self._preview_dirty:
            return
        self._preview_dirty = False
        started = time.perf_counter()
        try:
            pattern.recompute()
        except Exception:
            try:
                document = _document_for_object(pattern)
                if document is not None:
                    document.recompute()
            except Exception:
                pass
        try:
            updater = getattr(pattern, "updatePreview", None)
            if callable(updater):
                updater()
        except Exception:
            pass
        self._sync_task_ui()
        self._update_overlay(self._pattern_data(), redraw=False)
        try:
            if self._view is not None:
                self._view.redraw()
        except Exception:
            pass
        elapsed = time.perf_counter() - started
        if elapsed >= 0.25:
            _debug(
                "Linear Pattern preview update took %.3f seconds"
                % elapsed
            )
        if self._dragging and self._preview_dirty:
            try:
                if not self._recompute_timer.isActive():
                    self._recompute_timer.start()
            except Exception:
                pass

    def _status(self, message):
        try:
            status_bar = self.main.statusBar()
            if status_bar is not None:
                status_bar.showMessage(message)
        except Exception:
            pass

    # ---- lifecycle -----------------------------------------------------

    def _detach_task(self):
        session = self._preview_session
        if session is not None and not getattr(session, "done", True):
            session.finish(False)
        self._cancel_drag(restore=False)
        self._preview_dirty = False
        try:
            self._recompute_timer.stop()
        except Exception:
            pass
        self._remove_callbacks()
        self._remove_overlay()
        self._last_pointer_position = None
        checkbox = self._checkbox
        own_checkbox = self._own_checkbox
        self._checkbox = None
        self._own_checkbox = False
        self._task_root = None
        self._parameter_widget = None
        self._pattern = None
        if checkbox is not None and own_checkbox:
            try:
                checkbox.setParent(None)
                checkbox.deleteLater()
            except Exception:
                pass

    def _poll(self):
        if self._closed:
            return
        try:
            pattern = self._edit_object()
            if pattern is None or not self._is_linear_pattern(pattern):
                if self._pattern is not None:
                    self._detach_task()
                return

            same_pattern = (
                self._pattern is not None
                and _same_object(self._pattern, pattern)
            )
            task_widget = self._task_root
            update_box = None
            # The task root is stable for the lifetime of the native edit.
            # Searching the entire Qt widget tree every poll was unnecessary
            # and could become costly while FreeCAD was already rendering a
            # large pattern preview.
            if not same_pattern or task_widget is None or self._checkbox is None:
                dialog = self._active_dialog()
                task_widget, update_box = self._find_pattern_task(dialog)
            if (
                self._pattern is None
                or not same_pattern
                or (
                    task_widget is not None
                    and self._task_root is not None
                    and task_widget is not self._task_root
                )
            ):
                self._detach_task()
                self._pattern = pattern
                self._task_root = task_widget
                self._parameter_widget = task_widget
                self._attach_task_checkbox(task_widget, update_box)
            elif self._checkbox is None and task_widget is not None:
                self._task_root = task_widget
                self._parameter_widget = task_widget
                self._attach_task_checkbox(task_widget, update_box)

            if self._checkbox is None:
                return
            data = self._pattern_data(pattern)
            enabled = data is not None and _active_view() is not None
            self._checkbox.setEnabled(bool(enabled))
            if not enabled:
                if self._checkbox.isChecked():
                    self._checkbox.setChecked(False)
                return
            if self._checkbox.isChecked():
                self._view = _active_view()
                self._install_callbacks()
                self._update_overlay(data)
        except Exception:
            _debug(
                "Linear Pattern preview controller poll failed:\n%s"
                % traceback.format_exc()
            )

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._poll_timer.stop()
            self._poll_timer.deleteLater()
        except Exception:
            pass
        try:
            self._recompute_timer.stop()
            self._recompute_timer.deleteLater()
        except Exception:
            pass
        self._detach_task()


class GrabMoveCommand(object):
    """FreeCAD command wrapper that gives the modal session a G accelerator."""

    def GetResources(self):
        return {
            "MenuText": "Grab Move",
            "ToolTip": (
                "Move selected PartDesign Bodies or ShapeBinders with the mouse; "
                "use B to pick a snap source"
            ),
            "Accel": "G",
            "Pixmap": ":/icons/Std_Transform.svg",
        }

    def IsActive(self):
        return (
            getattr(App, SESSION_ATTRIBUTE, None) is None
            and not _linear_pattern_task_active()
            and bool(_selected_moveable_objects())
        )

    def Activated(self):
        _debug("command Activated() called")
        if getattr(App, SESSION_ATTRIBUTE, None) is not None:
            _debug("command ignored: another session is active")
            return
        if _linear_pattern_task_active():
            _debug("command ignored: Linear Pattern task is active")
            return

        moveable_items = _selected_moveable_items()
        if not moveable_items:
            try:
                selection = _selected_objects()
                _debug(
                    "command ignored: selection=%s"
                    % [
                        (_object_key(item), _text(getattr(item, "TypeId", "")))
                        for item in selection
                    ]
                )
            except Exception:
                _debug("command ignored: selection could not be inspected")
            App.Console.PrintMessage(
                "[GrabMove] Select one or more PartDesign Bodies or "
                "ShapeBinders first.\n"
            )
            return

        moveable_objects = [item[0] for item in moveable_items]
        visual_objects = [item[1] for item in moveable_items]
        session = GrabMoveSession(
            moveable_objects, visual_objects=visual_objects
        )
        setattr(App, SESSION_ATTRIBUTE, session)
        _debug("session stored on App as %s" % SESSION_ATTRIBUTE)
        try:
            session.start()
        except Exception:
            setattr(App, SESSION_ATTRIBUTE, None)
            try:
                session.finish(False)
            except Exception:
                pass
            App.Console.PrintError(
                "[GrabMove] Could not start modal move:\n"
                + traceback.format_exc()
            )


def install_gui():
    """Install the persistent GUI shortcut/filter and optional toolbar."""

    QtCore, QtGui, QtWidgets = _qt_modules()
    main = Gui.getMainWindow()
    if main is None:
        _debug("GUI setup skipped: no main window")
        return False

    _debug("GUI setup started in persistent GrabMove module")

    application_class = getattr(QtWidgets, "QApplication", None)
    application = (
        application_class.instance() if application_class is not None else None
    )
    if application is None:
        application = QtGui.QGuiApplication.instance()

    if application is not None:
        old_filter = getattr(App, "_GrabMoveShortcutFilter", None)
        if old_filter is not None:
            try:
                application.removeEventFilter(old_filter)
            except Exception:
                pass

        class _GrabMoveShortcutFilter(QtCore.QObject):
            """Catch G before FreeCAD's workbench shortcut dispatcher."""

            def __init__(self, app):
                super().__init__()
                self.app = app

            def _is_text_editor_focused(self):
                try:
                    focus = self.app.focusWidget()
                    if focus is None:
                        return False
                    class_name = str(focus.metaObject().className()).lower()
                    return any(
                        name in class_name
                        for name in (
                            "lineedit", "textedit", "plaintextedit", "spinbox"
                        )
                    )
                except Exception:
                    return False

            def _modal_key(self, event):
                """Map a Qt key press to the key names used by GrabMove."""

                try:
                    blocking_modifiers = 0
                    for modifier_name in (
                        "ShiftModifier",
                        "ControlModifier",
                        "AltModifier",
                        "MetaModifier",
                    ):
                        modifier = _qt_enum(
                            QtCore.Qt, modifier_name, "KeyboardModifier"
                        )
                        if modifier is not None:
                            blocking_modifiers |= int(modifier)
                    if int(event.modifiers()) & blocking_modifiers:
                        return None
                except Exception:
                    pass

                try:
                    text = str(event.text() or "")
                except Exception:
                    text = ""
                if len(text) == 1 and text in "0123456789.-+":
                    return text

                for digit in "0123456789":
                    qt_key = _qt_enum(QtCore.Qt, "Key_" + digit, "Key")
                    if qt_key is not None and event.key() == qt_key:
                        return digit

                key_names = (
                    ("Key_Period", "."),
                    ("Key_Decimal", "."),
                    ("Key_Minus", "-"),
                    ("Key_Plus", "+"),
                    ("Key_Backspace", "BACKSPACE"),
                    ("Key_Delete", "DELETE"),
                    ("Key_Return", "RETURN"),
                    ("Key_Enter", "ENTER"),
                    ("Key_Escape", "ESCAPE"),
                    ("Key_X", "X"),
                    ("Key_Y", "Y"),
                    ("Key_Z", "Z"),
                    ("Key_B", "B"),
                )
                for qt_name, key_name in key_names:
                    qt_key = _qt_enum(QtCore.Qt, qt_name, "Key")
                    if qt_key is not None and event.key() == qt_key:
                        return key_name

                # On X11 some physical keypad presses arrive with an
                # unknown Qt key while the native keysym still identifies
                # KP_0..KP_9.  Coin then reports those presses as Key=ANY,
                # which is too late for the addon to recover the digit.
                try:
                    native_virtual_key = int(event.nativeVirtualKey())
                except Exception:
                    native_virtual_key = 0
                keypad_keysyms = {
                    0xFFB0: "0",
                    0xFFB1: "1",
                    0xFFB2: "2",
                    0xFFB3: "3",
                    0xFFB4: "4",
                    0xFFB5: "5",
                    0xFFB6: "6",
                    0xFFB7: "7",
                    0xFFB8: "8",
                    0xFFB9: "9",
                    0xFFAE: ".",
                    0xFFAB: "+",
                    0xFFAD: "-",
                    0xFF8D: "ENTER",
                }
                if native_virtual_key in keypad_keysyms:
                    return keypad_keysyms[native_virtual_key]

                # Some X11/evdev combinations expose the keypad hardware
                # scan code instead of the keysym.  Use this table only when
                # Qt marks the event as a keypad event so normal number-row
                # scan codes cannot be mistaken for distance input.
                try:
                    keypad_modifier = _qt_enum(
                        QtCore.Qt, "KeypadModifier", "KeyboardModifier"
                    )
                    is_keypad = (
                        keypad_modifier is not None
                        and int(event.modifiers()) & int(keypad_modifier)
                    )
                except Exception:
                    is_keypad = False
                if is_keypad:
                    try:
                        scan_code = int(event.nativeScanCode())
                    except Exception:
                        scan_code = 0
                    keypad_scan_codes = {
                        79: "7", 80: "8", 81: "9",
                        83: "4", 84: "5", 85: "6",
                        87: "1", 88: "2", 89: "3",
                        90: "0", 91: ".",
                    }
                    if scan_code in keypad_scan_codes:
                        return keypad_scan_codes[scan_code]
                return None

            def eventFilter(self, _watched, event):
                try:
                    key_press = _qt_enum(QtCore.QEvent, "KeyPress", "Type")
                    if event.type() != key_press:
                        return False

                    session = getattr(App, SESSION_ATTRIBUTE, None)
                    key_g = _qt_enum(QtCore.Qt, "Key_G", "Key")
                    if self._is_text_editor_focused():
                        return False

                    if session is not None:
                        modal_key = self._modal_key(event)
                        if modal_key is not None:
                            _debug(
                                "application modal key captured key=%s"
                                % modal_key
                            )
                            session._handle_qt_key(modal_key)
                            return True

                        try:
                            _debug(
                                "application modal key not mapped qt=%s text=%r "
                                "modifiers=%s native=%s scan=%s"
                                % (
                                    event.key(),
                                    str(event.text() or ""),
                                    event.modifiers(),
                                    event.nativeVirtualKey(),
                                    event.nativeScanCode(),
                                )
                            )
                        except Exception:
                            pass

                        key_g = _qt_enum(QtCore.Qt, "Key_G", "Key")
                        if event.key() == key_g:
                            _debug(
                                "application G ignored: modal session already active"
                            )
                        return False

                    if event.key() != key_g:
                        return False

                    no_modifier = _qt_enum(
                        QtCore.Qt, "NoModifier", "KeyboardModifier"
                    )
                    if no_modifier is not None and event.modifiers() != no_modifier:
                        return False
                    if _linear_pattern_task_active():
                        _debug(
                            "application G ignored: Linear Pattern task is active"
                        )
                        return True
                    if not is_moveable_selection():
                        _debug(
                            "application G received but no moveable selection"
                        )
                        return False

                    _debug("application event filter captured G; running command")
                    result = Gui.runCommand(COMMAND_NAME)
                    _debug(
                        "Gui.runCommand(%s) returned %s"
                        % (COMMAND_NAME, result)
                    )
                    return True
                except Exception:
                    _debug(
                        "application G filter failed:\n%s"
                        % traceback.format_exc()
                    )
                    return False

        shortcut_filter = _GrabMoveShortcutFilter(application)
        application.installEventFilter(shortcut_filter)
        App._GrabMoveShortcutFilter = shortcut_filter
        _debug("application G event filter installed")

        # On the X11/xcb backend, some physical keypad keys are consumed by
        # the Coin viewer before Qt produces a useful QKeyEvent.  Coin then
        # reports them as Key=ANY and FreeCAD interprets the same key as a
        # camera-view shortcut.  A native filter runs one layer earlier and
        # can translate the X11 keysym while the modal move is active.
        old_native_filter = getattr(App, "_GrabMoveNativeEventFilter", None)
        if old_native_filter is not None:
            try:
                application.removeNativeEventFilter(old_native_filter)
            except Exception:
                pass
            try:
                old_native_filter.close()
            except Exception:
                pass

        class _GrabMoveNativeEventFilter(QtCore.QAbstractNativeEventFilter):
            """Capture modal X11 keys before Qt/Coin dispatches them."""

            _MODAL_KEYSYMS = {
                0x0030: "0",
                0x0031: "1",
                0x0032: "2",
                0x0033: "3",
                0x0034: "4",
                0x0035: "5",
                0x0036: "6",
                0x0037: "7",
                0x0038: "8",
                0x0039: "9",
                0x002E: ".",
                0x002B: "+",
                0x002D: "-",
                0x0067: "G",
                0x0078: "X",
                0x0079: "Y",
                0x007A: "Z",
                0x0062: "B",
                0xFF08: "BACKSPACE",  # XK_BackSpace
                0xFFFF: "DELETE",  # XK_Delete
                0xFF0D: "RETURN",  # XK_Return
                0xFF1B: "ESCAPE",  # XK_Escape
                0xFFB0: "0",  # XK_KP_0
                0xFFB1: "1",  # XK_KP_1
                0xFFB2: "2",  # XK_KP_2
                0xFFB3: "3",  # XK_KP_3
                0xFFB4: "4",  # XK_KP_4
                0xFFB5: "5",  # XK_KP_5
                0xFFB6: "6",  # XK_KP_6
                0xFFB7: "7",  # XK_KP_7
                0xFFB8: "8",  # XK_KP_8
                0xFFB9: "9",  # XK_KP_9
                0xFFAE: ".",  # XK_KP_Decimal
                0xFFAB: "+",  # XK_KP_Add
                0xFFAD: "-",  # XK_KP_Subtract
                0xFF8D: "ENTER",  # XK_KP_Enter
            }

            def __init__(self, app, qt_filter):
                super().__init__()
                self.app = app
                self.qt_filter = qt_filter
                self.x11 = None
                self.display = None
                self._open_x11()

            def _open_x11(self):
                try:
                    platform_name = _text(self.app.platformName()).lower()
                except Exception:
                    platform_name = ""
                if "xcb" not in platform_name:
                    _debug(
                        "native keypad filter skipped: Qt platform=%s"
                        % platform_name
                    )
                    return
                try:
                    self.x11 = ctypes.CDLL("libX11.so.6")
                    self.x11.XOpenDisplay.restype = ctypes.c_void_p
                    self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
                    self.x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
                    self.x11.XKeycodeToKeysym.argtypes = [
                        ctypes.c_void_p,
                        ctypes.c_uint,
                        ctypes.c_int,
                    ]
                    self.x11.XKeycodeToKeysym.restype = ctypes.c_ulong
                    self.display = self.x11.XOpenDisplay(None)
                except Exception:
                    self.x11 = None
                    self.display = None
                    _debug(
                        "native keypad filter unavailable:\n%s"
                        % traceback.format_exc()
                    )
                    return
                if not self.display:
                    _debug("native keypad filter unavailable: XOpenDisplay failed")
                    self.x11 = None
                    return
                _debug("native X11 keypad event filter installed")

            def close(self):
                if self.display is not None and self.x11 is not None:
                    try:
                        self.x11.XCloseDisplay(self.display)
                    except Exception:
                        pass
                self.display = None
                self.x11 = None

            def _keysym_name(self, keycode):
                if self.x11 is None or self.display is None:
                    return None
                # Keypad layouts normally use columns 0 and 1 for the
                # navigation and NumLock forms.  Check both so the modal
                # numeric entry remains usable with either NumLock state.
                for column in range(4):
                    try:
                        keysym = int(
                            self.x11.XKeycodeToKeysym(
                                self.display, int(keycode), column
                            )
                        )
                    except Exception:
                        continue
                    key_name = self._MODAL_KEYSYMS.get(keysym)
                    if key_name is not None:
                        return key_name
                return None

            def nativeEventFilter(self, event_type, message):
                try:
                    event_name = _text(event_type).lower()
                    if "xcb" not in event_name:
                        return False, 0

                    message_address = int(message)
                    if not message_address:
                        return False, 0
                    native_event = ctypes.cast(
                        message_address,
                        ctypes.POINTER(_XcbKeyPressEvent),
                    ).contents
                    # XCB event type 2 is KeyPress.  Bit 7 is set for a
                    # synthetic SendEvent wrapper and does not change it.
                    if (int(native_event.response_type) & 0x7F) != 2:
                        return False, 0

                    # Ignore ordinary modifier chords.  NumLock is a
                    # separate X11 modifier and is intentionally allowed.
                    if int(native_event.state) & (1 | 4 | 8 | 64 | 128):
                        return False, 0

                    key_name = self._keysym_name(native_event.detail)
                    if key_name is None:
                        return False, 0

                    session = getattr(App, SESSION_ATTRIBUTE, None)
                    if self.qt_filter._is_text_editor_focused():
                        return False, 0

                    if session is None or getattr(session, "done", True):
                        if key_name == "G" and _linear_pattern_task_active():
                            _debug(
                                "native G ignored: Linear Pattern task is active"
                            )
                            return True, 0
                        if key_name != "G" or not is_moveable_selection():
                            return False, 0
                        _debug("native application key captured key=G")
                        result = Gui.runCommand(COMMAND_NAME)
                        _debug(
                            "Gui.runCommand(%s) returned %s"
                            % (COMMAND_NAME, result)
                        )
                        return True, 0

                    _debug(
                        "native modal key captured key=%s keycode=%s"
                        % (key_name, int(native_event.detail))
                    )
                    session._handle_qt_key(key_name)
                    return True, 0
                except Exception:
                    _debug(
                        "native keypad filter failed:\n%s"
                        % traceback.format_exc()
                    )
                    return False, 0

        native_filter = _GrabMoveNativeEventFilter(application, shortcut_filter)
        try:
            application.installNativeEventFilter(native_filter)
            App._GrabMoveNativeEventFilter = native_filter
        except Exception:
            native_filter.close()
            App._GrabMoveNativeEventFilter = None
            _debug(
                "native keypad filter installation failed:\n%s"
                % traceback.format_exc()
            )
    else:
        _debug("GUI setup warning: no QApplication instance")

    toolbar_class = getattr(QtWidgets, "QToolBar", None)
    action_class = getattr(QtWidgets, "QAction", None)
    if action_class is None:
        action_class = getattr(QtGui, "QAction")
    if toolbar_class is None:
        toolbar_class = getattr(QtGui, "QToolBar")

    toolbar = main.findChild(toolbar_class, "GrabMoveToolbar")
    if toolbar is None:
        toolbar = toolbar_class("Grab Move", main)
        toolbar.setObjectName("GrabMoveToolbar")
        main.addToolBar(toolbar)

    action = None
    for candidate in toolbar.actions():
        if candidate.objectName() == "GrabMove_MoveAction":
            action = candidate
            break
    if action is None:
        action = action_class(main)
        action.setObjectName("GrabMove_MoveAction")
        action.setText("Grab Move")
        action.setToolTip(
            "Grab Move (G): move selected Bodies or ShapeBinders; "
            "B picks a snap source"
        )
        try:
            icon = QtGui.QIcon(":/icons/Std_Transform.svg")
            if not icon.isNull():
                action.setIcon(icon)
        except Exception:
            pass
        action.triggered.connect(lambda: Gui.runCommand(COMMAND_NAME))
        toolbar.addAction(action)

    shortcut_action = None
    for candidate in main.actions():
        if candidate.objectName() == "GrabMove_MoveShortcutAction":
            shortcut_action = candidate
            break
    if shortcut_action is None:
        shortcut_action = action_class(main)
        shortcut_action.setObjectName("GrabMove_MoveShortcutAction")
        shortcut_action.setText("Grab Move (G)")
        shortcut_action.setShortcut(QtGui.QKeySequence("G"))
        shortcut_context = _qt_enum(
            QtCore.Qt, "ApplicationShortcut", "ShortcutContext"
        )
        if shortcut_context is not None:
            shortcut_action.setShortcutContext(shortcut_context)
        shortcut_action.triggered.connect(
            lambda: Gui.runCommand(COMMAND_NAME)
        )
        main.addAction(shortcut_action)
        App.Console.PrintMessage(
            "[GrabMove] Application shortcut G installed.\n"
        )

    old_timer = getattr(App, "_GrabMoveToolbarTimer", None)
    if old_timer is not None:
        try:
            old_timer.stop()
            old_timer.deleteLater()
        except Exception:
            pass

    def refresh_enabled():
        try:
            resolved = _selected_moveable_objects()
            enabled = (
                getattr(App, SESSION_ATTRIBUTE, None) is None
                and not _linear_pattern_task_active()
                and bool(resolved)
            )
            action.setEnabled(bool(enabled))
            shortcut_action.setEnabled(bool(enabled))
            selection = list(Gui.Selection.getSelection())
            selection_state = tuple(
                (
                    getattr(item, "Name", ""),
                    getattr(item, "TypeId", ""),
                )
                for item in selection
            )
            if selection_state != refresh_enabled.last_selection:
                refresh_enabled.last_selection = selection_state
                _debug(
                    "selection=%s resolved=%s moveable=%s shortcut_enabled=%s"
                    % (
                        selection_state,
                        [_object_key(item) for item in resolved],
                        bool(resolved),
                        enabled,
                    )
                )
        except Exception:
            action.setEnabled(False)
            shortcut_action.setEnabled(False)
            _debug("toolbar refresh failed:\n%s" % traceback.format_exc())

    refresh_enabled.last_selection = None
    timer = QtCore.QTimer(main)
    timer.timeout.connect(refresh_enabled)
    timer.start(250)
    App._GrabMoveToolbarTimer = timer
    refresh_enabled()

    old_pattern_controller = getattr(
        App, "_GrabMoveLinearPatternPreviewController", None
    )
    if old_pattern_controller is not None:
        try:
            old_pattern_controller.close()
        except Exception:
            pass
    try:
        App._GrabMoveLinearPatternPreviewController = (
            LinearPatternPreviewGrabController(main)
        )
        _debug("Linear Pattern preview grab controller installed")
    except Exception:
        App._GrabMoveLinearPatternPreviewController = None
        _debug(
            "Linear Pattern preview controller installation failed:\n%s"
            % traceback.format_exc()
        )

    _debug("GUI setup finished")
    return True


def is_moveable_selection():
    """Used by the optional toolbar to update its enabled state."""

    return bool(_selected_moveable_objects())


def install():
    """Register or refresh the command during FreeCAD GUI startup."""

    old_session = getattr(App, SESSION_ATTRIBUTE, None)
    if old_session is not None and not getattr(old_session, "done", True):
        try:
            old_session.finish(False)
        except Exception:
            pass

    command = GrabMoveCommand()
    setattr(App, COMMAND_ATTRIBUTE, command)
    try:
        Gui.addCommand(COMMAND_NAME, command)
    except Exception:
        _debug("Gui.addCommand failed:\n%s" % traceback.format_exc())
        raise
    _debug("Gui.addCommand registered %s with accelerator G" % COMMAND_NAME)
    App.Console.PrintMessage(
        "[GrabMove] Loaded. Select one or more Bodies or ShapeBinders and "
        "press G.\n"
    )
