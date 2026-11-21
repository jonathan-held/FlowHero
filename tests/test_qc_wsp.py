"""Singlet band, time course, spillover tables and FlowJo workspace import."""
from copy import deepcopy

import numpy as np
import pytest

from flowhero.analysis import build_strategy, evaluate
from flowhero.export import read_spillover_csv, write_spillover_csv
from flowhero.gate_import import prepare_gate_import, read_gate_import
from flowhero.model import COMPENSATED_DOMAIN, Polygon, Project, RangeGate, Rectangle
from flowhero.qc import singlet_polygon, time_course
from test_scales_compensation import L, MATRIX, counts, sample


def test_singlet_band_numbers():
    rng = np.random.default_rng(3)
    area = np.concatenate([rng.normal(90000, 12000, 9000), rng.normal(170000, 15000, 1000)])
    height = np.concatenate([area[:9000] * 0.85 + rng.normal(0, 2000, 9000), area[9000:] * 0.5 + rng.normal(0, 3000, 1000)])
    vertices, d = singlet_polygon(area, height)
    assert d['median_ratio'] == pytest.approx(0.85, abs=0.01) and d['events'] == 10000
    ratio = height / area
    inside = (ratio >= d['ratio_low']) & (ratio <= d['ratio_high'])
    assert inside[:9000].mean() > 0.97 and inside[9000:].mean() < 0.02
    (a0, h0), (a1, h1), (a2, h2), (a3, h3) = vertices
    assert a0 == a3 and a1 == a2 and h0 / a0 == pytest.approx(d['ratio_low']) and h2 / a2 == pytest.approx(d['ratio_high'])
    with pytest.raises(ValueError, match='at least 100'):
        singlet_polygon(area[:50], height[:50])


def test_time_course_flags_a_burst_and_reports_medians():
    rng = np.random.default_rng(4)
    time = np.sort(np.concatenate([rng.uniform(0, 600, 30000), rng.uniform(200, 203, 600)]))
    signal = rng.normal(100, 10, len(time))
    result = time_course(time, signal)
    flagged = [i for i, f in enumerate(result['flagged']) if f]
    assert flagged == [33] and sum(result['counts']) == len(time) == result['events']
    assert result['flagged_events'] == result['counts'][33]
    assert all(90 < m < 110 for m in result['signal_medians'])
    steady = time_course(np.linspace(0, 100, 20000))
    assert not any(steady['flagged']) and steady['signal_medians'] is None
    with pytest.raises(ValueError, match='Too few'):
        time_course(np.ones(500))


def test_spillover_table_round_trip_and_checks(tmp_path):
    path = tmp_path / 'spill.csv'
    write_spillover_csv(path, MATRIX)
    assert read_spillover_csv(path) == MATRIX
    (tmp_path / 'bare.csv').write_text('FL1-A;FL2-A\n1;0,2\n'.replace('0,2', '0.2') + '0.1;1\n')
    assert read_spillover_csv(tmp_path / 'bare.csv') == MATRIX
    (tmp_path / 'bad.csv').write_text(',A,B\nA,1,x\nB,0,1\n')
    with pytest.raises(ValueError, match='not a number'):
        read_spillover_csv(tmp_path / 'bad.csv')
    (tmp_path / 'order.csv').write_text(',A,B\nB,1,0\nA,0,1\n')
    with pytest.raises(ValueError, match='same order'):
        read_spillover_csv(tmp_path / 'order.csv')


def workspace(tmp_path, source, gates, with_matrix=True):
    """A FlowJo-format workspace written by FlowKit, with the matrix where FlowJo stores it."""
    import flowkit as fk
    from flowkit._utils.wsp_utils import export_flowjo_wsp
    from lxml import etree
    matrix = fk.Matrix(np.array(MATRIX['values']), list(MATRIX['channels'])) if with_matrix else None
    strategy, _ = build_strategy(gates, matrix)
    path = tmp_path / 'test.wsp'
    with path.open('wb') as stream:
        export_flowjo_wsp(strategy, 'group', [fk.Sample(source.path, sample_id='a.fcs')], stream)
    if with_matrix:
        tree = etree.parse(str(path))
        root = tree.getroot()
        root.find('SampleList').find('Sample').insert(0, deepcopy(root.find('Matrices')[0]))
        tree.write(str(path))
    return path


def test_flowjo_workspace_gates_and_matrix_reproduce_counts(tmp_path):
    source, _ = sample(tmp_path)
    gates = [Polygon('cells', 'Cells', None, ('FSC-A', 'SSC-A'), ((35000., 18000.), (65000., 18000.), (65000., 42000.), (35000., 42000.))),
             Polygon('pos', 'Pos', 'cells', ('FL1-A', 'FL2-A'), ((0.3, 0.2), (0.8, 0.25), (0.8, 0.7), (0.3, 0.7)), (L, L)),
             Rectangle('rect', 'Rect', 'cells', ('FL1-A', 'FL2-A'), (0.4, 0.3), (0.9, 0.6), (L, L))]
    reference = counts(evaluate(source, gates, 'FL1-A', compensation=MATRIX))
    document = read_gate_import(workspace(tmp_path, source, gates))
    assert document.source_format == 'flowjo-wsp' and list(document.variants) == ['a.fcs']
    assert document.compensation['a.fcs'] == MATRIX and any('COMPENSATED' in note for note in document.notes)
    candidate = prepare_gate_import(Project(sources=[source]), document, variant='a.fcs', use_compensation=True)
    assert candidate.compensation == MATRIX and candidate.domain == COMPENSATED_DOMAIN
    assert candidate.gate_imports[0]['source_format'] == 'flowjo-wsp'
    candidate.validate()
    result = counts(evaluate(source, candidate.gates, 'FL1-A', compensation=candidate.compensation))
    assert sorted(result.values()) == sorted(reference.values())
    plain = prepare_gate_import(Project(sources=[source]), document, variant='a.fcs')
    assert plain.compensation is None


def test_flowjo_biex_axes_are_redrawn_on_logicle(tmp_path):
    """A polygon on FlowJo biex axes keeps its events after conversion (to edge tolerance)."""
    import flowkit as fk
    from flowkit._models.transforms._wsp_transforms import WSPBiexTransform
    from flowhero.wsp_import import _convert_gate
    source, events = sample(tmp_path)
    biex = WSPBiexTransform(negative=0, width=-10, positive=4.42, max_value=262144.0)
    box = np.array([[30., 20.], [3000., 20.], [3000., 2500.], [30., 2500.]])
    vertices = np.column_stack([biex.apply(box[:, [0]])[:, 0], biex.apply(box[:, [1]])[:, 0]])
    dimensions = [fk.Dimension('FL1-A', None, 'x'), fk.Dimension('FL2-A', None, 'y')]
    engine = fk.gates.PolygonGate('g', dimensions, vertices.tolist())
    gate, exact = _convert_gate(engine, 'g', 'G', None, {'x': biex, 'y': biex})
    assert not exact and gate.scales == (L, L) and len(gate.vertices) == 64
    result = counts(evaluate(source, [gate], 'FL1-A'))
    inside = (events[:, 3] >= 30) & (events[:, 3] <= 3000) & (events[:, 4] >= 20) & (events[:, 4] <= 2500)
    assert abs(result['g'] - int(inside.sum())) <= 0.002 * len(events)


def test_flowjo_unsupported_gates_are_named_not_guessed(tmp_path):
    import flowkit as fk
    from flowhero.wsp_import import _sample_gates
    strategy = fk.GatingStrategy()
    dims = [fk.Dimension('FSC-A', None, None, range_min=1, range_max=5)]
    strategy.add_gate(fk.gates.RectangleGate('keep', dims), ('root',))
    strategy.add_gate(fk.gates.RectangleGate('inverted', dims, use_complement=True), ('root',))
    strategy.add_gate(fk.gates.RectangleGate('orphan', dims), ('root', 'inverted'))
    strategy.add_gate(fk.gates.RectangleGate('open', [fk.Dimension('SSC-A', None, None, range_min=2)]), ('root',))
    gates, skipped, inexact = _sample_gates(strategy)
    assert [g.name for g in gates] == ['keep', 'open'] and not inexact
    assert isinstance(gates[1], RangeGate) and gates[1].maximum == 1e12
    assert len(skipped) == 2 and 'complement' in skipped[0] and 'parent was skipped' in skipped[1]
