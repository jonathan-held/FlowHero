"""Exports recompute from the files and are independent of any open plot."""
import csv
import json
import numpy as np
import pytest

from flowhero import export
from flowhero.model import Polygon, Project, RangeGate
from flowhero.storage import load_project, save_project
from test_analysis import polygons, source_file

EVENTS = [[2, 2, 2, 1], [3, 3, 3, 4], [4, 4, 4, 7], [5, 5, 5, 10000], [7, 7, 7, 500], [20, 2, 2, -10000]]


def project_for(tmp_path, **changes):
    source = source_file(tmp_path, EVENTS, changes.pop('metadata', None))
    return Project(sources=[source], gates=polygons(), channel='FL1-A', **changes), source


@pytest.mark.parametrize('values', [[1., 2., 4., 8., 16.], [5., 5., 5.], [3., 9.]])
def test_spread_statistics_match_their_definitions(values):
    data = np.array(values)
    assert export.extended_statistic(data, 'sd') == (pytest.approx(np.std(data, ddof=1)), 'ok')
    assert export.extended_statistic(data, 'cv')[0] == pytest.approx(100 * np.std(data, ddof=1) / data.mean())
    low, high = np.percentile(data, [15.87, 84.13])
    assert export.extended_statistic(data, 'robust_cv')[0] == pytest.approx(50 * (high - low) / np.median(data))
    assert export.extended_statistic(data, 'p25')[0] == pytest.approx(np.percentile(data, 25))
    assert export.extended_statistic(data, 'p97.5')[0] == pytest.approx(np.percentile(data, 97.5))
    assert export.extended_statistic(data, 'median')[0] == pytest.approx(np.median(data))


def test_undefined_statistics_report_a_reason_instead_of_a_number():
    assert export.extended_statistic([], 'sd') == (None, 'empty')
    assert export.extended_statistic([4.], 'sd') == (None, 'insufficient')
    assert export.extended_statistic([-1., 1.], 'cv') == (None, 'undefined')
    assert export.extended_statistic([-1., 0., 1.], 'robust_cv') == (None, 'undefined')
    assert export.extended_statistic([1., float('nan')], 'p50') == (None, 'nonfinite')
    assert export.extended_statistic([0., 1.], 'geometric_mean') == (None, 'nonpositive')
    for name in ('p', 'p100.123', 'mode', 'p-5'):
        assert not export.valid_statistic(name)
    with pytest.raises(ValueError):
        export.extended_statistic([1.], 'mode')


def test_statistics_do_not_depend_on_plots_and_cover_all_requested_channels(tmp_path):
    project, source = project_for(tmp_path)
    assert project.plots == []
    rows, failures = export.statistics_rows(project, ['FSC-A', 'FL1-A'], ['median', 'sd', 'p75'])
    assert failures == []
    assert {r['channel'] for r in rows} == {'FSC-A', 'FL1-A'}
    assert {r['statistic'] for r in rows} == {'median', 'sd', 'p75'}
    assert len(rows) == 3 * 2 * 3  # all events + two gates, two channels, three statistics
    parent = [r for r in rows if r['gate_id'] == 'population-id' and r['channel'] == 'FL1-A']
    values = np.array([1, 4, 7, 10000, 500.])
    assert parent[0]['event_count'] == 5 and parent[0]['percent_total'] == pytest.approx(100 * 5 / 6)
    assert {r['statistic']: r['value'] for r in parent} == {
        'median': pytest.approx(np.median(values)), 'sd': pytest.approx(np.std(values, ddof=1)),
        'p75': pytest.approx(np.percentile(values, 75))}
    child = next(r for r in rows if r['gate_id'] == 'singlets-id')
    assert child['event_count'] == 4 and child['percent_parent'] == pytest.approx(80)
    assert 'events_per_ul' not in child and 'percent_of_ancestor' not in child


def test_repeated_gate_names_keep_distinct_paths_and_wide_columns(tmp_path):
    project, _ = project_for(tmp_path)
    twin = Polygon('twin', 'Repeated display name', None, ('FSC-A', 'SSC-A'), ((0, 0), (4.5, 0), (4.5, 4.5), (0, 4.5)))
    project.gates.append(twin)
    rows, _ = export.statistics_rows(project, ['FL1-A'], ['median'])
    paths = {r['gate_id']: r['population_path'] for r in rows}
    assert len(set(paths.values())) == 4
    assert paths['singlets-id'] == 'Repeated display name/Repeated display name'
    fields, header, table = export.wide_table(rows)
    assert len(table) == 1 and len(fields) == len(set(fields))
    assert table[0]['Repeated display name [2] | events'] == 3
    assert table[0]['All events | FL1-A median'] == pytest.approx(np.median([1, 4, 7, 10000, 500, -10000]))


def test_ancestor_percentage_and_annotations_reach_both_layouts(tmp_path):
    project, source = project_for(tmp_path)
    project.annotations = {source.id: {'Genotype': 'WT', 'Dose': '10'}}
    rows, _ = export.statistics_rows(project, [], [], ancestor_id='population-id')
    assert [r['percent_of_ancestor'] for r in rows] == [None, pytest.approx(100), pytest.approx(80)]
    assert all(r['statistic'] == '' for r in rows)
    target = tmp_path / 'long.csv'
    export.write_csv(target, *export.long_table(rows))
    with target.open(newline='') as stream:
        table = list(csv.DictReader(stream))
    assert list(table[0])[:3] == ['sample', 'Genotype', 'Dose'] and table[2]['Genotype'] == 'WT'
    assert table[0]['percent_of_ancestor'] == '' and float(table[2]['percent_of_ancestor']) == pytest.approx(80)
    fields, header, wide = export.wide_table(rows)
    assert [header[f] for f in fields[:3]] == ['sample', 'Genotype', 'Dose']
    assert wide[0]['Repeated display name/Repeated display name | % of ancestor'] == pytest.approx(80)


def test_events_per_microlitre_only_when_the_file_records_volume(tmp_path):
    project, _ = project_for(tmp_path, metadata={'vol': '3000'})
    rows, failures = export.statistics_rows(project, [], [])
    assert failures == []
    assert rows[0]['acquired_volume_ul'] == pytest.approx(3) and rows[0]['events_per_ul'] == pytest.approx(2)
    assert rows[1]['events_per_ul'] == pytest.approx(5 / 3)


def test_failed_sample_is_reported_and_never_written_as_zero(tmp_path):
    project, source = project_for(tmp_path)
    open(source.path, 'ab').write(b'changed')
    rows, failures = export.statistics_rows(project, ['FL1-A'], ['median'])
    assert rows == [] and failures[0]['sample'] == 'vectors.fcs' and 'changed' in failures[0]['reason']
    project, _ = project_for(tmp_path)
    _, failures = export.statistics_rows(project, ['Missing-A'], ['median'])
    assert 'Missing-A' in failures[0]['reason']


def test_control_threshold_uses_every_event_in_gate_coordinates(tmp_path):
    project, source = project_for(tmp_path)
    value, count = export.control_threshold(project, source, 'population-id', 'FL1-A', 75)
    assert (value, count) == (pytest.approx(np.percentile([1, 4, 7, 10000, 500], 75)), 5)
    value, count = export.control_threshold(project, source, None, 'FL1-A', 50, 'log')
    assert count == 5  # the negative event cannot sit on a log axis
    assert value == pytest.approx(np.percentile(np.log10([1, 4, 7, 10000, 500]), 50))
    with pytest.raises(ValueError, match='percentile'):
        export.control_threshold(project, source, None, 'FL1-A', 100)
    # The resulting one-sided gate is an ordinary range gate with identical membership.
    gate = RangeGate('positive', 'FL1-A+', 'population-id', 'FL1-A',
                     export.control_threshold(project, source, 'population-id', 'FL1-A', 50)[0], export.OPEN_BOUND)
    project.gates.append(gate)
    rows, _ = export.statistics_rows(project, [], [])
    assert next(r for r in rows if r['gate_id'] == 'positive')['event_count'] == 3


def test_event_export_writes_untransformed_values_of_the_population(tmp_path):
    project, source = project_for(tmp_path)
    target = tmp_path / 'events.csv'
    assert export.write_events(target, project, [source], 'singlets-id', ['FSC-A', 'FL1-A']) == 4
    with target.open(newline='') as stream:
        table = list(csv.reader(stream))
    assert table[0] == ['sample', 'FSC-A', 'FL1-A']
    assert [[float(v) for v in row[1:]] for row in table[1:]] == [[2, 1], [3, 4], [4, 7], [5, 10000]]
    assert export.write_events(tmp_path / 'all.csv', project, [source], None, ['FL1-A']) == 6


def test_provenance_and_methods_report_state_how_numbers_were_made(tmp_path):
    project, source = project_for(tmp_path)
    project.annotations = {source.id: {'Genotype': 'WT'}}
    moved = Polygon('population-id', 'Repeated display name', None, ('FSC-A', 'SSC-A'), ((0, 0), (9, 0), (9, 9), (0, 9)))
    project.set_override(source.id, moved)
    project.gates.append(RangeGate('positive', 'FL1-A+', 'population-id', 'FL1-A', 5., export.OPEN_BOUND))
    record = export.provenance(project, project_path='x.flowhero.json', channels=['FL1-A'], statistics=['median', 'p95'])
    json.dumps(record, allow_nan=False)
    assert record['samples'][0]['sha256'] == source.sha256
    assert [g['id'] for g in record['samples'][0]['sample_specific_gates']] == ['population-id']
    assert set(record['statistics']['definitions']) == {'median', 'percentile'}
    assert 'open' in next(g for g in record['gating']['shared_gates'] if g['id'] == 'positive')['note']
    text = export.methods_report(record)
    for expected in ('FlowHero', 'FlowKit', source.sha256, 'Genotype', 'WT', 'uncompensated',
                     'Sample-specific gate positions', 'FL1-A+', '**median**'):
        assert expected in text
    target = tmp_path / 'p.json'
    export.write_provenance(target, record)
    assert json.loads(target.read_text())['format'] == 'flowhero-provenance'


def test_annotation_table_matches_file_names_and_reports_unmatched_rows(tmp_path):
    project, source = project_for(tmp_path)
    table = tmp_path / 'notes.csv'
    table.write_text('file;Genotype;Dose\nVECTORS.fcs;WT;10\nabsent.fcs;KO;0\n', encoding='utf-8')
    annotations, unmatched = export.read_annotations(table, project.sources)
    assert annotations == {source.id: {'Genotype': 'WT', 'Dose': '10'}} and unmatched == ['absent.fcs']
    table.write_text('file,Genotype\nvectors,WT\n')
    assert export.read_annotations(table, project.sources)[0] == {source.id: {'Genotype': 'WT'}}
    for text, message in [('file,Genotype\nother.fcs,WT\n', 'No row matched'), ('file\nvectors.fcs\n', 'header row'),
                          ('file,A,A\nvectors.fcs,1,2\n', 'distinct'),
                          ('file,A\nvectors.fcs,1\nvectors,2\n', 'twice')]:
        table.write_text(text)
        with pytest.raises(ValueError, match=message):
            export.read_annotations(table, project.sources)


def test_annotations_persist_and_older_projects_still_open(tmp_path):
    project, source = project_for(tmp_path)
    project.annotations = {source.id: {'Genotype': 'WT'}}
    path = tmp_path / 'p.flowhero.json'
    save_project(project, path)
    assert load_project(path) == project
    document = json.loads(path.read_text())
    assert document['version'] == 10
    document['version'] = 7
    del document['project']['annotations']
    del document['project']['compensation']
    path.write_text(json.dumps(document))
    assert load_project(path).annotations == {}
    project.annotations = {'not-a-sample': {'Genotype': 'WT'}}
    with pytest.raises(ValueError, match='Annotations'):
        project.validate()


def test_file_information_lists_channels_keywords_and_spillover(tmp_path):
    spill = '2,FL1-A,SSC-H,1,0.1,0.2,1'
    project, source = project_for(tmp_path, metadata={'spillover': spill, 'p4v': '450'})
    information = export.fcs_information(source)
    assert information['event_count'] == 6 and [c['name'] for c in information['channels']] == ['FSC-A', 'SSC-A', 'SSC-H', 'FL1-A']
    assert information['channels'][3]['voltage'] == '450'
    assert information['spillover'] == {'channels': ['FL1-A', 'SSC-H'], 'values': [['1', '0.1'], ['0.2', '1']]}
    plain, source = project_for(tmp_path)
    assert export.fcs_information(source)['spillover'] is None


def test_headless_commands_write_table_provenance_and_report(tmp_path, capsys):
    from flowhero.commands import run
    project, source = project_for(tmp_path)
    path = tmp_path / 'p.flowhero.json'
    save_project(project, path)
    table = tmp_path / 'wide.csv'
    assert run(['export-statistics', str(path), '--output', str(table), '--layout', 'wide',
                '--channel', 'FL1-A', '--statistic', 'median', '--statistic', 'p90']) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer['outcome'] == 'ok' and answer['rows'] == 1
    with table.open(newline='') as stream:
        row = next(csv.DictReader(stream))
    assert float(row['Repeated display name | FL1-A p90']) == pytest.approx(np.percentile([1, 4, 7, 10000, 500], 90))
    assert json.loads((tmp_path / 'wide.csv.provenance.json').read_text())['statistics']['requested'] == ['median', 'p90']
    assert run(['export-statistics', str(path), '--output', str(table)]) == 1  # never overwrites
    capsys.readouterr()
    report = tmp_path / 'record.md'
    assert run(['methods-report', str(path), '--output', str(report)]) == 0
    assert source.sha256 in report.read_text()
