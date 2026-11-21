"""Scientific dependency keys shared by analysis and GUI result ownership."""
from dataclasses import asdict
import json


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def gate_key(gate):
    data = asdict(gate)
    for field in ('name', 'group_id'):
        data.pop(field, None)
    return canonical(data)


def plot_key(spec):
    data = asdict(spec)
    for field in ('id', 'geometry', 'sample_ids', 'normalization', 'contours', 'stagger', 'overlay', 'pinned', 'legend'):
        data.pop(field, None)
    if spec.kind == 'histogram':
        # Y is rendering only: bins depend on X and the selected population.
        for field in ('y_scale', 'y_limits', 'y_channel'):
            data.pop(field, None)
    return canonical(data)


def result_key(project, source):
    return (source.path, source.sha256, project.domain, canonical(project.compensation), project.channel,
            tuple(gate_key(g) for g in project.gates_for(source.id)),
            tuple((p.id, plot_key(p)) for p in project.plots))


def relabel(result, project, source):
    names = {g.id: g.name for g in project.default_gates()}
    overridden = {g.id for g in project.overrides.get(source.id, [])}
    for row in result['rows']:
        row['gate'] = names.get(row['gate_id'], 'All events')
        row['gate_scope'] = 'sample' if row['gate_id'] in overridden else 'shared'
        from .review import population_status
        row['analysis_status'] = population_status(project, source.id, row['gate_id'])
    return result


def population_keys(project, source):
    from .model import ordered_gates
    keys = {'root': ('all-events',)}
    for gate in ordered_gates(project.gates_for(source.id)):
        keys[gate.id] = (gate_key(gate), keys[gate.parent_id or 'root'],
                         tuple(keys[operand] for operand in getattr(gate, 'operands', ())))
    return keys


def retain_valid(result, previous, current, source):
    """Retain only facts whose actual dependencies survived an edit."""
    original = next((s for s in previous.sources if s.id == source.id), None)
    if original != source or previous.compensation != current.compensation:
        return None
    before, after = population_keys(previous, source), population_keys(current, source)
    channels = {current.channel} | {c for p in current.plots for c in (p.x_channel, p.y_channel) if c}
    old_plots = {p.id: p for p in previous.plots}
    plots = {}
    for spec in current.plots:
        old = old_plots.get(spec.id)
        if (old and plot_key(old) == plot_key(spec)
                and before[old.population_id or 'root'] == after[spec.population_id or 'root']
                and spec.id in result['sheet_plots']):
            plots[spec.id] = result['sheet_plots'][spec.id]
    rows = [row for row in result['rows'] if row['gate_id'] in after
            and before.get(row['gate_id']) == after[row['gate_id']] and row['channel'] in channels]
    return relabel({**result, 'rows': rows, 'sheet_plots': plots}, current, source)
