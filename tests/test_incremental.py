from dataclasses import replace
import time
import pytest
from flowhero.analysis import evaluate
from flowhero.cache import AnalysisCache
from flowhero.dependencies import result_key, retain_valid
from flowhero.jobs import SessionJobs, sheet_worker
from flowhero.model import PlotSpec, Project, Rectangle
from test_analysis import source_file


def test_branch_and_plot_dependencies_match_fresh_computation(tmp_path):
    source = source_file(tmp_path, [[2,2,2,1], [4,4,4,7], [8,8,8,11]])
    gates = [Rectangle('parent','Parent',None,('FSC-A','SSC-A'),(0,0),(10,10)),
             Rectangle('child','Child','parent',('SSC-A','SSC-H'),(0,0),(6,6)),
             Rectangle('sibling','Sibling',None,('FSC-A','SSC-A'),(5,5),(10,10))]
    spec = PlotSpec('hist','histogram','FL1-A',population_id='child',x_limits=(0,20))
    cache = AnalysisCache()
    first = evaluate(source,gates,'FL1-A',plot_specs=[spec],cache=cache)
    old = Project([source],gates,'FL1-A',plots=[spec])
    # Normalization/labels/layout have no scientific dependencies.
    styled = replace(spec,normalization='peak',geometry=(5,5,600,400))
    renamed = [replace(gates[0],name='Renamed'), *gates[1:]]
    current = Project([source],renamed,'FL1-A',plots=[styled])
    assert result_key(old,source) == result_key(current,source)
    warm = evaluate(source,renamed,'FL1-A',plot_specs=[styled],cache=cache)
    assert {k:v['miss'] for k,v in warm['cache']['counts'].items()} == {k:v['miss'] for k,v in first['cache']['counts'].items()}
    # Rebinning preserves all memberships and statistics.
    rebinned = replace(styled,bins=32)
    bins = evaluate(source,renamed,'FL1-A',plot_specs=[rebinned],cache=cache)
    assert bins['cache']['counts']['gate']['miss'] == 3
    assert bins['cache']['counts']['plot']['miss'] == 2
    # Child edit only; preserve the parent and independent branch.
    changed = [renamed[0],replace(renamed[1],maximum=(3,3)),renamed[2]]
    new = Project([source],changed,'FL1-A',plots=[rebinned])
    retained = retain_valid(warm,current,new,source)
    assert {r['gate_id'] for r in retained['rows']} == {'root','parent','sibling'}
    assert retained['sheet_plots'] == {}
    edited = evaluate(source,changed,'FL1-A',plot_specs=[rebinned],cache=cache)
    assert edited['cache']['counts']['gate']['miss'] == 4
    fresh = evaluate(source,changed,'FL1-A',plot_specs=[rebinned])
    assert edited['rows'] == fresh['rows'] and edited['sheet_plots'] == fresh['sheet_plots']
    # Parent edit invalidates that parent and its child, not the root sibling.
    evaluate(source,[replace(changed[0],maximum=(5,5)),*changed[1:]],'FL1-A',plot_specs=[rebinned],cache=cache)
    assert cache.counts['gate']['miss'] == 6
    assert cache.counts['sample']['miss'] == 1


def test_cache_bounds_source_freshness_and_eviction(tmp_path):
    source = source_file(tmp_path, [[2,2,2,10]])
    cache=AnalysisCache(max_bytes=500)
    evaluate(source,[], 'FL1-A',plot_specs=[],cache=cache)
    assert cache.bytes <= 500
    from pathlib import Path
    Path(source.path).write_bytes(b'changed')
    with pytest.raises(ValueError,match='Source content changed'):
        evaluate(source,[], 'FL1-A',plot_specs=[],cache=cache)


def collect(jobs):
    messages=[]; deadline=time.monotonic()+15
    while jobs.busy and time.monotonic()<deadline:
        messages.extend(jobs.poll());time.sleep(.01)
    assert not jobs.busy
    return messages


def test_persistent_worker_reuse_supersede_cancel_and_retry(tmp_path):
    source=source_file(tmp_path,[[2,2,2,10]])
    project=Project([source],channel='FL1-A',plots=[PlotSpec('h','histogram','FL1-A')])
    jobs=SessionJobs()
    try:
        jobs.start(project,[source],target=sheet_worker)
        first=collect(jobs); pid=jobs._process.pid
        assert first[-1]['kind']=='done'
        jobs.start(project,[source],target=sheet_worker)
        second=collect(jobs)
        assert jobs._process.pid==pid
        assert second[0]['data']['cache']['counts']['sample']=={'hit':1,'miss':1}
        jobs.start(project,[source],target=sheet_worker)
        jobs.supersede()
        project.revision=10
        jobs.start(project,[source],target=sheet_worker)
        latest=collect(jobs)
        assert all(m['revision']==10 for m in latest)
        jobs.cancel()
        assert jobs._process is None
        jobs.start(project,[source],target=sheet_worker)
        assert collect(jobs)[-1]['kind']=='done'
    finally:
        jobs.cancel()
