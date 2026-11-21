"""Parse exported strategies once and append a reviewed gate set without mutation."""
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
from .model import DOMAINS, BooleanGate, PlotSpec, Project, RangeGate, Rectangle, Source, new_id, ordered_gates, valid_sha256
from .storage import GATE_COORDINATE_POLICY, GATE_COORDINATE_POLICY_2, GATE_VALIDITY_POLICY, parse_gate


def _json(contents):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'Duplicate JSON field: {key}')
            result[key] = value
        return result
    def constant(value):
        raise ValueError(f'Invalid JSON number: {value}')
    return json.loads(contents, object_pairs_hook=pairs, parse_constant=constant)


@dataclass(frozen=True)
class GateImport:
    source_path: str
    sha256: str
    source_format: str
    variants: dict[str | None, tuple]
    notes: tuple[str, ...]
    domain: str | None = None
    compensation: dict | None = None


def read_gate_import(path):
    path = Path(path).expanduser().absolute()
    if path.suffix.lower() == '.wsp':
        from .wsp_import import parse_wsp
        variants, compensation, notes, sha256 = parse_wsp(path)
        return GateImport(str(path), sha256, 'flowjo-wsp', variants, notes, None, compensation)
    contents = path.read_bytes()
    if path.suffix.lower() in ('.xml', '.gatingml') or contents.lstrip(b'\xef\xbb\xbf \t\r\n').startswith(b'<'):
        from .gatingml_import import parse_gatingml
        gates, notes = parse_gatingml(contents)
        ordered = ordered_gates(gates)
        if not ordered:
            raise ValueError('The file contains no importable gates')
        return GateImport(str(path), hashlib.sha256(contents).hexdigest(), 'gatingml-2.0', {None: tuple(ordered)}, notes)
    try:
        data = _json(contents)
        fields = {'format', 'version', 'domain', 'gate_validity_policy', 'coordinate_policy', 'gates', 'sample_overrides'}
        if (not isinstance(data, dict) or set(data) != fields or data['format'] != 'flowpilot-gates'
                or type(data['version']) is not int or data['version'] not in (1, 2)):
            raise ValueError('Choose exported FlowHero gate JSON or Gating-ML/XML. Open a project through File/Open instead.')
        if (data['domain'] not in DOMAINS or data['gate_validity_policy'] != GATE_VALIDITY_POLICY
                or data['coordinate_policy'] != (GATE_COORDINATE_POLICY if data['version'] == 1 else GATE_COORDINATE_POLICY_2)):
            raise ValueError('The exported coordinate/domain policy is unsupported; no gates were imported')
        if not isinstance(data['gates'], list) or not isinstance(data['sample_overrides'], dict):
            raise ValueError('Gates and sample adjustments have invalid types')
        gates = [parse_gate(item) for item in data['gates']]
        if not gates:
            raise ValueError('The file contains no importable gates')
        overrides = {}
        for sid, definitions in data['sample_overrides'].items():
            if not isinstance(sid, str) or not sid or not isinstance(definitions, list):
                raise ValueError('Invalid exported sample adjustments')
            overrides[sid] = [parse_gate(item) for item in definitions]
        # The model owns geometry-only override compatibility and hierarchy validation.
        project = Project(sources=[Source(sid, 'exported sample', '0'*64) for sid in overrides], gates=gates, overrides=overrides)
        project.validate()
        if data['domain'] != project.domain:
            notes_domain = ('These gates were drawn on COMPENSATED values. They are added as drawn; check them against '
                            'this project\'s compensation setting.',)
        else:
            notes_domain = ()
        variants = {None: tuple(ordered_gates(gates))}
        variants.update({sid: tuple(ordered_gates(project.gates_for(sid))) for sid in overrides})
        notes = ('Choose the exported gate set or a named sample adjustment. The selected geometry is added as NEW SHARED gates for all current samples; existing sample adjustments are retained.',)
        return GateImport(str(path), hashlib.sha256(contents).hexdigest(), 'flowpilot-gates', variants,
                          notes + notes_domain, data['domain'])
    except (TypeError, KeyError, AttributeError, UnicodeError) as exc:
        raise ValueError(f'Invalid exported gate JSON: {exc}') from exc


def prepare_gate_import(project, document, *, variant=None, channel_map=None, open_plot=True,
                        use_compensation=False):
    """Build one validated candidate; the caller applies it through normal Undo."""
    if variant not in document.variants:
        raise ValueError('Choose an available exported gate set')
    gates = document.variants[variant]
    required = {channel for gate in gates for channel in gate.channels}
    channels = {channel: channel for channel in required} if channel_map is None else dict(channel_map)
    if set(channels) != required or not all(isinstance(value, str) and value.strip() for value in channels.values()):
        raise ValueError('Map every imported channel to a nonempty channel name')
    existing = {gate.id for gate in project.default_gates()}
    existing.update(identity for record in project.gate_imports for identity in record['gate_id_map'].values())
    if project.analysis is not None:
        existing.update(identity for records in project.analysis['proposals'].values() for identity in records)
    # Reserve all original IDs so a generated collision replacement cannot steal one.
    reserved = existing | {gate.id for gate in gates}
    identities = {}
    for gate in gates:
        target = gate.id
        if target in existing:
            while target in reserved:
                target = new_id()
            reserved.add(target)
        identities[gate.id] = target
    groups = {group: new_id() for group in {gate.group_id for gate in gates if isinstance(gate, Rectangle) and gate.group_id}}
    imported = []
    for gate in gates:
        changes = dict(id=identities[gate.id], parent_id=identities.get(gate.parent_id))
        if isinstance(gate, BooleanGate):
            changes['operands'] = tuple(identities[operand] for operand in gate.operands)
        elif isinstance(gate, RangeGate):
            changes['channel'] = channels[gate.channel]
        else:
            changes['channels'] = tuple(channels[channel] for channel in gate.channels)
        if isinstance(gate, Rectangle) and gate.group_id:
            changes['group_id'] = groups[gate.group_id]
        imported.append(replace(gate, **changes))
    candidate = deepcopy(project)
    candidate.gates.extend(imported)
    if use_compensation:
        matrix = (document.compensation or {}).get(variant)
        if matrix is None:
            raise ValueError('This gate set carries no compensation matrix')
        if project.analysis is not None:
            raise ValueError('Compensation cannot change while automatically proposed gate positions are under review')
        # Detector names follow the same channel mapping as the gates.
        candidate.compensation = {'source': 'matrix', 'channels': [channels.get(c, c) for c in matrix['channels']],
                                  'values': [list(row) for row in matrix['values']]}
        from .model import COMPENSATED_DOMAIN
        candidate.domain = COMPENSATED_DOMAIN
    candidate.gate_imports.append({
        'format': 'flowpilot-gate-import', 'version': 1,
        'source_path': document.source_path, 'sha256': document.sha256,
        'source_format': document.source_format, 'variant': variant,
        'gate_id_map': identities, 'channel_map': channels,
        'original_gates': json.loads(json.dumps([asdict(gate) for gate in gates], allow_nan=False)),
        'notes': list(document.notes),
    })
    drawable = [gate for gate in imported if gate.channels]
    if open_plot and drawable:
        first = drawable[0]
        axes = first.channels
        candidate.plots.append(PlotSpec(new_id(), 'histogram' if len(axes) == 1 else 'scatter',
            axes[0], axes[1] if len(axes) > 1 else '', first.parent_id,
            first.scales[0], first.scales[1] if len(axes) > 1 else 'linear'))
    candidate.validate()
    return candidate


def validate_import_history(records):
    if not isinstance(records, list):
        raise ValueError('Invalid gate-import provenance')
    fields = {'format', 'version', 'source_path', 'sha256', 'source_format', 'variant',
              'gate_id_map', 'channel_map', 'original_gates', 'notes'}
    for record in records:
        if (not isinstance(record, dict) or set(record) != fields
                or record['format'] != 'flowpilot-gate-import' or type(record['version']) is not int or record['version'] != 1
                or not isinstance(record['source_path'], str) or not record['source_path']
                or not valid_sha256(record['sha256']) or record['source_format'] not in ('flowpilot-gates', 'gatingml-2.0', 'flowjo-wsp')
                or not (record['variant'] is None or isinstance(record['variant'], str))
                or not isinstance(record['notes'], list) or not all(isinstance(note, str) for note in record['notes'])
                or not isinstance(record['original_gates'], list) or not record['original_gates']):
            raise ValueError('Invalid gate-import provenance')
        gates = ordered_gates([parse_gate(gate) for gate in record['original_gates']])
        for key, expected in [('gate_id_map', {gate.id for gate in gates}),
                              ('channel_map', {channel for gate in gates for channel in gate.channels})]:
            mapping = record[key]
            if (not isinstance(mapping, dict) or set(mapping) != expected
                    or not all(isinstance(value, str) and value.strip() for value in mapping.values())):
                raise ValueError(f'Invalid imported {key}')
        if len(set(record['gate_id_map'].values())) != len(gates):
            raise ValueError('Imported gate identities must stay distinct')


def import_summary(record):
    return {key: record[key] for key in ('source_path', 'sha256', 'source_format', 'variant', 'notes')} | {'gate_count': len(record['gate_id_map'])}
