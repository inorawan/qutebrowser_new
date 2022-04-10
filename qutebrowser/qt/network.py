from qutebrowser.qt import machinery


if machinery.use_pyqt5:
    from PyQt5.QtNetwork import *
elif machinery.use_pyqt6:
    from PyQt6.QtNetwork import *
elif machinery.use_pyside2:
    from PySide2.QtNetwork import *
elif machinery.use_pyside6:
    from PySide6.QtNetwork import *
else:
    raise machinery.UnknownWrapper()
