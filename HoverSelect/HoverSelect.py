"""Blender-style hover selection for Part Design bodies.

The addon listens to the active 3D view so it knows what is under the mouse.
Pressing ``L`` selects the Body under the cursor.  If another Body is already
selected, the new Body is appended to the existing FreeCAD selection.  The
selected Body's document/group path is expanded and the tree scrolls to it.
"""

from __future__ import print_function

import os
import time
import traceback

import FreeCAD as App
import FreeCADGui as Gui


try:
    from PySide import QtCore, QtGui, QtWidgets
except ImportError:  # FreeCAD builds using PySide6
    from PySide6 import QtCore, QtGui, QtWidgets


COMMAND_NAME = "HoverSelect_SelectHoveredBody"
RUNTIME_ATTRIBUTE = "_HoverSelectRuntime"
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "HoverSelect-debug.log")


def _text(value):
    try:
        return str(value)
    except Exception:
        return ""


def _debug(message):
    line = "[%s] [HoverSelect][DEBUG] %s\n" % (
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


def _qt_enum(container, name, nested_name=None):
    value = getattr(container, name, None)
    if value is not None:
        return value
    nested = getattr(container, nested_name, None) if nested_name else None
    return getattr(nested, name, None) if nested is not None else None


def _active_view():
    try:
        active_document = Gui.activeDocument()
        if active_document is not None:
            return active_document.ActiveView
    except Exception:
        pass
    try:
        return Gui.ActiveDocument.ActiveView
    except Exception:
        return None


def _object_key(obj):
    try:
        return (
            _text(getattr(obj.Document, "Name", "")),
            _text(getattr(obj, "Name", "")),
        )
    except Exception:
        return ("", "")


def _same_object(left, right):
    return left is right or (
        left is not None and right is not None
        and _object_key(left) == _object_key(right)
    )


def _is_body(obj):
    return _text(getattr(obj, "TypeId", "")) == "PartDesign::Body"


def _parent_geo_feature_group(obj):
    getter = getattr(obj, "getParentGeoFeatureGroup", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:
        return None


def _body_for_object(obj):
    """Resolve a hit feature, face, or edge to its owning Part Design Body."""

    if obj is None:
        return None
    if _is_body(obj):
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
        if _is_body(current):
            return current

    # A few FreeCAD view providers expose the owning Body through Group but
    # not through getParentGeoFeatureGroup(). Keep this fallback bounded.
    document = getattr(obj, "Document", None)
    for body in getattr(document, "Objects", []) or []:
        if not _is_body(body):
            continue
        pending = [body]
        seen = set()
        while pending:
            candidate = pending.pop()
            candidate_key = _object_key(candidate)
            if candidate_key in seen:
                continue
            seen.add(candidate_key)
            if _same_object(candidate, obj):
                return body
            try:
                pending.extend(list(getattr(candidate, "Group", []) or []))
            except Exception:
                pass
            tip = getattr(candidate, "Tip", None)
            if tip is not None:
                pending.append(tip)
    return None


def _tree_widgets(main_window):
    """Return FreeCAD's tree widgets from the main window."""

    if main_window is None:
        return []
    try:
        widgets = list(main_window.findChildren(QtWidgets.QTreeWidget))
    except Exception:
        return []

    # FreeCAD normally exposes a Gui::TreeWidget here.  Keep a fallback for
    # builds that wrap it with a less specific Qt class name.
    tree_widgets = []
    for widget in widgets:
        try:
            class_name = _text(widget.metaObject().className()).lower()
        except Exception:
            class_name = ""
        try:
            object_name = _text(widget.objectName()).lower()
        except Exception:
            object_name = ""
        if "tree" in class_name or "tree" in object_name:
            tree_widgets.append(widget)
    return tree_widgets or widgets


def _walk_tree_item(item):
    """Yield a QTreeWidgetItem and all of its descendants."""

    if item is None:
        return
    yield item
    try:
        children = [item.child(index) for index in range(item.childCount())]
    except Exception:
        children = []
    for child in children:
        for descendant in _walk_tree_item(child):
            yield descendant


def _tree_items(tree):
    """Yield all currently materialized items in a QTreeWidget."""

    try:
        roots = [
            tree.topLevelItem(index)
            for index in range(tree.topLevelItemCount())
        ]
    except Exception:
        roots = []
    for root in roots:
        for item in _walk_tree_item(root):
            yield item


def _tree_root_item(item):
    current = item
    visited = set()
    while current is not None:
        marker = id(current)
        if marker in visited:
            break
        visited.add(marker)
        try:
            parent = current.parent()
        except Exception:
            parent = None
        if parent is None:
            return current
        current = parent
    return item


def _tree_root_matches_document(item, document):
    if document is None:
        return True
    root = _tree_root_item(item)
    try:
        root_text = _text(root.text(0)).strip()
    except Exception:
        return False
    document_label = _text(getattr(document, "Label", "")).strip()
    document_name = _text(getattr(document, "Name", "")).strip()
    return root_text in (document_label, document_name)


def _tree_item_for_object(tree, obj):
    """Find an object's tree row by FreeCAD's hidden internal-name column."""

    if tree is None or obj is None:
        return None
    object_name = _text(getattr(obj, "Name", "")).strip()
    if not object_name:
        return None
    document = getattr(obj, "Document", None)
    exact = []
    label_matches = []
    object_label = _text(getattr(obj, "Label", "")).strip()
    for item in _tree_items(tree):
        try:
            internal_name = _text(item.text(2)).strip()
        except Exception:
            internal_name = ""
        if internal_name == object_name:
            exact.append(item)
            continue
        if object_label and _text(item.text(0)).strip() == object_label:
            label_matches.append(item)

    for item in exact:
        if _tree_root_matches_document(item, document):
            return item
    if exact:
        return exact[0]

    for item in label_matches:
        if _tree_root_matches_document(item, document):
            return item
    if len(label_matches) == 1:
        return label_matches[0]
    return None


def _tree_document_item(tree, document):
    """Find the top-level document row that owns an object."""

    if tree is None or document is None:
        return None
    labels = {
        _text(getattr(document, "Label", "")).strip(),
        _text(getattr(document, "Name", "")).strip(),
    }
    try:
        count = tree.topLevelItemCount()
    except Exception:
        count = 0
    candidates = []
    for index in range(count):
        try:
            item = tree.topLevelItem(index)
            if _text(item.text(0)).strip() in labels:
                candidates.append(item)
        except Exception:
            pass
    if candidates:
        return candidates[0]
    if count == 1:
        try:
            return tree.topLevelItem(0)
        except Exception:
            pass
    return None


def _group_parent_for_object(obj):
    """Find a likely tree-container parent for a document object."""

    parent = _parent_geo_feature_group(obj)
    if parent is not None and not _same_object(parent, obj):
        return parent

    document = getattr(obj, "Document", None)
    for candidate in getattr(document, "Objects", []) or []:
        if _same_object(candidate, obj):
            continue
        try:
            children = list(getattr(candidate, "Group", []) or [])
        except Exception:
            children = []
        if any(_same_object(child, obj) for child in children):
            return candidate
    return None


def _object_container_chain(obj):
    """Return object containers from the document down to ``obj``."""

    chain = []
    current = obj
    visited = set()
    for _index in range(32):
        key = _object_key(current)
        if key in visited:
            break
        visited.add(key)
        parent = _group_parent_for_object(current)
        if parent is None or _same_object(parent, current):
            break
        chain.append(parent)
        current = parent
    chain.reverse()
    return chain


def _expand_tree_item(item):
    try:
        if not item.isExpanded():
            item.setExpanded(True)
    except Exception:
        pass


def _expand_tree_ancestors(item):
    parent = None
    try:
        parent = item.parent()
    except Exception:
        pass
    while parent is not None:
        _expand_tree_item(parent)
        try:
            parent = parent.parent()
        except Exception:
            parent = None


def _reveal_tree_object(tree, obj):
    """Expand the containing path and return the object's tree row."""

    item = _tree_item_for_object(tree, obj)
    if item is not None:
        _expand_tree_ancestors(item)
        return item

    document = getattr(obj, "Document", None)
    document_item = _tree_document_item(tree, document)
    if document_item is not None:
        _expand_tree_item(document_item)
        item = _tree_item_for_object(tree, obj)
        if item is not None:
            _expand_tree_ancestors(item)
            return item

    # Tree rows inside collapsed groups may not exist until their parent is
    # expanded. Follow the same group/Part chain used by the model tree.
    for container in _object_container_chain(obj):
        container_item = _tree_item_for_object(tree, container)
        if container_item is None:
            break
        _expand_tree_item(container_item)
        item = _tree_item_for_object(tree, obj)
        if item is not None:
            _expand_tree_ancestors(item)
            return item
    return None


def _record_value(record, name):
    for key, value in record.items():
        if _text(key).lower() == name.lower():
            return value
    return None


def _object_from_record(record):
    if not isinstance(record, dict):
        return None

    value = _record_value(record, "Object")
    if hasattr(value, "Name") and hasattr(value, "Document"):
        return value

    object_name = _text(value)
    if not object_name:
        return None

    document = None
    document_name = _text(_record_value(record, "Document"))
    if document_name:
        try:
            document = App.getDocument(document_name)
        except Exception:
            document = None
    if document is None:
        document = App.ActiveDocument
    if document is None:
        return None

    try:
        return document.getObject(object_name)
    except Exception:
        return None


def _records_at(view, position):
    """Return view hit records, with compatibility fallback for older builds."""

    if view is None or position is None:
        return []

    point = (int(position[0]), int(position[1]))
    try:
        records = view.getObjectsInfo(point)
    except Exception:
        records = None

    if records is None:
        records = []
    elif isinstance(records, dict):
        records = [records]
    try:
        records = list(records)
    except Exception:
        records = []
    if records:
        return records

    # Some versions return a record from getObjectInfo() but an empty list
    # from getObjectsInfo(). Try both accepted argument forms.
    for args in ((point,), point):
        try:
            record = view.getObjectInfo(*args)
        except Exception:
            continue
        if record is None:
            continue
        if isinstance(record, dict):
            return [record]
        try:
            return list(record)
        except Exception:
            return []
    return []


def _event_type(event):
    if not isinstance(event, dict):
        return ""
    return _text(event.get("Type", event.get("type", "")))


def _event_position(event):
    if not isinstance(event, dict):
        return None
    position = event.get("Position", event.get("position"))
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


class _ShortcutFilter(QtCore.QObject):
    """Capture plain L before FreeCAD's workbench shortcut dispatcher."""

    def __init__(self, runtime, application):
        super().__init__(runtime.main_window)
        self.runtime = runtime
        self.application = application

    def _text_editor_focused(self):
        try:
            focus = self.application.focusWidget()
            if focus is None:
                return False
            class_name = _text(focus.metaObject().className()).lower()
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
            if event.type() != key_press or self._text_editor_focused():
                return False

            key_l = _qt_enum(QtCore.Qt, "Key_L", "Key")
            if key_l is None or event.key() != key_l:
                return False

            no_modifier = _qt_enum(
                QtCore.Qt, "NoModifier", "KeyboardModifier"
            )
            if no_modifier is not None and event.modifiers() != no_modifier:
                return False

            # Let other FreeCAD commands keep L when there is no Body under
            # the cursor. We consume the event only after selecting a Body.
            return bool(self.runtime.select_hovered_body())
        except Exception:
            _debug("shortcut event filter failed:\n%s" % traceback.format_exc())
            return False


class HoverSelectRuntime(QtCore.QObject):
    """Persistent view listener and hover-selection controller."""

    def __init__(self, main_window):
        super().__init__(main_window)
        self.main_window = main_window
        self.view = None
        self.callback_id = None
        self.last_position = None
        self._tree_reveal_generation = 0
        self._last_reveal_body = None

        self.application = QtWidgets.QApplication.instance()
        if self.application is None:
            self.application = QtGui.QGuiApplication.instance()

        self.shortcut_filter = None
        if self.application is not None:
            self.shortcut_filter = _ShortcutFilter(self, self.application)
            self.application.installEventFilter(self.shortcut_filter)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._refresh_view)
        self.timer.start(250)
        self._refresh_view()
        _debug("runtime installed")

    def _refresh_view(self):
        view = _active_view()
        if view is self.view:
            return

        if self.view is not None and self.callback_id is not None:
            try:
                self.view.removeEventCallback("SoEvent", self.callback_id)
            except Exception:
                pass
        self.view = view
        self.callback_id = None
        self.last_position = None

        if self.view is None:
            _debug("no active 3D view")
            return
        try:
            self.callback_id = self.view.addEventCallback(
                "SoEvent", self._handle_event
            )
            _debug("view event callback installed")
        except Exception:
            _debug("view callback installation failed:\n%s" % traceback.format_exc())

    def _handle_event(self, event):
        if not isinstance(event, dict):
            return
        event_type = _event_type(event)
        if event_type.endswith("Location2Event"):
            position = _event_position(event)
            if position is not None:
                self.last_position = position

    def _queue_tree_reveal(self, preferred_body):
        """Reveal the latest L selection after FreeCAD updates its tree."""

        self._last_reveal_body = preferred_body
        self._tree_reveal_generation += 1
        generation = self._tree_reveal_generation

        def reveal():
            if generation != self._tree_reveal_generation:
                return
            self._reveal_current_selection()

        try:
            # The first pass runs after Gui.Selection has notified the tree.
            # The second pass also covers FreeCAD's normal delayed selection
            # synchronization and any lazy tree rows created by that pass.
            QtCore.QTimer.singleShot(0, reveal)
            QtCore.QTimer.singleShot(150, reveal)
        except Exception:
            reveal()

    def _reveal_current_selection(self):
        try:
            selected = list(Gui.Selection.getSelection())
        except Exception:
            selected = []

        bodies = []
        seen_body_keys = set()
        for selected_obj in selected:
            body = _body_for_object(selected_obj)
            if body is None:
                continue
            key = _object_key(body)
            if key in seen_body_keys:
                continue
            seen_body_keys.add(key)
            bodies.append(body)
        if not bodies:
            return

        preferred = self._last_reveal_body
        if not any(_same_object(preferred, body) for body in bodies):
            preferred = bodies[-1]

        widgets = _tree_widgets(self.main_window)
        if not widgets:
            _debug("tree reveal skipped: no tree widget")
            return

        def visible(widget):
            try:
                return bool(widget.isVisible())
            except Exception:
                return False

        widgets.sort(key=visible, reverse=True)
        found = []
        for body in bodies:
            for tree in widgets:
                item = _reveal_tree_object(tree, body)
                if item is not None:
                    found.append((body, tree, item))
                    break

        if not found:
            _debug(
                "tree reveal could not find selected bodies=%s"
                % ([_object_key(body) for body in bodies],)
            )
            return

        target = None
        for entry in found:
            if _same_object(entry[0], preferred):
                target = entry
                break
        if target is None:
            target = found[-1]
        try:
            target[1].scrollToItem(target[2])
        except Exception:
            _debug("tree reveal scroll failed:\n%s" % traceback.format_exc())
        _debug(
            "tree path revealed bodies=%s focused=%s"
            % (
                [_object_key(body) for body, _tree, _item in found],
                _object_key(target[0]),
            )
        )

    def select_hovered_body(self):
        self._refresh_view()
        if self.view is None or self.last_position is None:
            _debug("L ignored: no active view position")
            return False

        position = self.last_position
        body = None
        records = _records_at(self.view, position)
        for record in records:
            body = _body_for_object(_object_from_record(record))
            if body is not None:
                break

        if body is None:
            _debug(
                "L missed: no PartDesign Body at position=%s records=%d"
                % (position, len(records))
            )
            return False

        try:
            selected = list(Gui.Selection.getSelection())
        except Exception:
            selected = []

        # Keep the FreeCAD selection itself Body-only. A normal click on Body
        # geometry can leave a Tip/feature selected; if we retained that
        # feature, Shift+H would receive a mixed Body+feature selection and
        # could show more than the intended Body result. Convert any existing
        # Body-owned hits to their Body objects before appending the target.
        selected_bodies = []
        seen_body_keys = set()
        for selected_obj in selected:
            selected_body = _body_for_object(selected_obj)
            if selected_body is None:
                continue
            body_key = _object_key(selected_body)
            if body_key in seen_body_keys:
                continue
            seen_body_keys.add(body_key)
            selected_bodies.append(selected_body)

        selection_is_body_only = (
            len(selected) == len(selected_bodies)
            and all(_is_body(obj) for obj in selected)
        )
        if selected and not selection_is_body_only:
            try:
                Gui.Selection.clearSelection()
                for selected_body in selected_bodies:
                    Gui.Selection.addSelection(selected_body)
            except Exception:
                pass

        if not any(_same_object(body, item) for item in selected_bodies):
            try:
                Gui.Selection.addSelection(body)
            except Exception:
                _debug(
                    "could not select Body=%s:\n%s"
                    % (_object_key(body), traceback.format_exc())
                )
                return False
            _debug(
                "selected Body=%s append=%s position=%s"
                % (_object_key(body), bool(selected_bodies), position)
            )
            App.Console.PrintMessage(
                "[HoverSelect] Selected Body: %s\n" % _text(body.Label)
            )
        else:
            _debug("Body already selected=%s" % (_object_key(body),))
        self._queue_tree_reveal(body)
        return True

    def close(self):
        self._tree_reveal_generation += 1
        if self.timer is not None:
            try:
                self.timer.stop()
            except Exception:
                pass
        if self.application is not None and self.shortcut_filter is not None:
            try:
                self.application.removeEventFilter(self.shortcut_filter)
            except Exception:
                pass
        if self.view is not None and self.callback_id is not None:
            try:
                self.view.removeEventCallback("SoEvent", self.callback_id)
            except Exception:
                pass
        self.callback_id = None
        self.view = None


class _Command:
    def GetResources(self):
        return {
            "MenuText": "Select hovered Body",
            "ToolTip": "Select the Part Design Body under the mouse (L)",
            "Accel": "L",
        }

    def IsActive(self):
        return getattr(App, RUNTIME_ATTRIBUTE, None) is not None

    def Activated(self):
        runtime = getattr(App, RUNTIME_ATTRIBUTE, None)
        if runtime is not None:
            runtime.select_hovered_body()


def install():
    """Register the command and enable hover selection by default."""

    old_runtime = getattr(App, RUNTIME_ATTRIBUTE, None)
    if old_runtime is not None:
        try:
            old_runtime.close()
        except Exception:
            _debug("old runtime cleanup failed:\n%s" % traceback.format_exc())

    try:
        Gui.addCommand(COMMAND_NAME, _Command())
    except Exception:
        # FreeCAD may already have the command after an InitGui reload.
        pass

    main_window = Gui.getMainWindow()
    if main_window is None:
        _debug("GUI setup skipped: no main window")
        return

    runtime = HoverSelectRuntime(main_window)
    setattr(App, RUNTIME_ATTRIBUTE, runtime)
    App.Console.PrintMessage(
        "[HoverSelect] Loaded. Hover a Part Design Body and press L; "
        "repeat L to append another Body.\n"
    )
