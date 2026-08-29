"""FreeCAD GUI bootstrap for Grab Move.

The implementation is imported from GrabMove.py because FreeCAD may discard
the temporary InitGui namespace after startup. Delayed callbacks therefore
must not depend on functions or classes defined only in this file.
"""

import traceback

import FreeCAD as App


try:
    from GrabMove import install, install_gui, _debug

    _debug("InitGui.py entered")
    install()
    install_gui()
    _debug("GUI installation completed")
except Exception:
    try:
        _debug("InitGui.py failed:\n%s" % traceback.format_exc())
    except Exception:
        pass
    App.Console.PrintError(
        "[GrabMove] Initialization failed:\n" + traceback.format_exc()
    )

