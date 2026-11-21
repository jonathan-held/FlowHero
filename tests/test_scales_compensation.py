"""0.9.0 analysis core: logicle scale, compensation, ellipse and combined gates.

Every expected count is recomputed here with NumPy from the same file.
"""
import hashlib
import json
from copy import deepcopy

import flowio
import numpy as np
import pytest

from flowhero import scales
from flowhero.analysis import evaluate, file_spillover
from flowhero.gate_import import prepare_gate_import, read_gate_import
from flowhero.model import (COMPENSATED_DOMAIN, DOMAIN, BooleanGate, Ellipse, PlotSpec, Polygon, Project,
                             RangeGate, Source, ordered_gates)
from flowhero.storage import export_gates, export_gatingml, load_project, save_project

CH = ['FSC-A', 'FSC-H', 'SSC-A', 'FL1-A', 'FL2-A', 'Time']
SPILL = np.array([[1, 0.2], [0.1, 1]])
MATRIX = {'source': 'matrix', 'channels': ['FL1-A', 'FL2-A'], 'values': SPILL.tolist()}
L = scales.logicle_scale()


def sample(tmp_path, name='a.fcs', spillover=True, seed=1, n=6000):
    rng = np.random.default_rng(seed)
    true = np.column_stack([rng.lognormal(6, 1, n), rng.lognormal(5, 1.2, n)])
    observed = true @ SPILL + rng.normal(0, 20, (n, 2))
    fsc = rng.normal(50000, 8000, n)
    events = np.column_stack([fsc, fsc * 0.8 + rng.normal(0, 500, n), rng.normal(30000, 6000, n),
                              observed[:, 0], observed[:, 1], np.sort(rng.uniform(0, 100, n))])
    path = tmp_path / name
    metadata = {'spillover': '2,FL1-A,FL2-A,1,0.2,0.1,1'} if spillover else None
    with path.open('wb') as stream:
        flowio.create_fcs(stream, events.flatten().tolist(), CH, metadata_dict=metadata)
    stored = np.array(events, dtype=np.float32).astype(float)
    return Source('s-' + name, str(path), hashlib.sha256(path.read_bytes()).hexdigest()), stored


def gates():
    return [Ellipse('e', 'Cells', None, ('FSC-A', 'SSC-A'), (50000., 30000.), ((12000., 3000.), (-2000., 9000.))),
            Polygon('lp', 'Logicle box', 'e', ('FL1-A', 'FL2-A'), ((0.3, 0.2), (0.8, 0.2), (0.8, 0.7), (0.3, 0.7)), (L, L)),
            RangeGate('r', 'Log range', 'e', 'FL1-A', 2.0, 3.0, ('log',)),
            BooleanGate('both', 'both', 'e', 'and', ('lp', 'r')),
            BooleanGate('either', 'either', 'e', 'or', ('lp', 'r')),
            BooleanGate('outside', 'not box', 'e', 'not', ('lp',)),
            RangeGate('child', 'child', 'outside', 'FL2-A', 0.3, 0.6, (L,))]


def expected_counts(events, compensated):
    e = events.copy()
    if compensated:
        e[:, 3:5] = e[:, 3:5] @ np.linalg.inv(SPILL)
    centre, axes = np.array([50000., 30000.]), np.array([[12000., -2000.], [3000., 9000.]])
    unit = np.linalg.solve(axes, (e[:, [0, 2]] - centre).T).T
    cells = (unit ** 2).sum(axis=1) <= 1
    x, y = scales.forward(e[:, 3], L), scales.forward(e[:, 4], L)
    box = cells & (x >= .3) & (x <= .8) & (y >= .2) & (y <= .7)
    with np.errstate(all='ignore'):
        logged = np.log10(e[:, 3])
    ranged = cells & (e[:, 3] > 0) & (logged >= 2) & (logged < 3)
    return {'e': cells.sum(), 'lp': box.sum(), 'r': ranged.sum(), 'both': (box & ranged).sum(),
            'either': (box | ranged).sum(), 'outside': (cells & ~box).sum(),
            'child': (cells & ~box & (y >= .3) & (y < .6)).sum()}, e, box


def counts(result):
    return {row['gate_id']: row['event_count'] for row in result['rows']
            if row['channel'] == 'FL1-A' and row['statistic'] == 'median'}


def test_logicle_scale_text_round_trip_and_rejects_bad_parameters():
    assert L == 'logicle:262144:0.5:4.5:0' and scales.parse(L) == ('logicle', (262144.0, 0.5, 4.5, 0.0))
    values = np.array([-5000., -10., 0., 3., 1e3, 262144., 1e7])
    assert np.allclose(scales.inverse(scales.forward(values, L), L), values, rtol=1e-9, atol=1e-6)
    assert scales.forward(np.array([262144.]), L)[0] == pytest.approx(1.0)
    assert np.isnan(scales.forward(np.array([0., -1.]), 'log')).all()
    assert np.allclose(scales.convert(np.array([2.0]), 'log', 'linear'), [100.0])
    for bad in ('logicle', 'logicle:1:2:3', 'logicle:262144:3:4.5:0', 'logicle:-1:0.5:4.5:0',
                'logicle:262144.0:0.5:4.5:0', 'asinh'):
        assert not scales.valid(bad)
    major, minor = scales.logicle_ticks(L, -0.05, 1.05)
    assert [label for _, label in major][-1] == '10⁵' and any(label == '0' for _, label in major) and minor


@pytest.mark.parametrize('compensation', [None, {'source': 'file'}, MATRIX])
def test_new_gates_and_compensation_match_numpy(tmp_path, compensation):
    source, events = sample(tmp_path)
    plots = [PlotSpec('p', 'density', 'FL1-A', 'FL2-A', 'e', x_scale=L, y_scale=L),
             PlotSpec('h', 'histogram', 'FL1-A', x_scale=L)]
    result = evaluate(source, gates(), 'FL1-A', plot_specs=plots, compensation=compensation, highlight_id='lp')
    expected, values, box = expected_counts(events, compensation is not None)
    assert counts(result) == {**{k: int(v) for k, v in expected.items()}, 'root': len(events)}
    median = next(r['value'] for r in result['rows'] if r['gate_id'] == 'lp' and r['channel'] == 'FL1-A' and r['statistic'] == 'median')
    assert median == pytest.approx(float(np.median(values[box, 3])), rel=1e-9)
    assert {r['domain'] for r in result['rows']} == {COMPENSATED_DOMAIN if compensation else DOMAIN}
    assert result['compensated_channels'] == (['FL1-A', 'FL2-A'] if compensation else [])
    assert result['highlights']['p']['event_count'] == expected['lp'] and result['highlight_id'] == 'lp'
    histogram = result['sheet_plots']['h']
    assert histogram['nonpositive'] == 0 and sum(histogram['counts']) + histogram['outside'] == len(events)


def test_compensation_errors_are_explained(tmp_path):
    source, _ = sample(tmp_path, spillover=False)
    with pytest.raises(ValueError, match='stores no readable spillover'):
        evaluate(source, [], 'FL1-A', compensation={'source': 'file'})
    with pytest.raises(ValueError, match='lacks channel'):
        evaluate(source, [], 'FL1-A', compensation={'source': 'matrix', 'channels': ['FL1-A', 'PE-A'], 'values': SPILL.tolist()})
    with pytest.raises(ValueError, match='scatter or time'):
        evaluate(source, [], 'FL1-A', compensation={'source': 'matrix', 'channels': ['FSC-A', 'FL1-A'], 'values': SPILL.tolist()})
    for bad in ({'source': 'matrix', 'channels': ['a', 'b'], 'values': [[1, 2], [2, 4]]},
                {'source': 'matrix', 'channels': ['a', 'a'], 'values': [[1, 0], [0, 1]]},
                {'source': 'matrix', 'channels': ['a', 'b'], 'values': [[1, 0]]}, {'source': 'other'}):
        with pytest.raises(ValueError):
            Project(compensation=bad, domain=COMPENSATED_DOMAIN).validate()
    with pytest.raises(ValueError, match='domain'):
        Project(compensation=MATRIX).validate()


def test_file_spillover_is_read_as_stored(tmp_path):
    import flowkit as fk
    source, _ = sample(tmp_path)
    assert file_spillover(fk.Sample(source.path)) == MATRIX
    plain, _ = sample(tmp_path, 'plain.fcs', spillover=False)
    assert file_spillover(fk.Sample(plain.path)) is None


def test_model_rules_for_ellipse_and_combined_gates():
    with pytest.raises(ValueError, match='no area'):
        Ellipse('e', 'E', None, ('a', 'b'), (0, 0), ((1, 1), (2, 2)))
    with pytest.raises(ValueError, match='NOT takes one'):
        BooleanGate('b', 'B', None, 'not', ('x', 'y'))
    with pytest.raises(ValueError, match='two or more'):
        BooleanGate('b', 'B', None, 'and', ('x',))
    a = RangeGate('a', 'A', None, 'c', 0, 1)
    child = RangeGate('k', 'K', 'a', 'c', 0, 1)
    with pytest.raises(ValueError, match='own parent'):
        ordered_gates([a, child, BooleanGate('b', 'B', None, 'not', ('k',))])
    with pytest.raises(ValueError, match='cycle'):
        ordered_gates([BooleanGate('b1', 'B1', None, 'not', ('b2',)), BooleanGate('b2', 'B2', None, 'not', ('b1',))])
    listed = ordered_gates([BooleanGate('b', 'B', None, 'not', ('a',)), a])
    assert [g.id for g in listed] == ['a', 'b']
    with pytest.raises(ValueError, match='count axis'):
        PlotSpec('p', 'histogram', 'c', y_scale=L)
    with pytest.raises(ValueError, match='overlay'):
        PlotSpec('p', 'scatter', 'c', 'd', sample_ids=('s',))


def test_project_gate_json_and_gatingml_round_trip(tmp_path):
    source, events = sample(tmp_path)
    project = Project(sources=[source], gates=gates(), channel='FL1-A', compensation=deepcopy(MATRIX),
                      domain=COMPENSATED_DOMAIN,
                      plots=[PlotSpec('p', 'scatter', 'FL1-A', 'FL2-A', x_scale=L, overlay=True, sample_ids=(source.id,)),
                             PlotSpec('h', 'histogram', 'FL1-A', stagger=True)])
    path = tmp_path / 'p.flowhero.json'
    save_project(project, path)
    assert load_project(path) == project and json.loads(path.read_text())['version'] == 10
    export_gates(project, tmp_path / 'g.json')
    assert json.loads((tmp_path / 'g.json').read_text())['version'] == 2
    document = read_gate_import(tmp_path / 'g.json')
    assert document.domain == COMPENSATED_DOMAIN and any('COMPENSATED' in note for note in document.notes)
    imported = prepare_gate_import(Project(sources=[source]), document)
    assert [type(g) for g in imported.gates] == [type(g) for g in ordered_gates(gates())]
    result = evaluate(source, imported.gates, 'FL1-A', compensation=MATRIX)
    assert sorted(counts(result).values()) == sorted(int(v) for v in expected_counts(events, True)[0].values())
    # Gating-ML: geometry, logicle transform and the matrix are written; ellipse and logicle come back.
    export_gatingml(project, tmp_path / 'g.xml')
    text = (tmp_path / 'g.xml').read_text()
    assert 'EllipsoidGate' in text and 'BooleanGate' in text and 'spectrumMatrix' in text and 'logicle' in text
    plain = Project(sources=[source], gates=gates()[:2], channel='FL1-A')
    export_gatingml(plain, tmp_path / 'plain.xml')
    back = read_gate_import(tmp_path / 'plain.xml').variants[None]
    assert isinstance(back[0], Ellipse) and back[1].scales == (L, L)
    again = evaluate(source, list(back), 'FL1-A')
    first = evaluate(source, gates()[:2], 'FL1-A')
    assert sorted(counts(again).values()) == sorted(counts(first).values())


def test_gate_files_without_new_features_stay_version_one(tmp_path):
    project = Project(gates=[RangeGate('a', 'A', None, 'c', 0, 1)])
    export_gates(project, tmp_path / 'g.json')
    assert json.loads((tmp_path / 'g.json').read_text())['version'] == 1


def test_file_compensation_is_written_as_fcs_reference(tmp_path):
    project = Project(gates=[RangeGate('a', 'A', None, 'FL1-A', 0, 1)], compensation={'source': 'file'},
                      domain=COMPENSATED_DOMAIN)
    with pytest.raises(ValueError, match='Calculate the selected sample'):
        export_gatingml(project, tmp_path / 'g.xml')
    export_gatingml(project, tmp_path / 'g.xml', None, ['FL1-A', 'FL2-A'])
    assert 'compensation-ref="FCS"' in (tmp_path / 'g.xml').read_text()
