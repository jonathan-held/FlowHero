"""Supported agent workflow: saved strategy -> proposals -> reproducible artifacts."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
import csv
import json
import math
import os
import shutil
import tempfile

from . import __version__
from .adaptation import propose_translation
from .analysis import evaluate
from .cache import AnalysisCache
from .histograms import histogram_statistics_rows
from .model import ordered_gates
from .review import gate_record, population_status
from .storage import bind_source, digest, load_project, load_project_snapshot, save_project, verify_source


def summary(result, gate_id):
    row=next((r for r in result['rows'] if r['gate_id']==gate_id),None)
    if row is None:
        return None
    return {**{field:row.get(field) for field in ('event_count','percent_parent','percent_total')},
            'statistics':[{field:item[field] for field in ('channel','statistic','value','status')}
                          for item in result['rows'] if item['gate_id']==gate_id]}


def histogram_rows(project, results):
    rows=[]
    for source in project.sources:
        if source.id not in results:
            continue
        for plot in project.plots:
            if plot.kind!='histogram':
                continue
            spec=replace(plot,sample_ids=(),pinned=False)
            for row in histogram_statistics_rows(spec,results,source.id):
                status=population_status(project,source.id,spec.population_id)
                rows.append({**row,'plot_id':plot.id,'analysis_status':status})
    return rows


def evaluate_project(project, cache=None):
    results, failures={},[]
    for source in project.sources:
        try:
            results[source.id]=evaluate(source,project.gates_for(source.id),project.channel,
                                        statistic=project.statistic,plot_specs=project.plots,cache=cache)
            for pid,data in results[source.id]['sheet_plots'].items():
                if 'error' in data:
                    failures.append({'path':source.path,'source_id':source.id,'stage':'plot '+pid,'reason':data['error']})
        except Exception as exc:
            failures.append({'path':source.path,'source_id':source.id,'stage':'evaluation','reason':str(exc)})
    return results,failures


def analyze(template_path, input_paths, output, *, reference_id=None, bins=64,
            max_shift_fraction=.5, min_events=30, adapt_channels=()):
    if type(bins) is not int or not 8<=bins<=256:
        raise ValueError('Translation density grid requires 8–256 bins (bounded 2D correlation memory)')
    if not math.isfinite(max_shift_fraction) or max_shift_fraction<=0 or type(min_events) is not int or min_events<1:
        raise ValueError('Require a finite positive search fraction and positive minimum event count')
    if not input_paths:
        raise ValueError('Provide named new FCS inputs')
    template_path=Path(template_path).expanduser().resolve(strict=True)
    template,template_digest=load_project_snapshot(template_path)
    if template.compensation is not None:
        raise ValueError('Adaptive analysis is defined for uncompensated projects only')
    if any(hasattr(gate,'operands') for gate in template.default_gates()):
        raise ValueError('Adaptive analysis does not support combined (AND/OR/NOT) gates')
    if reference_id is None and len(template.sources)==1:
        reference_id=template.sources[0].id
    reference=next((s for s in template.sources if s.id==reference_id),None)
    if reference is None:
        raise ValueError('Select an explicit reference source ID from the saved project; use flowhero inspect PROJECT')
    verify_source(reference)
    if not any(p.kind=='histogram' for p in template.plots):
        raise ValueError('Save at least one configured histogram for the requested scoped statistics')
    output=Path(output).expanduser().absolute()
    if os.path.lexists(output):
        raise ValueError('Output directory exists; choose a new run directory')
    output.parent.mkdir(parents=True,exist_ok=True)
    cache=AnalysisCache()
    base=template.gates_for(reference.id)
    reference_result=evaluate(reference,base,template.channel,plot_specs=template.plots,cache=cache)
    project=deepcopy(template)
    project.sources=[]
    project.annotations={}
    project.overrides={}
    project.analysis=None
    project.revision=0
    project.gates=[g for g in base if not template.viability or g.id!=template.viability.id]
    project.viability=next((g for g in base if template.viability and g.id==template.viability.id),None)
    project.plots=[replace(p,sample_ids=(),pinned=False) for p in project.plots]
    if project.histogram:
        project.histogram=replace(project.histogram,sample_ids=())
    failures=[]
    for path in input_paths:
        try:
            project.sources.append(bind_source(str(path)))
        except Exception as exc:
            failures.append({'path':str(path),'stage':'binding','reason':str(exc)})
    provenance={'format':'flowpilot-adaptive-analysis','version':1,
                'created_utc':datetime.now(timezone.utc).isoformat(),
                'template':{'path':str(template_path),'sha256':template_digest},
                'reference':asdict(reference),'inputs':[asdict(s) for s in project.sources],
                'method':{'id':'parent-density-translation-v1','bins':bins,
                          'max_shift_fraction':max_shift_fraction,'min_events':min_events,
                          'adapt_channels':list(adapt_channels),
                          'fixed_threshold_policy':'Only FSC-/SSC- 2D gates translate by default; other channels require explicit authorization. Ranges/open quadrants stay fixed for review.',
                          'override_policy':'Chosen reference effective gates become originals; no unrelated sample override is transferred.',
                          'plot_scope':'Reopened histograms focus current input; statistics export enumerates every successful new input for each configured histogram.',
                          'app_version':__version__},
                'failures':failures,'proposals':{}}
    for source in project.sources:
        records={}
        provenance['proposals'][source.id]=records
        try:
            before=evaluate(source,base,project.channel,plot_specs=project.plots,cache=cache)
        except Exception as exc:
            before=None
            failures.append({'path':source.path,'source_id':source.id,'stage':'baseline','reason':str(exc)})
        for gate in ordered_gates(base):
            try:
                proposal,diagnostics,status=propose_translation(reference,source,base,project.gates_for(source.id),
                        gate,cache,bins=bins,max_shift_fraction=max_shift_fraction,min_events=min_events,
                        adapt_channels=adapt_channels)
            except Exception as exc:
                proposal,diagnostics,status=gate,{'flags':[str(exc)],'method':'parent-density-translation-v1'},'unresolved'
            if proposal != gate:
                project.set_override(source.id,proposal)
            records[gate.id]={'original':gate_record(gate),'proposed':gate_record(proposal),
                    'status':status,'diagnostics':diagnostics,
                    'reference_result':summary(reference_result,gate.id),
                    'before':summary(before,gate.id) if before else None,'after':None}
    project.analysis=provenance
    results,calculation_failures=evaluate_project(project,cache)
    failures.extend(calculation_failures)
    for source in project.sources:
        for gid,record in provenance['proposals'][source.id].items():
            record['after']=summary(results[source.id],gid) if source.id in results else None
    project.validate()
    staging=Path(tempfile.mkdtemp(prefix='.flowhero-analysis-',dir=output.parent))
    try:
        save_project(project,staging/'project.flowhero.json')
        # Reopen from disk and recompute with fresh, uncached analysis before
        # publishing any success report. Results never depend on retained pixels.
        reopened=load_project(staging/'project.flowhero.json')
        replay,replay_failures=evaluate_project(reopened)
        rows=histogram_rows(project,results)
        replay_rows=histogram_rows(reopened,replay)
        if rows!=replay_rows or calculation_failures!=replay_failures:
            raise ValueError('Reopen/recompute verification disagrees with generated results')
        fields=['plot_id','analysis_status','sample_id','sample','source_path','source_sha256',
                'gate_id','gate','parent_id','channel','event_count','percent_parent','percent_total',
                'statistic','value','status','domain']
        with (staging/'histogram-statistics.csv').open('w',newline='',encoding='utf-8') as stream:
            writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerows(rows)
        report={'format':'flowpilot-analysis-outcome','version':1,'app_version':__version__,
                'outcome':'partial' if failures else 'review_required',
                'review_required':True,'method':provenance['method'],'template':provenance['template'],
                'reference':asdict(reference),'inputs':provenance['inputs'],'failures':failures,
                'completed_samples':len(results),'requested_inputs':len(input_paths),
                'reference_original_included':False,
                'histogram_statistic_rows':len(rows),'reopen_recompute_verified':True,
                'artifacts':{p.name:digest(p) for p in staging.iterdir() if p.is_file()}}
        (staging/'outcome.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
        if os.path.lexists(output):
            raise ValueError('Output directory appeared; refusing to overwrite it')
        os.rename(staging,output)
        return {**report,'output':str(output),'project':str(output/'project.flowhero.json')}
    finally:
        if staging.exists():
            shutil.rmtree(staging)
