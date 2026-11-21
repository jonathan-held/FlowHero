"""Versioned JSON definitions and atomic persistence, never live engine objects."""
import csv
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile
from .model import BooleanGate, Ellipse, Histogram, Polygon, PlotSpec, Project, RangeGate, Rectangle, Source, new_id

FORMAT = "flowpilot-project"
VERSION = 10
GATE_VALIDITY_POLICY = 'Exclude nonfinite gate coordinates; logarithmic gate dimensions require positive acquisition-preprocessed values.'
GATE_COORDINATE_POLICY = 'Gate coordinates are linear values or log10(values), per gate scales. Statistics use untransformed acquisition-preprocessed values.'
GATE_COORDINATE_POLICY_2 = 'Gate coordinates are linear values, log10(values) or logicle(values; T, W, M, A), per gate scales. Statistics use untransformed values of the stated domain.'
CSV_FIELDS = ['sample_id', 'sample', 'source_path', 'source_sha256', 'gate_id', 'gate',
              'channel', 'event_count', 'statistic', 'value', 'status', 'domain', 'gate_scope', 'parent_id', 'percent_parent', 'percent_total', 'analysis_status']


def digest(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def bind_source(path: str) -> Source:
    resolved = Path(path).expanduser().resolve(strict=True)
    return Source(new_id(), str(resolved), digest(resolved))


def verify_source(source: Source):
    try:
        current = digest(source.path)
    except OSError as exc:
        raise ValueError(f"Cannot read {source.path}. Relink this sample to the original FCS file. {exc}") from exc
    if current != source.sha256:
        raise ValueError(f"Source content changed: {source.path}. Add it as a new sample, or relink to the original bytes.")


def relink(source: Source, path: str) -> Source:
    candidate = Source(source.id, str(Path(path).expanduser().resolve(strict=True)), source.sha256)
    verify_source(candidate)
    return candidate


def atomic_write(path: str | Path, write):
    target = Path(path)
    # Temporary file is on the destination filesystem so os.replace is atomic.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='',
                                         dir=target.parent, prefix='.' + target.name + '.',
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_project(project: Project, path: str | Path):
    project.validate()
    data = {'format': FORMAT, 'version': VERSION, 'project': asdict(project)}
    atomic_write(path, lambda stream: json.dump(data, stream, indent=2, allow_nan=False))


def parse_gate(g, polygon_only=False):
    if not isinstance(g, dict):
        raise ValueError('Invalid gate definition')
    data = dict(g)
    if not isinstance(data.get('id'), str) or not isinstance(data.get('name'), str) or not (
            data.get('parent_id') is None or isinstance(data.get('parent_id'), str)):
        raise ValueError('Invalid gate identity')
    if 'scales' in data:
        if not isinstance(data['scales'], list):
            raise ValueError('Invalid gate scales')
        data['scales'] = tuple(data['scales'])
    if 'operands' in data:
        if set(data) != {'id', 'name', 'parent_id', 'operation', 'operands'} or not isinstance(data['operands'], list):
            raise ValueError('Invalid combined-gate definition')
        return BooleanGate(data['id'], data['name'], data['parent_id'], data['operation'], tuple(data['operands']))
    if 'center' in data:
        if (set(data) - {'scales'} != {'id', 'name', 'parent_id', 'channels', 'center', 'axes'}
                or not all(isinstance(data[k], list) for k in ('channels', 'center', 'axes'))
                or not all(isinstance(a, list) for a in data['axes'])):
            raise ValueError('Invalid ellipse definition')
        data['channels'] = tuple(data['channels'])
        data['center'] = tuple(data['center'])
        data['axes'] = tuple(tuple(a) for a in data['axes'])
        return Ellipse(**data)
    if 'vertices' in data:
        expected = {'id', 'name', 'parent_id', 'channels', 'vertices'}
        if set(data) - {'scales'} != expected or not isinstance(data['vertices'], list):
            raise ValueError('Invalid polygon definition')
        if not isinstance(data['channels'], list) or not all(isinstance(v, str) for v in data['channels']):
            raise ValueError('Invalid channel names')
        data['channels'] = tuple(data['channels'])
        data['vertices'] = tuple(tuple(v) for v in data['vertices'])
        return Polygon(**data)
    if polygon_only:
        raise ValueError('Expected polygon definition')
    if 'channels' in data:
        if set(data) - {'scales', 'group_id'} != {'id', 'name', 'parent_id', 'channels', 'minimum', 'maximum'}:
            raise ValueError('Invalid rectangle definition')
        for key in ['channels', 'minimum', 'maximum']:
            if not isinstance(data[key], list):
                raise ValueError(f'Rectangle {key} must be a JSON array')
            data[key] = tuple(data[key])
        return Rectangle(**data)
    if set(data) - {'scales'} != {'id', 'name', 'parent_id', 'channel', 'minimum', 'maximum'}:
        raise ValueError('Invalid range definition')
    return RangeGate(**data)


def load_project(path: str | Path) -> Project:
    return _parse_project(Path(path).read_bytes())


def load_project_snapshot(path: str | Path) -> tuple[Project, str]:
    contents = Path(path).read_bytes()
    return _parse_project(contents), hashlib.sha256(contents).hexdigest()


def _parse_project(contents) -> Project:
    try:
        def reject_constant(value):
            raise ValueError(f"Invalid JSON number: {value}")
        data = json.loads(contents, parse_constant=reject_constant)
        if not isinstance(data, dict) or set(data) != {'format', 'version', 'project'}:
            raise ValueError('Invalid FlowHero project document')
        if data['format'] != FORMAT or type(data['version']) is not int or data['version'] not in range(1, VERSION + 1):
            raise ValueError('Unsupported project format/version')
        p = data['project']
        fields = {'sources', 'gates', 'channel', 'domain', 'revision'}
        if data['version'] >= 2:
            fields.add('statistic')
        if data['version'] >= 3:
            fields.update({'viability', 'overrides', 'histogram'})
        if data['version'] >= 4:
            fields.add('plots')
        if data['version'] >= 6:
            fields.add('analysis')
        if data['version'] >= 7:
            fields.add('gate_imports')
        if data['version'] >= 8:
            fields.add('annotations')
        if data['version'] >= 9:
            fields.add('compensation')
        if not isinstance(p, dict) or set(p) != fields:
            raise ValueError('Invalid project fields')
        if not isinstance(p['sources'], list) or not isinstance(p['gates'], list):
            raise ValueError('Sources and gates must be lists')
        sources = []
        for s in p['sources']:
            if not isinstance(s, dict) or set(s) != {'id', 'path', 'sha256'} or not all(isinstance(v, str) for v in s.values()):
                raise ValueError('Invalid sample binding')
            sources.append(Source(**s))
        gates = [parse_gate(g) for g in p['gates']]
        viability = parse_gate(p['viability']) if p.get('viability') is not None else None
        overrides = {}
        if 'overrides' in p:
            if not isinstance(p['overrides'], dict):
                raise ValueError('Invalid sample overrides')
            for source_id, definitions in p['overrides'].items():
                if not isinstance(definitions, list):
                    raise ValueError('Invalid sample override definitions')
                overrides[source_id] = [parse_gate(g) for g in definitions]
        histogram = None
        h = p.get('histogram')
        if h is not None:
            if (not isinstance(h, dict) or set(h) != {'gate_id', 'minimum', 'maximum',
                                                     'bins', 'normalized', 'sample_ids'}
                    or not isinstance(h['sample_ids'], list)):
                raise ValueError('Invalid histogram definition')
            histogram = Histogram(**{**h, 'sample_ids': tuple(h['sample_ids'])})
        plots = []
        for item in p.get('plots', []):
            d = dict(item)
            for field in ['x_limits', 'y_limits', 'geometry', 'sample_ids']:
                if d.get(field) is not None:
                    d[field] = tuple(d[field])
            plots.append(PlotSpec(**d))
        project = Project(sources, gates, p['channel'], p['domain'], p['revision'],
                          p.get('statistic', 'median'), viability, overrides, histogram, plots, p.get('analysis'), p.get('gate_imports', []),
                          p.get('annotations', {}), p.get('compensation'))
        project.validate()
        return project
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f'Invalid project: {exc}') from exc


def export_csv(rows: list[dict], path: str | Path):
    def write(stream):
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    atomic_write(path, write)


def export_gates(project, path, source_id=None):
    project.validate()
    gates = project.gates_for(source_id) if source_id else project.default_gates()
    # Files that need nothing new stay readable by earlier versions.
    extended = (project.compensation is not None
                or any(isinstance(g, (Ellipse, BooleanGate)) or any(s not in ('linear', 'log') for s in g.scales)
                       for g in gates + [g for v in project.overrides.values() for g in v]))
    data = {'format': 'flowpilot-gates', 'version': 2 if extended else 1, 'domain': project.domain,
            'gate_validity_policy': GATE_VALIDITY_POLICY,
            'coordinate_policy': GATE_COORDINATE_POLICY_2 if extended else GATE_COORDINATE_POLICY,
            'gates': [asdict(g) for g in gates],
            'sample_overrides': {k: [asdict(g) for g in v] for k, v in project.overrides.items()}}
    atomic_write(path, lambda stream: json.dump(data, stream, indent=2, allow_nan=False))


def export_gatingml(project, path, source_id=None, file_compensated=None):
    """Gating-ML 2.0. A project matrix is embedded; file compensation is written
    as the standard reference ``FCS`` for the channels in ``file_compensated``."""
    import io
    import flowkit as fk
    import numpy as np
    from .analysis import build_strategy
    project.validate()
    gates = project.gates_for(source_id) if source_id else project.default_gates()
    matrix = None
    if project.compensation and project.compensation['source'] == 'matrix':
        matrix = fk.Matrix(np.array(project.compensation['values'], dtype=float), list(project.compensation['channels']))
    elif project.compensation and file_compensated is None:
        raise ValueError('Calculate the selected sample first: its spillover channels are written into the file.')
    strategy, _ = build_strategy(gates, matrix, file_compensated if matrix is None else None)
    stream = io.BytesIO()
    fk.export_gatingml(strategy, stream)
    text = stream.getvalue().decode('utf-8')
    atomic_write(path, lambda output: output.write(text))
