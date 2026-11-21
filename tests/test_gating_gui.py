"""Window actions added in 0.9.0, driven through the same methods the menus call."""
import json
from pathlib import Path

import numpy as np
import pytest
from PySide6 import QtCore, QtWidgets

from flowhero.gating_actions import CombineDialog, CompensationDialog
from flowhero.model import COMPENSATED_DOMAIN, DOMAIN, BooleanGate, Ellipse, Polygon, Rectangle
from flowhero.plot_sheet import DARK, LIGHT, AxisDialog
from flowhero.workbench import Workbench
from test_actions_gui import wait_tasks
from test_scales_compensation import L, MATRIX, sample
from test_sheet_gui import wait


def start(app, window, tmp_path, names=('a.fcs',)):
    paths = [sample(tmp_path, name, seed=i + 1)[0].path for i, name in enumerate(names)]
    window.add_paths(paths); wait(app, window)
    assert len(window.project.sources) == len(names), window.errors.toPlainText()
    return window.project.sources


def scatter(window):
    return next(d for d in window.documents.values() if d.spec.kind == 'scatter')


def draw(app, window, document, tool, points, name):
    window.tool.setCurrentIndex(window.tool.findData(tool)); window.gate_name.setText(name)
    document.points = list(points); document.finish(); wait(app, window)
    assert not window.errors.toPlainText(), window.errors.toPlainText()
    return window.project.gates[-1]


def count(window, gate_id):
    rows = window.results[window.selected_source().id]['rows']
    return next(r['event_count'] for r in rows if r['gate_id'] == gate_id)


def select(window, gate_id):
    def find(item):
        if item.data(0, QtCore.Qt.ItemDataRole.UserRole) == gate_id:
            return item
        for i in range(item.childCount()):
            found = find(item.child(i))
            if found is not None:
                return found
    window.tree.setCurrentItem(find(window.tree.topLevelItem(0)))


def test_ellipse_tool_draws_and_handles_reshape(app, bench, tmp_path):
    start(app, bench, tmp_path)
    document = scatter(bench)
    gate = draw(app, bench, document, 'ellipse', [(38000, 20000), (62000, 40000)], 'Cells')
    assert isinstance(gate, Ellipse) and gate.center == (50000, 30000) and gate.axes == ((12000, 0.0), (0.0, 10000))
    inside = count(bench, gate.id)
    curve, middle, first, second = document.gate_items[gate.id]
    first.setPos(64000, 36000); first.sigPositionChangeFinished.emit(first); wait(app, bench)
    tilted = bench.project.gates[-1]
    assert tilted.id == gate.id and tilted.axes == ((14000.0, 6000.0), (0.0, 10000.0)) and count(bench, gate.id) != inside
    curve, middle, first, second = scatter(bench).gate_items[gate.id]
    middle.setPos(52000, 31000); app.processEvents()
    assert (first.pos().x(), first.pos().y()) == (66000, 37000)  # rim handles follow the centre while dragging
    middle.sigPositionChangeFinished.emit(middle); wait(app, bench)
    assert bench.project.gates[-1].center == (52000.0, 31000.0) and bench.project.gates[-1].axes == tilted.axes
    bench.undo(); bench.undo(); wait(app, bench)
    assert bench.project.gates[-1] == gate


def test_logicle_axis_dialog_ticks_and_gate_coordinates(app, bench, tmp_path):
    start(app, bench, tmp_path)
    document = scatter(bench)
    bench.edit_plot(document.plot_id, x_channel='FL1-A', y_channel='FL2-A'); wait(app, bench)
    dialog = AxisDialog(document, 'x')
    assert [dialog.scale.itemText(i) for i in range(dialog.scale.count())] == ['linear', 'log', 'logicle']
    dialog.scale.setCurrentText('logicle'); dialog.top.setText('1000000'); dialog.width.setText('1')
    assert dialog.chosen_scale() == 'logicle:1000000:1:4.5:0'
    count_axis = AxisDialog(next(d for d in bench.documents.values() if d.spec.kind == 'histogram'), 'y')
    assert count_axis.scale.count() == 2
    bench.edit_plot(document.plot_id, x_scale=L, y_scale=L); wait(app, bench)
    data = bench.results[bench.selected_source().id]['sheet_plots'][document.plot_id]
    assert data['nonpositive'] == 0 and not bench.errors.toPlainText()
    labels = [text for _, text in document.axes['bottom']._tickLevels[0]]
    assert '0' in labels and '10³' in labels and not document.axes['bottom'].logMode
    gate = draw(app, bench, document, 'rectangle', [(0.3, 0.2), (0.8, 0.7)], 'Box')
    assert gate.scales == (L, L) and count(bench, gate.id) > 0
    # On a differently scaled plot the gate is shown, converted, but cannot be dragged.
    bench.edit_plot(document.plot_id, x_scale='log', y_scale='log'); wait(app, bench)
    item = scatter(bench).gate_items[gate.id]
    assert not item.translatable and not bench.errors.toPlainText()


def test_combined_gates_counts_and_deletion(app, bench, tmp_path):
    start(app, bench, tmp_path)
    document = scatter(bench)
    left = draw(app, bench, document, 'rectangle', [(30000, 10000), (52000, 50000)], 'Left')
    low = draw(app, bench, document, 'rectangle', [(30000, 10000), (70000, 31000)], 'Low')
    select(bench, left.id)
    dialog = CombineDialog(bench, left, [left, low])
    assert dialog.choice() == ('and', (left.id,), 'Left')
    dialog.gates.item(1).setCheckState(QtCore.Qt.CheckState.Checked)
    assert dialog.choice() == ('and', (left.id, low.id), 'Left and Low')
    bench.combine_gates(('and', (left.id, low.id), 'both')); wait(app, bench)
    bench.combine_gates(('or', (left.id, low.id), 'either')); wait(app, bench)
    select(bench, left.id); bench.combine_gates(('not', (left.id,), 'not left')); wait(app, bench)
    both, either, outside = bench.project.gates[-3:]
    assert all(isinstance(g, BooleanGate) for g in (both, either, outside))
    total = bench.results[bench.selected_source().id]['event_count']
    assert count(bench, both.id) + count(bench, either.id) == count(bench, left.id) + count(bench, low.id)
    assert count(bench, outside.id) == total - count(bench, left.id)
    bench.combine_gates(('and', (left.id,), 'bad'))
    assert 'two or more' in bench.errors.toPlainText()
    select(bench, low.id); bench.delete_branch(); wait(app, bench)
    assert [g.name for g in bench.project.gates] == ['Left', 'not left']  # combined gates using Low went with it


def test_copy_and_paste_gate_branch(app, bench, tmp_path):
    start(app, bench, tmp_path)
    document = scatter(bench)
    parent = draw(app, bench, document, 'rectangle', [(30000, 10000), (70000, 50000)], 'Cells')
    select(bench, parent.id); bench.add_plot('scatter'); wait(app, bench)
    inner = next(d for d in bench.documents.values() if d.spec.population_id == parent.id)
    a = draw(app, bench, inner, 'rectangle', [(40000, 20000), (60000, 40000)], 'A')
    quadrants = draw(app, bench, inner, 'quadrants', [(50000, 30000)], 'Q')
    select(bench, a.id); bench.combine_gates(('not', (a.id,), 'not A')); wait(app, bench)
    before = len(bench.project.gates)
    select(bench, bench.project.gates[-1].id); bench.copy_gate()
    assert 'combined gate refers' in bench.errors.toPlainText()
    select(bench, parent.id); bench.copy_gate()
    assert len(bench.gate_clipboard) == before
    select(bench, None); bench.paste_gate(); wait(app, bench)
    pasted = bench.project.gates[before:]
    assert len(pasted) == before and pasted[0].parent_id is None and pasted[0].id != parent.id
    assert {g.parent_id for g in pasted[1:]} == {pasted[0].id}
    groups = {g.group_id for g in pasted if isinstance(g, Rectangle) and g.group_id}
    assert len(groups) == 1 and quadrants.group_id not in groups
    copied_not = next(g for g in pasted if isinstance(g, BooleanGate))
    assert copied_not.operands == (next(g.id for g in pasted if g.name == 'A'),)
    assert count(bench, pasted[0].id) == count(bench, parent.id) and count(bench, copied_not.id) == count(bench, bench.project.gates[before - 1].id)
    bench.undo(); wait(app, bench)
    assert len(bench.project.gates) == before


def test_highlight_marks_population_in_other_plots_and_clears(app, bench, tmp_path):
    start(app, bench, tmp_path)
    document = scatter(bench)
    gate = draw(app, bench, document, 'rectangle', [(30000, 10000), (52000, 50000)], 'Left')
    assert 'highlights' in bench.results[bench.selected_source().id] and not bench.results[bench.selected_source().id]['highlights']
    bench.set_highlight(gate.id); wait(app, bench)
    result = bench.results[bench.selected_source().id]
    assert result['highlights'][document.plot_id]['event_count'] == count(bench, gate.id)
    assert 'Highlighted: Left' in scatter(bench).summary.text()
    bench.clear_highlight(); wait(app, bench)
    assert bench.highlight_id is None and 'Highlighted' not in scatter(bench).summary.text()
    bench.set_highlight(gate.id); wait(app, bench)
    select(bench, gate.id); bench.delete_branch(); wait(app, bench)
    assert bench.highlight_id is None


def test_singlet_gate_is_an_editable_polygon_with_its_plot(app, bench, tmp_path):
    start(app, bench, tmp_path)
    assert bench.singlet_pairs() == [('FSC-A', 'FSC-H')]
    plots = len(bench.project.plots)
    bench.add_singlet_gate(); wait_tasks(app, bench); wait(app, bench)
    gate = bench.project.gates[-1]
    assert isinstance(gate, Polygon) and gate.name == 'Singlets' and gate.channels == ('FSC-A', 'FSC-H')
    assert len(bench.project.plots) == plots + 1 and bench.project.plots[-1].y_channel == 'FSC-H'
    total = bench.results[bench.selected_source().id]['event_count']
    assert count(bench, gate.id) > 0.97 * total and 'robust SD' in bench.statusBar().currentMessage()
    bench.undo(); wait(app, bench)
    assert not bench.project.gates and len(bench.project.plots) == plots


def test_compensation_setting_changes_domain_and_is_undoable(app, bench, tmp_path):
    source = start(app, bench, tmp_path)[0]
    assert 'Compensation: <a href="#compensation">off</a>' in bench.domain_label.text()
    before = next(r['value'] for r in bench.results[source.id]['rows'] if r['gate_id'] == 'root' and r['channel'] == 'FL1-A' and r['statistic'] == 'median')
    assert bench.set_compensation({'source': 'file'}); wait(app, bench)
    assert bench.project.domain == COMPENSATED_DOMAIN and 'from each file' in bench.domain_label.text()
    rows = bench.results[source.id]['rows']
    after = next(r['value'] for r in rows if r['gate_id'] == 'root' and r['channel'] == 'FL1-A' and r['statistic'] == 'median')
    assert after != before and {r['domain'] for r in rows} == {COMPENSATED_DOMAIN}
    assert bench.results[source.id]['compensated_channels'] == ['FL1-A', 'FL2-A']
    bench.set_compensation(MATRIX); wait(app, bench)
    assert 'one matrix, 2 channels' in bench.domain_label.text()
    same = next(r['value'] for r in bench.results[source.id]['rows'] if r['gate_id'] == 'root' and r['channel'] == 'FL1-A' and r['statistic'] == 'median')
    assert same == pytest.approx(after)
    bench.undo(); bench.undo(); wait(app, bench)
    assert bench.project.compensation is None and bench.project.domain == DOMAIN and 'off' in bench.domain_label.text()


def test_compensation_dialog_reads_file_matrix_and_validates(app, bench, tmp_path, monkeypatch):
    start(app, bench, tmp_path)
    dialog = CompensationDialog(bench); bench._compensation_dialog = dialog
    assert dialog.off.isChecked()
    dialog.matrix.setChecked(True); dialog.accept()
    assert 'Take a matrix' in dialog.message.text() and dialog.result() != QtWidgets.QDialog.DialogCode.Accepted
    dialog.load_selected(); wait_tasks(app, bench)
    assert dialog.table.rowCount() == 2 and 'Matrix taken from a.fcs' in dialog.message.text()
    assert dialog.table_matrix() == MATRIX
    dialog.table.item(0, 1).setText('0,25')
    dialog.accept()
    assert dialog.result_value['values'][0] == [1.0, 0.25]
    dialog.table.item(0, 1).setText('x'); dialog.accept()
    assert 'needs a number' in dialog.message.text()
    target = tmp_path / 'spill.csv'
    dialog.table.item(0, 1).setText('0.2')
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', lambda *a, **k: (str(target), ''))
    dialog.export_csv()
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileName', lambda *a, **k: (str(target), ''))
    dialog.import_csv()
    assert dialog.table_matrix() == MATRIX
    bench._compensation_dialog = None; dialog.deleteLater()


def test_scatter_overlay_and_offset_histograms(app, bench, tmp_path):
    first, second = start(app, bench, tmp_path, ('a.fcs', 'b.fcs'))
    document = scatter(bench)
    histogram = next(d for d in bench.documents.values() if d.spec.kind == 'histogram')
    bench.samples.selectAll(); bench.apply_sample_selection(); wait(app, bench)
    assert histogram.spec.sample_ids == (first.id, second.id) and document.spec.sample_ids == ()
    bench.set_overlay(document.plot_id, True); wait(app, bench)
    spec = document.spec
    assert spec.overlay and spec.sample_ids == (first.id, second.id) and spec.x_limits and spec.y_limits
    legend = [label.text for _, label in scatter(bench).graph.getPlotItem().legend.items]
    assert legend == ['1: a.fcs', '2: b.fcs']
    states = len(bench.undo_states)
    bench.edit_plot(histogram.plot_id, stagger=True); wait(app, bench)
    shown = next(d for d in bench.documents.values() if d.spec.kind == 'histogram')
    curves = [item for item in shown.graph.getPlotItem().listDataItems()]
    assert len(curves) == 2 and np.nanmin(curves[1].yData) > 0 and np.nanmin(curves[0].yData) == 0
    assert 'Offset layout' in shown.summary.text() and len(bench.undo_states) == states + 1
    bench.set_overlay(document.plot_id, False); wait(app, bench)
    assert not document.spec.overlay and document.spec.sample_ids == ()
    bench.set_overlay(document.plot_id, True); wait(app, bench)
    bench.change_kind(document.plot_id, 'density'); wait(app, bench)
    assert not document.spec.overlay and document.spec.sample_ids == ()


def test_light_plot_background_is_remembered_and_exports_stay_white(app, bench, tmp_path):
    start(app, bench, tmp_path)
    document = scatter(bench)
    assert document.theme is DARK
    bench.set_light_plots(True); app.processEvents()
    assert document.theme is LIGHT and bench.read_settings()['light_plots'] is True
    assert document.export_image(str(tmp_path / 'plot.png')) and document.theme is LIGHT
    document.render()
    assert '▾' in document.axes['bottom'].labelText
    other = Workbench()
    try:
        assert other.screen_theme is LIGHT and other.light_action.isChecked()
    finally:
        other.close()
    bench.set_light_plots(False)
    assert document.theme is DARK and bench.read_settings()['light_plots'] is False


def test_autosave_keeps_a_recovery_copy_only_after_a_crash(app, bench, tmp_path):
    start(app, bench, tmp_path)
    draw(app, bench, scatter(bench), 'rectangle', [(30000, 10000), (52000, 50000)], 'Left')
    assert bench.dirty and bench.autosave()
    copy = bench._autosave_path
    assert copy.is_file() and json.loads(Path(str(copy) + '.note').read_text())['project_path'] is None
    other = Workbench()
    try:
        assert other.recovery_candidates() == []          # the owning window is still running
        bench._autosave_lock.unlock(); bench._autosave_lock = bench._autosave_path = None   # as after a crash
        candidates = other.recovery_candidates()
        assert [c[0] for c in candidates] == [copy]
        other.restore_recovery(*candidates[0]); wait(app, other)
        assert [g.name for g in other.project.gates] == ['Left'] and other.dirty and other.project_path is None
        assert not copy.exists() and 'Recovered unsaved work' in other.statusBar().currentMessage()
    finally:
        other.dirty = False; other.close()
    # A normal save removes this window's own copy.
    assert bench.autosave()
    copy = bench._autosave_path
    bench.project_path = str(tmp_path / 'saved.flowhero.json')
    assert bench.save() and not copy.exists() and bench._autosave_path is None
    assert not bench.autosave()                             # nothing unsaved, nothing written


def test_time_course_window_reports_flagged_bins(app, bench, tmp_path):
    start(app, bench, tmp_path)
    bench.show_time_course(); wait_tasks(app, bench)
    window = bench._time_window
    assert 'equal time bins' in window.summary.text() and 'No event is removed' in window.summary.text()
    assert len(window.plots) == 2
    window.close()


def test_gatingml_export_with_file_compensation_needs_a_calculated_sample(app, bench, tmp_path, monkeypatch):
    start(app, bench, tmp_path)
    document = scatter(bench)
    bench.edit_plot(document.plot_id, x_channel='FL1-A', y_channel='FL2-A'); wait(app, bench)
    draw(app, bench, scatter(bench), 'rectangle', [(100, 100), (5000, 5000)], 'Box')
    bench.set_compensation({'source': 'file'}); wait(app, bench)
    target = tmp_path / 'gates.gatingml'
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', lambda *a, **k: (str(target), ''))
    bench.export_gate_ml()
    assert not bench.errors.toPlainText() and target.read_text().count('compensation-ref="FCS"') == 2


def test_flowjo_workspace_import_dialog_offers_its_matrix(app, bench, tmp_path):
    from flowhero.gate_import import read_gate_import
    from flowhero.gate_import_dialog import GateImportDialog
    from test_qc_wsp import workspace
    source = start(app, bench, tmp_path)[0]
    gates = [Polygon('cells', 'Cells', None, ('FSC-A', 'SSC-A'), ((35000., 18000.), (65000., 18000.), (65000., 42000.), (35000., 42000.))),
             Rectangle('rect', 'Rect', 'cells', ('FL1-A', 'FL2-A'), (0.4, 0.3), (0.9, 0.6), (L, L))]
    document = read_gate_import(workspace(tmp_path, source, gates))
    dialog = GateImportDialog(bench.project, document, bench.channels, bench)
    assert dialog.variant.count() == 1 and dialog.variant.itemText(0) == 'Workspace sample: a.fcs'
    assert dialog.use_compensation.isChecked()
    dialog.accept()
    assert dialog.candidate is not None, dialog.error_label.text()
    assert bench.changed(dialog.candidate, rebuild=True); wait(app, bench)
    assert bench.project.compensation == MATRIX and 'one matrix' in bench.domain_label.text()
    assert [g.name for g in bench.project.gates] == ['cells', 'rect'] and count(bench, bench.project.gates[1].id) > 0
    dialog.deleteLater()
