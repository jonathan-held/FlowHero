"""Gating-ML's supported geometry projected into FlowHero's uncompensated domain.

Validate XML and references once here. Temporary XML-name aliases accommodate
Exported IDs with spaces; aliases never become project gate identities.
"""
from copy import deepcopy
import io

from lxml import etree

from .model import Ellipse, Polygon, RangeGate, Rectangle, ordered_gates
from . import scales as scale_rules

GATING = 'http://www.isac-net.org/std/Gating-ML/v2.0/gating'
DATATYPES = 'http://www.isac-net.org/std/Gating-ML/v2.0/datatypes'
TRANSFORMS = 'http://www.isac-net.org/std/Gating-ML/v2.0/transformations'
XSI = 'http://www.w3.org/2001/XMLSchema-instance'
G, D, T = (f'{{{namespace}}}' for namespace in (GATING, DATATYPES, TRANSFORMS))


def _element_label(element):
    return etree.QName(element).localname


def _check_content(root):
    """Reject semantics FlowKit's parser otherwise ignores, before projection."""
    attributes = {
        G + 'Gating-ML': {f'{{{XSI}}}schemaLocation'},
        G + 'PolygonGate': {G + 'id', G + 'parent_id'},
        G + 'RectangleGate': {G + 'id', G + 'parent_id'},
        G + 'EllipsoidGate': {G + 'id', G + 'parent_id'},
        G + 'mean': set(),
        G + 'covarianceMatrix': set(),
        G + 'row': set(),
        G + 'entry': {D + 'value'},
        G + 'distanceSquare': {D + 'value'},
        T + 'logicle': {T + 'T', T + 'W', T + 'M', T + 'A'},
        G + 'dimension': {G + 'compensation-ref', G + 'transformation-ref', G + 'min', G + 'max'},
        G + 'vertex': set(),
        G + 'coordinate': {D + 'value'},
        D + 'fcs-dimension': {D + 'name'},
        T + 'transformation': {T + 'id'},
        T + 'flin': {T + 'T', T + 'A'},
        T + 'flog': {T + 'T', T + 'M'},
    }
    for element in root.iter():
        allowed = attributes.get(element.tag)
        if allowed is None:
            raise ValueError(f'Unsupported Gating-ML element: {_element_label(element)}. '
                             'Import supports polygons, ellipses, one/two-dimensional rectangles and identity/log10/logicle transforms.')
        for name, value in element.attrib.items():
            # Some exporters supply a nonstandard inside flag. A false flag is
            # a complement, which the geometry model cannot represent.
            if etree.QName(name).localname == 'inside':
                if value not in ('true', '1'):
                    raise ValueError('Complement/outside gates (inside=false) are not supported.')
                del element.attrib[name]
            elif name not in allowed:
                raise ValueError(f'Unsupported Gating-ML attribute: {_element_label(element)} / '
                                 f'{etree.QName(name).localname}. Nothing was imported.')
        if element.text and element.text.strip():
            raise ValueError(f'Unexpected text in Gating-ML {_element_label(element)}.')
        if element.tail and element.tail.strip():
            raise ValueError('Unexpected text between Gating-ML elements.')
        # The polygon algorithm does not support per-dimension clipping bounds.
        if element.tag == G + 'dimension' and element.getparent().tag == G + 'PolygonGate':
            if G + 'min' in element.attrib or G + 'max' in element.attrib:
                raise ValueError('Polygon dimension bounds are not supported; export polygon vertices without clipping bounds.')


def _alias_identifiers(root):
    """Resolve references to their owners and use XML-safe IDs only internally."""
    gates, transforms, all_ids = {}, {}, set()
    for element in root:
        owners = transforms if element.tag == T + 'transformation' else gates
        attribute = T + 'id' if owners is transforms else G + 'id'
        identifier = element.get(attribute)
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError('Every imported gate and transformation needs a nonempty identifier.')
        if identifier in all_ids:
            raise ValueError(f'Duplicate Gating-ML identifier: {identifier}')
        all_ids.add(identifier)
        owners[identifier] = f'fp_import_{len(all_ids)}'
        element.set(attribute, owners[identifier])
    if not gates:
        raise ValueError('Gating-ML contains no supported gates.')
    for element in root.iter():
        for attribute, owners in ((G + 'parent_id', gates), (G + 'transformation-ref', transforms)):
            reference = element.get(attribute)
            if reference is not None:
                if reference not in owners:
                    raise ValueError(f'Missing Gating-ML reference: {reference}')
                element.set(attribute, owners[reference])
    return {alias: original for original, alias in gates.items()}


def ellipse_axes(covariance, distance_square):
    """Perpendicular half-axes of the ellipse (x-c)' C^-1 (x-c) = d^2."""
    import numpy as np
    matrix = np.asarray(covariance, dtype=float) * float(distance_square)
    if matrix.shape != (2, 2) or not np.isfinite(matrix).all() or not np.allclose(matrix, matrix.T):
        raise ValueError('An ellipse needs a symmetric 2 x 2 covariance matrix')
    values, vectors = np.linalg.eigh(matrix)
    if values.min() <= 0:
        raise ValueError('The ellipse covariance matrix is not positive definite')
    return tuple(tuple(float(v) for v in vectors[:, i] * np.sqrt(values[i])) for i in (1, 0))


def parse_gatingml(contents: bytes) -> tuple[list[Polygon | Rectangle | RangeGate], tuple[str, ...]]:
    """Parse a supported Gating-ML strategy, or reject the complete import.

    No entity/DTD resolution or network loading is permitted. Schema validation
    and FlowKit parsing use only its bundled local Gating-ML 2.0 definitions.
    """
    import flowkit as fk

    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True,
                             remove_comments=True, remove_pis=True)
    try:
        document = etree.parse(io.BytesIO(contents), parser)
    except (etree.XMLSyntaxError, ValueError, TypeError) as exc:
        raise ValueError(f'Cannot read Gating-ML XML: {exc}') from exc
    if document.docinfo.doctype:
        raise ValueError('Gating-ML DTD/entity declarations are not supported. Export XML without a DTD.')
    original_root = document.getroot()
    if original_root.tag != G + 'Gating-ML':
        raise ValueError('Expected a Gating-ML 2.0 document. FlowJo workspaces and FEY files are not supported.')
    # Canonical namespace prefixes also allow otherwise valid default-namespace
    # Gating-ML through FlowKit's prefix-based XML adapter.
    root = etree.Element(G + 'Gating-ML', nsmap={'gating': GATING, 'data-type': DATATYPES, 'transforms': TRANSFORMS})
    root.attrib.update(original_root.attrib)
    root.text = original_root.text
    root.extend(deepcopy(child) for child in original_root)
    _check_content(root)
    original_ids = _alias_identifiers(root)
    try:
        strategy = fk.parse_gating_xml(io.BytesIO(etree.tostring(root)))
    except Exception as exc:
        # FlowKit exposes schema, graph and parser errors through several types.
        # Preserve the concrete reason while rejecting the whole strategy.
        raise ValueError(f'Invalid Gating-ML structure or hierarchy: {exc}') from exc

    scales = {}
    for identity, transform in strategy.transformations.items():
        if type(transform) is fk.transforms.LinearTransform and transform.param_t == 1 and transform.param_a == 0:
            scales[identity] = 'linear'
        elif type(transform) is fk.transforms.LogTransform and transform.param_t == 10 and transform.param_m == 1:
            scales[identity] = 'log'
        elif type(transform) is fk.transforms.LogicleTransform:
            try:
                scales[identity] = scale_rules.logicle_scale(transform.param_t, transform.param_w,
                                                             transform.param_m, transform.param_a)
            except ValueError as exc:
                raise ValueError(f'Unsupported logicle parameters in {identity}: {exc}') from exc
        else:
            raise ValueError('Unsupported Gating-ML transformation. Export identity linear, exact log10 or logicle '
                             'coordinates; other transformed domains cannot be imported.')

    notes = ['Gating-ML gate identifiers are used as names; separate display labels and quadrant-editing groups are not encoded.']
    gates = []
    normalized_fcs = False
    for alias, path in strategy.get_gate_ids():
        engine_gate = strategy.get_gate(alias, path)
        dimensions = engine_gate.dimensions
        identity = original_ids[alias]
        parent = original_ids[path[-1]] if path[-1] != 'root' else None
        if any(type(dimension) is not fk.Dimension for dimension in dimensions):
            raise ValueError(f'Gate {identity}: ratio/derived dimensions are not supported.')
        channels = tuple(dimension.id for dimension in dimensions)
        if any(dimension.compensation_ref == 'FCS' for dimension in dimensions):
            if not all(channel.lower()[:4] in ('fsc-', 'ssc-') for channel in channels):
                raise ValueError(f'Gate {identity}: FCS compensation is supported only for gates whose dimensions are all FSC-/SSC- scatter channels. '
                                 'Export an uncompensated strategy; compensation of fluorescence channels is not supported.')
            normalized_fcs = True
        for dimension in dimensions:
            if dimension.compensation_ref not in (None, 'uncompensated', 'FCS'):
                raise ValueError(f'Gate {identity}: named compensation matrices are not supported. Export uncompensated gates.')
        gate_scales = tuple(scales[dimension.transformation_ref] if dimension.transformation_ref else 'linear'
                            for dimension in dimensions)
        if getattr(engine_gate, 'use_complement', False):
            raise ValueError(f'Gate {identity}: complement/outside gates are not supported.')
        if isinstance(engine_gate, fk.gates.EllipsoidGate):
            if len(dimensions) != 2:
                raise ValueError(f'Gate {identity}: only two-dimensional ellipses are supported.')
            gate = Ellipse(identity, identity, parent, channels, tuple(float(v) for v in engine_gate.coordinates),
                           ellipse_axes(engine_gate.covariance_matrix, engine_gate.distance_square), gate_scales)
        elif type(engine_gate) is fk.gates.PolygonGate:
            gate = Polygon(identity, identity, parent, channels, tuple(tuple(v) for v in engine_gate.vertices), gate_scales)
        elif type(engine_gate) is fk.gates.RectangleGate and len(dimensions) == 2:
            gate = Rectangle(identity, identity, parent, channels,
                             tuple(d.min for d in dimensions), tuple(d.max for d in dimensions), gate_scales)
        elif type(engine_gate) is fk.gates.RectangleGate and len(dimensions) == 1:
            dimension = dimensions[0]
            if dimension.min is None or dimension.max is None:
                raise ValueError(f'Gate {identity}: one-dimensional ranges must have both lower and upper bounds.')
            gate = RangeGate(identity, identity, parent, channels[0], dimension.min, dimension.max, gate_scales)
        else:
            raise ValueError(f'Gate {identity}: import supports polygons, ellipses and one/two-dimensional rectangles only.')
        gates.append(gate)
    gates = ordered_gates(gates)
    if normalized_fcs:
        notes.append('FCS compensation references on scatter-only FSC-/SSC- gates were resolved to uncompensated scatter values; '
                     'the pinned engine compensates fluorescence channels only. No fluorescence compensation was imported.')
    return gates, tuple(notes)
