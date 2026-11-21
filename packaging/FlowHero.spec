from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH).parent
import runpy
runpy.run_path(str(root / "packaging/python3120_compat.py"))["apply"]()
metadata = []
for distribution in ['flowkit', 'flowio', 'flowutils', 'bokeh']:
    metadata += copy_metadata(distribution)
a = Analysis(
    [str(root / 'packaging/entrypoint.py')], pathex=[str(root / 'src')],
    datas=metadata + collect_data_files('flowkit') + [(str(root / 'build/third-party-notices'), 'third-party-notices')],
    hiddenimports=['flowhero.analysis', 'flowhero.jobs', 'flowhero.smoke', 'flowhero.workbench', 'flowhero.plot_sheet', 'flowhero.sheet_smoke',
                   'flowhero.task_jobs', 'flowhero.export', 'flowhero.scales', 'flowhero.qc',
                   'flowhero.wsp_import', 'flowhero.gating_actions'],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'tkinter', 'pytest', 'IPython', 'notebook'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='FlowHero',
          console=sys.platform != 'darwin', target_arch='arm64' if sys.platform == 'darwin' else None)
collection = COLLECT(exe, a.binaries, a.datas, name='FlowHero')
if sys.platform == 'darwin':
    app = BUNDLE(collection, name='FlowHero.app', bundle_identifier='org.flowhero.desktop',
                 info_plist={'CFBundleShortVersionString': '0.10.2',
                             'CFBundleVersion': '0.10.2', 'NSHighResolutionCapable': True})
