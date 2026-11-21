"""FlowKit owns parsing, compensation and gate membership; statistics use full data.

Domain: acquisition-preprocessed, untransformed FlowKit values, uncompensated
unless the project names a spillover source. Plot sampling never participates
in scientific results.
"""
from pathlib import Path
from dataclasses import replace
from .cache import AnalysisCache
from .dependencies import canonical, gate_key, plot_key
from . import scales as scale_rules

import flowkit as fk
import numpy as np

from .model import (COMPENSATED_DOMAIN, DOMAIN, BooleanGate, Ellipse, Histogram, Polygon, Project, RangeGate,
                    Rectangle, Source, ordered_gates, validate_compensation)
from .storage import verify_source

PLOT_LIMIT = 12_000
HIGHLIGHT_LIMIT = 4_000
COMPENSATION_ID = 'spillover'


def file_spillover(sample):
    """Channels and spillover values stored in the file, or None.

    FCS writes ``n, name1..namen, n*n values`` row by row; row = fluorochrome,
    column = detector.
    """
    metadata = sample.get_metadata()
    text = next((metadata[k] for k in ('spillover', 'spill', 'comp') if metadata.get(k)), '')
    parts = [p.strip() for p in str(text).split(',')] if text else []
    if not parts or not parts[0].isdigit():
        return None
    n = int(parts[0])
    if n < 2 or len(parts) != 1 + n + n * n:
        return None
    try:
        values = [[float(v) for v in parts[1 + n + r * n:1 + n + (r + 1) * n]] for r in range(n)]
    except ValueError:
        return None
    return {'source': 'matrix', 'channels': parts[1:1 + n], 'values': values}


def resolve_compensation(sample, compensation, name='sample'):
    """The FlowKit matrix this sample is compensated with, or None."""
    if compensation is None:
        return None
    validate_compensation(compensation)
    if compensation['source'] == 'file':
        compensation = file_spillover(sample)
        if compensation is None:
            raise ValueError(f'{name} stores no readable spillover matrix. Choose one matrix for all samples, '
                             'or switch compensation off.')
        validate_compensation(compensation)
    labels = list(sample.pnn_labels)
    missing = [c for c in compensation['channels'] if c not in labels]
    if missing:
        raise ValueError(f'{name} lacks channel(s) named in the spillover matrix: ' + ', '.join(missing))
    fluorescence = {labels[i] for i in sample.fluoro_indices}
    other = [c for c in compensation['channels'] if c not in fluorescence]
    if other:
        raise ValueError('The spillover matrix names scatter or time channel(s), which cannot be compensated: '
                         + ', '.join(other))
    return fk.Matrix(np.array(compensation['values'], dtype=np.float64), list(compensation['channels']))


def _plot(events, channels, axes, mask=None):
    missing = [name for name in axes if name not in channels]
    if missing:
        raise ValueError(f"Plot requires missing channel(s): {', '.join(missing)}")
    points = events[:, [channels.index(name) for name in axes]]
    if mask is not None:
        points = points[mask]
    points = points[np.isfinite(points).all(axis=1)]
    if len(points) > PLOT_LIMIT:
        points = points[np.linspace(0, len(points) - 1, PLOT_LIMIT, dtype=int)]
    return {"x": points[:, 0].tolist(), "y": points[:, 1].tolist()}


def analysis_state(source, gates, cache=None, checkpoint=lambda: None, compensation=None):
    """Owner of verified parsing, compensation and hierarchical membership for all workflows."""
    Project(sources=[source], gates=gates).validate()
    validate_compensation(compensation)
    checkpoint()
    verify_source(source)
    cache = cache if cache is not None else AnalysisCache(max_bytes=0)
    file_key = (source.path, source.sha256, DOMAIN)
    source_key = file_key if compensation is None else (source.path, source.sha256, COMPENSATED_DOMAIN, canonical(compensation))
    # Pinned FlowKit 1.2.3 always acquisition-preprocesses here; unlike newer
    # releases its constructor has no ``preprocess`` switch.
    sample = cache.get('sample', file_key, lambda: fk.Sample(source.path, sample_id=source.id))
    channels = list(sample.pnn_labels)
    if len(set(channels)) != len(channels):
        raise ValueError("Sample has duplicate channel labels; channel lookup is ambiguous")
    required = {name for gate in gates for name in gate.channels}
    missing = sorted(required.difference(channels))
    if missing:
        raise ValueError(f"Sample {Path(source.path).name} is missing channel(s): {', '.join(missing)}")

    matrix = resolve_compensation(sample, compensation, Path(source.path).name)
    if matrix is None:
        events = sample.get_events(source="raw")
    else:
        # Matrix.apply is the routine FlowKit itself runs inside gate evaluation.
        events = cache.get('sample', source_key, lambda: matrix.apply(sample))
    gates = ordered_gates(gates)
    memberships, membership_keys = {}, {}
    for gate in gates:
        checkpoint()
        key = (source_key, gate_key(gate), membership_keys.get(gate.parent_id),
               tuple(membership_keys[operand] for operand in getattr(gate, 'operands', ())))
        membership_keys[gate.id] = key
        def produce_membership(gate=gate):
            if isinstance(gate, BooleanGate):
                parts = [memberships[operand] for operand in gate.operands]
                if gate.operation == 'and':
                    mask = np.logical_and.reduce(parts)
                elif gate.operation == 'or':
                    mask = np.logical_or.reduce(parts)
                else:
                    mask = ~parts[0]
                if gate.parent_id is not None:
                    mask = mask & memberships[gate.parent_id]
                return mask
            # FlowKit evaluates the gate once; the cached parent mask owns hierarchy.
            standalone = replace(gate, parent_id=None)
            strategy, paths = build_strategy([standalone], matrix)
            with np.errstate(divide='ignore', invalid='ignore', over='ignore'):
                results = strategy.gate_sample(sample, cache_events=False)
            mask = results.get_gate_membership(gate.id, paths[gate.id]).copy()
            for name, scale in zip(gate.channels, gate.scales):
                values = events[:, channels.index(name)]
                mask &= np.isfinite(values)
                if scale_rules.requires_positive(scale):
                    mask &= values > 0
            if gate.parent_id is not None:
                mask &= memberships[gate.parent_id]
            return mask
        memberships[gate.id] = cache.get('gate', key, produce_membership)
    return sample, events, channels, gates, memberships, membership_keys, source_key, cache


def scaled_columns(values, scales):
    """Keep rows that every scale can show and express them in scale coordinates.

    Returns the transformed rows and a mask of rows dropped only because a log
    axis cannot show a nonpositive value.
    """
    nonpositive = np.zeros(len(values), dtype=bool)
    for i, scale in enumerate(scales):
        if scale_rules.requires_positive(scale):
            nonpositive |= values[:, i] <= 0
    data = values[~nonpositive].copy()
    for i, scale in enumerate(scales):
        if scale != 'linear':
            data[:, i] = scale_rules.forward(data[:, i], scale)
    return data, nonpositive


def population_values(source, gates, population_id, channels, scales, cache=None, compensation=None):
    """Full eligible population in declared gate coordinates, never display dots."""
    _, events, labels, _, memberships, _, _, _ = analysis_state(source, gates, cache, compensation=compensation)
    missing = set(channels).difference(labels)
    if missing:
        raise ValueError('Population channels unavailable: ' + ', '.join(sorted(missing)))
    mask = memberships[population_id] if population_id else np.ones(len(events), dtype=bool)
    values = events[mask][:, [labels.index(c) for c in channels]].astype(np.float64)
    values = values[np.isfinite(values).all(axis=1)]
    return scaled_columns(values, scales)[0]


def evaluate(source: Source, gates: list[Polygon], channel: str,
             include_plot: bool = False, *, statistic: str = "median",
             viability: RangeGate | None = None, histogram: Histogram | None = None,
             plot_specs=None, cache=None, checkpoint=lambda: None, compensation=None,
             highlight_id=None) -> dict:
    """Evaluate one verified source sequentially, without nested multiprocessing.

    Empty populations return no numerical value. A nonfinite selected statistic
    marks the complete result nonfinite rather than silently dropping data.
    Display names never participate in FlowKit hierarchy paths.
    """
    Project(sources=[source], gates=gates, channel=channel, statistic=statistic,
            viability=viability).validate()
    gates = list(gates) + ([viability] if viability else [])
    if histogram and (histogram.gate_id not in {g.id for g in gates} or not channel):
        raise ValueError("Histogram needs an existing gate and statistic channel")
    sample, events, channels, gates, memberships, membership_keys, source_key, cache = analysis_state(
        source, gates, cache, checkpoint, compensation)
    domain = COMPENSATED_DOMAIN if compensation else DOMAIN
    if channel and channel not in channels:
        raise ValueError(f"Sample {Path(source.path).name} is missing channel(s): {channel}")
    rows = []
    if channel:
        values = events[:, channels.index(channel)].astype(np.float64)
        for gate in gates:
            selected = values[memberships[gate.id]]
            count = len(selected)
            value, status = statistic_value(selected, statistic)
            rows.append({
                "sample_id": source.id, "sample": Path(source.path).name,
                "source_path": source.path, "source_sha256": source.sha256,
                "gate_id": gate.id, "gate": gate.name, "channel": channel,
                "event_count": count, "statistic": statistic, "value": value,
                "status": status, "domain": domain,
            })

    plots = {}
    histogram_data = None
    suggestion = None
    if channel and gates and include_plot:
        finite = events[memberships[gates[-1].id], channels.index(channel)]
        finite = finite[np.isfinite(finite)]
        if len(finite):
            lo, hi = float(finite.min()), float(finite.max())
            suggestion = [lo, hi if hi > lo else lo + max(1., abs(lo) * 1e-6)]
    if histogram:
        selected = events[memberships[histogram.gate_id], channels.index(channel)]
        finite = selected[np.isfinite(selected)]
        edges = np.linspace(histogram.minimum, histogram.maximum, histogram.bins + 1)
        if not np.isfinite(edges).all() or np.any(np.diff(edges) <= 0):
            raise ValueError("Histogram bounds cannot form distinct finite bins")
        counts, _ = np.histogram(finite, bins=edges)
        histogram_data = {
            "edges": edges.tolist(), "counts": counts.tolist(), "event_count": len(selected),
            "nonfinite": int(len(selected) - len(finite)),
            "underflow": int(np.sum(finite < histogram.minimum)),
            "overflow": int(np.sum(finite > histogram.maximum)),
        }
    if include_plot:
        plots["population"] = _plot(events, channels, ("FSC-A", "SSC-A"))
        parent = memberships[gates[0].id] if gates else None
        plots["singlets"] = _plot(events, channels, ("SSC-A", "SSC-H"), parent)
    sheet_plots, highlights = {}, {}
    if plot_specs is not None:
        for spec in plot_specs:
            try:
                mask = memberships[spec.population_id] if spec.population_id else np.ones(len(events), dtype=bool)
                checkpoint()
                key = (source_key, membership_keys.get(spec.population_id), plot_key(spec))
                sheet_plots[spec.id] = cache.get('plot', key, lambda: plot_data(events, channels, mask, spec))
                if highlight_id in memberships and spec.kind != 'histogram':
                    highlights[spec.id] = highlight_points(events, channels, mask & memberships[highlight_id],
                                                           spec, sheet_plots[spec.id])
            except (ValueError, KeyError) as exc:
                sheet_plots[spec.id] = {'error': str(exc)}
        metric_channels = list(dict.fromkeys([channel] + [c for spec in plot_specs for c in
                              ([spec.x_channel] if spec.kind == 'histogram' else [spec.x_channel, spec.y_channel])]))
        rows = []
        all_populations = [(None, 'All events', None, np.ones(len(events), dtype=bool))] + [
            (g.id, g.name, g.parent_id, memberships[g.id]) for g in gates]
        for gate_id, name, parent_id, mask in all_populations:
            count = int(mask.sum())
            parent_count = int(memberships[parent_id].sum()) if parent_id else len(events)
            for metric in metric_channels:
                if not metric or metric not in channels:
                    continue
                values = events[mask, channels.index(metric)].astype(np.float64)
                for stat in ('median', 'arithmetic_mean', 'geometric_mean'):
                    checkpoint()
                    key = (source_key, membership_keys.get(gate_id), metric, stat)
                    value, status = cache.get('statistic', key, lambda: statistic_value(values, stat))
                    rows.append({'sample_id': source.id, 'sample': Path(source.path).name,
                                 'source_path': source.path, 'source_sha256': source.sha256,
                                 'gate_id': gate_id or 'root', 'gate': name, 'parent_id': parent_id or 'root',
                                 'channel': metric, 'event_count': count, 'statistic': stat,
                                 'value': value, 'status': status, 'domain': domain,
                                 'percent_parent': 100 * count / parent_count if parent_count else None,
                                 'percent_total': 100 * count / len(events) if len(events) else None})
    matrix = resolve_compensation(sample, compensation, Path(source.path).name)
    return {"cache": {"retained_bytes": cache.bytes, "counts": {k: dict(v) for k, v in cache.counts.items()}}, "sheet_plots": sheet_plots,
            "highlights": highlights, "highlight_id": highlight_id if highlight_id in memberships else None,
            "compensated_channels": list(matrix.detectors) if matrix is not None else [], "channel_labels": dict(zip(channels, sample.pns_labels)), "channels": channels, "event_count": len(events), "rows": rows, "plots": plots,
            "histogram": histogram_data, "histogram_suggestion": suggestion}


def statistic_value(values, statistic):
    if not len(values):
        return None, 'empty'
    if not np.isfinite(values).all():
        return None, 'nonfinite'
    if statistic == 'geometric_mean' and np.any(values <= 0):
        return None, 'nonpositive'
    with np.errstate(over='ignore', invalid='ignore'):
        if statistic == 'median':
            result = float(np.median(values))
        elif statistic == 'arithmetic_mean':
            result = float(np.mean(values))
        else:
            result = float(np.exp(np.mean(np.log(values))))
    return (result, 'ok') if np.isfinite(result) else (None, 'nonfinite')


def build_strategy(gates, matrix=None, file_compensated=None):
    """FlowKit strategy for these gates.

    ``matrix`` embeds one spillover matrix; ``file_compensated`` (channel names)
    instead writes the Gating-ML reference ``FCS`` — each file's own spillover.
    """
    strategy = fk.GatingStrategy()
    for scale in dict.fromkeys(s for g in gates for s in g.scales):
        name, parameters = scale_rules.parse(scale)
        if name == 'log':
            strategy.add_transform('log10', fk.transforms.LogTransform(10, 1))
        elif name == 'logicle':
            t, w, m, a = parameters
            strategy.add_transform(scale_rules.transform_id(scale), fk.transforms.LogicleTransform(t, w, m, a))
    compensated = set()
    reference = 'uncompensated'
    if matrix is not None:
        strategy.add_comp_matrix(COMPENSATION_ID, matrix)
        compensated, reference = set(matrix.detectors), COMPENSATION_ID
    elif file_compensated:
        compensated, reference = set(file_compensated), 'FCS'
    paths = {}
    for gate in ordered_gates(gates):
        path = ('root',) if gate.parent_id is None else paths[gate.parent_id] + (gate.parent_id,)
        paths[gate.id] = path
        if isinstance(gate, BooleanGate):
            references = [{'ref': operand, 'path': paths[operand], 'complement': False} for operand in gate.operands]
            strategy.add_gate(fk.gates.BooleanGate(gate.id, gate.operation, references), path)
            continue
        dimensions = []
        for i, (name, scale) in enumerate(zip(gate.channels, gate.scales)):
            bounds = {}
            if isinstance(gate, RangeGate):
                bounds = dict(range_min=gate.minimum, range_max=gate.maximum)
            elif isinstance(gate, Rectangle):
                bounds = dict(range_min=gate.minimum[i], range_max=gate.maximum[i])
            dimensions.append(fk.Dimension(name, compensation_ref=reference if name in compensated else 'uncompensated',
                              transformation_ref=scale_rules.transform_id(scale), **bounds))
        if isinstance(gate, Polygon):
            engine_gate = fk.gates.PolygonGate(gate.id, dimensions, gate.vertices)
        elif isinstance(gate, Ellipse):
            engine_gate = fk.gates.EllipsoidGate(gate.id, dimensions, list(gate.center),
                                                 [list(row) for row in gate.covariance()], 1.0)
        else:
            engine_gate = fk.gates.RectangleGate(gate.id, dimensions)
        strategy.add_gate(engine_gate, path)
    return strategy, paths


def _bounds(values, limits, scale):
    if limits is not None:
        return scale_rules.forward(np.array(limits, dtype=float), scale)
    if not len(values):
        return np.array([0., 1.])
    lo, hi = float(values.min()), float(values.max())
    if lo == hi:
        delta = max(1., abs(lo) * .05) if scale == 'linear' else .5 if scale == 'log' else .05
        lo, hi = lo - delta, hi + delta
    return np.array([lo, hi])


def highlight_points(events, channels, mask, spec, data):
    """Display points of one population inside another plot's axes (backgating)."""
    values = events[mask][:, [channels.index(spec.x_channel), channels.index(spec.y_channel)]].astype(np.float64)
    count = len(values)
    values = values[np.isfinite(values).all(axis=1)]
    points, _ = scaled_columns(values, (spec.x_scale, spec.y_scale))
    (x0, x1), (y0, y1) = data['x_bounds'], data['y_bounds']
    points = points[(points[:, 0] >= x0) & (points[:, 0] <= x1) & (points[:, 1] >= y0) & (points[:, 1] <= y1)]
    if len(points) > HIGHLIGHT_LIMIT:
        points = points[np.linspace(0, len(points) - 1, HIGHLIGHT_LIMIT, dtype=int)]
    return {'x': points[:, 0].tolist(), 'y': points[:, 1].tolist(), 'event_count': count}


def plot_data(events, channels, mask, spec):
    axes = [spec.x_channel] if spec.kind == 'histogram' else [spec.x_channel, spec.y_channel]
    if any(c not in channels for c in axes):
        raise ValueError('Plot channel unavailable: ' + ', '.join(c for c in axes if c not in channels))
    values = events[mask][:, [channels.index(c) for c in axes]].astype(np.float64)
    count = len(values)
    finite = np.isfinite(values).all(axis=1)
    values = values[finite]
    data, nonpositive = scaled_columns(values, (spec.x_scale, spec.y_scale)[:len(axes)])
    x_bounds = _bounds(data[:, 0], spec.x_limits, spec.x_scale)
    result = {'event_count': count, 'nonfinite': int(count - finite.sum()),
              'nonpositive': int(nonpositive.sum()), 'x_bounds': x_bounds.tolist(),
              'kind': spec.kind, 'population_id': spec.population_id, 'x_channel': spec.x_channel,
              'y_channel': spec.y_channel, 'x_scale': spec.x_scale, 'y_scale': spec.y_scale}
    x_edges = np.linspace(*x_bounds, spec.bins + 1)
    if spec.kind == 'histogram':
        bins, _ = np.histogram(data[:, 0], x_edges)
        result.update(edges=x_edges.tolist(), counts=bins.tolist(), outside=int(len(data) - bins.sum()))
    else:
        y_bounds = _bounds(data[:, 1], spec.y_limits, spec.y_scale)
        result['y_bounds'] = y_bounds.tolist()
        if spec.kind == 'density':
            y_edges = np.linspace(*y_bounds, spec.bins + 1)
            density, _, _ = np.histogram2d(data[:, 0], data[:, 1], bins=(x_edges, y_edges))
            result.update(x_edges=x_edges.tolist(), y_edges=y_edges.tolist(), counts=density.astype(int).tolist(), outside=int(len(data) - density.sum()))
            from .density import density_points
            result.update(density_points(data, x_edges, y_edges, density, limit=PLOT_LIMIT))
        else:
            visible = data[(data[:, 0] >= x_bounds[0]) & (data[:, 0] <= x_bounds[1]) &
                           (data[:, 1] >= y_bounds[0]) & (data[:, 1] <= y_bounds[1])]
            result['outside'] = len(data) - len(visible)
            if len(visible) > PLOT_LIMIT:
                visible = visible[np.linspace(0, len(visible)-1, PLOT_LIMIT, dtype=int)]
            result.update(x=visible[:, 0].tolist(), y=visible[:, 1].tolist())
    return result
