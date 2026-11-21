import os
import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


@pytest.fixture(autouse=True, scope='session')
def isolated_settings():
    """Keep recent-project and template files out of the user's real settings."""
    from PySide6 import QtCore
    QtCore.QStandardPaths.setTestModeEnabled(True)
    # Start every run without remembered settings or recovery copies of earlier runs.
    import shutil
    from pathlib import Path
    locations = QtCore.QStandardPaths.StandardLocation
    for folder in (Path(QtCore.QStandardPaths.writableLocation(locations.AppDataLocation)).with_name('FlowHero'),
                   Path(QtCore.QStandardPaths.writableLocation(locations.GenericDataLocation)) / 'FlowHero'):
        (folder / 'settings.json').unlink(missing_ok=True)
        shutil.rmtree(folder / 'autosave', ignore_errors=True)
    yield


@pytest.fixture(scope='module')
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def bench(app):
    from flowhero.workbench import Workbench
    window = Workbench(); window.show(); app.processEvents()
    yield window
    window.dirty = False; window.close(); app.processEvents()
