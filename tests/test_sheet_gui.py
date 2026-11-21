import csv
import json
import time
from pathlib import Path
import pytest
from PySide6 import QtCore, QtWidgets, QtTest
from flowhero.workbench import Workbench
from flowhero.plot_sheet import AxisDialog
from test_analysis import source_file


@pytest.fixture(scope='module')
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def bench(app):
    window = Workbench(); window.show(); app.processEvents()
    yield window
    window.dirty = False; window.close(); app.processEvents()


def wait(app, window):
    deadline = time.monotonic()+35
    while (window.jobs.busy or window._binding or window._refresh_timer.isActive()) and time.monotonic()<deadline:
        app.processEvents(); window.poll(); time.sleep(.02)
    assert not window.jobs.busy and not window._binding and not window._refresh_timer.isActive(), window.errors.toPlainText()
    app.processEvents()


def load(app, window, tmp_path):
    source = source_file(tmp_path, [[2, 2, 2, 1], [3, 3, 3, 4], [4, 4, 4, 7],
                                    [5, 5, 5, 10000], [7, 7, 7, 500], [20, 2, 2, -10000]])
    window.add_paths([source.path]); wait(app, window)
    assert len(window.results) == 1, window.errors.toPlainText()
    return source


def activate(app, window, document):
    window.sheet.setActiveSubWindow(document)
    document.showNormal(); document.setGeometry(5, 5, 640, 420); document.raise_()
    app.processEvents()


def click(app, document, x, y):
    point = document.graph.getPlotItem().vb.mapViewToScene(QtCore.QPointF(x,y))
    QtTest.QTest.mouseClick(document.graph.viewport(), QtCore.Qt.MouseButton.LeftButton,
                           QtCore.Qt.KeyboardModifier.NoModifier, document.graph.mapFromScene(point))
    app.processEvents()


def draw(app, window, document, tool, points, name):
    activate(app, window, document)
    window.gate_name.setText(name)
    window.tool.setCurrentIndex(window.tool.findData(tool))
    document.graph.setXRange(-2,22,padding=0); document.graph.setYRange(-2,12,padding=0)
    app.processEvents()
    for x,y in points: click(app, document, x,y)
    if tool == 'polygon': window.finish_gate()
    wait(app, window)


def count(window, name):
    return next(r['event_count'] for r in window.all_rows() if r['gate']==name)


def test_mouse_sheet_hierarchy_quadrants_exports_and_persistence(app, bench, tmp_path, monkeypatch):
    load(app, bench, tmp_path)
    scatter = next(d for d in bench.documents.values() if d.spec.kind=='scatter')
    draw(app, bench, scatter, 'polygon', [(0,0),(10,0),(10,10),(0,10)], 'U937')
    assert count(bench,'U937') == 5
    population = bench.selected_population()
    bench.add_plot('scatter'); wait(app, bench)
    child_plot = bench.sheet.activeSubWindow()
    bench.edit_plot(child_plot.plot_id, x_channel='SSC-A', y_channel='SSC-H'); wait(app, bench)
    draw(app, bench, child_plot, 'rectangle', [(0,0),(6,6)], 'Singlets')
    assert count(bench,'Singlets') == 4
    assert bench.project.gates[-1].parent_id == population
    singlets = bench.selected_population()
    bench.add_plot('histogram'); wait(app, bench)
    hist = bench.sheet.activeSubWindow()
    bench.edit_plot(hist.plot_id, x_channel='FL1-A', x_limits=(0,12)); wait(app, bench)
    draw(app, bench, hist, 'range', [(0,1),(8,1)], 'Viable')
    assert count(bench,'Viable') == 3
    assert bench.project.gates[-1].parent_id == singlets
    # Root-level quadrants form four independent child populations.
    bench.tree.setCurrentItem(bench.tree.topLevelItem(0))
    bench.add_plot('scatter'); wait(app, bench)
    quadrants = bench.sheet.activeSubWindow()
    bench.edit_plot(quadrants.plot_id, x_channel='FSC-A', y_channel='SSC-A'); wait(app, bench)
    draw(app, bench, quadrants, 'quadrants', [(3.5,3.5)], 'Split')
    assert [count(bench,'Split Q'+str(i)) for i in range(1,5)] == [3,0,2,1]
    assert bench.tree.topLevelItem(0).childCount() == 5
    bench.edit_plot(quadrants.plot_id, kind='density', bins=16); wait(app, bench)
    data = bench.results[bench.selected_source().id]['sheet_plots'][quadrants.plot_id]
    assert sum(sum(row) for row in data['counts']) == 6
    # Save actual geometry and all definitions; reopen and recompute.
    quadrants.setGeometry(20,30,600,410); app.processEvents()
    geometry = bench.plot_spec(quadrants.plot_id).geometry
    project_path=tmp_path/'sheet.flowhero.json'
    monkeypatch.setattr(QtWidgets.QFileDialog,'getSaveFileName',lambda *a,**k:(str(project_path),''))
    assert bench.save()
    before = [r for r in bench.all_rows() if r['statistic']=='median']
    monkeypatch.setattr(QtWidgets.QFileDialog,'getOpenFileName',lambda *a,**k:(str(project_path),''))
    bench.open_project(); wait(app, bench)
    assert bench.plot_spec(quadrants.plot_id).geometry == geometry
    assert [r for r in bench.all_rows() if r['statistic']=='median'] == before
    stats=tmp_path/'stats.csv'
    monkeypatch.setattr(QtWidgets.QFileDialog,'getSaveFileName',lambda *a,**k:(str(stats),''))
    bench.export_statistics({'layout': 'long', 'channels': bench.default_statistic_channels(),
                             'statistics': ['median', 'arithmetic_mean', 'geometric_mean'], 'ancestor_id': None})
    while bench.task_jobs.busy:
        app.processEvents(); bench.poll(); time.sleep(.02)
    with stats.open(newline='') as stream: rows=list(csv.DictReader(stream))
    assert {'median','arithmetic_mean','geometric_mean'} == {r['statistic'] for r in rows}
    assert all('percent_parent' in r for r in rows)
    gate_file=tmp_path/'gates.json'
    monkeypatch.setattr(QtWidgets.QFileDialog,'getSaveFileName',lambda *a,**k:(str(gate_file),''))
    bench.export_gate_json()
    assert len(json.loads(gate_file.read_text())['gates']) == 7
    bins=tmp_path/'histogram.csv'
    monkeypatch.setattr(QtWidgets.QFileDialog,'getSaveFileName',lambda *a,**k:(str(bins),''))
    bench.export_plot(hist.plot_id)
    with bins.open(newline='') as stream: rows=list(csv.DictReader(stream))
    assert sum(int(r['count']) for r in rows if r['record']=='bin') == 3
    assert all(r['record']=='bin' for r in rows)
    Path('results').mkdir(exist_ok=True)
    bench.tile(); app.processEvents()
    assert bench.grab().save('results/plot-sheet.png')
    # Close a plot and restore it through Undo.
    n=len(bench.project.plots)
    bench.documents[hist.plot_id].close(); wait(app, bench)
    assert len(bench.project.plots)==n-1
    bench.undo(); wait(app, bench)
    assert len(bench.project.plots)==n


def test_axis_label_click_and_log_limits(app, bench, tmp_path, monkeypatch):
    load(app, bench, tmp_path)
    document=next(d for d in bench.documents.values() if d.spec.kind=='scatter')
    activate(app, bench, document)
    called=[]
    def dialog_exec(dialog):
        called.append(True)
        dialog.channel.setCurrentIndex(dialog.channel.findData('FL1-A'))
        dialog.scale.setCurrentText('log')
        dialog.auto.setChecked(False)
        dialog.minimum.setText('1'); dialog.maximum.setText('100000')
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(AxisDialog,'exec',dialog_exec)
    axis=document.axes['bottom']
    scene=axis.label.sceneBoundingRect().center()
    QtTest.QTest.mouseClick(document.graph.viewport(),QtCore.Qt.MouseButton.LeftButton,
                           QtCore.Qt.KeyboardModifier.NoModifier,document.graph.mapFromScene(scene))
    wait(app, bench)
    assert called, 'Axis label did not open channel/scale/limits dialog'
    assert document.spec.x_channel=='FL1-A' and document.spec.x_scale=='log'
    data=bench.results[bench.selected_source().id]['sheet_plots'][document.plot_id]
    assert data['nonpositive']==1 and data['x_bounds']==[0,5]
    assert any(r['statistic']=='median' and r['channel']=='FL1-A' and r['value']==5.5 for r in bench.all_rows())


def test_sheet_failed_source_clears_stale_data_and_close_reaps(app, bench, tmp_path):
    source=load(app, bench, tmp_path)
    Path(source.path).write_bytes(b'changed')
    bench.request(); wait(app, bench)
    assert bench.all_rows()==[]
    assert 'Source content changed' in bench.errors.toPlainText()
    assert all(d._key is None for d in bench.documents.values())
    bench.request(); process=bench.jobs._process; pid=process.pid
    bench.dirty=False; bench.close()
    assert not bench.jobs.busy
    import multiprocessing
    assert pid not in [p.pid for p in multiprocessing.active_children()]


def test_sample_geometry_scope_and_quadrant_adjustments(app, bench, tmp_path):
    load(app, bench, tmp_path)
    document=next(d for d in bench.documents.values() if d.spec.kind=='scatter')
    draw(app,bench,document,'polygon',[(0,0),(10,0),(10,10),(0,10)],'Population')
    gate=bench.project.gates[0]
    bench.scope.setCurrentIndex(1)
    roi=document.gate_items[gate.id]
    roi.setPos(30,0);wait(app,bench)
    assert bench.project.gates[0]==gate
    assert bench.project.overrides[bench.selected_source().id][0].vertices[0][0]>29
    assert count(bench,'Population')==0
    bench.scope.setCurrentIndex(0)
    assert not document.gate_items[gate.id].isEnabled()
    bench.reset_overrides();wait(app,bench)
    assert count(bench,'Population')==5 and bench.project.overrides=={}
    bench.tree.setCurrentItem(bench.tree.topLevelItem(0))
    draw(app,bench,document,'quadrants',[(3.5,3.5)],'Q')
    group=next(g.group_id for g in bench.project.gates if hasattr(g,'group_id') and g.group_id)
    vertical,horizontal=document.gate_items[group]
    vertical.setValue(4.5)
    document.quadrants_edited(group,(vertical,horizontal));wait(app,bench)
    assert sum(count(bench,'Q Q'+str(i)) for i in range(1,5))==6
    assert count(bench,'Q Q2')==1


def test_drag_rectangle_and_real_right_click_menu(app, bench, tmp_path):
    load(app, bench, tmp_path)
    document=next(d for d in bench.documents.values() if d.spec.kind=='scatter')
    activate(app,bench,document)
    bench.gate_name.setText('Dragged')
    bench.tool.setCurrentIndex(bench.tool.findData('rectangle'))
    document.graph.setXRange(-2,22,padding=0);document.graph.setYRange(-2,12,padding=0)
    app.processEvents()
    view=document.graph.getPlotItem().vb
    a=document.graph.mapFromScene(view.mapViewToScene(QtCore.QPointF(0,0)))
    b=document.graph.mapFromScene(view.mapViewToScene(QtCore.QPointF(10,10)))
    QtTest.QTest.mousePress(document.graph.viewport(),QtCore.Qt.MouseButton.LeftButton,QtCore.Qt.KeyboardModifier.NoModifier,a)
    for fraction in [.25,.5,.75,1.]:
        point=QtCore.QPoint(int(a.x()+(b.x()-a.x())*fraction),int(a.y()+(b.y()-a.y())*fraction))
        QtTest.QTest.mouseMove(document.graph.viewport(),point,20);app.processEvents()
    QtTest.QTest.mouseRelease(document.graph.viewport(),QtCore.Qt.MouseButton.LeftButton,QtCore.Qt.KeyboardModifier.NoModifier,b)
    wait(app,bench)
    assert count(bench,'Dragged')==5
    menus=[]
    def choose_scale():
        # Native Cocoa need not expose QMenu through activePopupWidget.
        menu=getattr(document,'_context_menu',None)
        if not isinstance(menu,QtWidgets.QMenu):
            return
        try:
            menus.append([action.text() for action in menu.actions()])
            scale=next(a.menu() for a in menu.actions() if a.text()=='X scale')
            next(a for a in scale.actions() if a.text()=='log').trigger()
        finally:
            menu.close()
    QtCore.QTimer.singleShot(60,choose_scale)
    position=document.graph.mapFromScene(view.mapViewToScene(QtCore.QPointF(5,5)))
    QtTest.QTest.mouseClick(document.graph.viewport(),QtCore.Qt.MouseButton.RightButton,QtCore.Qt.KeyboardModifier.NoModifier,position)
    wait(app,bench)
    assert menus and document.spec.x_scale=='log'
    assert count(bench,'Dragged')==5


def test_normalization_and_optional_statistics_do_not_recalculate(app,bench,tmp_path,monkeypatch):
    load(app,bench,tmp_path)
    hist=next(d for d in bench.documents.values() if d.spec.kind=='histogram')
    assert bench.table.isHidden()
    process=bench.jobs._process
    ticket=bench.jobs._ticket
    data=bench.results[bench.selected_source().id]['sheet_plots'][hist.plot_id]
    counts=list(data['counts'])
    # No worker submission for normalization, Y scale, gate display-name or layout.
    starts=[]
    original=bench.jobs.start
    monkeypatch.setattr(bench.jobs,'start',lambda *a,**k: starts.append((a,k)))
    bench.edit_plot(hist.plot_id,normalization='event_fraction',y_limits=None)
    wait(app,bench)
    assert starts==[]
    assert bench.results[bench.selected_source().id]['sheet_plots'][hist.plot_id]['counts']==counts
    assert hist.ready()
    hist.setGeometry(20,20,600,400);app.processEvents()
    assert starts==[]
    bench.show_histogram_statistics(hist.plot_id)
    stats=bench._statistics_window
    assert stats.table.rowCount()==1 and stats.table.columnCount()==9  # one row per sample since 0.10.1
    assert all(r['channel']==hist.spec.x_channel for r in stats.rows)
    path=tmp_path/'hist-statistics.csv'
    monkeypatch.setattr(QtWidgets.QFileDialog,'getSaveFileName',lambda *a,**k:(str(path),''))
    stats.export()
    with path.open(newline='') as f: rows=list(csv.DictReader(f))
    assert len(rows)==1 and 'count' not in rows[0] and {'median','arithmetic_mean','geometric_mean'}<=set(rows[0])
    bins=tmp_path/'hist-bins.csv'
    monkeypatch.setattr(QtWidgets.QFileDialog,'getSaveFileName',lambda *a,**k:(str(bins),''))
    bench.export_plot_data(hist.plot_id)
    with bins.open(newline='') as f: rows=list(csv.DictReader(f))
    assert all(r['record']=='bin' and r['normalization']=='event_fraction' for r in rows)
    assert sum(float(r['normalized_value']) for r in rows)==pytest.approx(1)
    stats.close()
    monkeypatch.setattr(bench.jobs,'start',original)


def test_multi_sample_selection_automatically_compares_histograms(app,bench,tmp_path):
    source=load(app,bench,tmp_path)
    other=tmp_path/'other.fcs';other.write_bytes(Path(source.path).read_bytes())
    bench.add_paths([str(other)]);wait(app,bench)
    bench.samples.item(0).setSelected(True)
    bench.samples.item(1).setSelected(True)
    bench.apply_sample_selection();wait(app,bench)
    hist=next(d for d in bench.documents.values() if d.spec.kind=='histogram')
    assert len(hist.spec.sample_ids)==2
    assert len(bench.results)==2
    assert len(hist.graph.getPlotItem().listDataItems())==2
    assert all(bench.samples.item(i).checkState()==QtCore.Qt.CheckState.Unchecked for i in range(2))


def test_cancelled_source_verification_is_not_exportable_or_gateable(app,bench,tmp_path):
    load(app,bench,tmp_path)
    assert bench.all_rows()
    bench.request()
    assert bench.all_rows()==[]
    bench.cancel()
    assert bench.all_rows()==[]
    assert all(not d.ready() for d in bench.documents.values())
    bench.request();wait(app,bench)
    assert bench.all_rows()


def test_duplicate_plot_from_right_click_menu_copies_every_setting(app, bench, tmp_path):
    load(app, bench, tmp_path)
    histogram = next(d for d in bench.documents.values() if d.spec.kind == 'histogram')
    bench.edit_plot(histogram.plot_id, x_scale='log', normalization='peak', legend=False, stagger=True); wait(app, bench)
    activate(app, bench, histogram)
    before = len(bench.project.plots)
    seen = []
    def choose_duplicate():
        menu = getattr(histogram, '_context_menu', None)
        if not isinstance(menu, QtWidgets.QMenu):
            return
        try:
            seen.append([action.text() for action in menu.actions()])
            next(a for a in menu.actions() if a.text() == 'Duplicate plot').trigger()
        finally:
            menu.close()
    QtCore.QTimer.singleShot(60, choose_duplicate)
    centre = histogram.graph.viewport().rect().center()
    QtTest.QTest.mouseClick(histogram.graph.viewport(), QtCore.Qt.MouseButton.RightButton, QtCore.Qt.KeyboardModifier.NoModifier, centre)
    wait(app, bench)
    assert seen and 'Duplicate plot' in seen[0]
    assert len(bench.project.plots) == before + 1
    original = bench.plot_spec(histogram.plot_id); copy = bench.project.plots[-1]
    assert copy.id != original.id and copy.id in bench.documents
    assert copy.geometry[:2] != original.geometry[:2]
    from dataclasses import asdict
    strip = lambda s: {k: v for k, v in asdict(s).items() if k not in ('id', 'geometry')}
    assert strip(copy) == strip(original)
    sid = bench.project.sources[0].id
    a = bench.results[sid]['sheet_plots'][original.id]; b = bench.results[sid]['sheet_plots'][copy.id]
    assert list(a['counts']) == list(b['counts'])
    # Changing the copy leaves the original alone, and undo removes the copy.
    bench.edit_plot(copy.id, x_scale='linear'); wait(app, bench)
    assert bench.plot_spec(histogram.plot_id).x_scale == 'log'
    bench.undo(); bench.undo(); wait(app, bench)
    assert len(bench.project.plots) == before and copy.id not in bench.documents
