import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import pytest
from PySide6 import QtWidgets
from flowhero.gate_import import prepare_gate_import, read_gate_import
from flowhero.gate_import_dialog import GateImportDialog
from flowhero.model import Polygon, Project, RangeGate, Rectangle, Source
from flowhero.storage import digest, export_gates, export_gatingml, load_project, save_project
from test_sheet_gui import app, bench, count, load, wait


def strategy():
    return [Polygon('p', 'Population', None, ('FSC-A', 'SSC-A'), ((0,0),(10,0),(10,10),(0,10))),
            Rectangle('r', 'Singlets', 'p', ('SSC-A','SSC-H'), (0,0), (6,6)),
            RangeGate('v','Viable','r','FL1-A',0,8)]


def export(tmp_path, project=None):
    path = tmp_path/'exported gates.json'
    export_gates(project or Project(gates=strategy()),path)
    return path


def test_roundtrip_collision_mapping_and_persisted_provenance(tmp_path):
    project = Project(gates=strategy())
    path = export(tmp_path, project)
    original = deepcopy(project)
    document = read_gate_import(path)
    result = prepare_gate_import(project, document, channel_map={'FSC-A':'Forward','SSC-A':'Side','SSC-H':'Height','FL1-A':'Fluorescence'})
    assert project == original and len(result.default_gates()) == 6
    imported = result.gates[3:]
    assert not {gate.id for gate in imported} & {'p','r','v'}
    assert imported[1].parent_id == imported[0].id and imported[2].parent_id == imported[1].id
    assert imported[0].vertices == project.gates[0].vertices
    assert imported[0].channels == ('Forward','Side')
    record = result.gate_imports[0]
    assert record['sha256'] == digest(path) and record['source_path'] == str(path)
    assert record['gate_id_map']['v'] == imported[2].id
    assert record['original_gates'][0]['channels'] == ['FSC-A','SSC-A']
    saved = tmp_path/'imported.flowhero.json';save_project(result,saved)
    assert load_project(saved) == result
    # Audit history remains valid when an imported gate is later deleted.
    result.gates = result.gates[:3];result.plots=[]
    result.validate()


def test_choose_sample_geometry_without_mapping_adjustments_to_current_samples(tmp_path):
    gates = strategy()
    project = Project([Source('old-sample','old.fcs','a'*64)],gates,
        overrides={'old-sample':[replace(gates[0],vertices=((0,0),(4,0),(4,4),(0,4)))]})
    path = export(tmp_path,project)
    document = read_gate_import(path)
    target = Project([Source('different-sample','current.fcs','b'*64)])
    result = prepare_gate_import(target,document,variant='old-sample')
    assert result.gates[0].vertices == project.overrides['old-sample'][0].vertices
    assert result.gates[1] == gates[1] and result.overrides == {}
    assert result.gate_imports[0]['variant'] == 'old-sample'
    assert target.gates == []


@pytest.mark.parametrize('mutation',[
    lambda d:d.update(domain='compensated'),
    lambda d:d.update(coordinate_policy='unknown'),
    lambda d:d.update(version=True),
    lambda d:d['gates'][1].update(parent_id='missing'),
    lambda d:d['gates'][0].update(parent_id='r'),
    lambda d:d['gates'].append(d['gates'][0]),
    lambda d:d['gates'][0].update(vertices=[[0,0],[1,1],[2,2]]),
    lambda d:d.update(sample_overrides={'s':[dict(d['gates'][0],channels=['wrong','channels'])]}),
    lambda d:d['gates'][0].update(vertices=None),
])
def test_invalid_gate_documents_fail_without_changes(tmp_path,mutation):
    path = export(tmp_path)
    document = json.loads(path.read_text());mutation(document)
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):read_gate_import(path)


def test_duplicate_json_fields_and_nonfinite_numbers_reject(tmp_path):
    path = export(tmp_path)
    text = path.read_text()
    path.write_text(text.replace('"version": 1','"version": 1, "version": 1'))
    with pytest.raises(ValueError,match='Duplicate'):read_gate_import(path)
    path.write_text(text.replace('"minimum": 0','"minimum": NaN'))
    with pytest.raises(ValueError,match='number'):read_gate_import(path)


def test_channel_mapping_cannot_collapse_gate_dimensions(tmp_path):
    document = read_gate_import(export(tmp_path))
    with pytest.raises(ValueError,match='distinct'):
        prepare_gate_import(Project(),document,channel_map={name:'same' for name in ('FSC-A','SSC-A','SSC-H','FL1-A')})
    with pytest.raises(ValueError,match='Map every'):
        prepare_gate_import(Project(),document,channel_map={'FSC-A':'Forward'})


def test_quadrant_group_collision_stays_separate(tmp_path):
    gates = [Rectangle('q1','Upper',None,('FSC-A','SSC-A'),(4,4),(None,None),group_id='q'),
             Rectangle('q2','Lower',None,('FSC-A','SSC-A'),(None,None),(4,4),group_id='q')]
    document = read_gate_import(export(tmp_path,Project(gates=gates)))
    result = prepare_gate_import(Project(gates=gates),document)
    assert result.gates[2].group_id == result.gates[3].group_id != 'q'
    assert result.gates[2].minimum == (4,4)


def test_version_six_project_migrates_without_import_history(tmp_path):
    path = tmp_path/'old.json';save_project(Project(gates=strategy()),path)
    data = json.loads(path.read_text());data['version']=6;del data['project']['gate_imports'];del data['project']['annotations'];del data['project']['compensation']
    path.write_text(json.dumps(data))
    assert load_project(path).gate_imports == []
    assert load_project(path).gates == strategy()


@pytest.mark.parametrize('xml', [False, True])
def test_gui_import_recompute_undo_save_and_reopen(app,bench,tmp_path,monkeypatch,xml):
    load(app,bench,tmp_path)
    path=export(tmp_path)
    if xml:
        path=tmp_path/'exported.xml';export_gatingml(Project(gates=strategy()),path)
    names=('p','r','v') if xml else ('Population','Singlets','Viable')
    original=deepcopy(bench.project)
    monkeypatch.setattr(QtWidgets.QFileDialog,'getOpenFileName',lambda *a,**kw:(str(path),''))
    def accept(dialog):
        dialog.accept()
        assert dialog.candidate is not None,dialog.error_label.text()
        return dialog.result()
    monkeypatch.setattr(GateImportDialog,'exec',accept)
    bench.import_gate_file();wait(app,bench)
    assert count(bench,names[0])==5 and count(bench,names[1])==4 and count(bench,names[2])==3
    assert len(bench.project.plots)==len(original.plots)+1
    assert len(bench.project.gate_imports)==1
    saved=tmp_path/'imported.json';save_project(bench.project,saved)
    assert load_project(saved)==bench.project
    bench.undo();wait(app,bench)
    assert bench.project.gates==original.gates and bench.project.gate_imports==[]
    bench.redo();wait(app,bench)
    assert count(bench,names[2])==3 and len(bench.project.gate_imports)==1


def test_gui_cancel_or_changed_project_preserves_current_strategy(app,bench,tmp_path,monkeypatch):
    path=export(tmp_path)
    monkeypatch.setattr(QtWidgets.QFileDialog,'getOpenFileName',lambda *a,**kw:(str(path),''))
    original=deepcopy(bench.project)
    monkeypatch.setattr(GateImportDialog,'exec',lambda self:QtWidgets.QDialog.DialogCode.Rejected)
    bench.import_gate_file()
    assert bench.project==original
    def changed(dialog):
        dialog.accept()
        bench.project=Project(channel='intervening edit')
        return dialog.result()
    monkeypatch.setattr(GateImportDialog,'exec',changed)
    bench.import_gate_file()
    assert bench.project.channel=='intervening edit' and not bench.project.gates
    assert 'changed during import' in bench.errors.toPlainText()


def test_dialog_requires_explicit_mapping_for_missing_channels(app,tmp_path):
    document=read_gate_import(export(tmp_path))
    dialog=GateImportDialog(Project(),document,['Forward','SSC-A','SSC-H','FL1-A'])
    dialog.accept()
    assert dialog.candidate is None and 'missing channel' in dialog.error_label.text()
    dialog.mapping['FSC-A'].setCurrentText('Forward');dialog.accept()
    assert dialog.candidate.gates[0].channels==('Forward','SSC-A')


def test_headless_import_preserves_inputs_and_rejects_existing_output(tmp_path,capsys):
    from flowhero.commands import run
    original=tmp_path/'original.json';save_project(Project(),original)
    gates=export(tmp_path);before=(digest(original),digest(gates))
    output=tmp_path/'imported.json'
    assert run(['import-gates',str(original),str(gates),'--output',str(output),'--channel','FSC-A','Forward'])==0
    result=json.loads(capsys.readouterr().out)
    assert result['gate_import']['sha256']==before[1]
    assert load_project(output).gates[0].channels==('Forward','SSC-A')
    assert before==(digest(original),digest(gates))
    assert run(['import-gates',str(original),str(gates),'--output',str(output)])==1
    assert 'exists' in capsys.readouterr().out


def test_import_history_survives_portable_package_without_export_file(tmp_path):
    from flowhero.portable import create_package, import_package
    from test_analysis import source_file
    source=source_file(tmp_path,[[2,2,2,1],[3,3,3,4]])
    path=export(tmp_path)
    project=prepare_gate_import(Project(sources=[source],channel='FL1-A'),read_gate_import(path))
    archive=tmp_path/'imported.flowhero.zip';create_package(project,archive)
    path.unlink()
    restored,_=import_package(archive,tmp_path/'restored')
    assert restored.gate_imports==project.gate_imports
    assert restored.gates==project.gates


@pytest.mark.parametrize('field,value', [('channels','xy'),('channels',{'x':1,'y':2}),('minimum','01'),('maximum',{'x':2,'y':2})])
def test_rectangle_json_arrays_are_not_reinterpreted(tmp_path,field,value):
    path=export(tmp_path)
    data=json.loads(path.read_text());data['gates'][1][field]=value
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='JSON array'):read_gate_import(path)


def test_reimport_reserves_deleted_import_identities(tmp_path):
    document=read_gate_import(export(tmp_path))
    first=prepare_gate_import(Project(),document)
    first.gates=[];first.plots=[];first.validate()
    second=prepare_gate_import(first,document)
    assert not set(second.gate_imports[0]['gate_id_map'].values()) & set(second.gate_imports[1]['gate_id_map'].values())


def test_reimport_does_not_reuse_removed_analysis_proposal_identity(tmp_path):
    from flowhero.automation import analyze
    from test_adaptive import experiment
    template, _, new, _ = experiment(tmp_path)
    analyze(template,[new.path],tmp_path/'analysis')
    project=load_project(tmp_path/'analysis/project.flowhero.json')
    for records in project.analysis['proposals'].values():
        for record in records.values():
            record['status']='removed'
    project.gates=[];project.plots=[];project.overrides={};project.validate()
    result=prepare_gate_import(project,read_gate_import(export(tmp_path)))
    assert 'p' not in {gate.id for gate in result.gates}
    assert all(record['status']=='removed' for records in result.analysis['proposals'].values() for record in records.values())


@pytest.mark.parametrize('mutation',[
    lambda gates:replace(gates[0],minimum=(None,None),maximum=(None,None)),
    lambda gates:replace(gates[0],minimum=(0,0),maximum=(4,4)),
    lambda gates:replace(gates[0],group_id='q2'),
    lambda gates:replace(gates[0],minimum=(5,4)),
])
def test_invalid_quadrant_group_metadata_is_rejected(tmp_path,mutation):
    gates=[Rectangle('q1','Upper',None,('FSC-A','SSC-A'),(4,4),(None,None),group_id='group'),
           Rectangle('q2','Lower',None,('FSC-A','SSC-A'),(None,None),(4,4),group_id='group')]
    path=export(tmp_path,Project(gates=gates))
    from dataclasses import asdict
    data=json.loads(path.read_text());data['gates'][0]=asdict(mutation(gates));path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='Quadrant group'):read_gate_import(path)


def test_ungrouped_open_rectangle_outline_tracks_view_and_is_retired(app,bench,tmp_path):
    import numpy as np
    load(app,bench,tmp_path)
    gate=Rectangle('open','Open bounds',None,('FSC-A','SSC-A'),(4,4),(None,None))
    bench.add_gates([gate]);wait(app,bench)
    document=next(document for document in bench.documents.values() if document.spec.kind=='scatter')
    item=document.gate_items[gate.id]
    document.graph.setXRange(-2,30,padding=0);document.graph.setYRange(-2,40,padding=0);app.processEvents()
    x,y=item.getData()
    points=np.column_stack((x,y))
    assert [4,40] in points.tolist() and [30,4] in points.tolist()
    before=bench.project.revision
    document.render()
    assert document.gate_items[gate.id] is not item and item.scene() is None
    assert bench.project.revision==before
