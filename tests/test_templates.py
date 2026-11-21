from dataclasses import replace
import pytest
from flowhero.model import Project, PlotSpec
from flowhero.templates import apply_template, save_template
from flowhero.storage import load_project
from test_analysis import source_file, polygons


def test_shared_template_rebinds_sources_without_foreign_adjustments(tmp_path):
    source=source_file(tmp_path,[[2,2,2,10]])
    gates=polygons()
    project=Project([source],gates,'FL1-A',plots=[PlotSpec('h','histogram','FL1-A',sample_ids=(source.id,))],
                    overrides={source.id:[replace(gates[0],vertices=((0,0),(5,0),(5,5),(0,5)))]})
    path=save_template(project,tmp_path/'templates','Shared experiment')
    saved=load_project(path)
    assert saved.sources==[] and saved.overrides=={} and saved.plots[0].sample_ids==()
    new=replace(source,id='new-sample')
    applied=apply_template(path,[new])
    assert applied.sources==[new] and applied.gates==gates and applied.overrides=={}
    assert project.overrides
    with pytest.raises(ValueError,match='exists'):
        save_template(project,tmp_path/'templates','Shared experiment')
