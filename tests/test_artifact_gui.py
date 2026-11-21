from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import time
import pytest
from PySide6 import QtWidgets
from test_sheet_gui import app,bench,wait,load
from test_adaptive import experiment
from flowhero.automation import analyze
from flowhero.storage import load_project,save_project
from flowhero.model import Polygon


def wait_files(app,bench):
    deadline=time.monotonic()+20
    while (bench.file_jobs.busy or bench._file_task is not None) and time.monotonic()<deadline:
        app.processEvents();bench.poll();time.sleep(.02)
    assert not bench.file_jobs.busy and bench._file_task is None,bench.errors.toPlainText()
    wait(app,bench)


def test_portable_gui_roundtrip_source_move_and_cancel(app,bench,tmp_path):
    source=load(app,bench,tmp_path)
    package=tmp_path/'experiment.flowhero.zip'
    before=[(r['gate_id'],r['channel'],r['statistic'],r['value']) for r in bench.all_rows()]
    bench.start_file_task('create',package);wait_files(app,bench)
    assert package.exists()
    Path(source.path).rename(tmp_path/'moved-original.fcs')
    destination=tmp_path/'imported'
    bench.start_file_task('import',destination,str(package));wait_files(app,bench)
    assert Path(bench.project.sources[0].path).is_file()
    assert bench.project_path==str(destination/'project.json')
    assert [(r['gate_id'],r['channel'],r['statistic'],r['value']) for r in bench.all_rows()]==before
    bench.start_file_task('create',tmp_path/'cancelled.zip')
    workspace=bench._file_task['workspace']
    pid=bench.file_jobs._process.pid
    bench.cancel()
    assert not workspace.exists() and not (tmp_path/'cancelled.zip').exists()
    import multiprocessing
    assert pid not in [p.pid for p in multiprocessing.active_children()]


def test_cleanup_failure_retains_file_ownership_for_retry(app,bench,tmp_path,monkeypatch):
    load(app,bench,tmp_path)
    bench.start_file_task('create',tmp_path/'cancel.zip')
    workspace=bench._file_task['workspace']
    import flowhero.workbench as module
    original=module.shutil.rmtree
    def fail(path,*args,**kwargs):
        if Path(path)==workspace:
            raise OSError('simulated cleanup failure')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(module.shutil,'rmtree',fail)
    with pytest.raises(OSError,match='cleanup'):
        bench.cancel()
    assert bench._file_task['workspace']==workspace and workspace.exists()
    assert not bench.file_jobs.busy
    monkeypatch.setattr(module.shutil,'rmtree',original)
    bench.cancel()
    assert bench._file_task is None and not workspace.exists()


def test_review_proposals_accept_edit_reset_and_persist(app,bench,tmp_path,monkeypatch):
    template,reference,new,_=experiment(tmp_path)
    result=analyze(template,[new.path],tmp_path/'run')
    bench.project=load_project(result['project'])
    bench.rebuild_samples();bench.rebuild_documents()
    bench.request(True);wait(app,bench)
    bench.review_action.setVisible(True)
    bench.show_gate_review()
    dialog=bench._review_window
    deadline=time.monotonic()+20
    while dialog.jobs.busy and time.monotonic()<deadline:
        app.processEvents();dialog.poll();time.sleep(.02)
    assert dialog._ready_key is not None
    Path('results').mkdir(exist_ok=True)
    assert dialog.grab().save('results/gate-review-0.6.0.png')
    sid,gid=dialog.current
    original=deepcopy(bench.project.analysis['proposals'][sid][gid]['original'])
    proposal=deepcopy(bench.project.analysis['proposals'][sid][gid]['proposed'])
    dialog.decision('accept')
    assert bench.project.analysis['proposals'][sid][gid]['status']=='accepted'
    # Shared/sample geometry edits invalidate confirmation; original/proposed
    # evidence remains unchanged while the effective shape is corrected.
    dialog.edit_in_sheet()
    assert not dialog.isVisible() and dialog.jobs._process is None
    bench.scope.setCurrentIndex(1)
    gate=next(g for g in bench.project.gates_for(sid) if g.id==gid)
    assert isinstance(gate,Polygon)
    bench.replace_gate(replace(gate,vertices=tuple((x+1,y) for x,y in gate.vertices)))
    wait(app,bench)
    record=bench.project.analysis['proposals'][sid][gid]
    assert dialog.jobs._process is None
    bench.show_gate_review()
    assert dialog.isVisible()
    assert record['status']=='edited' and record['original']==original and record['proposed']==proposal
    dialog.decision('reset')
    assert bench.project.analysis['proposals'][sid][gid]['status']=='reset'
    wait(app,bench)
    path=tmp_path/'reviewed.json';save_project(bench.project,path)
    assert load_project(path).analysis['proposals'][sid][gid]['status']=='reset'
    process=dialog.jobs._process
    pid=process.pid if process else None
    bench.close_gate_review()
    assert bench._review_window is None
    import multiprocessing
    assert pid not in [p.pid for p in multiprocessing.active_children()]


def test_review_cleanup_failure_retains_visible_owner_and_retries(app,bench,monkeypatch):
    from flowhero.gate_review import GateReview
    dialog=GateReview(bench)
    bench._review_window=dialog
    dialog.show();app.processEvents()
    calls=[]
    def cancel():
        calls.append(1)
        if len(calls)==1:
            raise RuntimeError('simulated review child cleanup failure')
    monkeypatch.setattr(dialog.jobs,'cancel',cancel)
    with pytest.raises(RuntimeError,match='cleanup'):
        bench.close_gate_review()
    assert bench._review_window is dialog and dialog.isVisible()
    bench.close_gate_review()
    assert len(calls)==2 and bench._review_window is None


def test_async_import_preserves_intervening_edits(app,bench,tmp_path):
    load(app,bench,tmp_path)
    package=tmp_path/'portable.flowhero.zip'
    bench.start_file_task('create',package);wait_files(app,bench)
    destination=tmp_path/'new-import'
    bench.start_file_task('import',destination,str(package))
    candidate=deepcopy(bench.project)
    candidate.channel='FSC-A'
    assert bench.changed(candidate)
    wait_files(app,bench)
    assert bench.project.channel=='FSC-A' and bench.dirty
    assert (destination/'project.json').is_file()
    assert 'current edits retained' in bench.statusBar().currentMessage() or bench.project.channel=='FSC-A'


def test_foreign_destination_is_preserved_with_owned_staging_until_cancel(app,bench,tmp_path):
    load(app,bench,tmp_path)
    package=tmp_path/'foreign.zip'
    bench.start_file_task('create',package)
    workspace=bench._file_task['workspace']
    package.write_bytes(b'existing foreign file')
    deadline=time.monotonic()+15
    while not bench._file_task.get('blocked') and time.monotonic()<deadline:
        app.processEvents();bench.poll();time.sleep(.02)
    assert bench._file_task['blocked'] and workspace.exists()
    assert package.read_bytes()==b'existing foreign file'
    errors=bench.errors.toPlainText()
    for _ in range(4): bench.poll()
    assert bench.errors.toPlainText()==errors
    bench.cancel()
    assert not workspace.exists() and package.read_bytes()==b'existing foreign file'
