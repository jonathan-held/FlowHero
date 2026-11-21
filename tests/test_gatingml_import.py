"""Real exported XML and FCS inputs through the supported import boundary."""
from dataclasses import replace
import io

import flowio
import flowkit as fk
from lxml import etree
import numpy as np
import pytest

from flowhero.analysis import build_strategy, evaluate
from flowhero.gatingml_import import DATATYPES, G, GATING, T, TRANSFORMS, parse_gatingml
from flowhero.model import Polygon, Project, RangeGate, Rectangle
from flowhero.storage import bind_source, export_gatingml
from test_analysis import source_file


def xml_bytes(children):
    return (f'<g:Gating-ML xmlns:g="{GATING}" xmlns:d="{DATATYPES}" xmlns:t="{TRANSFORMS}">'
            f'{children}</g:Gating-ML>').encode()


def polygon_xml(identity='Population', parent='', compensation='uncompensated', transform='', x='FSC-A', y='SSC-A'):
    parent_attribute = f' g:parent_id="{parent}"' if parent else ''
    transformation = f' g:transformation-ref="{transform}"' if transform else ''
    return f'''<g:PolygonGate g:id="{identity}"{parent_attribute}>
      <g:dimension g:compensation-ref="{compensation}"{transformation}><d:fcs-dimension d:name="{x}"/></g:dimension>
      <g:dimension g:compensation-ref="{compensation}"{transformation}><d:fcs-dimension d:name="{y}"/></g:dimension>
      <g:vertex><g:coordinate d:value="0"/><g:coordinate d:value="0"/></g:vertex>
      <g:vertex><g:coordinate d:value="10"/><g:coordinate d:value="0"/></g:vertex>
      <g:vertex><g:coordinate d:value="10"/><g:coordinate d:value="10"/></g:vertex>
      <g:vertex><g:coordinate d:value="0"/><g:coordinate d:value="10"/></g:vertex>
    </g:PolygonGate>'''


def test_exported_names_identity_transforms_and_hierarchy_preserved():
    # Some Gating-ML exports uses spaces in xs:ID/IDREF values.
    # This small public fixture reproduces that representation, not lab geometry.
    contents = xml_bytes(
        polygon_xml('Single Cells', 'U937', 'FCS', 'Scatter Identity', 'SSC-A', 'SSC-H') +
        polygon_xml('U937', compensation='FCS', transform='Scatter Identity') +
        '<t:transformation t:id="Scatter Identity"><t:flin t:T="1" t:A="0"/></t:transformation>')
    with pytest.raises(ValueError):
        fk.parse_gating_xml(io.BytesIO(contents))
    gates, notes = parse_gatingml(contents)
    assert [(g.id, g.name, g.parent_id, g.channels, g.scales) for g in gates] == [
        ('U937', 'U937', None, ('FSC-A', 'SSC-A'), ('linear', 'linear')),
        ('Single Cells', 'Single Cells', 'U937', ('SSC-A', 'SSC-H'), ('linear', 'linear'))]
    assert gates[1].vertices == ((0, 0), (10, 0), (10, 10), (0, 10))
    assert any('FCS compensation references' in note for note in notes)


def test_export_import_roundtrip_preserves_geometry_and_full_event_results(tmp_path):
    gates = [
        Polygon('population', 'Separate display label', None, ('FSC-A', 'SSC-A'),
                ((0, 0), (1, 0), (1, 1), (0, 1)), ('log', 'log')),
        Rectangle('quadrant', 'Quadrant label', 'population', ('SSC-A', 'SSC-H'),
                  (None, 0), (6, None), group_id='group'),
        RangeGate('viable', 'Viability label', 'quadrant', 'FL1-A', 0, 10),
    ]
    path = tmp_path / 'strategy.gatingml'
    export_gatingml(Project(gates=gates), path)
    imported, notes = parse_gatingml(path.read_bytes())
    assert imported == [replace(g, name=g.id, **({'group_id': None} if isinstance(g, Rectangle) else {})) for g in gates]
    assert any('identifiers are used as names' in note for note in notes)
    source = source_file(tmp_path, [[2, 2, 2, 1], [3, 3, 3, 4], [7, 7, 7, 500], [20, 2, 2, -10000]])
    before = evaluate(source, gates, 'FL1-A')['rows']
    after = evaluate(source, imported, 'FL1-A')['rows']
    project_rows = lambda rows: [(r['gate_id'], r['event_count'], r['value'], r['status']) for r in rows]
    assert project_rows(before) == project_rows(after)
    assert [row['event_count'] for row in after] == [3, 2, 2]


def test_scatter_fcs_reference_agrees_with_engine_even_with_nonidentity_spillover(tmp_path):
    path = tmp_path / 'spill.fcs'
    events = [[2, 3, 4, 100, 50], [20, 3, 4, 20, 150], [4, 5, 6, 60, 80]]
    channels = ['FSC-A', 'SSC-A', 'SSC-H', 'FL1-A', 'FL2-A']
    spill = '2,FL1-A,FL2-A,1,0.2,0.1,1'
    with path.open('wb') as stream:
        flowio.create_fcs(stream, np.asarray(events).flatten().tolist(), channels, metadata_dict={'spillover': spill})
    sample = fk.Sample(str(path))
    matrix = fk.Matrix(spill, ['FL1-A', 'FL2-A'])
    compensated = matrix.apply(sample)
    raw = sample.get_events(source='raw')
    assert np.array_equal(compensated[:, :3], raw[:, :3])
    assert not np.array_equal(compensated[:, 3:], raw[:, 3:])
    contents = xml_bytes(polygon_xml(compensation='FCS'))
    reference = fk.parse_gating_xml(io.BytesIO(contents)).gate_sample(sample).get_gate_membership('Population', ('root',))
    imported, _ = parse_gatingml(contents)
    restored, _ = build_strategy(imported)
    actual = restored.gate_sample(sample).get_gate_membership('Population', ('root',))
    assert np.array_equal(actual, reference)
    assert actual.tolist() == [True, False, True]
    assert evaluate(bind_source(str(path)), imported, 'FL1-A')['rows'][0]['value'] == 80


@pytest.mark.parametrize('children, error', [
    (polygon_xml() + polygon_xml(), 'Duplicate'),
    (polygon_xml(parent='missing'), 'Missing'),
    (polygon_xml('a', 'b') + polygon_xml('b', 'a'), 'hierarchy'),
    (polygon_xml(transform='missing'), 'Missing'),
    (polygon_xml(compensation='matrix-1'), 'named compensation'),
    (polygon_xml(compensation='FCS', x='FL1-A'), 'FCS compensation'),
    (polygon_xml().replace('<g:PolygonGate ', '<g:PolygonGate inside="false" '), 'Complement'),
    (polygon_xml().replace('d:value="10"', 'd:value="NaN"'), 'finite'),
    (polygon_xml().replace('<g:dimension ', '<g:dimension g:unknown="yes" ', 1), 'attribute'),
    (polygon_xml().replace('<d:fcs-dimension ', '<d:new-dimension '), 'element'),
    (polygon_xml().replace('<g:PolygonGate', '<g:EllipsoidGate').replace('</g:PolygonGate>', '</g:EllipsoidGate>'), 'Invalid Gating-ML|element'),
    (polygon_xml(transform='scaled') + '<t:transformation t:id="scaled"><t:flin t:T="262144" t:A="0"/></t:transformation>', 'transformation'),
    (polygon_xml(transform='scaled') + '<t:transformation t:id="scaled"><t:flog t:T="262144" t:M="4.5"/></t:transformation>', 'transformation'),
    (polygon_xml() + '<t:transformation t:id="unused"><t:hyperlog t:T="262144" t:W="0.5" t:M="4.5" t:A="0"/></t:transformation>', 'element'),
])
def test_unsupported_or_malformed_xml_rejects_complete_strategy(children, error):
    with pytest.raises(ValueError, match=error):
        parse_gatingml(xml_bytes(children))


def test_mixed_fcs_scatter_and_uncompensated_fluorescence_rejected():
    # Pinned FlowKit compensates the entire gate data array when any dimension
    # refers to FCS. A raw fluorescence axis in the same gate is not equivalent.
    contents = xml_bytes(polygon_xml(compensation='FCS', y='FL1-A'))
    root = etree.fromstring(contents)
    root.find(G + 'PolygonGate').findall(G + 'dimension')[1].set(G + 'compensation-ref', 'uncompensated')
    with pytest.raises(ValueError, match='all FSC-/SSC-'):
        parse_gatingml(etree.tostring(root))


@pytest.mark.parametrize('contents', [b'not xml', b'<Gating-ML/>',
    b'<!DOCTYPE x [<!ENTITY leak SYSTEM "file:///etc/passwd">]><x>&leak;</x>'])
def test_wrong_format_and_entities_are_rejected(contents):
    with pytest.raises(ValueError):
        parse_gatingml(contents)


def test_default_namespace_and_open_two_dimensional_bounds(tmp_path):
    gate = Rectangle('open quadrant', 'open quadrant', None, ('FSC-A', 'SSC-A'), (0, None), (None, 10))
    path = tmp_path / 'quadrant.xml'
    export_gatingml(Project(gates=[gate]), path)
    root = etree.fromstring(path.read_bytes())
    default_root = etree.Element(G + 'Gating-ML', nsmap={None: GATING, 'd': DATATYPES, 't': TRANSFORMS})
    default_root.extend(root)
    imported, _ = parse_gatingml(etree.tostring(default_root))
    assert imported == [gate]


def test_open_range_or_three_dimensional_box_rejected():
    dimension = '<g:dimension g:compensation-ref="uncompensated" g:min="0"><d:fcs-dimension d:name="FSC-A"/></g:dimension>'
    with pytest.raises(ValueError, match='both lower and upper'):
        parse_gatingml(xml_bytes(f'<g:RectangleGate g:id="open">{dimension}</g:RectangleGate>'))
    with pytest.raises(ValueError, match='one/two-dimensional'):
        parse_gatingml(xml_bytes(f'<g:RectangleGate g:id="box">{dimension * 3}</g:RectangleGate>'))
