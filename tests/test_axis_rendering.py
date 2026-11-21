"""Mode changes must never interpret a previous raw range as log10 ticks."""
from copy import deepcopy
import warnings

import numpy as np
import pyqtgraph as pg
import pytest

from flowhero.model import Polygon
from flowhero.storage import digest
from test_analysis import source_file
from test_sheet_gui import app, bench, wait


def assert_plot_matches_result(document, data):
    spec = document.spec
    x_range, y_range = document.graph.viewRange()
    assert x_range[0] <= data['x_bounds'][0] <= data['x_bounds'][1] <= x_range[1]
    assert np.isfinite(x_range).all() and np.isfinite(y_range).all()
    if spec.kind == 'histogram':
        curve = document.graph.listDataItems()[0]
        assert np.array_equal(curve.xData, data['edges'])
        expected = np.asarray(data['counts'], dtype=float)
        if spec.y_scale == 'log':
            positive = expected > 0
            expected[positive] = np.log10(expected[positive])
            expected[~positive] = np.nan
        assert np.array_equal(curve.yData, expected, equal_nan=True)
    else:
        scatter = next(item for item in document.graph.getPlotItem().items if isinstance(item, pg.ScatterPlotItem))
        x, y = scatter.getData()
        assert np.array_equal(x, data['x']) and np.array_equal(y, data['y'])
        assert y_range[0] <= data['y_bounds'][0] <= data['y_bounds'][1] <= y_range[1]
    assert document.axes['bottom'].logMode == (spec.x_scale == 'log')
    assert document.axes['left'].logMode == (spec.y_scale == 'log')


@pytest.mark.parametrize('kind', ['scatter', 'density', 'histogram'])
def test_high_raw_ranges_switch_to_log_and_back_without_overflow_or_scientific_changes(app, bench, tmp_path, kind):
    source = source_file(tmp_path, [[100000, 200000, 300000, 100000],
                                    [200000, 300000, 400000, 200000],
                                    [500000, 600000, 700000, 0],
                                    [0, 100000, 200000, -1]])
    original_hash = digest(source.path)
    bench.add_paths([source.path]); wait(app, bench)
    gate = Polygon('population', 'Population', None, ('FSC-A', 'SSC-A'),
                   ((50000, 50000), (400000, 50000), (400000, 400000), (50000, 400000)))
    bench.add_gates([gate]); wait(app, bench)
    document = next(doc for doc in bench.documents.values() if doc.spec.kind == 'scatter')
    bench.edit_plot(document.plot_id, kind=kind, x_channel='FSC-A', y_channel='' if kind == 'histogram' else 'SSC-A', bins=16)
    wait(app, bench)
    bench.sheet.setActiveSubWindow(document)
    document.showNormal(); document.setGeometry(10, 10, 640, 440); document.raise_()
    app.processEvents()
    gates = deepcopy(bench.project.default_gates())
    rows = deepcopy(bench.all_rows())
    population = next(row for row in rows if row['gate_id'] == 'population')
    assert population['event_count'] == 2 and population['value'] == 150000
    assert document.graph.viewRange()[0][1] > 100000
    if kind == 'histogram':
        # A manually displayed count span must not survive as a log10 span
        # when the new nonzero bins all have count1 (log10 count0).
        document.graph.setYRange(0, 100000, padding=0)

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter('always', RuntimeWarning)
        bench.edit_plot(document.plot_id, x_scale='log', y_scale='log')
        # Deliver the actual ViewBox resize notification while results are
        # pending; offscreen layouts otherwise coalesce this transient event.
        view = document.graph.getViewBox()
        view.sigResized.emit(view)
        # Render the pending state as well as the finished state. It used to
        # switch ticks to log mode while this raw range was still on screen.
        document.resize(document.width() + 70, document.height() + 45)
        app.processEvents()
        document.graph.grab(); app.processEvents()
        wait(app, bench)
        data = bench.results[bench.selected_source().id]['sheet_plots'][document.plot_id]
        assert 4 < data['x_bounds'][0] < data['x_bounds'][1] < 7
        assert data['nonpositive'] == 1
        assert_plot_matches_result(document, data)
        if kind == 'histogram':
            assert max(abs(value) for value in document.graph.viewRange()[1]) < 10
        document.graph.grab(); app.processEvents()
        assert bench.project.default_gates() == gates
        assert bench.all_rows() == rows

        # The reverse transition also must not expose new raw ranges to the
        # old log mode while data or viewport geometry changes.
        bench.edit_plot(document.plot_id, x_scale='linear', y_scale='linear')
        document.resize(document.width() + 9, document.height() + 7)
        app.processEvents()
        document.graph.grab(); app.processEvents()
        wait(app, bench)
        data = bench.results[bench.selected_source().id]['sheet_plots'][document.plot_id]
        assert data['x_bounds'][1] > 100000
        assert_plot_matches_result(document, data)
        document.graph.grab(); app.processEvents()

    assert not [str(w.message) for w in recorded if 'overflow' in str(w.message).lower()]
    assert bench.project.default_gates() == gates and bench.all_rows() == rows
    assert digest(source.path) == original_hash


def test_empty_log_histogram_honors_explicit_limits_then_uses_neutral_auto_range(app, bench, tmp_path):
    source = source_file(tmp_path, [[100000, 200000, 300000, 100000],
                                    [200000, 300000, 400000, 200000]])
    bench.add_paths([source.path]); wait(app, bench)
    document = next(doc for doc in bench.documents.values() if doc.spec.kind == 'histogram')
    document.graph.setYRange(0, 100000, padding=0)
    rows = deepcopy(bench.all_rows())
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter('always', RuntimeWarning)
        bench.edit_plot(document.plot_id, x_scale='log', y_scale='log',
                        x_limits=(10000000, 100000000), y_limits=(1, 100000))
        view = document.graph.getViewBox(); view.sigResized.emit(view)
        wait(app, bench)
        data = bench.results[bench.selected_source().id]['sheet_plots'][document.plot_id]
        assert np.array_equal(data['x_bounds'], (7, 8))
        assert_plot_matches_result(document, data)
        assert document.graph.viewRange()[1] == [0, 5]
        assert sum(data['counts']) == 0 and data['outside'] == 2
        document.graph.grab(); app.processEvents()

        bench.edit_plot(document.plot_id, y_limits=None)
        wait(app, bench)
        data = bench.results[bench.selected_source().id]['sheet_plots'][document.plot_id]
        assert_plot_matches_result(document, data)
        assert document.graph.viewRange()[1] == [0, 1]
        assert np.isnan(document.graph.listDataItems()[0].yData).all()
        document.graph.grab(); app.processEvents()
    assert not [str(w.message) for w in recorded if 'overflow' in str(w.message).lower()]
    assert bench.all_rows() == rows
