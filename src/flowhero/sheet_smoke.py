"""Installed/frozen smoke of the human-facing sheet and real worker/export path."""
from dataclasses import replace
import json
from pathlib import Path
import time


def run(output):
    import flowio
    from PySide6 import QtWidgets
    from . import __version__
    from .model import Polygon, RangeGate, Rectangle, PlotSpec, Project
    from .workbench import Workbench
    from .storage import bind_source, save_project, load_project, export_csv, export_gates, export_gatingml
    root = Path(output).resolve(); root.mkdir(parents=True, exist_ok=False)
    events = [[2,2,2,1],[3,3,3,4],[4,4,4,7],[5,5,5,10000],[7,7,7,500],[20,2,2,-10000]]
    sources=[]
    for multiplier in (1,2):
        path=root/f'demo-{multiplier}.fcs'
        with path.open('wb') as stream:
            flowio.create_fcs(stream,[value*multiplier if i==3 else value for row in events for i,value in enumerate(row)],['FSC-A','SSC-A','SSC-H','FL1-A'])
        sources.append(bind_source(str(path)))
    gates=[Polygon('population','U937',None,('FSC-A','SSC-A'),((0,0),(10,0),(10,10),(0,10))),
           Rectangle('singlets','Singlets','population',('SSC-A','SSC-H'),(0,0),(6,6)),
           RangeGate('viable','Viable','singlets','FL1-A',0,8),
           RangeGate('deep','Nested range','viable','FL1-A',0,5),
           Polygon('logpoly','Log population',None,('FSC-A','SSC-A'),((0,0),(1,0),(1,10),(0,10)),('log','linear'))]
    for i,lo,hi in [(1,(3.5,3.5),(None,None)),(2,(None,3.5),(3.5,None)),(3,(None,None),(3.5,3.5)),(4,(3.5,None),(None,3.5))]:
        gates.append(Rectangle(f'q{i}',f'Q{i}',None,('FSC-A','SSC-A'),lo,hi,group_id='quadrants'))
    plots=[PlotSpec('scatter','scatter','FSC-A','SSC-A',geometry=(0,0,510,370)),
           PlotSpec('density','density','SSC-A','SSC-H',population_id='population',bins=16,geometry=(510,0,510,370)),
           PlotSpec('hist','histogram','FL1-A',population_id='singlets',x_scale='log',x_limits=(1,100000),bins=16,
                    geometry=(0,370,510,370),sample_ids=tuple(s.id for s in sources)),
           PlotSpec('logscatter','scatter','FSC-A','SSC-A',x_scale='log',geometry=(510,370,510,370))]
    project=Project(sources,gates,'FL1-A',plots=plots)
    project.set_override(sources[1].id,replace(gates[2],maximum=15))
    app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window=Workbench()
    try:
        window.project=project;window.set_channels(['FSC-A','SSC-A','SSC-H','FL1-A'])
        window.rebuild_samples();window.update_tree();window.rebuild_documents();window.show()
        window.request(True)
        deadline=time.monotonic()+90
        while window.jobs.busy and time.monotonic()<deadline:
            app.processEvents();window.poll();time.sleep(.02)
        if window.jobs.busy or len(window.results)!=2:
            raise RuntimeError('Sheet worker failed: '+window.errors.toPlainText())
        counts=[]
        for source in sources:
            rows=window.results[source.id]['rows']
            counts.append({r['gate_id']:r['event_count'] for r in rows})
        for count in counts:
            assert [count[g] for g in ['population','singlets','viable','q1','q2','q3','q4']]==[5,4,3,3,0,2,1]
            assert count['logpoly']==5
        assert counts[0]['deep']==2 and counts[1]['deep']==1
        assert all(sum(window.results[s.id]['sheet_plots']['hist']['counts'])==4 for s in sources)
        assert len(window.documents)==4
        save_project(project,root/'project.flowhero.json')
        assert load_project(root/'project.flowhero.json')==project
        export_csv(window.all_rows(),root/'statistics.csv')
        export_gates(project,root/'gates.json')
        export_gatingml(project,root/'gates.gatingml')
        window.tile();app.processEvents();window.grab().save(str(root/'sheet.png'))
        result={'version':__version__,'result':'PASS','qt_platform':app.platformName(),'counts':counts,
                'checks':['plot sheet','histogram/scatter/density','log gate domains','quadrants','deep hierarchy',
                          'sample override','full-event statistics','save/reopen','CSV','gate JSON/Gating-ML','spawned worker cleanup']}
    finally:
        window.dirty=False;window.close();app.processEvents()
    assert not window.jobs.busy
    (root/'smoke.json').write_text(json.dumps(result,indent=2)+'\n')
    print('PASS FlowHero sheet/worker/export smoke:',root)
