"""Histogram statistics window: one row per sample, three statistic columns (0.10.1)."""
import csv
from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest
from PySide6 import QtWidgets

from flowhero.model import RangeGate
from flowhero.sample_drag import encode
from test_sample_drop import drag, histogram, send_drop, start
from test_scales_compensation import L
from test_sheet_gui import wait

HEADERS = ['Sample', 'Population', 'Channel', 'Events', 'Median', 'Arithmetic mean', 'Geometric mean', 'Status', 'Gate review']


def opened(window, plot_id):
    window.show_histogram_statistics(plot_id)
    dialog = window._statistics_window
    table = dialog.table
    cells = [[table.item(r, c).text() for c in range(table.columnCount())] for r in range(table.rowCount())]
    return dialog, [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())], cells


def test_window_and_csv_have_one_row_per_sample(app, bench, tmp_path, monkeypatch):
    sources, events = start(app, bench, tmp_path, 4, n=3000)
    gate = RangeGate('cells', 'Cells', None, 'FSC-A', 45000., 60000.)
    bench.changed(replace(bench.project, gates=[gate]), rebuild=True); wait(app, bench)
    document = histogram(bench); bench.edit_plot(document.plot_id, population_id='cells'); wait(app, bench)
    assert drag(app, bench, [1, 2, 3], document)
    dialog, headers, cells = opened(bench, document.plot_id)
    assert headers == HEADERS
    assert [row[0] for row in cells] == [f's{i:03d}.fcs' for i in range(4)]
    channel = document.spec.x_channel
    column = bench.results[sources[0].id]['channels'].index(channel)
    for row, record, values in zip(cells, dialog.rows, events):
        selected = values[(values[:, 0] >= 45000.) & (values[:, 0] < 60000.), column].astype(float)
        assert row[1:4] == ['Cells', channel, str(len(selected))]
        assert record['median'] == pytest.approx(float(np.median(selected)), rel=1e-12)
        assert record['arithmetic_mean'] == pytest.approx(float(np.mean(selected)), rel=1e-12)
        assert row[4] == f"{record['median']:.6g}" and row[5] == f"{record['arithmetic_mean']:.6g}"
        if (selected <= 0).any():
            assert record['geometric_mean'] is None and row[6] == '' and row[7] == 'geometric_mean: nonpositive'
        else:
            assert record['geometric_mean'] == pytest.approx(float(np.exp(np.log(selected).mean())), rel=1e-12)
            assert row[6] == f"{record['geometric_mean']:.6g}" and row[7] == 'ok'
    target = tmp_path / 'statistics.csv'
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', lambda *a, **k: (str(target), ''))
    dialog.export()
    with target.open(newline='', encoding='utf-8') as stream:
        written = list(csv.DictReader(stream))
    assert len(written) == 4 and 'statistic' not in written[0] and 'value' not in written[0]
    for line, record in zip(written, dialog.rows):
        assert line['sample'] == record['sample'] and line['gate'] == 'Cells' and line['channel'] == channel
        assert int(line['event_count']) == record['event_count'] and line['status'] == record['status']
        for name in ('median', 'arithmetic_mean', 'geometric_mean'):
            assert (line[name] == '') if record[name] is None else (float(line[name]) == record[name])
        assert line['source_sha256'] == record['source_sha256'] and line['domain']
    dialog.close()


def test_single_sample_and_empty_population(app, bench, tmp_path):
    sources, _ = start(app, bench, tmp_path, 2, n=400)
    document = histogram(bench)
    dialog, headers, cells = opened(bench, document.plot_id)
    assert headers == HEADERS and len(cells) == 1 and cells[0][0] == 's000.fcs'
    dialog.close()
    gate = RangeGate('empty', 'Empty', None, 'FSC-A', -200., -100.)
    bench.changed(replace(bench.project, gates=[gate]), rebuild=True); wait(app, bench)
    bench.edit_plot(document.plot_id, population_id='empty', x_limits=(0., 5000.)); wait(app, bench)
    assert drag(app, bench, [1], document)
    dialog, headers, cells = opened(bench, document.plot_id)
    assert len(cells) == 2
    assert all(row[3] == '0' and row[4:7] == ['', '', ''] and row[7] == 'empty' for row in cells)
    dialog.close()


def test_refused_drop_is_not_accepted(app, bench, tmp_path):
    """A refused drop must not be acknowledged to Qt as accepted."""
    sources, _ = start(app, bench, tmp_path, 2, n=400)
    document = histogram(bench)
    bench.edit_plot(document.plot_id, x_scale=L, x_limits=None)
    bench._refresh_timer.stop()   # no result exists yet on the newly chosen axis
    assert all(document.plot_id not in result.get('sheet_plots', {}) for result in bench.results.values())
    before = deepcopy(bench.project)
    assert not send_drop(document, encode(bench.drag_token, [sources[1].id]))
    assert bench.project == before
    assert 'Wait until this histogram has been calculated' in bench.errors.toPlainText()
    bench.request(True); wait(app, bench)
    assert send_drop(document, encode(bench.drag_token, [sources[1].id]))
    assert document.spec.pinned and document.spec.sample_ids == tuple(s.id for s in sources)
