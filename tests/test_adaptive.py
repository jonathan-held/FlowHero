from copy import deepcopy
from dataclasses import replace
import csv,json,subprocess,sys
from pathlib import Path
import numpy as np
import pytest
from flowhero.automation import analyze
from flowhero.model import Polygon,RangeGate,PlotSpec,Project
from flowhero.review import decide,reconcile_edits
from flowhero.storage import save_project,load_project,digest
from test_analysis import source_file


def experiment(tmp_path):
    rng=np.random.default_rng(112)
    count=1500
    events=np.column_stack([rng.normal(100,3,count),rng.normal(100,4,count),
                            rng.normal(100,4,count),rng.lognormal(3,.2,count)])
    refdir=tmp_path/'reference';refdir.mkdir()
    newdir=tmp_path/'new';newdir.mkdir()
    reference=source_file(refdir,events)
    changed=events.copy();changed[:,:3]+=np.array([6.,-4.,2.]);changed[:,3]*=4
    new=source_file(newdir,changed)
    gates=[Polygon('p','Population',None,('FSC-A','SSC-A'),((85,80),(115,80),(115,120),(85,120))),
           Polygon('s','Singlets','p',('SSC-A','SSC-H'),((80,80),(120,80),(120,120),(80,120))),
           RangeGate('threshold','Fixed fluorescence','s','FL1-A',0,100)]
    project=Project([reference],gates,'FL1-A',plots=[PlotSpec('h','histogram','FL1-A',population_id='s',x_limits=(0,200))])
    path=tmp_path/'template.json';save_project(project,path)
    return path,reference,new,project


def test_adaptive_cli_outputs_proposals_reopen_counts_and_unmoved_fluorescence(tmp_path):
    template,reference,new,original=experiment(tmp_path)
    before_hash=digest(template)
    output=tmp_path/'run'
    result=analyze(template,[new.path],output)
    assert result['outcome']=='review_required' and result['reopen_recompute_verified']
    project=load_project(output/'project.flowhero.json')
    source=project.sources[0]
    records=project.analysis['proposals'][source.id]
    assert project.analysis['template']['sha256']==before_hash
    assert project.analysis['reference']['sha256']==reference.sha256
    for gid,expected in [('p',[6,-4]),('s',[-4,2])]:
        record=records[gid]
        assert record['status']=='proposed',record['diagnostics']
        assert np.asarray(record['diagnostics']['shift'])==pytest.approx(expected,abs=1)
        old=np.asarray(record['original']['vertices'])
        proposed=np.asarray(record['proposed']['vertices'])
        assert np.diff(old,axis=0)==pytest.approx(np.diff(proposed,axis=0))
        assert record['after']['event_count']>1400
    assert records['threshold']['status']=='unresolved'
    assert records['threshold']['original']==records['threshold']['proposed']
    with (output/'histogram-statistics.csv').open(newline='') as f:
        rows=list(csv.DictReader(f))
    assert len(rows)==3 and all(r['analysis_status']=='provisional' for r in rows)
    from flowhero.analysis import evaluate
    reference_result=evaluate(reference,original.gates,'FL1-A',plot_specs=original.plots)
    median=next(float(r['value']) for r in rows if r['statistic']=='median')
    refmedian=next(r['value'] for r in reference_result['rows'] if r['gate_id']=='s' and r['channel']=='FL1-A' and r['statistic']=='median')
    assert median/refmedian==pytest.approx(4,rel=.02)
    assert digest(template)==before_hash
    # A partial failure is explicit and preserves useful complete artifacts.
    partial=analyze(template,[new.path,str(tmp_path/'missing.fcs')],tmp_path/'partial')
    assert partial['outcome']=='partial' and partial['completed_samples']==1
    assert partial['failures'][0]['stage']=='binding'
    proc=subprocess.run([sys.executable,'-m','flowhero','inspect',str(output/'project.flowhero.json')],
                        text=True,capture_output=True)
    assert proc.returncode==0 and json.loads(proc.stdout)['sources'][0]['sha256']==new.sha256


def test_review_accept_edit_descendants_reset_and_provenance_validation(tmp_path):
    template,reference,new,_=experiment(tmp_path)
    analyze(template,[new.path],tmp_path/'run')
    project=load_project(tmp_path/'run/project.flowhero.json')
    sid=project.sources[0].id
    accepted=decide(project,sid,'p','accept')
    accepted=decide(accepted,sid,'s','accept')
    assert accepted.analysis['proposals'][sid]['p']['status']=='accepted'
    edited=deepcopy(accepted)
    gate=next(g for g in edited.gates_for(sid) if g.id=='p')
    edited.set_override(sid,replace(gate,vertices=tuple((x+1,y) for x,y in gate.vertices)))
    reconcile_edits(accepted,edited)
    assert edited.analysis['proposals'][sid]['p']['status']=='edited'
    assert edited.analysis['proposals'][sid]['s']['status']=='proposed'
    reset=decide(edited,sid,'p','reset')
    assert reset.gates_for(sid)[0]==project.gates[0]
    assert reset.analysis['proposals'][sid]['p']['status']=='reset'
    bad=deepcopy(reset);bad.analysis['template']['sha256']='x'*64
    with pytest.raises(ValueError,match='provenance'):
        bad.validate()


def test_missing_population_and_ambiguous_cluster_are_not_auto_applied(tmp_path):
    template,reference,new,project=experiment(tmp_path)
    missingdir=tmp_path/'empty-pop';missingdir.mkdir()
    missing=source_file(missingdir,[[300,300,300,1]]*100)
    result=analyze(template,[missing.path],tmp_path/'missing-run')
    loaded=load_project(tmp_path/'missing-run/project.flowhero.json')
    assert loaded.analysis['proposals'][loaded.sources[0].id]['p']['status']=='unresolved'
    from flowhero.adaptation import propose_translation
    from flowhero.cache import AnalysisCache
    from flowhero.analysis import population_values
    reference_values=population_values(reference,[],None,('FSC-A','SSC-A'),('linear','linear'))
    ambiguousdir=tmp_path/'ambiguous';ambiguousdir.mkdir()
    # Two equally supported alternatives; no reference-count target chooses one.
    base=np.column_stack([reference_values[:,0],reference_values[:,1],np.full(len(reference_values),100),np.ones(len(reference_values))])
    left=base.copy();right=base.copy();left[:,0]-=10;right[:,0]+=10
    ambiguous=source_file(ambiguousdir,np.vstack([left,right]))
    proposed,diagnostics,status=propose_translation(reference,ambiguous,project.gates,project.gates,project.gates[0],AnalysisCache())
    assert status=='unresolved',diagnostics
    assert proposed==project.gates[0]
    assert any('Ambiguous' in flag for flag in diagnostics['flags'])


def test_manual_inputs_do_not_rewrite_historical_agent_provenance(tmp_path):
    template,reference,new,_=experiment(tmp_path)
    analyze(template,[new.path],tmp_path/'run')
    project=load_project(tmp_path/'run/project.flowhero.json')
    provenance=deepcopy(project.analysis)
    from flowhero.storage import bind_source
    from flowhero.review import population_status
    manual=bind_source(reference.path)
    project.sources.append(manual)
    project.validate()
    assert project.analysis==provenance
    assert population_status(project,manual.id,'p')=='manual'
    path=tmp_path/'manual-plus-agent.json';save_project(project,path)
    assert load_project(path).analysis==provenance


def test_identical_source_preserves_shape_but_not_missing_population_validity(tmp_path):
    template,reference,new,project=experiment(tmp_path)
    from flowhero.adaptation import propose_translation
    from flowhero.cache import AnalysisCache
    same=replace(reference,id='same-bytes-new-binding')
    gate=project.gates[0]
    proposal,diagnostics,status=propose_translation(reference,same,project.gates,project.gates,gate,AnalysisCache())
    assert proposal==gate and status=='proposed' and 'identity_evidence' in diagnostics
    missing=replace(gate,vertices=((300,300),(330,300),(330,330),(300,330)))
    proposal,diagnostics,status=propose_translation(reference,same,[missing],[missing],missing,AnalysisCache())
    assert status=='unresolved' and 'Insufficient' in diagnostics['flags'][0]
