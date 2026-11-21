"""Owner of proposal provenance and explicit human-review transitions."""
from copy import deepcopy
from dataclasses import asdict
import json
from .dependencies import gate_key
from .model import valid_sha256

STATUSES = {'proposed', 'unresolved', 'accepted', 'edited', 'reset', 'removed'}


def gate_record(gate):
    return json.loads(json.dumps(asdict(gate), allow_nan=False))


def validate_analysis(project):
    from .storage import parse_gate
    a = project.analysis
    fields = {'format', 'version', 'created_utc', 'method', 'template', 'reference', 'inputs', 'failures', 'proposals'}
    if (not isinstance(a, dict) or set(a) != fields or a['format'] != 'flowpilot-adaptive-analysis'
            or type(a['version']) is not int or a['version'] != 1):
        raise ValueError('Invalid adaptive-analysis provenance')
    if not isinstance(a['created_utc'], str) or not isinstance(a['method'], dict):
        raise ValueError('Missing adaptation method/time')
    for key in ('template', 'reference'):
        item = a[key]
        if (not isinstance(item, dict) or not isinstance(item.get('path'), str)
                or not valid_sha256(item.get('sha256'))):
            raise ValueError('Invalid reference/template provenance')
    if not isinstance(a['inputs'], list) or not isinstance(a['failures'], list):
        raise ValueError('Invalid analysis input/failure inventory')
    inputs = {s.id: s for s in project.sources}
    input_ids={s.get('id') for s in a['inputs'] if isinstance(s,dict)}
    if len(input_ids)!=len(a['inputs']) or not input_ids<=set(inputs):
        raise ValueError('Analysis provenance must identify distinct existing inputs')
    for item in a['inputs']:
        if item.get('sha256') != inputs[item['id']].sha256 or not isinstance(item.get('path'), str):
            raise ValueError('Analysis input hash mismatch')
    if not isinstance(a['proposals'], dict) or not set(a['proposals']) <= input_ids:
        raise ValueError('Proposals must reference existing samples')
    defaults = {g.id:g for g in project.default_gates()}
    for sid, definitions in a['proposals'].items():
        if not isinstance(definitions, dict):
            raise ValueError('Invalid gate proposals')
        for gid, record in definitions.items():
            if (not isinstance(record,dict) or set(record) != {'original','proposed','status','diagnostics','reference_result','before','after'}
                    or record['status'] not in STATUSES or not isinstance(record['diagnostics'],dict)):
                raise ValueError('Invalid proposal/review status')
            original, proposed = parse_gate(record['original']), parse_gate(record['proposed'])
            if original.id != gid or proposed.id != gid or type(original) != type(proposed):
                raise ValueError('Proposal identity/type mismatch')
            if (original.channels,original.scales,original.parent_id) != (proposed.channels,proposed.scales,proposed.parent_id):
                raise ValueError('Adaptation must preserve gate channels, domains and hierarchy')
            if record['status'] != 'removed':
                current = defaults.get(gid)
                if current is None or (current.channels,current.scales,current.parent_id) != (original.channels,original.scales,original.parent_id):
                    raise ValueError('Proposal no longer matches the project gate contract')
    try:
        json.dumps(a,allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError('Analysis provenance must contain finite JSON values') from exc


def reconcile_edits(previous, current):
    """Semantic edits invalidate confirmation of changed gates and descendants."""
    if current.analysis is None or previous.analysis is None:
        return
    for source in current.sources:
        old = {g.id:g for g in previous.gates_for(source.id)}
        new = {g.id:g for g in current.gates_for(source.id)}
        changed = {gid for gid in old if gid not in new or gate_key(old[gid]) != gate_key(new[gid])}
        descendants = set(changed)
        while True:
            following = descendants | {g.id for g in new.values() if g.parent_id in descendants}
            if following == descendants:
                break
            descendants = following
        records = current.analysis['proposals'].get(source.id,{})
        for gid in descendants:
            if gid not in records:
                continue
            record = records[gid]
            if gid not in new:
                record['status'] = 'removed'
            elif gid in changed:
                record['status'] = 'edited'
            elif record['status'] in ('accepted','reset'):
                record['status'] = 'proposed'
                record['diagnostics']['review_note'] = 'Ancestor geometry changed; review this child again.'


def decide(project, source_id, gate_id, action):
    from .storage import parse_gate
    candidate = deepcopy(project)
    record = candidate.analysis['proposals'][source_id][gate_id]
    if record['status'] == 'removed':
        raise ValueError('Deleted gates cannot be reviewed')
    if action == 'accept':
        record['status'] = 'accepted'
    elif action in ('reset','restore'):
        gate = parse_gate(record['original' if action == 'reset' else 'proposed'])
        candidate.set_override(source_id, gate)
        # Invalidate children before recording this explicit decision.
        reconcile_edits(project,candidate)
        record['status'] = 'reset' if action == 'reset' else 'proposed'
    else:
        raise ValueError('Unknown review decision')
    candidate.validate()
    return candidate


def review_rows(project):
    if project.analysis is None:
        return []
    return [(sid,gid,record) for sid,records in project.analysis['proposals'].items()
            for gid,record in records.items()]


def population_status(project, source_id, population_id):
    """Numerical success and human gate review are independent facts."""
    if project.analysis is None or source_id not in {s['id'] for s in project.analysis['inputs']}:
        return 'manual'
    gid = None if population_id in (None,'root') else population_id
    by_id={g.id:g for g in project.default_gates()}
    records=project.analysis['proposals'].get(source_id,{})
    statuses=[]
    while gid is not None:
        statuses.append(records.get(gid,{}).get('status','unresolved'))
        gid=by_id[gid].parent_id
    if not statuses:
        return 'not_applicable'
    return 'reviewed' if all(s in ('accepted','reset') for s in statuses) else 'provisional'
