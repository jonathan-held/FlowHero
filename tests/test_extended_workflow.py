"""Known scientific boundaries for viability, sample geometry and full histograms."""
from dataclasses import replace
import json

import pytest

from flowhero.analysis import PLOT_LIMIT, evaluate
from flowhero.jobs import Jobs
from flowhero.model import Histogram, Project, RangeGate
from flowhero.storage import load_project, save_project
from test_analysis import polygons, source_file
from test_jobs import collect


def viable(minimum=0, maximum=3):
    return RangeGate('viable', 'Viable cells', 'singlets-id', 'FL1-A', minimum, maximum)


def test_viability_bounds_hierarchy_and_statistics(tmp_path):
    events = [[2, 2, 2, x] for x in [-1, 0, 1, 2, 3, 4, float('nan')]]
    # These events satisfy the range, but fail a polygon ancestor.
    events += [[7, 7, 7, 1], [20, 2, 2, 1]]
    source = source_file(tmp_path, events)
    result = evaluate(source, polygons(), 'FL1-A', viability=viable())
    assert [row['event_count'] for row in result['rows']] == [8, 7, 3]
    assert result['rows'][-1]['value'] == 1
    assert result['rows'][1]['status'] == 'nonfinite'
    assert result['rows'][-1]['status'] == 'ok'
    for statistic, expected in [('arithmetic_mean', 1), ('geometric_mean', None)]:
        row = evaluate(source, polygons(), 'FL1-A', viability=viable(), statistic=statistic)['rows'][-1]
        assert row['value'] == expected
        assert row['status'] == ('nonpositive' if expected is None else 'ok')
    empty = evaluate(source, polygons(), 'FL1-A', viability=viable(100, 200))['rows'][-1]
    assert (empty['event_count'], empty['status'], empty['value']) == (0, 'empty', None)


def test_histogram_common_edges_full_counts_and_exclusions(tmp_path):
    values = [-3, -2, -1, 0, 1, 2, 3, 4, 5, float('nan')]
    source = source_file(tmp_path, [[2, 2, 2, x] for x in values])
    h = Histogram('singlets-id', -2, 4, bins=3)
    result = evaluate(source, polygons(), 'FL1-A', histogram=h)
    data = result['histogram']
    assert data == {'edges': [-2, 0, 2, 4], 'counts': [2, 2, 3], 'event_count': 10,
                    'underflow': 1, 'overflow': 1, 'nonfinite': 1}
    assert sum(data['counts']) + data['underflow'] + data['overflow'] + data['nonfinite'] == data['event_count']
    assert result['rows'][-1]['status'] == 'nonfinite'
    # A second file has different values but exactly the same edges.
    other = source_file(tmp_path, [[2, 2, 2, 4], [3, 3, 3, 4]])
    assert evaluate(other, polygons(), 'FL1-A', histogram=h)['histogram']['counts'] == [0, 0, 2]
    empty = evaluate(other, polygons(), 'FL1-A', viability=viable(100, 200),
                     histogram=replace(h, gate_id='viable'))['histogram']
    assert empty['counts'] == [0, 0, 0] and empty['event_count'] == 0


def test_viability_histogram_and_suggested_bounds_are_not_plot_sampled(tmp_path):
    size = PLOT_LIMIT + 137
    source = source_file(tmp_path, [[2, 2, 2, x] for x in range(size)])
    result = evaluate(source, polygons(), 'FL1-A', include_plot=True,
                      viability=viable(1000, size), histogram=Histogram('viable', 0, size, bins=4))
    assert len(result['plots']['singlets']['x']) == PLOT_LIMIT
    assert result['rows'][-1]['event_count'] == size - 1000
    assert result['rows'][-1]['value'] == (1000 + size - 1) / 2
    assert sum(result['histogram']['counts']) == size - 1000
    assert result['histogram_suggestion'] == [1000, size - 1]


def test_worker_applies_overrides_only_to_the_bound_sample(tmp_path):
    first = tmp_path / 'first'; first.mkdir()
    second = tmp_path / 'second'; second.mkdir()
    one = source_file(first, [[2, 2, 2, 1], [4, 4, 4, 2], [5, 5, 5, 3]])
    two = replace(source_file(second, [[2, 2, 2, 1], [4, 4, 4, 2], [5, 5, 5, 3]]), id='second')
    gates = polygons()
    project = Project([one, two], gates, 'FL1-A', viability=viable(0, 10),
                      histogram=Histogram('viable', 0, 4, bins=2, sample_ids=(one.id, two.id)))
    child = replace(gates[1], vertices=((0, 0), (3, 0), (3, 3), (0, 3)))
    project.set_override(one.id, child)
    project.set_override(two.id, viable(2, 4))
    project.validate()
    jobs = Jobs()
    jobs.start(project, project.sources)
    messages = collect(jobs)
    results = {m['source_id']: m['data'] for m in messages if m['kind'] == 'result'}
    assert set(results) == {one.id, two.id}
    assert [r['event_count'] for r in results[one.id]['rows']] == [3, 1, 1]
    assert [r['event_count'] for r in results[two.id]['rows']] == [3, 3, 2]
    assert results[one.id]['rows'][1]['gate_scope'] == 'sample'
    assert results[two.id]['rows'][1]['gate_scope'] == 'shared'
    assert results[two.id]['rows'][2]['gate_scope'] == 'sample'
    assert results[one.id]['histogram']['counts'] == [1, 0]
    assert results[two.id]['histogram']['counts'] == [0, 2]
    path = tmp_path / 'extended.flowhero.json'
    save_project(project, path)
    assert load_project(path) == project
    # Changing a shared polygon preserves the other sample's explicit geometry.
    project.gates[1] = replace(gates[1], vertices=((0, 0), (9, 0), (9, 9), (0, 9)))
    assert project.gates_for(one.id)[1] == child
    assert project.gates_for(two.id)[1] == project.gates[1]


@pytest.mark.parametrize('mutation', [
    lambda p: setattr(p, 'viability', viable(2, 2)),
    lambda p: setattr(p, 'viability', viable(float('nan'), 4)),
    lambda p: setattr(p, 'viability', replace(viable(), parent_id='unknown')),
    lambda p: p.overrides.update(unknown=[polygons()[0]]),
    lambda p: p.overrides.update({'sample-vector': [replace(polygons()[0], channels=('x', 'y'))]}),
    lambda p: setattr(p, 'histogram', Histogram('unknown', 0, 10)),
    lambda p: setattr(p, 'histogram', Histogram('singlets-id', 0, 10, sample_ids=('unknown',))),
    lambda p: setattr(p, 'histogram', Histogram('singlets-id', 0, 10, bins=True)),
])
def test_invalid_scientific_settings_fail_closed(tmp_path, mutation):
    source = source_file(tmp_path, [[2, 2, 2, 1]])
    project = Project([source], polygons(), 'FL1-A')
    with pytest.raises(ValueError):
        mutation(project)
        project.validate()


def test_version_two_migrates_without_new_gates_or_overrides(tmp_path):
    path = tmp_path / 'old.json'
    project = Project(channel='FL1-A', statistic='arithmetic_mean')
    save_project(project, path)
    data = json.loads(path.read_text())
    data['version'] = 2
    for field in ['viability', 'overrides', 'histogram', 'plots', 'analysis', 'gate_imports', 'annotations', 'compensation']:
        del data['project'][field]
    path.write_text(json.dumps(data))
    assert load_project(path) == project


def test_malformed_range_json_is_rejected(tmp_path):
    source = source_file(tmp_path, [[2, 2, 2, 1]])
    project = Project([source], polygons(), 'FL1-A', viability=viable())
    path = tmp_path / 'bad.json'
    save_project(project, path)
    document = json.loads(path.read_text())
    document['project']['viability']['maximum'] = '3'
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match='Range limits'):
        load_project(path)
