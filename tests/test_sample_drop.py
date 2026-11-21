"""Samples dragged onto a histogram, fixed sample lists and overlays without a fixed cap (0.10.0)."""
import csv
import json
import time
from dataclasses import replace

import flowio
import numpy as np
import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from flowhero import sample_drag
from flowhero.histograms import histogram_statistics_rows
from flowhero.model import Histogram, PlotSpec, Project, RangeGate
from flowhero.portable import create_package, import_package
from flowhero.storage import load_project, save_project
from flowhero.templates import apply_template, shared_template
from test_scales_compensation import L, sample
from test_sheet_gui import wait

COPY = QtCore.Qt.DropAction.CopyAction
LEFT = QtCore.Qt.MouseButton.LeftButton
NONE = QtCore.Qt.KeyboardModifier.NoModifier


def start(app, window, tmp_path, count, n=6000):
    made = [sample(tmp_path, f's{i:03d}.fcs', seed=i + 1, n=n) for i in range(count)]
    window.add_paths([source.path for source, _ in made]); wait(app, window)
    assert len(window.project.sources) == count, window.errors.toPlainText()
    return window.project.sources, [events for _, events in made]


def histogram(window, index=0):
    return [d for d in window.documents.values() if d.spec.kind == 'histogram'][index]


def send_drop(document, mime, on_plot=True):
    """Deliver the drag events Qt sends; True when the drop was accepted."""
    target = document.graph.viewport() if on_plot else document
    enter = QtGui.QDragEnterEvent(QtCore.QPoint(60, 60), COPY, mime, LEFT, NONE)
    enter.ignore()
    QtWidgets.QApplication.sendEvent(target, enter)
    if not enter.isAccepted():
        return False
    drop = QtGui.QDropEvent(QtCore.QPointF(60, 60), COPY, mime, LEFT, NONE)
    drop.ignore()
    QtWidgets.QApplication.sendEvent(target, drop)
    return drop.isAccepted()


def drag(app, window, rows, document, on_plot=True):
    """What a mouse drag does: press a row (which selects it), drop, release."""
    window.remember_sample_press()
    if not all(window.samples.item(r).isSelected() for r in rows):
        window.samples.clearSelection()
        window.samples.setCurrentRow(rows[0])
        for row in rows:
            window.samples.item(row).setSelected(True)
        app.processEvents(); window._selection_timer.stop(); window.apply_sample_selection(); wait(app, window)
    mime = sample_drag.encode(window.drag_token, window.selected_sample_ids())
    accepted = send_drop(document, mime, on_plot)
    window.finish_sample_drag(); wait(app, window)
    return accepted


def exported(window, plot_id, tmp_path, monkeypatch):
    """Rows of "Export plot data…" as written to the CSV file."""
    target = tmp_path / f'bins-{len(list(tmp_path.glob("bins-*")))}.csv'
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', lambda *a, **k: (str(target), ''))
    window.export_plot(plot_id)
    with target.open(newline='', encoding='utf-8-sig') as stream:
        return list(csv.DictReader(stream))


def curves(document):
    return document.graph.getPlotItem().listDataItems()


def test_drop_adds_to_one_histogram_and_the_list_stays(app, bench, tmp_path):
    sources, events = start(app, bench, tmp_path, 4)
    a, b, c, d = (s.id for s in sources)
    bench.add_plot('histogram'); wait(app, bench)
    first, second = histogram(bench, 0), histogram(bench, 1)
    gate = RangeGate('pos', 'Positive', None, 'FL1-A', 300.0, 5000.0)
    candidate = replace(bench.project, gates=[gate]); bench.changed(candidate, rebuild=True); wait(app, bench)
    bench.edit_plot(first.plot_id, population_id='pos', x_scale='log', normalization='peak'); wait(app, bench)
    first, second = histogram(bench, 0), histogram(bench, 1)
    before = first.spec
    steps = len(bench.undo_states)

    assert drag(app, bench, [1], first)
    spec = first.spec
    assert spec.sample_ids == (a, b) and spec.pinned and second.spec.sample_ids == () and not second.spec.pinned
    assert (spec.population_id, spec.x_scale, spec.bins, spec.normalization) == ('pos', 'log', before.bins, 'peak')
    assert spec.x_limits is not None and bench.project.gates == [gate]
    assert bench.samples.currentRow() == 0 and bench.selected_sample_ids() == (a,)  # selection as before the drag
    assert len(bench.undo_states) == steps + 1
    assert len(curves(first)) == 2 and len(curves(second)) == 1
    assert 'fixed list, 2 samples' in first.windowTitle() and 'Fixed list of 2' in first.summary.text()
    # Each curve is the gated population of its own sample, counted on every event.
    for sid, values in ((a, events[0]), (b, events[1])):
        expected = int(((values[:, 3] >= 300) & (values[:, 3] < 5000)).sum())
        assert bench.results[sid]['sheet_plots'][first.plot_id]['event_count'] == expected

    # Several rows at once, one of them already shown: no duplicate curve.
    assert drag(app, bench, [1, 2], first)
    assert first.spec.sample_ids == (a, b, c) and len(curves(first)) == 3
    assert drag(app, bench, [1], first) and first.spec.sample_ids == (a, b, c)
    # A second histogram is independent; a drop on the note below the plot works too.
    assert drag(app, bench, [3], second, on_plot=False)
    assert second.spec.sample_ids == (a, d) and first.spec.sample_ids == (a, b, c)

    # Selecting other samples afterwards leaves fixed lists alone.
    bench.samples.clearSelection(); bench.samples.setCurrentRow(3)
    bench._selection_timer.stop(); bench.apply_sample_selection(); wait(app, bench)
    assert first.spec.sample_ids == (a, b, c) and len(curves(first)) == 3 and not bench.errors.toPlainText()

    bench.undo(); wait(app, bench)
    assert histogram(bench, 1).spec.sample_ids == () and histogram(bench, 0).spec.sample_ids == (a, b, c)
    bench.redo(); wait(app, bench)
    assert histogram(bench, 1).spec.sample_ids == (a, d) and histogram(bench, 1).spec.pinned


def test_follow_selection_legend_and_refused_drags(app, bench, tmp_path, monkeypatch):
    sources, _ = start(app, bench, tmp_path, 3)
    a, b, c = (s.id for s in sources)
    document = histogram(bench)
    scatter = next(d for d in bench.documents.values() if d.spec.kind == 'scatter')
    assert not send_drop(scatter, sample_drag.encode(bench.drag_token, [b]))              # not a histogram
    assert not send_drop(document, sample_drag.encode('another-window', [b]))            # foreign window
    assert not send_drop(document, sample_drag.encode(bench.drag_token, ['unknown']))     # unknown sample
    assert not send_drop(document, sample_drag.encode(bench.drag_token, [b, b]))          # repeated
    broken = QtCore.QMimeData(); broken.setData(sample_drag.MIME, b'{not json')
    assert not send_drop(document, broken)
    assert document.spec.sample_ids == () and not document.spec.pinned and not bench.errors.toPlainText()
    # Files from the file manager still reach the window as before.
    files = QtCore.QMimeData(); files.setUrls([QtCore.QUrl.fromLocalFile(sources[0].path)])
    enter = QtGui.QDragEnterEvent(QtCore.QPoint(60, 60), COPY, files, LEFT, NONE); enter.ignore()
    QtWidgets.QApplication.sendEvent(document.graph.viewport(), enter)
    assert enter.isAccepted()

    assert drag(app, bench, [1], document) and document.spec.sample_ids == (a, b)
    bench.edit_plot(document.plot_id, legend=False); wait(app, bench)
    assert not document.graph.getPlotItem().legend.isVisible() and len(curves(document)) == 2
    assert len(exported(bench, document.plot_id, tmp_path, monkeypatch)) == 2 * document.spec.bins   # hidden legend hides nothing else
    bench.edit_plot(document.plot_id, legend=True); wait(app, bench)
    assert document.graph.getPlotItem().legend.isVisible()

    bench.samples.clearSelection(); bench.samples.setCurrentRow(2)
    bench.follow_selection(document.plot_id, True); wait(app, bench)
    assert document.spec.sample_ids == () and not document.spec.pinned and 'fixed' not in document.windowTitle()
    bench.samples.selectAll(); bench._selection_timer.stop(); bench.apply_sample_selection(); wait(app, bench)
    assert document.spec.sample_ids == (a, b, c)
    bench.follow_selection(document.plot_id, False); wait(app, bench)   # keep what is shown now
    assert document.spec.pinned and document.spec.sample_ids == (a, b, c)
    bench.change_kind(document.plot_id, 'density'); wait(app, bench)
    assert bench.plot_spec(document.plot_id).sample_ids == () and not bench.plot_spec(document.plot_id).pinned


def test_a_failed_sample_is_named_and_does_not_hide_the_others(app, bench, tmp_path, monkeypatch):
    sources, _ = start(app, bench, tmp_path, 2)
    other = tmp_path / 'other.fcs'
    with other.open('wb') as stream:
        flowio.create_fcs(stream, np.random.default_rng(3).normal(100, 10, 2000).tolist(), ['FSC-A', 'XX-A'])
    bench.add_paths([str(other)]); wait(app, bench)
    bench.errors.clear()
    document = histogram(bench)
    assert document.spec.x_channel == 'FL1-A'
    assert drag(app, bench, [1, 2], document)
    assert len(document.spec.sample_ids) == 3
    assert len(curves(document)) == 2
    text = document.summary.text()
    assert '3: other.fcs: not shown' in text and '1: s000.fcs: 6,000 gated' in text
    rows = exported(bench, document.plot_id, tmp_path, monkeypatch)
    assert {r['sample'] for r in rows} == {'s000.fcs', 's001.fcs'} and all(r['count'] != '' for r in rows)
    # With the failed sample selected the two valid curves stay visible.
    bench.samples.clearSelection(); bench.samples.setCurrentRow(2); wait(app, bench)
    assert len(curves(document)) == 2 and 'other.fcs: not shown' in document.summary.text()


def test_overrides_compensation_and_logicle_per_sample(app, bench, tmp_path):
    sources, events = start(app, bench, tmp_path, 3)
    gate = RangeGate('pos', 'Positive', None, 'FSC-A', 45000.0, 60000.0)
    narrow = replace(gate, minimum=50000.0)
    candidate = replace(bench.project, gates=[gate], overrides={sources[1].id: [narrow]})
    bench.changed(candidate, rebuild=True); wait(app, bench)
    bench.set_compensation({'source': 'file'}); wait(app, bench)
    document = histogram(bench)
    bench.edit_plot(document.plot_id, population_id='pos', x_scale=L); wait(app, bench)
    document = histogram(bench)
    assert drag(app, bench, [1, 2], document), bench.errors.toPlainText()
    assert len(curves(document)) == 3 and bench.project.compensation == {'source': 'file'}
    assert bench.project.overrides == {sources[1].id: [narrow]}
    for source, values, low in zip(sources, events, (45000, 50000, 45000)):
        expected = int(((values[:, 0] >= low) & (values[:, 0] < 60000)).sum())
        assert bench.results[source.id]['sheet_plots'][document.plot_id]['event_count'] == expected
    statistics = histogram_statistics_rows(document.spec, bench.results, sources[0].id)
    assert list(dict.fromkeys(r['sample_id'] for r in statistics)) == [s.id for s in sources]


@pytest.mark.parametrize('count', [9, 25, 100])
def test_many_samples_are_all_drawn_exported_saved_and_reopened(app, bench, tmp_path, count, monkeypatch):
    sources, events = start(app, bench, tmp_path, count, n=400)
    ids = tuple(s.id for s in sources)
    gate = RangeGate('pos', 'Positive', None, 'FL1-A', 200.0, 3000.0)
    bench.changed(replace(bench.project, gates=[gate]), rebuild=True); wait(app, bench)
    document = histogram(bench)
    bench.edit_plot(document.plot_id, population_id='pos'); wait(app, bench)
    document = histogram(bench)
    began = time.monotonic()
    assert drag(app, bench, list(range(1, count)), document), bench.errors.toPlainText()
    elapsed = time.monotonic() - began
    spec = document.spec
    assert spec.sample_ids == ids and spec.pinned and not bench.errors.toPlainText()
    assert len(curves(document)) == count
    labels = [label.text for _, label in document.graph.getPlotItem().legend.items]
    assert labels == [f'{i + 1}: s{i:03d}.fcs' for i in range(count)]
    colours = {curve.opts['pen'].color().name() for curve in curves(document)}
    assert len(colours) == count
    assert f'{count} samples shown' in document.summary.text() and document.summary.toolTip().count('\n') == count - 1
    for source, values in zip(sources, events):
        expected = int(((values[:, 3] >= 200) & (values[:, 3] < 3000)).sum())
        data = bench.results[source.id]['sheet_plots'][document.plot_id]
        assert data['event_count'] == expected
        assert sum(data['counts']) + data['outside'] + data['nonfinite'] + data['nonpositive'] == expected
    rows = exported(bench, document.plot_id, tmp_path, monkeypatch)
    assert [r['sample_id'] for r in rows[::spec.bins]] == list(ids) and len(rows) == count * spec.bins
    statistics = histogram_statistics_rows(spec, bench.results, ids[0])
    assert list(dict.fromkeys(r['sample_id'] for r in statistics)) == list(ids)
    assert all(r['event_count'] == bench.results[r['sample_id']]['sheet_plots'][document.plot_id]['event_count'] for r in statistics)
    image = tmp_path / 'overlay.png'
    assert document.export_image(str(image)) and image.stat().st_size > 0
    document.render()

    path = tmp_path / 'many.flowhero.json'
    save_project(bench.project, path)
    reopened = load_project(path)
    assert reopened == bench.project and json.loads(path.read_text())['version'] == 10
    assert next(p for p in reopened.plots if p.id == spec.id).sample_ids == ids
    assert all(not p.sample_ids and not p.pinned for p in shared_template(reopened).plots)
    print(f'{count} samples dropped and calculated in {elapsed:.1f} s')


def test_fixed_lists_in_files_templates_packages_and_removal(app, bench, tmp_path):
    sources, _ = start(app, bench, tmp_path, 3)
    a, b, c = (s.id for s in sources)
    document = histogram(bench)
    assert drag(app, bench, [1, 2], document)
    project = bench.project
    path = tmp_path / 'fixed.flowhero.json'
    save_project(project, path)
    assert load_project(path) == project
    # Format 9 files have neither setting: every histogram opens automatic, with its legend.
    data = json.loads(path.read_text()); data['version'] = 9
    older = tmp_path / 'older.flowhero.json'
    for plot in data['project']['plots']:
        plot.pop('pinned'); plot.pop('legend')
    older.write_text(json.dumps(data))
    assert all(not p.pinned and p.legend for p in load_project(older).plots)

    template = tmp_path / 'template.flowhero.json'
    save_project(shared_template(project), template)
    applied = apply_template(template, project.sources)
    assert all(not p.sample_ids and not p.pinned for p in applied.plots)

    package = tmp_path / 'fixed.flowhero.zip'
    create_package(project, package)
    restored = import_package(package, tmp_path / 'unpacked')
    restored = restored[0] if isinstance(restored, tuple) else restored
    restored = load_project(restored) if not isinstance(restored, Project) else restored
    fixed = next(p for p in restored.plots if p.id == document.plot_id)
    assert fixed.pinned and len(fixed.sample_ids) == 3

    bench.samples.clearSelection(); bench.samples.setCurrentRow(1); wait(app, bench)
    bench.remove_sample(); wait(app, bench)
    assert histogram(bench).spec.sample_ids == (a, c) and histogram(bench).spec.pinned
    bench.samples.setCurrentRow(0); bench.remove_sample(); wait(app, bench)
    bench.samples.setCurrentRow(0); bench.remove_sample(); wait(app, bench)
    assert not bench.project.sources and not histogram(bench).spec.pinned and not bench.errors.toPlainText()
    bench.undo(); wait(app, bench)
    assert histogram(bench).spec.sample_ids == (c,) and histogram(bench).spec.pinned


def test_model_has_no_fixed_cap_but_refuses_invalid_lists():
    ids = tuple(f's{i}' for i in range(500))
    assert PlotSpec('p', 'histogram', 'FL1-A', sample_ids=ids, pinned=True).sample_ids == ids
    assert Histogram('g', 0, 1, sample_ids=ids).sample_ids == ids
    for bad in (dict(sample_ids=('a', 'a')), dict(sample_ids=('',)), dict(pinned=True),
                dict(sample_ids=('a',), pinned=1), dict(legend=None)):
        with pytest.raises(ValueError):
            PlotSpec('p', 'histogram', 'FL1-A', **bad)
    with pytest.raises(ValueError):
        PlotSpec('p', 'scatter', 'a', 'b', overlay=True, sample_ids=('a',), pinned=True)


def test_sample_list_starts_a_drag_with_the_selected_rows(app, bench, tmp_path, monkeypatch):
    sources, _ = start(app, bench, tmp_path, 3)
    seen = {}
    def fake_exec(drag, *args):
        seen['ids'] = sample_drag.decode(drag.mimeData(), bench.drag_token, [s.id for s in sources])
        return COPY
    monkeypatch.setattr(QtGui.QDrag, 'exec', fake_exec)
    bench.samples.item(0).setSelected(True); bench.samples.item(2).setSelected(True)
    bench.samples.startDrag(COPY)
    assert seen['ids'] == (sources[0].id, sources[2].id)
    assert bench.samples.dragEnabled() and bench._drag_origin is None
