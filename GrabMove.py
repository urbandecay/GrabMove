"""Blender-style modal grab/move for PartDesign bodies and shape binders.

The command deliberately lives in Python rather than changing FreeCAD's
core.  It uses the public view event and placement APIs:

* ``G`` starts a modal translation.
* ``X``, ``Y`` or ``Z`` constrains the translation to a global axis.
* ``B`` enters snap-source mode.  Click a point on the selected object, then
  hover and click a point on another object to align the two points.
* Left mouse/Enter confirms; right mouse/Escape cancels.

This is intentionally a small, self-contained first implementation.  FreeCAD
does not currently expose Blender's complete transform/snap modal operator to
Python, so the modal state machine is implemented here and Placement remains
the source of truth for the actual move.  When a Body contains ShapeBinders,
the Body moves during the grab and the Binder placements are synchronized with
the Body's final translation immediately before recompute.
"""

from __future__ import print_function

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


def _selected_object():
    """Return the one raw object selected in the tree or 3D view."""

    try:
        selection = list(Gui.Selection.getSelection())
    except Exception:
        return None

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
    return _resolve_moveable_object(_selected_object())


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
    """One running modal move."""

    def __init__(self, obj, visual_obj=None):
        self.obj = obj
        # Keep the selected object separate from the snap roots.  In the
        # normal Body case these are the same object; direct Binder selection
        # can still use its owning Body as a visual snap proxy.
        self.visual_obj = visual_obj if visual_obj is not None else obj
        self.document = _document_for_object(obj)
        self.view = _active_view()
        self.phase = "move"
        self.axis = None
        self.done = False
        self.finish_pending = False
        self.pending_commit = None
        self.cleanup_done = False

        self.original_local = _copy_placement(obj.Placement)
        self.original_global = _global_placement(obj)
        self.preview_global = _copy_placement(self.original_global)

        # A synchronized ShapeBinder can make a Body's visible result return
        # to its old position during recompute.  Let the Body be the live
        # target, then copy its final local translation to every Binder in the
        # Body immediately before the commit recompute.
        self.follow_binder_states = []
        for binder in _binders_for_body(obj):
            try:
                self.follow_binder_states.append(
                    (binder, _copy_placement(binder.Placement))
                )
            except Exception:
                _debug(
                    "could not snapshot body-follow Binder=%s"
                    % (_object_key(binder),)
                )

        self.initial_cursor_world = None
        self.last_screen_position = None

        self.snap_baseline_global = None
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
            "session created for %s type=%s view=%s"
            % (
                _object_key(obj),
                _text(getattr(obj, "TypeId", "")),
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

        try:
            self.callback_id = self.view.addEventCallback(
                "SoEvent", self._handle_event
            )
            _debug("SoEvent callback installed id=%s" % self.callback_id)
        except Exception:
            self._remove_scene_markers()
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
            "Grab Move: move mouse | X/Y/Z constrain | B pick snap source | "
            "LMB/Enter accept | RMB/Esc cancel"
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

        try:
            self.view.setFocus()
            _debug("focus set through ActiveView.setFocus()")
        except Exception:
            _debug("ActiveView.setFocus() unavailable")

    def _install_scene_markers(self):
        # Do not mutate Coin's scene graph while a SoEvent callback is active.
        # Removing a temporary node from the graph during the Escape/confirm
        # event can leave Coin traversing a detached child and crash FreeCAD.
        # The snap state is still shown in the status bar; visual markers can
        # be added later through a deferred, non-modal update.
        self.marker_root = None
        _debug("scene markers disabled during modal event handling")

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
            if not isinstance(record, dict):
                continue
            obj = self._object_from_record(record)
            point = self._point_from_record(record)
            component = _text(record.get("Component", ""))
            if obj is not None and point is not None:
                result.append({
                    "record": record,
                    "object": obj,
                    "point": point,
                    "component": component,
                })

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
            if not isinstance(record, dict):
                continue
            obj = self._object_from_record(record)
            point = self._point_from_record(record)
            component = _text(record.get("Component", ""))
            if obj is not None and point is not None:
                result.append({
                    "record": record,
                    "object": obj,
                    "point": point,
                    "component": component,
                })
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

    def _parent_group(self, obj):
        getter = getattr(obj, "getParentGeoFeatureGroup", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:
            return None

    def _belongs_to_moving_object(self, obj):
        if obj is None:
            return False

        # Snapping should recognize both the actual Placement target and the
        # object the user thinks they grabbed.  The latter is normally the
        # selected Body when a binder is being moved on its behalf.
        roots = [self.obj]
        if not _same_object(self.visual_obj, self.obj):
            roots.append(self.visual_obj)

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
                enriched = dict(hit)
                enriched["snap_screen_position"] = sample
                enriched["snap_screen_distance"] = sample_distance
                enriched["snap_sample_index"] = sample_index
                hits.append(enriched)

                # Off-center face/solid hits describe a broad surface rather
                # than a nearby snap point. Keep them for an explicit B
                # fallback, but only use precise components for magnetic
                # searching away from the exact cursor position.
                if sample_distance > 0.0 and _component_priority(
                    hit.get("component", "")
                ) > 2:
                    continue
                belongs = self._belongs_to_moving_object(hit["object"])
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

    def _apply_global_translation(self, baseline, delta):
        desired = _copy_placement(baseline)
        desired.Base = _copy_vector(baseline.Base) + _copy_vector(delta)
        local = _global_to_local(self.obj, desired)
        try:
            self.obj.Placement = local
        except Exception:
            raise RuntimeError("The selected object has a read-only Placement")

        self.preview_global = desired
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

    def _sync_body_binders(self, final_local):
        """Make internal ShapeBinders follow the Body's committed translation."""

        if not self.follow_binder_states:
            return

        body_delta = (
            _copy_vector(final_local.Base) - _copy_vector(self.original_local.Base)
        )
        _debug(
            "body follow delta=(%.3f, %.3f, %.3f) binders=%s"
            % (
                body_delta.x,
                body_delta.y,
                body_delta.z,
                [_object_key(item[0]) for item in self.follow_binder_states],
            )
        )

        for binder, original in self.follow_binder_states:
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
            return

        delta = world - self.initial_cursor_world
        self._apply_global_translation(
            self.original_global, _constrain(delta, self.axis)
        )

    def _update_source_hover(self, screen_position):
        hit = self._pick_source(screen_position)
        if hit is None:
            self.source_marker.hide()
            self._status(
                "Grab Move: B mode — hover a point on the selected Body/ShapeBinder"
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
        _debug(
            "snap source selected object=%s component=%s point=(%.3f, %.3f, %.3f)"
            % (
                _object_key(hit["object"]),
                hit["component"] or "point",
                self.source_world.x, self.source_world.y, self.source_world.z,
            )
        )
        self._status(
            "Grab Move: source set — hover a target object and click | "
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
            self.snap_baseline_global, _constrain(delta, self.axis)
        )

    def _set_axis(self, key):
        if key not in ("X", "Y", "Z"):
            return
        self.axis = None if self.axis == key else key
        axis_text = self.axis if self.axis is not None else "free"

        if self.phase == "move" and self.last_screen_position is not None:
            self._update_move(self.last_screen_position)
        elif self.phase == "snap_target" and self.last_screen_position is not None:
            self._update_snap_target(self.last_screen_position)
        else:
            self._status("Grab Move: constraint %s" % axis_text)

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
        if key == "B" and self.phase == "move":
            self.phase = "pick_source"
            self.snap_baseline_global = _copy_placement(self.preview_global)
            self.source_marker.hide()
            self.target_marker.hide()
            if self.last_screen_position is not None:
                self._update_source_hover(self.last_screen_position)
            self._status(
                "Grab Move: B mode — hover a point on the selected Body/ShapeBinder "
                "and click"
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

        final_local = _copy_placement(self.obj.Placement)
        _debug(
            "final placement before transaction base=(%.3f, %.3f, %.3f)"
            % (
                final_local.Base.x,
                final_local.Base.y,
                final_local.Base.z,
            )
        )

        try:
            if not commit:
                self.obj.Placement = _copy_placement(self.original_local)
        except Exception:
            pass

        if self.callback_id is not None:
            try:
                self.view.removeEventCallback("SoEvent", self.callback_id)
            except Exception:
                pass
            self.callback_id = None

        self._remove_scene_markers()

        if commit:
            # Apply the Body's final translation to its internal synchronized
            # Binders before recompute, so the PartDesign result does not
            # resolve back to the pre-grab location.
            self._sync_body_binders(final_local)
            # Recompute while the transaction is still open. Some PartDesign
            # documents can refresh a Body during recompute; preserve the
            # final snapped Placement before committing the undo record.
            try:
                self.document.recompute()
            except Exception:
                _debug("document recompute during commit failed:\n%s" % traceback.format_exc())
            try:
                if not _same_translation(self.obj.Placement, final_local):
                    _debug(
                        "recompute changed final placement; restoring snapped placement"
                    )
                    self.obj.Placement = _copy_placement(final_local)
            except Exception:
                _debug(
                    "could not restore final placement after recompute:\n%s"
                    % traceback.format_exc()
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
            finished_local = _copy_placement(self.obj.Placement)
            _debug(
                "final placement after transaction base=(%.3f, %.3f, %.3f)"
                % (
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


class GrabMoveCommand(object):
    """FreeCAD command wrapper that gives the modal session a G accelerator."""

    def GetResources(self):
        return {
            "MenuText": "Grab Move",
            "ToolTip": (
                "Move a PartDesign Body or ShapeBinder with the mouse; "
                "use B to pick a snap source"
            ),
            "Accel": "G",
            "Pixmap": ":/icons/Std_Transform.svg",
        }

    def IsActive(self):
        return getattr(App, SESSION_ATTRIBUTE, None) is None and (
            _selected_moveable_object() is not None
        )

    def Activated(self):
        _debug("command Activated() called")
        if getattr(App, SESSION_ATTRIBUTE, None) is not None:
            _debug("command ignored: another session is active")
            return

        selected_obj = _selected_object()
        obj = _resolve_moveable_object(selected_obj)
        if obj is None:
            try:
                selection = list(Gui.Selection.getSelection())
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
                "[GrabMove] Select one PartDesign Body or ShapeBinder first.\n"
            )
            return

        visual_obj = _body_for_object(selected_obj) or selected_obj
        session = GrabMoveSession(obj, visual_obj=visual_obj)
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

            def eventFilter(self, _watched, event):
                try:
                    key_press = _qt_enum(QtCore.QEvent, "KeyPress", "Type")
                    if event.type() != key_press:
                        return False

                    key_g = _qt_enum(QtCore.Qt, "Key_G", "Key")
                    if event.key() != key_g:
                        return False

                    no_modifier = _qt_enum(
                        QtCore.Qt, "NoModifier", "KeyboardModifier"
                    )
                    if no_modifier is not None and event.modifiers() != no_modifier:
                        return False
                    if self._is_text_editor_focused():
                        return False

                    if getattr(App, SESSION_ATTRIBUTE, None) is not None:
                        _debug("application G ignored: modal session already active")
                        return False
                    if not is_moveable_selection():
                        _debug(
                            "application G received but selection is not moveable"
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
            "Grab Move (G): move a Body or ShapeBinder; B picks a snap source"
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
            resolved = _selected_moveable_object()
            enabled = (
                getattr(App, SESSION_ATTRIBUTE, None) is None
                and resolved is not None
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
                        _object_key(resolved) if resolved is not None else None,
                        resolved is not None,
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
    _debug("GUI setup finished")
    return True


def is_moveable_selection():
    """Used by the optional toolbar to update its enabled state."""

    return _selected_moveable_object() is not None


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
        "[GrabMove] Loaded. Select a Body or ShapeBinder and press G.\n"
    )
