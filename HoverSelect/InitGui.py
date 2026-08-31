"""FreeCAD GUI bootstrap for HoverSelect."""

import importlib
import sys
import traceback

import FreeCAD as App


try:
    if "HoverSelect" in sys.modules:
        HoverSelect = importlib.reload(sys.modules["HoverSelect"])
    else:
        HoverSelect = importlib.import_module("HoverSelect")
    HoverSelect.install()
except Exception:
    App.Console.PrintError(
        "[HoverSelect] Initialization failed:\n" + traceback.format_exc()
    )
