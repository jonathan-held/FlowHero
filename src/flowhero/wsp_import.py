"""FlowJo 10 workspace (.wsp) gates read through FlowKit's workspace parser.

FlowKit parses the file and converts each sample's gates; this module projects
them onto FlowHero's gate types and scales. What cannot be represented is
left out and named in the notes, never guessed:

* linear, log and logicle axes are kept exactly;
* gates drawn on FlowJo "biex" or ArcSinh axes are redrawn on a logicle axis,
  with each polygon edge subdivided so the outline follows the original;
* FlowJo ellipses arrive from FlowKit as 128-vertex polygons;
* complemented gates and combined gates that do not combine sibling gates are
  skipped together with their descendants.
"""
import hashlib
from pathlib import Path

import numpy as np

from .model import BooleanGate, Polygon, RangeGate, Rectangle, new_id
from . import scales as scale_rules

EDGE_PARTS = 16
OPEN = 1e12


def _raw(transform, values):
    """Transformed gate coordinates back to stored channel values."""
    import flowkit as fk
    from flowkit._models.transforms import _wsp_transforms
    values = np.asarray(values, dtype=np.float64)
    if transform is None:
        return values
    if isinstance(transform, _wsp_transforms.WSPLogTransform):
        return np.power(10.0, values * transform.decades + np.log10(transform.offset))
    if isinstance(transform, fk.transforms.LogicleTransform):
        return scale_rules.inverse(values, scale_rules.logicle_scale(transform.param_t, transform.param_w,
                                                                    transform.param_m, transform.param_a))
    return np.asarray(transform.inverse(values.reshape(-1, 1)), dtype=np.float64).reshape(values.shape)


def _target_scale(transform):
    """(scale, exact): the FlowHero scale a FlowJo axis is mapped to."""
    import flowkit as fk
    from flowkit._models.transforms import _wsp_transforms
    if transform is None or type(transform) is fk.transforms.LinearTransform:
        return 'linear', True
    if isinstance(transform, _wsp_transforms.WSPLogTransform):
        return 'log', True
    if isinstance(transform, fk.transforms.LogicleTransform):
        try:
            return scale_rules.logicle_scale(transform.param_t, transform.param_w, transform.param_m, transform.param_a), True
        except ValueError:
            return scale_rules.logicle_scale(), False
    return scale_rules.logicle_scale(), False


def _bound(transform, scale, value, upper):
    if value is None:
        raw = OPEN if upper else (1.0 / OPEN if scale == 'log' else -OPEN)
    else:
        raw = float(_raw(transform, [value])[0])
        if scale == 'log' and raw <= 0:
            raw = 1.0 / OPEN
    return float(scale_rules.forward([raw], scale)[0])


def _convert_gate(gate, identity, name, parent, transforms):
    import flowkit as fk
    dimensions = gate.dimensions
    if any(type(d) is not fk.Dimension for d in dimensions):
        raise ValueError('derived or ratio dimensions')
    if getattr(gate, 'use_complement', False):
        raise ValueError('an inverted (complement) gate')
    channels = tuple(d.id for d in dimensions)
    sources = [transforms.get(d.transformation_ref) if d.transformation_ref else None for d in dimensions]
    targets = [_target_scale(t) for t in sources]
    scales = tuple(scale for scale, _ in targets)
    exact = all(flag for _, flag in targets)
    if isinstance(gate, fk.gates.PolygonGate):
        if len(channels) != 2:
            raise ValueError('a polygon that is not two-dimensional')
        vertices = np.asarray(gate.vertices, dtype=np.float64)
        if not exact:
            closed = np.vstack([vertices, vertices[:1]])
            vertices = np.vstack([np.linspace(a, b, EDGE_PARTS, endpoint=False) for a, b in zip(closed, closed[1:])])
        columns = []
        for axis in (0, 1):
            raw = _raw(sources[axis], vertices[:, axis])
            if scales[axis] == 'log':
                raw = np.maximum(raw, 1.0 / OPEN)
            columns.append(scale_rules.forward(raw, scales[axis]))
        points = np.column_stack(columns)
        if not np.isfinite(points).all():
            raise ValueError('vertices outside the range of its axis scale')
        return Polygon(identity, name, parent, channels, tuple((float(x), float(y)) for x, y in points), scales), exact
    if isinstance(gate, fk.gates.RectangleGate):
        if len(channels) == 1:
            low = _bound(sources[0], scales[0], dimensions[0].min, False)
            high = _bound(sources[0], scales[0], dimensions[0].max, True)
            return RangeGate(identity, name, parent, channels[0], low, high, scales), True
        if len(channels) == 2:
            minimum = tuple(None if d.min is None else _bound(t, s, d.min, False) for d, t, s in zip(dimensions, sources, scales))
            maximum = tuple(None if d.max is None else _bound(t, s, d.max, True) for d, t, s in zip(dimensions, sources, scales))
            if all(v is None for v in minimum + maximum):
                raise ValueError('a rectangle without any bound')
            return Rectangle(identity, name, parent, channels, minimum, maximum, scales), True
        raise ValueError('a rectangle with more than two dimensions')
    raise ValueError(f'a {type(gate).__name__}')


def _sample_gates(strategy):
    """Project one sample's FlowKit strategy; returns gates and what was skipped."""
    import flowkit as fk
    identities, gates, skipped, inexact = {}, [], [], []
    for name, path in strategy.get_gate_ids():
        key = (name, tuple(path))
        parent_key = (path[-1], tuple(path[:-1])) if len(path) > 1 else None
        label = '/'.join(list(path[1:]) + [name])
        if parent_key is not None and parent_key not in identities:
            skipped.append(f'{label} (its parent was skipped)')
            continue
        parent = identities.get(parent_key)
        engine = strategy.get_gate(name, tuple(path))
        try:
            if isinstance(engine, fk.gates.BooleanGate):
                operands = []
                for reference in engine.gate_refs:
                    operand_key = (reference['ref'], tuple(reference['path']))
                    if tuple(reference['path']) != tuple(path) or operand_key not in identities:
                        raise ValueError('a combined gate that does not combine gates of its own parent')
                    if reference.get('complement') and engine.type != 'not':
                        raise ValueError('a combined gate with inverted members')
                    operands.append(identities[operand_key])
                gate = BooleanGate(new_id(), name, parent, engine.type, tuple(operands))
                exact = True
            else:
                gate, exact = _convert_gate(engine, new_id(), name, parent, strategy.transformations)
        except ValueError as exc:
            skipped.append(f'{label} ({exc})')
            continue
        identities[key] = gate.id
        gates.append(gate)
        if not exact:
            inexact.append(label)
    return gates, skipped, inexact


def parse_wsp(path):
    """Return ``(variants, compensation, notes, sha256)`` for a FlowJo workspace.

    ``variants`` maps each workspace sample name to its gates; ``compensation``
    maps the same names to that sample's spillover matrix, where it has one.
    """
    import flowkit as fk
    path = Path(path)
    contents = path.read_bytes()
    try:
        workspace = fk.parse_wsp(str(path))
    except Exception as exc:
        # FlowKit raises several types for unsupported or damaged workspaces.
        raise ValueError(f'FlowKit could not read this FlowJo workspace: {exc}') from exc
    variants, compensation, notes = {}, {}, []
    for name, sample in workspace['samples'].items():
        try:
            gates, skipped, inexact = _sample_gates(sample['gating_strategy'])
        except Exception as exc:
            notes.append(f'{name}: not importable ({exc}).')
            continue
        if not gates:
            continue
        variants[str(name)] = tuple(gates)
        if skipped:
            notes.append(f'{name}: left out ' + '; '.join(skipped) + '.')
        if inexact:
            notes.append(f'{name}: redrawn on logicle axes from FlowJo biex/ArcSinh axes (edges subdivided; events '
                         'close to a gate edge can fall on the other side): ' + ', '.join(inexact) + '.')
        matrix = sample.get('compensation')
        if matrix is not None:
            used = {c for gate in gates for c in gate.channels}
            detectors = list(matrix['detectors'])
            compensation[str(name)] = {'source': 'matrix', 'channels': detectors,
                                       'values': [[float(v) for v in row] for row in np.asarray(matrix['matrix'].matrix)]}
            drawn = sorted(used & set(detectors))
            if drawn:
                notes.append(f'{name}: gates on ' + ', '.join(drawn) + ' were drawn on COMPENSATED values.')
    if not variants:
        raise ValueError('The workspace holds no gates that can be imported. ' + ' '.join(notes))
    notes.insert(0, 'FlowJo workspace read by FlowKit. Choose the workspace sample whose gates to add; they become new '
                    'shared gates for all current samples. FlowJo statistics, plots and layouts are not imported.')
    return variants, compensation, tuple(notes), hashlib.sha256(contents).hexdigest()
