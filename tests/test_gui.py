"""Real Qt + spawned FlowKit worker workflow; no mocked scientific results."""
import csv
import time
from pathlib import Path
import pytest
from PySide6 import QtCore, QtWidgets, QtTest
from flowhero.gui import Window
from flowhero.storage import digest
from test_analysis import polygons, source_file


@pytest.fixture(scope='module')
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app):
    w = Window()
    w.show()
    app.processEvents()
    yield w
    w.dirty = False
    w.close()
    app.processEvents()


def wait(app, w):
    until = time.monotonic() + 20
    while (w.jobs.busy or w._after_job) and time.monotonic() < until:
        app.processEvents()
        w.poll()
        time.sleep(.02)
    assert not w.jobs.busy and not w._after_job, 'Worker exceeded bounded test deadline'
    app.processEvents()


def draw(app, w, index, points):
    w.begin_gate(index)
    assert w._drawing == index
    plot = w.plots[index]
    plot.setXRange(-2, 22, padding=0)
    plot.setYRange(-2, 12, padding=0)
    app.processEvents()
    for x, y in points:
        scene = plot.getPlotItem().vb.mapViewToScene(QtCore.QPointF(x, y))
        position = plot.mapFromScene(scene)
        QtTest.QTest.mouseClick(plot.viewport(), QtCore.Qt.MouseButton.LeftButton,
                               QtCore.Qt.KeyboardModifier.NoModifier, position)
    assert len(w._points) == len(points)
    w.finish_gate()
    wait(app, w)


def test_desktop_draw_batch_save_reopen_csv(app, window, tmp_path, monkeypatch):
    source = source_file(tmp_path, [[2, 2, 2, 1], [3, 3, 3, 4], [4, 4, 4, 7],
                                    [5, 5, 5, 10000], [7, 7, 7, 500], [20, 2, 2, -10000]])
    before = digest(source.path)
    window.add_paths([source.path])
    wait(app, window)
    assert window.project.channel == 'FL1-A'
    assert window._preview_key is not None
    draw(app, window, 0, [(0, 0), (10, 0), (10, 10), (0, 10)])
    draw(app, window, 1, [(0, 0), (6, 0), (6, 6), (0, 6)])
    rows = window.all_rows()
    assert [(r['event_count'], r['value']) for r in rows] == [(5, 7), (4, 5.5)]
    window.analyse_all()
    wait(app, window)
    assert window.all_rows() == rows
    project_path = tmp_path / 'project.flowhero.json'
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', lambda *a, **kw: (str(project_path), ''))
    assert window.save()
    # Exercise public open action and recompute from original bytes.
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileName', lambda *a, **kw: (str(project_path), ''))
    window.open_project()
    wait(app, window)
    assert window.all_rows() == rows
    csv_path = tmp_path / 'statistics.csv'
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', lambda *a, **kw: (str(csv_path), ''))
    window.export()
    with csv_path.open(newline='') as f:
        exported = list(csv.DictReader(f))
    assert [r['event_count'] for r in exported] == ['5', '4']
    assert [r['value'] for r in exported] == ['7.0', '5.5']
    assert digest(source.path) == before
    # A translated ROI must persist view coordinates, not ROI-local handles.
    original_vertices = window.project.gates[1].vertices
    window._rois[1].setPos(1, 0)
    wait(app, window)
    translated = window.project.gates[1].vertices
    assert translated == tuple((x+1, y) for x, y in original_vertices)
    Path('results').mkdir(exist_ok=True)
    assert window.grab().save('results/gui-smoke.png')


def test_cancel_stale_preview_and_failed_source_clear_rows(app, window, tmp_path):
    source = source_file(tmp_path, [[2, 2, 2, 10], [4, 4, 4, 20]])
    window.add_paths([source.path])
    wait(app, window)
    draw(app, window, 0, [(0, 0), (10, 0), (10, 10), (0, 10)])
    assert window.all_rows()
    window.preview()
    # Drawing cannot cancel the current preview and use old source/revision dots.
    window.begin_gate(0)
    assert window._drawing is None
    assert len(window.scatters[0].getData()[0]) == 0
    window.cancel()
    assert not window.jobs.busy
    window.preview()
    wait(app, window)
    assert window.all_rows()
    Path(source.path).write_bytes(b'changed bytes')
    window.preview()
    wait(app, window)
    assert window.all_rows() == []
    assert 'Source content changed' in window.errors.toPlainText()


def test_close_cancels_and_reaps_running_worker(app, window, tmp_path):
    source = source_file(tmp_path, [[2, 2, 2, 10]])
    window.add_paths([source.path])
    wait(app, window)
    window.preview()
    process = window.jobs._process
    pid = process.pid
    window.dirty = False
    assert window.close()
    assert not window.jobs.busy
    import multiprocessing
    assert pid not in [p.pid for p in multiprocessing.active_children()]


def test_relink_preserves_identity_and_requires_matching_bytes(app, window, tmp_path, monkeypatch):
    source = source_file(tmp_path, [[2, 2, 2, 10]])
    window.add_paths([source.path])
    wait(app, window)
    identity = window.project.sources[0].id
    relocated = tmp_path / 'moved.fcs'
    relocated.write_bytes(Path(source.path).read_bytes())
    Path(source.path).unlink()
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileName', lambda *a, **kw: (str(relocated), ''))
    window.relink_selected()
    wait(app, window)
    assert window.project.sources[0].id == identity
    assert window.project.sources[0].path == str(relocated)
    assert window._preview_key is not None
    # A different but valid FCS must not silently replace the original evidence.
    replacement = source_file(tmp_path, [[3, 3, 3, 20]])
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileName', lambda *a, **kw: (replacement.path, ''))
    window.relink_selected()
    wait(app, window)
    assert window.project.sources[0].path == str(relocated)
    assert 'different bytes' in window.errors.toPlainText()


def test_statistic_selection_recomputes_persists_and_exports(app, window, tmp_path, monkeypatch):
    source = source_file(tmp_path, [[2, 2, 2, 1], [3, 3, 3, 9], [4, 4, 4, 2]])
    before = digest(source.path)
    window.add_paths([source.path])
    wait(app, window)
    window.project.gates = polygons()
    window.invalidate()
    window.preview()
    wait(app, window)
    assert window.all_rows()[1]['value'] == 2
    # Change the setting while a production worker has a median job in flight.
    window.analyse_all()
    previous_revision = window.project.revision
    window.statistic.setCurrentIndex(window.statistic.findData('arithmetic_mean'))
    assert window.project.revision > previous_revision
    assert window.all_rows() == []
    wait(app, window)
    assert all(r['statistic'] == 'arithmetic_mean' and r['value'] == 4
               for r in window.all_rows())
    window.statistic.setCurrentIndex(window.statistic.findData('geometric_mean'))
    wait(app, window)
    window.analyse_all()
    wait(app, window)
    rows = window.all_rows()
    assert all(r['statistic'] == 'geometric_mean' and r['value'] == pytest.approx(18 ** (1/3))
               for r in rows)
    assert window.table.item(0, 4).text() == 'Geometric mean'
    project_path = tmp_path / 'means.flowhero.json'
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', lambda *a, **kw: (str(project_path), ''))
    assert window.save()
    window.statistic.setCurrentIndex(0)
    wait(app, window)
    window.dirty = False
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileName', lambda *a, **kw: (str(project_path), ''))
    window.open_project()
    wait(app, window)
    assert window.statistic.currentData() == 'geometric_mean'
    assert window.all_rows() == rows
    csv_path = tmp_path / 'means.csv'
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', lambda *a, **kw: (str(csv_path), ''))
    window.export()
    with csv_path.open(newline='') as stream:
        exported = list(csv.DictReader(stream))
    assert all(r['statistic'] == 'geometric_mean' for r in exported)
    assert float(exported[1]['value']) == pytest.approx(18 ** (1/3))
    assert digest(source.path) == before


def test_viability_sample_adjustments_overlay_save_reopen_and_csv(app, window, tmp_path, monkeypatch):
    from dataclasses import replace
    first = tmp_path / 'one'; first.mkdir()
    second = tmp_path / 'two'; second.mkdir()
    values = [1, 4, 7, 10000, 500, -10000]
    axes = [(2, 2, 2), (3, 3, 3), (4, 4, 4), (5, 5, 5), (7, 7, 7), (20, 2, 2)]
    one = source_file(first, [[*a, v] for a, v in zip(axes, values)])
    two = source_file(second, [[*a, v*2] for a, v in zip(axes, values)])
    window.add_paths([one.path, two.path])
    wait(app, window)
    ids = [s.id for s in window.project.sources]
    window.project.gates = polygons()
    window.invalidate(); window.preview(); wait(app, window)
    window.viability_channel.setCurrentText('FL1-A')
    window.viability_min.setText('0'); window.viability_max.setText('8')
    window.apply_viability(); wait(app, window)
    assert [r['event_count'] for r in window.all_rows()] == [5, 4, 3]
    assert window.all_rows()[-1]['value'] == 4
    window.edit_scope.setCurrentIndex(1)
    draw(app, window, 1, [(0, 0), (3.5, 0), (3.5, 3.5), (0, 3.5)])
    assert window.all_rows()[1]['event_count'] == 2
    assert window.all_rows()[1]['gate_scope'] == 'sample'
    assert window.project.gates[1] == polygons()[1]
    window.samples.setCurrentRow(1); wait(app, window)
    assert window.all_rows()[-1]['event_count'] == 1
    window.viability_max.setText('15')
    window.apply_viability(); wait(app, window)
    assert window.all_rows()[-1]['event_count'] == 3
    assert window.all_rows()[-1]['value'] == 8
    assert window.project.viability.maximum == 8
    window.histogram_gate.setCurrentIndex(2)
    window.histogram_min.setText('0'); window.histogram_max.setText('20')
    window.histogram_bins.setValue(4)
    window.histogram_normalized.setChecked(True)
    window.compare_samples(); wait(app, window)
    expected = dict(window.histograms)
    assert expected[ids[0]]['counts'] == [2, 0, 0, 0]
    assert expected[ids[1]]['counts'] == [1, 1, 1, 0]
    curves = window.histogram_plot.listDataItems()
    assert len(curves) == 2
    assert list(curves[0].yData) == [100, 0, 0, 0]
    assert curves[1].yData[0] == pytest.approx(100 / 3)
    assert '2/2 overlays complete' in window.histogram_info.text()
    settings = window.project.histogram
    overrides = dict(window.project.overrides)
    path = tmp_path / 'complete.flowhero.json'
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', lambda *a, **kw: (str(path), ''))
    assert window.save()
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileName', lambda *a, **kw: (str(path), ''))
    window.open_project(); wait(app, window)
    assert window.project.histogram == settings and window.project.overrides == overrides
    window.compare_samples(); wait(app, window)
    assert window.histograms == expected
    output = tmp_path / 'complete.csv'
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', lambda *a, **kw: (str(output), ''))
    window.export()
    with output.open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 6
    assert [r['gate_scope'] for r in rows] == ['shared', 'sample', 'shared', 'shared', 'shared', 'sample']
    assert [rows[i]['value'] for i in [2, 5]] == ['2.5', '8.0']
    tabs = window.findChild(QtWidgets.QTabWidget)
    tabs.setCurrentIndex(1); app.processEvents()
    assert window.grab().save('results/gui-overlays.png')
    # Invalid limits leave current science and results untouched.
    window.viability_min.setText('20'); window.viability_max.setText('0')
    revision = window.project.revision
    window.apply_viability()
    assert window.project.revision == revision and window.project.viability.maximum == 8
    # Removing the shared optional gate also removes its per-sample adjustments/settings.
    window.remove_viability(); wait(app, window)
    assert window.project.viability is None and window.project.histogram is None
    assert ids[1] not in window.project.overrides
    assert window.project.overrides[ids[0]][0].id == 'singlets-id'
    assert window.histograms == {}
    window.reset_sample_gates(); wait(app, window)
    assert window.project.overrides == {}
    assert window.all_rows()[1]['event_count'] == 4


def test_changed_source_removes_overlay_without_fabricating_zero_counts(app, window, tmp_path):
    source = source_file(tmp_path, [[2, 2, 2, 1], [3, 3, 3, 2]])
    window.add_paths([source.path]); wait(app, window)
    window.project.gates = polygons()
    window.invalidate(); window.preview(); wait(app, window)
    window.histogram_gate.setCurrentIndex(1)
    window.histogram_max.setText('10')
    window.histogram_bins.setValue(2)
    window.compare_samples(); wait(app, window)
    identity = window.project.sources[0].id
    assert window.histograms[identity]['counts'] == [2, 0]
    Path(source.path).write_bytes(b'changed original')
    window.preview()
    assert identity not in window.histograms
    wait(app, window)
    assert window.histograms == {} and window.all_rows() == []
    assert window.histogram_plot.listDataItems() == []
    assert 'Source content changed' in window.errors.toPlainText()


def prepare_legacy_rois(window, tmp_path):
    source = source_file(tmp_path, [[2, 2, 2, 1], [3, 3, 3, 2]])
    window.project.sources = [source]
    window.project.gates = polygons()
    window.samples.addItem('vectors.fcs')
    window.samples.setCurrentRow(0)
    window.cancel()
    window._preview_key = (source.id, window.project.revision)
    window.render_gates()


def test_legacy_replaced_roi_callback_is_ignored_before_reading_geometry(app, window, tmp_path):
    prepare_legacy_rois(window, tmp_path)
    old = window._rois[0]
    window.render_gates()
    revision, errors = window.project.revision, window.errors.toPlainText()
    window.roi_changed(0, old)
    assert window.project.revision == revision
    assert window.errors.toPlainText() == errors
    window.roi_changed(10, old)
    assert window.errors.toPlainText() == errors


def test_legacy_close_retires_current_rois_and_rejects_late_callback(app, window, tmp_path):
    prepare_legacy_rois(window, tmp_path)
    old = window._rois[0]
    revision = window.project.revision
    window.dirty = False
    assert window.close()
    assert window._rois == [None, None]
    assert window._preview_key is None
    window.roi_changed(0, old)
    assert window.project.revision == revision
