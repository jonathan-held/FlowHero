"""Replaced gate items must not crash the collector or write outdated geometry.

MAC-003 against 0.6.0 reported a reproducible SIGSEGV under ``gc_collect_main``:
a re-render replaces every gate ROI, leaving pyqtgraph ROI/handle/segment cycles.
The observed crash and controls suggest a Qt/Python destruction-order conflict;
the exact native destructor sequence is not proved. Replaced items also kept
writing outdated geometry into the project (report F2).

Phase A drives the ``movePoint(..., finish=True)`` path a finished drag uses on
items a re-render has already replaced. Phase B drives the current item of every
gate kind, so the disposal cannot pass by simply making editing inert.

The scenario runs in a subprocess so that a crash fails this test instead of
killing the pytest process.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROUNDS = 15
KINDS = {'PolyLineROI', 'RectROI', 'LinearRegionItem', 'InfiniteLine'}


def test_replaced_gate_items_are_disposed_without_crash_or_stale_edits(tmp_path):
    tests = Path(__file__).resolve().parent
    environment = {
        **os.environ,
        'PYTHONPATH': os.pathsep.join([str(tests.parent / 'src'), str(tests)]),
        'QT_QPA_PLATFORM': os.environ.get('QT_QPA_PLATFORM', 'offscreen'),
        'OPENBLAS_NUM_THREADS': '1',
        'OMP_NUM_THREADS': '1',
    }
    out_path, err_path = tmp_path / 'scenario-stdout.txt', tmp_path / 'scenario-stderr.txt'
    with out_path.open('w') as out, err_path.open('w') as err:
        finished = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(tmp_path), str(ROUNDS)],
                                  stdout=out, stderr=err, env=environment, timeout=900)
    printed, failures = out_path.read_text(), err_path.read_text()
    # A segfault is reported as a negative return code here instead of killing pytest.
    assert finished.returncode == 0, f'exit {finished.returncode}\n{printed}\n{failures}'
    report = json.loads(printed.strip().splitlines()[-1])
    # Polygon, rectangle, range and quadrant gates all take part.
    assert KINDS.issubset(report['rendered_kinds']), report
    # No replaced item reached the project, and the hazard was really exercised.
    assert report['stale_edits'] == [], report
    assert report['stale_attempts'] >= ROUNDS, report
    assert report['tracked_resources'] > 0, report
    assert report['retired_resource_garbage'] == [], report
    assert report['unrelated_cycle_found'], report
    # Editing the current item of every gate kind still works.
    assert set(report['live_kinds']) == KINDS, report


def nudge(item, delta, index):
    """Drive the real finished-edit signal path for one gate item.

    Returns False when the item cannot be driven, including a replaced item Qt
    has already destroyed or whose handles are gone: it cannot edit either way.
    """
    import pyqtgraph as pg
    from PySide6 import QtCore
    try:
        if isinstance(item, pg.LinearRegionItem):
            low, high = item.getRegion()
            item.setRegion((low + delta, high + delta))
        elif isinstance(item, pg.InfiniteLine):
            item.setValue(item.value() + delta)
            item.sigPositionChangeFinished.emit(item)
        elif getattr(item, 'handles', None):
            handle = item.handles[index % len(item.handles)]['item']
            item.movePoint(handle, handle.scenePos(), finish=False)
            target = item.mapToParent(handle.pos()) + QtCore.QPointF(delta, delta)
            item.movePoint(handle, item.mapToScene(item.mapFromParent(target)), finish=True)
        else:
            return False
    except RuntimeError:
        return False
    return True


def items_of(value):
    return value if isinstance(value, tuple) else (value,)


def scenario(tmp_path, rounds):
    import faulthandler
    import gc
    from PySide6 import QtWidgets
    faulthandler.enable()
    from test_sheet_gui import draw, load, wait
    from flowhero.workbench import Workbench

    app = QtWidgets.QApplication([])
    bench = Workbench()
    bench.show()
    app.processEvents()
    load(app, bench, tmp_path)

    scatter = next(d for d in bench.documents.values() if d.spec.kind == 'scatter')
    draw(app, bench, scatter, 'polygon', [(1, 1), (10, 1), (10, 8), (1, 8)], 'P'); wait(app, bench)
    draw(app, bench, scatter, 'rectangle', [(12, 2), (18, 6)], 'R'); wait(app, bench)
    bench.add_plot('histogram'); wait(app, bench)
    histogram = bench.sheet.activeSubWindow()
    bench.edit_plot(histogram.plot_id, x_channel='FL1-A', x_limits=(0, 12)); wait(app, bench)
    draw(app, bench, histogram, 'range', [(0, 1), (8, 1)], 'V'); wait(app, bench)
    bench.tree.setCurrentItem(bench.tree.topLevelItem(0))
    bench.add_plot('scatter'); wait(app, bench)
    quadrants = bench.sheet.activeSubWindow()
    bench.edit_plot(quadrants.plot_id, x_channel='FSC-A', y_channel='SSC-A'); wait(app, bench)
    draw(app, bench, quadrants, 'quadrants', [(3.5, 3.5)], 'Q'); wait(app, bench)
    bench.tool.setCurrentIndex(bench.tool.findData('navigate'))
    app.processEvents()

    rendered_kinds = {type(item).__name__ for document in bench.documents.values()
                      for value in document.gate_items.values() for item in items_of(value)}

    # Phase A: a snapshot held across re-renders, as a user holding a handle is.
    stale_attempts, stale_edits, live_edits = 0, [], 0
    for index in range(rounds):
        targets = [(document, gate_id, item)
                   for document in bench.documents.values()
                   for gate_id, value in list(document.gate_items.items())
                   for item in items_of(value)]
        for document, gate_id, item in targets:
            current = any(entry is item for entry in items_of(document.gate_items.get(gate_id)))
            before = bench.project.revision
            if not nudge(item, .3 if index % 2 else -.3, index):
                continue
            app.processEvents()
            edited = bench.project.revision != before
            if current:
                live_edits += edited
            else:
                stale_attempts += 1
                if edited:
                    stale_edits.append([index, type(item).__name__, gate_id])
            wait(app, bench)
            gc.collect()

    # Phase B: the item each document currently shows, re-read before every edit.
    live_kinds = {}
    for index in range(3):
        for plot_id in list(bench.documents):
            for gate_id in list(getattr(bench.documents.get(plot_id), 'gate_items', {})):
                document = bench.documents.get(plot_id)
                if document is None:
                    continue
                for item in items_of(document.gate_items.get(gate_id)):
                    if item is None or not any(entry is item for entry in
                                               items_of(document.gate_items.get(gate_id))):
                        continue
                    before = bench.project.revision
                    if not nudge(item, .3 if index % 2 else -.3, index):
                        continue
                    app.processEvents()
                    if bench.project.revision != before:
                        kind = type(item).__name__
                        live_kinds[kind] = live_kinds.get(kind, 0) + 1
                    wait(app, bench)
                    gc.collect()

    # Phase C: scope the collector assertion to this owner's retired resources.
    # Unrelated Python/Qt cycles must not turn a safe disposal into a false fail.
    import weakref
    from PySide6 import QtCore

    def resource_refs(item):
        resources = []
        for owner in [item, *(getattr(item, 'segments', None) or [])]:
            resources.append(owner)
            resources.extend(getattr(owner, name, None) for name in ('menu', 'mouseDragHandler'))
            for info in getattr(owner, 'handles', None) or []:
                handle = info.get('item') if isinstance(info, dict) else None
                if handle is not None:
                    resources.extend((handle, getattr(handle, 'menu', None)))
        # Menus own actions too; record their wrappers without keeping them alive.
        for resource in list(resources):
            if isinstance(resource, QtWidgets.QMenu):
                resources.extend(resource.actions())
        return [weakref.ref(resource) for resource in resources if resource is not None]

    targets, item = [], None
    tracked = [reference for document in bench.documents.values()
               for value in document.gate_items.values() for gate_item in items_of(value)
               for reference in resource_refs(gate_item)]
    tracked_count = len(tracked)
    # Keep automatic GC from consuming retired cycles before we inspect them.
    collection_enabled = gc.isenabled()
    gc.disable()
    for document in bench.documents.values():
        document.discard_plot_items()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()
    # Capture identities before collection clears weakrefs to cyclic objects.
    retired_ids = {id(reference()) for reference in tracked if reference() is not None}
    previous_debug = gc.get_debug()
    gc.set_debug(gc.DEBUG_SAVEALL)
    unrelated = []
    unrelated.append(unrelated)
    unrelated_id = id(unrelated)
    del unrelated
    try:
        gc.collect()
        retired_garbage = sorted(type(obj).__name__ for obj in gc.garbage if id(obj) in retired_ids)
        unrelated_found = any(id(obj) == unrelated_id for obj in gc.garbage)
    finally:
        gc.set_debug(previous_debug)
        if collection_enabled:
            gc.enable()

    print(json.dumps({'rendered_kinds': sorted(rendered_kinds), 'live_edits': live_edits,
                      'stale_attempts': stale_attempts, 'stale_edits': stale_edits,
                      'live_kinds': live_kinds, 'tracked_resources': tracked_count,
                      'retired_resource_garbage': retired_garbage,
                      'unrelated_cycle_found': unrelated_found}), flush=True)
    # Reap the persistent scientific worker; it would otherwise outlive this
    # process, keep the inherited handles open and never be collected.
    try:
        bench.jobs.cancel()
    except (OSError, RuntimeError):
        pass
    os._exit(0)


if __name__ == '__main__':
    scenario(Path(sys.argv[1]), int(sys.argv[2]))
