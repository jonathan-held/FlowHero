from dataclasses import replace
import json
import math
import numpy as np
import pytest
import flowkit as fk
from flowhero.analysis import evaluate, PLOT_LIMIT
from flowhero.model import Polygon, RangeGate, Rectangle, PlotSpec, Project, ordered_gates
from flowhero.storage import save_project, load_project, export_gates, export_gatingml
from test_analysis import source_file


def test_unrestricted_branching_and_depth(tmp_path):
    source = source_file(tmp_path, [[2, 2, 2, 1], [4, 4, 4, 9], [6, 6, 6, 100]])
    gates = []
    for i in range(24):
        gates.append(Rectangle(f'g{i}', f'Level {i}', None if i == 0 else f'g{i-1}',
                               ('FSC-A', 'SSC-A'), (0, 0), (5, 5)))
    gates.append(RangeGate('branch', 'Branch', 'g1', 'FL1-A', 0, 5))
    project = Project([source], gates, 'FL1-A'); project.validate()
    result = evaluate(source, gates, 'FL1-A', plot_specs=[])
    rows = [r for r in result['rows'] if r['statistic'] == 'median']
    assert rows[0]['event_count'] == 3
    assert rows[-2]['event_count'] == 2
    branch = next(r for r in rows if r['gate_id'] == 'branch')
    assert branch['event_count'] == 1 and branch['percent_parent'] == 50
    assert branch['percent_total'] == pytest.approx(100/3)
    assert len({r['gate_id'] for r in rows}) == 26
    with pytest.raises(ValueError, match='cycle'):
        ordered_gates([replace(gates[0], parent_id='g1'), gates[1]])


def test_log_gates_and_display_do_not_transform_statistics(tmp_path):
    source = source_file(tmp_path, [[-1, 2, 2, 10], [0, 2, 2, 20], [1, 2, 2, 1],
                                    [10, 2, 2, 9], [100, 2, 2, 100], [1000, 2, 2, 200]])
    gate = Rectangle('loggate', 'Log rectangle', None, ('FSC-A', 'SSC-A'), (0, 0), (2, 5), ('log', 'linear'))
    spec = PlotSpec('p', 'scatter', 'FSC-A', 'SSC-A', x_scale='log')
    result = evaluate(source, [gate], 'FL1-A', plot_specs=[spec])
    row = next(r for r in result['rows'] if r['gate_id'] == 'loggate' and r['statistic'] == 'median' and r['channel'] == 'FL1-A')
    assert row['event_count'] == 2 and row['value'] == 5
    data = result['sheet_plots']['p']
    assert data['nonpositive'] == 2 and data['x'] == [0, 1, 2, 3]
    # Existing gate membership is independent of a plot display change.
    changed = evaluate(source, [gate], 'FL1-A', plot_specs=[replace(spec, x_scale='linear')])
    assert changed['rows'] == result['rows']
    polygon = Polygon('poly', 'Log polygon', None, ('FSC-A', 'SSC-A'),
                      ((-.1, 0), (1.1, 0), (1.1, 5), (-.1, 5)), ('log', 'linear'))
    assert evaluate(source, [polygon], 'FL1-A')['rows'][0]['event_count'] == 2


def test_quadrants_partition_edges_and_exclude_invalid_coordinates(tmp_path):
    points = [[2, 2, 2, 1], [2, 3, 2, 2], [3, 2, 2, 3], [3, 3, 2, 4],
              [float('nan'), 2, 2, 5], [float('inf'), 2, 2, 6]]
    source = source_file(tmp_path, points)
    gates = [Rectangle(f'q{i}', f'Q{i}', None, ('FSC-A', 'SSC-A'), lo, hi, group_id='group')
             for i, lo, hi in [(1, (3, 3), (None, None)), (2, (None, 3), (3, None)),
                               (3, (None, None), (3, 3)), (4, (3, None), (None, 3))]]
    rows = evaluate(source, gates, 'FL1-A')['rows']
    assert [r['event_count'] for r in rows] == [1, 1, 1, 1]
    assert [r['value'] for r in rows] == [4, 2, 1, 3]


def test_histogram_and_density_are_full_event_and_log_exclusions_are_explicit(tmp_path):
    size = PLOT_LIMIT + 200
    values = np.full((size, 4), 2.0); values[:, 3] = np.arange(size)
    source = source_file(tmp_path, values)
    specs = [PlotSpec('h', 'histogram', 'FL1-A', x_scale='log', x_limits=(1, size), bins=16),
             PlotSpec('d', 'density', 'FL1-A', 'FSC-A', bins=16),
             PlotSpec('s', 'scatter', 'FL1-A', 'FSC-A')]
    result = evaluate(source, [], 'FL1-A', plot_specs=specs)
    assert sum(result['sheet_plots']['h']['counts']) == size - 1
    assert result['sheet_plots']['h']['nonpositive'] == 1
    assert np.asarray(result['sheet_plots']['d']['counts']).sum() == size
    assert len(result['sheet_plots']['s']['x']) == PLOT_LIMIT
    row = next(r for r in result['rows'] if r['channel'] == 'FL1-A' and r['statistic'] == 'median')
    assert row['value'] == (size-1)/2 and row['event_count'] == size


def test_sheet_persistence_gate_json_and_gatingml_roundtrip(tmp_path):
    source = source_file(tmp_path, [[1, 2, 2, 1], [10, 3, 3, 4], [100, 4, 4, 7]])
    root = Rectangle('rect', 'Population', None, ('FSC-A', 'SSC-A'), (-.1, 0), (2.1, 10), ('log', 'linear'))
    child = RangeGate('range', 'Child', 'rect', 'FL1-A', 0, 5)
    project = Project([source], [root, child], 'FL1-A', plots=[PlotSpec('p', 'histogram', 'FL1-A', population_id='range', x_scale='log', geometry=(25, 35, 510, 390))])
    project.set_override(source.id, replace(child, maximum=10))
    file = tmp_path/'sheet.json'; save_project(project, file)
    assert load_project(file) == project
    export_gates(project, tmp_path/'gates.json')
    exported = json.loads((tmp_path/'gates.json').read_text())
    assert exported['gates'][0]['scales'] == ['log', 'linear']
    assert exported['sample_overrides'][source.id][0]['maximum'] == 10
    xml = tmp_path/'gates.gatingml'; export_gatingml(project, xml)
    strategy = fk.parse_gating_xml(str(xml))
    sample = fk.Sample(source.path, sample_id=source.id)
    result = strategy.gate_sample(sample)
    assert result.get_gate_membership('rect', ('root',)).sum() == 3
    assert result.get_gate_membership('range', ('root', 'rect')).sum() == 2
    export_gatingml(project, xml, source.id)
    custom = fk.parse_gating_xml(str(xml)).gate_sample(sample)
    assert custom.get_gate_membership('range', ('root', 'rect')).sum() == 3


@pytest.mark.parametrize('change', [dict(x_scale='log', x_limits=(0, 10)), dict(x_limits=(2, 1)), dict(kind='unknown'), dict(bins=True)])
def test_plot_settings_fail_closed(change):
    with pytest.raises(ValueError):
        PlotSpec(**{**dict(id='p', kind='histogram', x_channel='FL1-A'), **change})


@pytest.mark.parametrize('values', [dict(group_id=[]), dict(channels=('FSC-A', 1))])
def test_malformed_rectangle_metadata_is_rejected(values):
    with pytest.raises(ValueError):
        Rectangle(**{**dict(id='r', name='R', parent_id=None, channels=('FSC-A','SSC-A'), minimum=(0,0), maximum=(10,10)), **values})


def test_missing_metric_reports_real_channels_for_ui_recovery(tmp_path):
    from flowhero.jobs import Jobs, sheet_worker
    from flowhero.storage import bind_source
    from test_jobs import collect
    import flowio
    path=tmp_path/'other.fcs'
    with path.open('wb') as stream:
        flowio.create_fcs(stream,[1,2,3,4],['Detector-A','Detector-B'])
    source=bind_source(str(path))
    project=Project([source],channel='Unavailable')
    jobs=Jobs();jobs.start(project,[source],target=sheet_worker)
    messages=collect(jobs)
    assert [m['kind'] for m in messages]==['error','done']
    assert messages[0]['channels']==['Detector-A','Detector-B']
    assert 'Unavailable' in messages[0]['message']
