"""A developer smoke check for installed/frozen GUI and spawned analysis workers."""
from dataclasses import replace
import json
from pathlib import Path
import time


def run(output):
    from PySide6 import QtWidgets
    import flowio
    from . import __version__
    from .gui import Window
    from .model import Histogram, Polygon, Project, RangeGate
    from .storage import bind_source, export_csv, load_project, save_project

    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    sources = []
    events = [[2, 2, 2, 1], [3, 3, 3, 4], [4, 4, 4, 7], [5, 5, 5, 10000],
              [7, 7, 7, 500], [20, 2, 2, -10000]]
    for multiplier in (1, 2):
        path = root / f'demo-{multiplier}.fcs'
        with path.open('wb') as stream:
            data = [v * multiplier if i == 3 else v for row in events for i, v in enumerate(row)]
            flowio.create_fcs(stream, data, ['FSC-A', 'SSC-A', 'SSC-H', 'FL1-A'])
        sources.append(bind_source(str(path)))
    gates = [Polygon('population', 'U937', None, ('FSC-A', 'SSC-A'),
                     ((0, 0), (10, 0), (10, 10), (0, 10))),
             Polygon('singlets', 'Single cells', 'population', ('SSC-A', 'SSC-H'),
                     ((0, 0), (6, 0), (6, 6), (0, 6)))]
    viability = RangeGate('viable', 'Viable cells', 'singlets', 'FL1-A', 0, 8)
    project = Project(sources, gates, 'FL1-A', viability=viability,
                      histogram=Histogram('viable', 0, 20, bins=4,
                                          sample_ids=tuple(s.id for s in sources)))
    project.set_override(sources[1].id, replace(viability, maximum=15))
    project.validate()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = Window()
    try:
        window.project = project
        window.samples.blockSignals(True)
        for source in sources:
            window.add_sample_item(source)
        window.samples.setCurrentRow(0)
        window.samples.blockSignals(False)
        window.set_channels(['FSC-A', 'SSC-A', 'SSC-H', 'FL1-A'])
        window.show()
        window.analyse_all()
        deadline = time.monotonic() + 90
        while window.jobs.busy and time.monotonic() < deadline:
            app.processEvents()
            window.poll()
            time.sleep(.02)
        if window.jobs.busy:
            raise RuntimeError('Packaged worker exceeded 90-second smoke deadline')
        rows = window.all_rows()
        if len(rows) != 6 or [r['event_count'] for r in rows] != [5, 4, 3, 5, 4, 3]:
            raise RuntimeError(f'Unexpected worker counts: {rows}; {window.errors.toPlainText()}')
        if rows[2]['value'] != 4 or rows[5]['value'] != 8 or rows[5]['gate_scope'] != 'sample':
            raise RuntimeError('Packaged viability/override statistics mismatch')
        actual_histograms = [window.histograms[s.id]['counts'] for s in sources]
        if actual_histograms != [[2, 1, 0, 0], [1, 1, 1, 0]]:
            raise RuntimeError(f'Unexpected histogram counts: {actual_histograms}')
        save_project(project, root/'project.flowhero.json')
        if load_project(root/'project.flowhero.json') != project:
            raise RuntimeError('Packaged project persistence mismatch')
        export_csv(rows, root/'statistics.csv')
        window.findChild(QtWidgets.QTabWidget).setCurrentIndex(1)
        app.processEvents()
        window.grab().save(str(root/'overlays.png'))
        result = {'version': __version__, 'result': 'PASS', 'rows': rows,
                  'histograms': actual_histograms, 'qt_platform': app.platformName(),
                  'checks': ['Qt launch', 'spawned FlowKit batch', 'viability', 'sample override',
                             'full-event histogram overlays', 'save/reopen', 'CSV', 'worker cleanup']}
    finally:
        window.dirty = False
        window.close()
        app.processEvents()
    if window.jobs.busy:
        raise RuntimeError('Packaged worker was not reaped')
    (root/'smoke.json').write_text(json.dumps(result, indent=2, allow_nan=False)+'\n')
    print('PASS FlowHero installed/frozen smoke:', root)
