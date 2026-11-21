"""Menu actions added in 0.8.0: exports, annotations, thresholds, sample handling."""
import csv
import json
import time
from copy import deepcopy
from pathlib import Path

import flowio
import numpy as np
import pytest
import pyqtgraph as pg
from PySide6 import QtWidgets

from flowhero.export import OPEN_BOUND
from flowhero.model import RangeGate
from flowhero.plot_sheet import DARK
from test_analysis import CHANNELS
from test_sheet_gui import load, wait


def wait_tasks(app, window):
    deadline = time.monotonic() + 35
    while window.task_jobs.busy and time.monotonic() < deadline:
        app.processEvents(); window.poll(); time.sleep(.02)
    assert not window.task_jobs.busy, window.errors.toPlainText()
    app.processEvents()


def second_sample(app, window, tmp_path, name='second.fcs'):
    folder = tmp_path / 'more'; folder.mkdir(exist_ok=True)
    path = folder / name
    events = [[2, 2, 2, 10], [3, 3, 3, 20], [4, 4, 4, 30], [5, 5, 5, 40]]
    with path.open('wb') as stream:
        flowio.create_fcs(stream, np.asarray(events).flatten().tolist(), CHANNELS)
    window.add_paths([str(path)]); wait(app, window)
    assert len(window.project.sources) == 2, window.errors.toPlainText()
    return window.project.sources[1]


def save_to(monkeypatch, path):
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', lambda *a, **k: (str(path), ''))


def test_exports_moved_from_toolbar_to_file_menu(app, bench):
    toolbar = [a.text() for bar in bench.findChildren(QtWidgets.QToolBar) for a in bar.actions()]
    assert not any('Export' in text for text in toolbar)
    menus = {m.title(): [a.text() for a in m.actions()] for m in bench._menus}
    assert menus['Export'][:3] == ['Statistics…', 'Plot images…', 'Methods report…']
    assert 'Sample annotations (CSV)…' in menus['Import'] and 'About FlowHero' in menus['Help']


def test_statistics_export_is_independent_of_open_plots(app, bench, tmp_path, monkeypatch):
    load(app, bench, tmp_path)
    for document in list(bench.documents.values()):
        document.close()
    app.processEvents(); wait(app, bench)
    assert not bench.project.plots
    target = tmp_path / 'statistics.csv'
    save_to(monkeypatch, target)
    bench.export_statistics({'layout': 'long', 'channels': ['FSC-A', 'SSC-H', 'FL1-A'],
                             'statistics': ['median', 'sd', 'p95'], 'ancestor_id': None})
    wait_tasks(app, bench)
    with target.open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert {r['channel'] for r in rows} == {'FSC-A', 'SSC-H', 'FL1-A'}
    assert {r['statistic'] for r in rows} == {'median', 'sd', 'p95'}
    record = json.loads((tmp_path / 'statistics.csv.provenance.json').read_text())
    assert record['samples'][0]['sha256'] == bench.project.sources[0].sha256
    assert 'Exported' in bench.statusBar().currentMessage()


def test_wide_statistics_export_has_one_row_per_sample(app, bench, tmp_path, monkeypatch):
    load(app, bench, tmp_path)
    second_sample(app, bench, tmp_path)
    target = tmp_path / 'wide.csv'
    save_to(monkeypatch, target)
    bench.export_statistics({'layout': 'wide', 'channels': ['FL1-A'], 'statistics': ['median'], 'ancestor_id': None})
    wait_tasks(app, bench)
    with target.open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert [r['sample'] for r in rows] == ['vectors.fcs', 'second.fcs']
    assert float(rows[1]['All events | FL1-A median']) == 25 and rows[1]['All events | events'] == '4'


def test_statistics_dialog_defaults_and_more_options(app, bench, tmp_path):
    from flowhero.actions import StatisticsExportDialog
    load(app, bench, tmp_path)
    dialog = StatisticsExportDialog(bench, bench.default_statistic_channels())
    options = dialog.options()
    assert options['layout'] == 'long' and options['statistics'] == ['median', 'arithmetic_mean', 'geometric_mean']
    assert set(options['channels']) == set(bench.default_statistic_channels()) and options['ancestor_id'] is None
    dialog.statistics['cv'].setChecked(True); dialog.percentiles.setText('5, 95')
    assert dialog.options()['statistics'] == options['statistics']  # hidden until More options is on
    dialog.more.setChecked(True); dialog.wide.setChecked(True)
    assert dialog.options()['statistics'] == options['statistics'] + ['cv', 'p5', 'p95']
    assert dialog.options()['layout'] == 'wide'
    dialog.percentiles.setText('120')
    with pytest.raises(ValueError, match='percentile'):
        dialog.options()
    dialog.deleteLater()


def test_undo_does_not_abort_a_running_portable_save(app, bench, tmp_path):
    load(app, bench, tmp_path)
    candidate = deepcopy(bench.project); candidate.channel = 'FSC-A'
    assert bench.changed(candidate)
    package = tmp_path / 'kept.flowhero.zip'
    bench.start_file_task('create', package)
    bench.undo()
    assert bench._file_task is not None and bench.project.channel == 'FL1-A'
    deadline = time.monotonic() + 30
    while bench._file_task is not None and time.monotonic() < deadline:
        app.processEvents(); bench.poll(); time.sleep(.02)
    assert package.is_file(), bench.errors.toPlainText()
    wait(app, bench)


def test_choosing_overlays_adds_no_undo_step(app, bench, tmp_path):
    load(app, bench, tmp_path)
    second_sample(app, bench, tmp_path)
    bench.request(True); wait(app, bench)
    steps = len(bench.undo_states)
    for row in range(2):
        bench.samples.item(row).setSelected(True)
    bench._selection_timer.stop(); bench.apply_sample_selection(); wait(app, bench)
    histogram = next(p for p in bench.project.plots if p.kind == 'histogram')
    assert len(histogram.sample_ids) == 2 and len(bench.undo_states) == steps


def test_remove_sample_keeps_the_file_and_can_be_undone(app, bench, tmp_path):
    load(app, bench, tmp_path); first = bench.project.sources[0]
    second = second_sample(app, bench, tmp_path)
    candidate = deepcopy(bench.project); candidate.annotations = {second.id: {'Group': 'B'}}
    assert bench.changed(candidate)
    bench.samples.setCurrentRow(1); wait(app, bench)
    bench.remove_sample(); wait(app, bench)
    assert [s.id for s in bench.project.sources] == [first.id] and bench.samples.count() == 1
    assert bench.project.annotations == {} and Path(second.path).is_file()
    bench.undo(); wait(app, bench)
    assert len(bench.project.sources) == 2 and bench.project.annotations == {second.id: {'Group': 'B'}}


def test_annotations_import_shows_in_sample_list_and_export(app, bench, tmp_path, monkeypatch):
    load(app, bench, tmp_path)
    second_sample(app, bench, tmp_path)
    table = tmp_path / 'notes.csv'
    table.write_text('file,Genotype,Dose\nvectors.fcs,WT,10\nsecond.fcs,KO,0\nmissing.fcs,x,y\n')
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileName', lambda *a, **k: (str(table), ''))
    bench.import_annotations(); wait(app, bench)
    assert bench.samples.item(1).text() == 'second.fcs   [KO · 0]'
    assert 'Genotype: KO' in bench.samples.item(1).toolTip()
    assert 'missing.fcs' in bench.errors.toPlainText()
    target = tmp_path / 'annotated.csv'
    save_to(monkeypatch, target)
    bench.export_statistics({'layout': 'wide', 'channels': [], 'statistics': [], 'ancestor_id': None})
    wait_tasks(app, bench)
    with target.open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert [(r['sample'], r['Genotype'], r['Dose']) for r in rows] == [('vectors.fcs', 'WT', '10'), ('second.fcs', 'KO', '0')]


def test_threshold_from_control_becomes_an_ordinary_range_gate(app, bench, tmp_path):
    load(app, bench, tmp_path); source = bench.project.sources[0]
    histogram = next(d for d in bench.documents.values() if d.spec.kind == 'histogram')
    bench.edit_plot(histogram.plot_id, x_channel='FL1-A', population_id=None, x_scale='linear'); wait(app, bench)
    bench.threshold_from_control(histogram.plot_id, {'sample_id': source.id, 'percentile': 50.0})
    wait_tasks(app, bench); wait(app, bench)
    gate = bench.project.gates[-1]
    assert isinstance(gate, RangeGate) and gate.name == 'FL1-A+' and gate.maximum == OPEN_BOUND
    assert gate.minimum == pytest.approx(np.percentile([1, 4, 7, 10000, 500, -10000], 50))
    row = next(r for r in bench.results[source.id]['rows'] if r['gate_id'] == gate.id)
    assert row['event_count'] == 3
    bench.undo(); wait(app, bench)
    assert all(g.id != gate.id for g in bench.project.gates)


def test_threshold_on_log_axis_is_stored_in_log_coordinates(app, bench, tmp_path):
    load(app, bench, tmp_path); source = bench.project.sources[0]
    histogram = next(d for d in bench.documents.values() if d.spec.kind == 'histogram')
    bench.edit_plot(histogram.plot_id, x_channel='FL1-A', population_id=None, x_scale='log'); wait(app, bench)
    bench.threshold_from_control(histogram.plot_id, {'sample_id': source.id, 'percentile': 50.0})
    wait_tasks(app, bench); wait(app, bench)
    gate = bench.project.gates[-1]
    assert gate.scales == ('log',) and gate.maximum == 12
    assert gate.minimum == pytest.approx(np.percentile(np.log10([1, 4, 7, 10000, 500]), 50))
    assert next(r for r in bench.results[source.id]['rows'] if r['gate_id'] == gate.id)['event_count'] == 3


def test_plot_images_use_white_background_and_restore_the_screen(app, bench, tmp_path):
    load(app, bench, tmp_path)
    document = next(d for d in bench.documents.values() if d.spec.kind == 'scatter')
    for suffix, magic in [('png', b'\x89PNG'), ('svg', b'<?xml'), ('pdf', b'%PDF')]:
        target = tmp_path / f'plot.{suffix}'
        assert document.export_image(str(target))
        assert target.read_bytes()[:len(magic)] == magic
    assert document.theme is DARK and document._source is None
    from PySide6 import QtGui
    image = QtGui.QImage(str(tmp_path / 'plot.png'))
    assert image.width() >= 1600 and image.pixelColor(2, 2).name() == '#ffffff'
    with pytest.raises(ValueError):
        document.export_image(str(tmp_path / 'plot.gif'))


def test_batch_images_cover_every_sample_and_never_overwrite(app, bench, tmp_path):
    load(app, bench, tmp_path)
    second_sample(app, bench, tmp_path)
    folder = tmp_path / 'figures'; folder.mkdir()
    options = {'folder': str(folder), 'format': 'png', 'sample_ids': [s.id for s in bench.project.sources]}
    bench.export_images(options)  # second sample has no result yet: calculation runs first
    deadline = time.monotonic() + 40
    while (bench._pending_images is not None or bench.jobs.busy) and time.monotonic() < deadline:
        app.processEvents(); bench.poll(); time.sleep(.02)
    names = sorted(p.name for p in folder.iterdir())
    assert len(names) == 2 * len(bench.documents), bench.errors.toPlainText()
    assert any(n.startswith('vectors__') for n in names) and any(n.startswith('second__') for n in names)
    bench.export_images(options)
    assert len(list(folder.iterdir())) == 4 * len(bench.documents)
    assert all(d.theme is DARK for d in bench.documents.values())


def test_gate_names_and_percentages_are_drawn_and_can_be_hidden(app, bench, tmp_path):
    load(app, bench, tmp_path)
    histogram = next(d for d in bench.documents.values() if d.spec.kind == 'histogram')
    bench.edit_plot(histogram.plot_id, x_channel='FL1-A', population_id=None); wait(app, bench)
    bench.add_gates([RangeGate('pos', 'Positive', None, 'FL1-A', 5., 20000.)]); wait(app, bench)
    def labels():
        return [i.toPlainText() for i in bench.documents[histogram.plot_id].graph.getPlotItem().items if isinstance(i, pg.TextItem)]
    assert labels() == ['Positive  50%']
    bench.set_gate_labels(False)
    assert labels() == []
    bench.set_gate_labels(True)
    assert labels() == ['Positive  50%']


def test_nonpositive_geometric_mean_alone_does_not_flag_a_sample(app, bench, tmp_path):
    load(app, bench, tmp_path); source = bench.project.sources[0]  # holds a negative FL1-A value
    assert any(r['status'] == 'nonpositive' for r in bench.results[source.id]['rows'])
    assert [e[2] for e in bench.quality_entries()] == ['Analyzed']


def test_file_information_and_event_export_run_in_the_background(app, bench, tmp_path, monkeypatch):
    load(app, bench, tmp_path)
    bench.show_file_information(); wait_tasks(app, bench)
    dialog = bench._information_window
    assert 'vectors.fcs' in dialog.windowTitle() and dialog.findChild(QtWidgets.QTabWidget).count() == 3
    dialog.close()
    scatter = next(d for d in bench.documents.values() if d.spec.kind == 'scatter')
    target = tmp_path / 'events.csv'
    save_to(monkeypatch, target)
    bench.export_plot_data(scatter.plot_id); wait_tasks(app, bench)
    with target.open(newline='') as stream:
        table = list(csv.reader(stream))
    assert table[0] == ['sample', scatter.spec.x_channel, scatter.spec.y_channel] and len(table) == 7


def test_recent_projects_and_opening_by_path(app, bench, tmp_path, monkeypatch):
    load(app, bench, tmp_path)
    path = tmp_path / 'recent.flowhero.json'
    save_to(monkeypatch, path)
    assert bench.save()
    assert bench.recent_projects()[0] == str(path)
    bench.fill_recent_menu()
    assert bench.recent_menu.actions()[0].text() == 'recent.flowhero.json'
    bench.open_project(str(path)); wait(app, bench)
    assert bench.project_path == str(path) and len(bench.project.sources) == 1


def test_error_messages_name_the_file_not_its_internal_identity(app, bench, tmp_path):
    load(app, bench, tmp_path); source = bench.project.sources[0]
    with open(source.path, 'ab') as stream:
        stream.write(b'changed')
    bench.request(True); wait(app, bench)
    text = bench.errors.toPlainText()
    assert 'vectors.fcs:' in text and not text.startswith(source.id)


def test_menu_clicks_open_their_dialogs_and_cancelling_changes_nothing(app, bench, tmp_path, monkeypatch):
    from flowhero.actions import StatisticsExportDialog
    load(app, bench, tmp_path)
    opened = []
    monkeypatch.setattr(StatisticsExportDialog, 'exec', lambda self: opened.append('statistics') or QtWidgets.QDialog.DialogCode.Rejected)
    monkeypatch.setattr(QtWidgets.QInputDialog, 'getItem', lambda *a, **k: (opened.append('images') or '', False))
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileName', lambda *a, **k: (opened.append('open') or '', ''))
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', lambda *a, **k: (opened.append('save') or '', ''))
    actions = {a.text(): a for m in bench._menus for a in m.actions()}
    for name in ('Statistics…', 'Plot images…', 'Methods report…', 'Open project…', 'Save project',
                 'Sample annotations (CSV)…', 'Gates as JSON…'):
        actions[name].trigger()
    toolbar = {a.text(): a for bar in bench.findChildren(QtWidgets.QToolBar) for a in bar.actions()}
    toolbar['Open…'].trigger(); toolbar['Save'].trigger()
    assert opened == ['statistics', 'images', 'save', 'open', 'save', 'open', 'save', 'open', 'save']
    assert bench.errors.toPlainText() == '' and not bench.task_jobs.busy
