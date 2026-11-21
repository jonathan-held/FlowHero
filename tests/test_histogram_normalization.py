from dataclasses import replace
import json
from types import SimpleNamespace

import numpy as np
import pytest

from flowhero.analysis import evaluate
from flowhero.histograms import histogram_statistics_rows, histogram_values, NORMALIZATION_LABELS
from flowhero.model import PlotSpec, Project, Source
from flowhero.storage import load_project, save_project
from test_analysis import source_file


def test_event_fraction_uses_full_population_including_plot_exclusions(tmp_path):
    source = source_file(tmp_path, [[2, 2, 2, value] for value in
                                    [float('nan'), -1, 0, 1, 10, 100]])
    spec = PlotSpec('hist', 'histogram', 'FL1-A', x_scale='log', x_limits=(1, 10), bins=8)
    result = evaluate(source, [], 'FL1-A', plot_specs=[spec])
    data = result['sheet_plots'][spec.id]
    raw_counts = list(data['counts'])
    assert (data['event_count'], data['nonfinite'], data['nonpositive'], data['outside']) == (6, 1, 2, 1)
    values, status = histogram_values(data, 'event_fraction')
    assert status == 'ok' and sum(values) == pytest.approx(2 / 6)
    assert np.array_equal(values, np.asarray(raw_counts) / 6)
    assert data['counts'] == raw_counts


def test_peak_count_and_zero_modes_preserve_raw_counts():
    data = {'counts': [0, 2, 6, 4], 'event_count': 20}
    values, status = histogram_values(data, 'peak')
    assert status == 'ok' and values.tolist() == [0, 1 / 3, 1, 2 / 3]
    counts, status = histogram_values(data, 'count')
    assert status == 'ok' and counts.tolist() == data['counts']
    counts[1] = 999
    assert data['counts'] == [0, 2, 6, 4]
    for mode in NORMALIZATION_LABELS:
        for population in (0, 10):
            values, status = histogram_values({'counts': [0, 0], 'event_count': population}, mode)
            assert status == 'empty' and values.tolist() == [0, 0]
    with pytest.raises(ValueError, match='Unknown histogram normalization'):
        histogram_values(data, 'density')


@pytest.mark.parametrize('mode', ['count', 'event_fraction', 'peak'])
def test_normalization_persists_and_old_schema_defaults_to_count(tmp_path, mode):
    spec = PlotSpec('hist', 'histogram', 'FL1-A', normalization=mode)
    project = Project(plots=[spec])
    path = tmp_path / 'project.flowhero.json'
    save_project(project, path)
    assert load_project(path) == project
    document = json.loads(path.read_text())
    assert document['version'] == 10
    document['version'] = 4
    del document['project']['analysis']
    del document['project']['gate_imports']
    del document['project']['annotations']
    del document['project']['compensation']
    del document['project']['plots'][0]['normalization']
    path.write_text(json.dumps(document))
    assert load_project(path).plots == [replace(spec, normalization='count')]
    with pytest.raises(ValueError, match='normalization'):
        replace(spec, normalization='unknown')


def test_histogram_statistics_are_population_channel_and_display_scope_specific():
    spec = PlotSpec('hist', 'histogram', 'FL1-A', population_id='child', sample_ids=('b', 'a'))
    results = {}
    for sid in ('a', 'b', 'c'):
        rows = []
        for gate in ('root', 'parent', 'child'):
            for channel in ('FL1-A', 'FSC-A'):
                for statistic in ('median', 'arithmetic_mean', 'geometric_mean'):
                    rows.append({'sample_id': sid, 'gate_id': gate, 'channel': channel,
                                 'statistic': statistic, 'event_count': 12,
                                 'percent_parent': 50, 'percent_total': 10, 'value': 7, 'status': 'ok'})
        results[sid] = {'rows': rows, 'sheet_plots': {'hist': {'counts': [1], 'event_count': 12}}}
    rows = histogram_statistics_rows(spec, results, 'c')
    assert len(rows) == 6
    assert [row['sample_id'] for row in rows] == ['b'] * 3 + ['a'] * 3
    assert all(row['gate_id'] == 'child' and row['channel'] == 'FL1-A'
               and row['percent_parent'] == 50 and row['percent_total'] == 10 for row in rows)
    rows[0]['value'] = 999
    assert results['b']['rows'][-3]['value'] == 7
    single = replace(spec, population_id=None, sample_ids=())
    assert {row['sample_id'] for row in histogram_statistics_rows(single, results, 'c')} == {'c'}
    assert all(row['gate_id'] == 'root' for row in histogram_statistics_rows(single, results, 'c'))
    results['b']['sheet_plots']['hist'] = {'error': 'Missing channel'}
    del results['a']['sheet_plots']['hist']
    assert histogram_statistics_rows(spec, results, 'c') == []


def test_normalization_render_and_separate_statistics_bins_menu_without_analysis():
    from PySide6 import QtCore, QtWidgets
    from flowhero.plot_sheet import PlotDocument
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    spec = PlotSpec('hist', 'histogram', 'FL1-A', bins=8)
    source = Source('a', '/local/sample.fcs', '0' * 64)
    project = Project(sources=[source], plots=[spec])
    data = {'counts': [2, 6], 'event_count': 10, 'edges': [0, 1, 2], 'x_bounds': [0, 2],
            'nonfinite': 1, 'nonpositive': 0, 'outside': 1}
    owner = SimpleNamespace(project=project, rebuilding=True, closing=True,
                            results={'a': {'sheet_plots': {'hist': data}}}, calls=[])
    owner.plot_spec = lambda pid: owner.project.plots[0]
    owner.selected_source = lambda: source
    def edit_plot(pid, **changes):
        owner.calls.append(('edit', pid, changes))
        owner.project.plots[0] = replace(owner.project.plots[0], **changes)
    owner.edit_plot = edit_plot
    owner.show_histogram_statistics = lambda pid: owner.calls.append(('statistics', pid))
    owner.export_plot_data = lambda pid: owner.calls.append(('bins', pid))
    document = PlotDocument(owner, spec)
    document.render()
    assert document.graph.listDataItems()[0].yData.tolist() == [2, 6]
    failures = []
    def inspect_menu():
        menu = document._context_menu
        try:
            submenu = next(action.menu() for action in menu.actions() if action.text() == 'Histogram normalization')
            next(action for action in submenu.actions() if action.text() == NORMALIZATION_LABELS['event_fraction']).trigger()
            next(action for action in menu.actions() if action.text() == 'Show histogram statistics…').trigger()
            next(action for action in menu.actions() if action.text() == 'Export plot data…').trigger()
            assert not any('bins / statistics' in action.text() for action in menu.actions())
        except Exception as exc:
            failures.append(exc)
        finally:
            menu.close()
    QtCore.QTimer.singleShot(0, inspect_menu)
    document.context_menu(QtCore.QPoint(0, 0))
    assert not failures
    assert owner.calls == [('edit', 'hist', {'normalization': 'event_fraction', 'y_limits': None}),
                           ('statistics', 'hist'), ('bins', 'hist')]
    document.render()
    assert document.graph.listDataItems()[0].yData.tolist() == [0.2, 0.6]
    assert document.axes['left'].labelText == NORMALIZATION_LABELS['event_fraction'] + '  ▾'
    assert data['counts'] == [2, 6]
    document.close()
    app.processEvents()


def long_rows(sample_id, values, **extra):
    return [{'sample_id': sample_id, 'sample': sample_id + '.fcs', 'gate_id': 'root', 'gate': 'All events',
             'channel': 'FL1-A', 'event_count': 12, 'statistic': statistic, 'value': value, 'status': status, **extra}
            for statistic, (value, status) in values.items()]


def test_statistics_table_has_one_row_per_sample_and_three_statistic_columns():
    from flowhero.histograms import histogram_statistics_table
    full = {'median': (2.0, 'ok'), 'arithmetic_mean': (3.5, 'ok'), 'geometric_mean': (2.75, 'ok')}
    rows = (long_rows('b', full) + long_rows('a', {**full, 'geometric_mean': (None, 'nonpositive')})
            + long_rows('c', {name: (None, 'empty') for name in full})
            + long_rows('d', {'median': (None, 'nonfinite'), 'arithmetic_mean': (None, 'nonfinite'), 'geometric_mean': (None, 'nonpositive')}))
    before = [dict(row) for row in rows]
    table = histogram_statistics_table(rows)
    assert rows == before
    assert [row['sample_id'] for row in table] == ['b', 'a', 'c', 'd']
    assert all('statistic' not in row and 'value' not in row and row['event_count'] == 12 for row in table)
    assert (table[0]['median'], table[0]['arithmetic_mean'], table[0]['geometric_mean'], table[0]['status']) == (2.0, 3.5, 2.75, 'ok')
    assert (table[1]['median'], table[1]['arithmetic_mean'], table[1]['geometric_mean']) == (2.0, 3.5, None)
    assert table[1]['status'] == 'geometric_mean: nonpositive'
    assert table[2]['status'] == 'empty' and table[2]['median'] is None
    assert table[3]['status'] == 'median: nonfinite; arithmetic_mean: nonfinite; geometric_mean: nonpositive'
    # A statistic that was never calculated is named instead of being shown as a number.
    partial = histogram_statistics_table(long_rows('e', {'median': (1.0, 'ok')}))
    assert partial[0]['status'] == 'arithmetic_mean: not calculated; geometric_mean: not calculated'
    with pytest.raises(ValueError):
        histogram_statistics_table(long_rows('f', full) + long_rows('f', {'median': (9.0, 'ok')}))
    with pytest.raises(ValueError):
        histogram_statistics_table(long_rows('g', {'median': (1.0, 'ok')}) + long_rows('g', {'arithmetic_mean': (1.0, 'ok')}, event_count=13))
    with pytest.raises(ValueError):
        histogram_statistics_table(long_rows('h', {'mode': (1.0, 'ok')}))
