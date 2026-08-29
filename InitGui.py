"""FreeCAD GUI bootstrap for Grab Move.

The implementation is imported from GrabMove.py because FreeCAD may discard
the temporary InitGui namespace after startup. Delayed callbacks therefore
must not depend on functions or classes defined only in this file.
"""

import traceback
import importlib
import sys

import FreeCAD as App


try:
    # FreeCAD can re-run InitGui.py without restarting Python. Reload the
    # implementation in that case so addon updates are not hidden by the
    # import cache while the workbench is being developed.
    if "GrabMove" in sys.modules:
        GrabMove = importlib.reload(sys.modules["GrabMove"])
    else:
        GrabMove = importlib.import_module("GrabMove")

    GrabMove._debug("InitGui.py entered")
    GrabMove.install()
    GrabMove.install_gui()
    GrabMove._debug("GUI installation completed")
except Exception:
    try:
        GrabMove._debug("InitGui.py failed:\n%s" % traceback.format_exc())
    except Exception:
        pass
    App.Console.PrintError(
        "[GrabMove] Initialization failed:\n" + traceback.format_exc()
    )
