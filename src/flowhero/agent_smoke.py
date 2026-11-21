"""Headless installed-artifact check for adaptation, replays and portable inputs."""
import json
from pathlib import Path
import numpy as np
import flowio
from . import __version__
from .automation import analyze, evaluate_project, histogram_rows
from .model import Polygon,RangeGate,PlotSpec,Project
from .portable import create_package,import_package
from .storage import bind_source,save_project,load_project,digest


def run(output):
    root=Path(output).absolute()
    root.mkdir(parents=True,exist_ok=False)
    rng=np.random.default_rng(112);count=1500
    events=np.column_stack([rng.normal(100,3,count),rng.normal(100,4,count),
                            rng.normal(100,4,count),rng.lognormal(3,.2,count)])
    sources=[]
    for name,data in [('reference.fcs',events),('new.fcs',events+np.array([6,-4,2,0]))]:
        data=data.copy()
        if name=='new.fcs': data[:,3]*=4
        path=root/name
        with path.open('wb') as stream:
            flowio.create_fcs(stream,data.flatten().tolist(),['FSC-A','SSC-A','SSC-H','FL1-A'])
        sources.append(bind_source(str(path)))
    gates=[Polygon('p','Population',None,('FSC-A','SSC-A'),((85,80),(115,80),(115,120),(85,120))),
           Polygon('s','Singlets','p',('SSC-A','SSC-H'),((80,80),(120,80),(120,120),(80,120))),
           RangeGate('threshold','Fixed fluorescence','s','FL1-A',0,100)]
    template=Project([sources[0]],gates,'FL1-A',plots=[PlotSpec('h','histogram','FL1-A',population_id='s',x_limits=(0,200))])
    save_project(template,root/'template.json')
    outcome=analyze(root/'template.json',[sources[1].path],root/'run')
    project=load_project(root/'run/project.flowhero.json')
    sid=project.sources[0].id
    records=project.analysis['proposals'][sid]
    for gid,shift in [('p',[6,-4]),('s',[-4,2])]:
        assert records[gid]['status']=='proposed',records[gid]['diagnostics']
        assert np.max(np.abs(np.asarray(records[gid]['diagnostics']['shift'])-shift))<1
    assert records['threshold']['status']=='unresolved'
    original,errors=evaluate_project(project);assert not errors
    before=histogram_rows(project,original)
    create_package(project,root/'experiment.flowhero.zip')
    original_hash=digest(sources[1].path)
    Path(sources[1].path).rename(root/'original-new-input-moved.fcs')
    imported,path=import_package(root/'experiment.flowhero.zip',root/'imported')
    replay,errors=evaluate_project(imported);assert not errors
    after=histogram_rows(imported,replay)
    compared=['plot_id','analysis_status','sample_id','source_sha256','gate_id','channel','event_count','statistic','value','status']
    assert [[r[k] for k in compared] for r in before]==[[r[k] for k in compared] for r in after]
    assert imported.analysis==project.analysis
    assert digest(imported.sources[0].path)==original_hash
    result={'result':'PASS','version':__version__,'module_root':str(Path(__file__).parent),
            'proposal_shifts':{gid:record['diagnostics'].get('shift') for gid,record in records.items()},
            'counts':{gid:record['after'] for gid,record in records.items()},
            'analysis_outcome':outcome['outcome'],'reopen_recompute_verified':True,
            'portable_recompute_after_original_moved':True,'reference_original_in_package':False}
    (root/'smoke.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
