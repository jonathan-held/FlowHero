"""Agent-only review window: original/proposed/current geometry stays inspectable."""
from copy import deepcopy
import json
from pathlib import Path
from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg
from .jobs import SessionJobs
from .model import Ellipse, PlotSpec, Polygon, RangeGate
from . import scales as scale_rules
from .review import decide, review_rows
from .storage import parse_gate


class GateReview(QtWidgets.QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner=owner
        self.jobs=SessionJobs()
        self.records=[]
        self.current=None
        self._ready_key=None
        self.setWindowTitle('Review agent-proposed gates')
        self.resize(1100,750)
        layout=QtWidgets.QVBoxLayout(self)
        self.notice=QtWidgets.QLabel('Provisional estimates require human inspection. White dashed = original; purple dotted = proposed; gold = current. Counts/MFI are outputs, never fitting targets.')
        self.notice.setWordWrap(True);layout.addWidget(self.notice)
        self.table=QtWidgets.QTableWidget(0,9)
        self.table.setHorizontalHeaderLabels(['Sample','Gate','Review','Reference count','Original on new data','Initial proposal count','Current count','% parent before / after','Flags'])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.currentCellChanged.connect(self.select_record)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table)
        self.graph=pg.PlotWidget()
        self.graph.addLegend()
        layout.addWidget(self.graph,1)
        self.details=QtWidgets.QPlainTextEdit();self.details.setReadOnly(True);self.details.setMaximumHeight(110)
        layout.addWidget(self.details)
        controls=QtWidgets.QHBoxLayout()
        for name,callback in [('Accept current geometry',lambda:self.decision('accept')),
                              ('Reset to original',lambda:self.decision('reset')),
                              ('Restore algorithm proposal',lambda:self.decision('restore')),
                              ('Edit current in sheet',self.edit_in_sheet)]:
            button=QtWidgets.QPushButton(name);button.clicked.connect(callback);controls.addWidget(button)
            if name=='Accept current geometry': self.accept_button=button
        layout.addLayout(controls)
        self.timer=QtCore.QTimer(self);self.timer.timeout.connect(self.poll);self.timer.start(60)

    def refresh(self, preview=True):
        preview = preview and self.isVisible()
        previous=self.current
        self.records=review_rows(self.owner.project)
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.records))
        for i,(sid,gid,record) in enumerate(self.records):
            source=next(s for s in self.owner.project.sources if s.id==sid)
            before,after=record['before'] or {},record['after'] or {}
            reference=record['reference_result'] or {}
            result=self.owner.results.get(sid,{})
            current=next((r['event_count'] for r in result.get('rows',[]) if r['gate_id']==gid),None) if result.get('_verified',False) else None
            values=[Path(source.path).name,record['original']['name'],record['status'],
                    str(reference.get('event_count','unknown')),str(before.get('event_count','unknown')),str(after.get('event_count','unknown')),str(current) if current is not None else 'pending',
                    f"{before.get('percent_parent','unknown')} / {after.get('percent_parent','unknown')}",
                    '; '.join(record['diagnostics'].get('flags',[]))]
            for j,value in enumerate(values):
                self.table.setItem(i,j,QtWidgets.QTableWidgetItem(value))
        index=next((i for i,(sid,gid,r) in enumerate(self.records) if (sid,gid)==previous),0)
        if self.records:
            self.table.setCurrentCell(index,0)
        self.table.blockSignals(False)
        if self.records:
            if preview: self.select_record(index)
        else:
            self.current=None;self.graph.clear()

    def select_record(self,row,*args):
        if not 0<=row<len(self.records):
            return
        sid,gid,record=self.records[row]
        self.current=(sid,gid)
        self._ready_key=None
        self.accept_button.setEnabled(False)
        self.details.setPlainText(json.dumps(record['diagnostics'],indent=2))
        self.graph.clear()
        if record['status']=='removed':
            self.jobs.supersede()
            return
        gate=next(g for g in self.owner.project.gates_for(sid) if g.id==gid)
        spec=PlotSpec('review','histogram' if isinstance(gate,RangeGate) else 'scatter',
                      gate.channels[0],'' if isinstance(gate,RangeGate) else gate.channels[1],
                      population_id=gate.parent_id,x_scale=gate.scales[0],
                      y_scale='linear' if isinstance(gate,RangeGate) else gate.scales[1])
        project=deepcopy(self.owner.project);project.plots=[spec]
        source=next(s for s in project.sources if s.id==sid)
        self.jobs.start(project,[source])
        self.details.appendPlainText('Loading current parent population…')

    def poll(self):
        for message in self.jobs.poll():
            if message['kind']=='error' or message['kind']=='crash':
                self.details.appendPlainText(message['message'])
            if message['kind']!='result' or self.current is None:
                continue
            sid,gid=self.current
            if message['source_id']!=sid:
                continue
            data=message['data']['sheet_plots']['review']
            if 'error' in data:
                self.details.appendPlainText(data['error']);continue
            self.graph.clear()
            if data['kind']=='histogram':
                self.graph.plot(data['edges'],data['counts'],stepMode='center',pen=pg.mkPen('#4ab9df'))
            else:
                self.graph.addItem(pg.ScatterPlotItem(data['x'],data['y'],size=2,pen=None,brush=pg.mkBrush(80,185,220,130)))
            record=self.owner.project.analysis['proposals'][sid][gid]
            original,proposed=parse_gate(record['original']),parse_gate(record['proposed'])
            current=next(g for g in self.owner.project.gates_for(sid) if g.id==gid)
            for gate,color,style,label in [(original,'#eeeeee',QtCore.Qt.PenStyle.DashLine,'Original'),
                                           (proposed,'#db86f5',QtCore.Qt.PenStyle.DotLine,'Algorithm proposal'),
                                           (current,'#ffc857',QtCore.Qt.PenStyle.SolidLine,'Current')]:
                self.outline(gate,pg.mkPen(color,width=2,style=style),label)
            self.graph.setLabel('bottom',current.channels[0]+('' if current.scales[0]=='linear' else f' ({scale_rules.describe(current.scales[0])})'))
            if len(current.channels)>1:
                self.graph.setLabel('left',current.channels[1]+('' if current.scales[1]=='linear' else f' ({scale_rules.describe(current.scales[1])})'))
            self.graph.autoRange()
            self._ready_key=(sid,gid,self.owner.project.revision)
            self.accept_button.setEnabled(True)

    def outline(self,gate,pen,label):
        if isinstance(gate,Ellipse):
            from .plot_sheet import ellipse_outline
            points=ellipse_outline(gate.center,gate.axes)
            self.graph.plot(points[:,0],points[:,1],pen=pen,name=label)
        elif isinstance(gate,Polygon):
            points=gate.vertices+gate.vertices[:1]
            self.graph.plot([p[0] for p in points],[p[1] for p in points],pen=pen,name=label)
        elif isinstance(gate,RangeGate):
            for value in (gate.minimum,gate.maximum):
                self.graph.addItem(pg.InfiniteLine(value,angle=90,pen=pen))
        elif all(v is not None for v in gate.minimum+gate.maximum):
            (x,y),(u,v)=gate.minimum,gate.maximum
            self.graph.plot([x,u,u,x,x],[y,y,v,v,y],pen=pen,name=label)
        else:
            for i,(lo,hi) in enumerate(zip(gate.minimum,gate.maximum)):
                for value in (lo,hi):
                    if value is not None:
                        self.graph.addItem(pg.InfiniteLine(value,angle=90 if i==0 else 0,pen=pen))

    def decision(self,action):
        if self.current is None:
            return
        sid,gid=self.current
        if action=='accept' and self._ready_key!=(sid,gid,self.owner.project.revision):
            self.owner.error('Wait for the current review population to load before accepting.')
            return
        try:
            candidate=decide(self.owner.project,sid,gid,action)
        except ValueError as exc:
            self.owner.error(exc)
            return
        # This explicit decision is applied after generic edit reconciliation;
        # restoring/resetting must not be overwritten as an incidental edit.
        self.owner.changed(candidate,gid,review_decision=(sid,gid,candidate.analysis['proposals'][sid][gid]['status']))

    def edit_in_sheet(self):
        if self.current is None:
            return
        sid,gid=self.current
        source_index=next(i for i,s in enumerate(self.owner.project.sources) if s.id==sid)
        self.owner.samples.setCurrentRow(source_index)
        gate=next((g for g in self.owner.project.gates_for(sid) if g.id==gid),None)
        if gate is None:
            return
        spec=next((p for p in self.owner.project.plots if p.population_id==gate.parent_id and p.x_channel==gate.channels[0]
                   and (len(gate.channels)==1 or p.y_channel==gate.channels[1])
                   and p.x_scale==gate.scales[0] and (len(gate.channels)==1 or p.y_scale==gate.scales[1])),None)
        if spec is None:
            from .model import new_id
            spec=PlotSpec(new_id(),'histogram' if len(gate.channels)==1 else 'scatter',gate.channels[0],
                          '' if len(gate.channels)==1 else gate.channels[1],population_id=gate.parent_id,
                          x_scale=gate.scales[0],y_scale='linear' if len(gate.channels)==1 else gate.scales[1])
            candidate=deepcopy(self.owner.project);candidate.plots.append(spec)
            if not self.owner.changed(candidate,gid):
                return
            self.owner.add_document(spec)
        self.owner.scope.setCurrentIndex(1)
        self.owner.tool.setCurrentIndex(0)
        self.owner.sheet.setActiveSubWindow(self.owner.documents[spec.id])
        self.owner.statusBar().showMessage('Edit the sample-specific gate handles in the sheet; return here to accept the corrected geometry.')
        try:
            self.jobs.cancel()
        except RuntimeError as exc:
            self.owner.error(f'Review cleanup incomplete; retry Close. {exc}')
            return
        self.hide()
        self.timer.stop()

    def showEvent(self,event):
        super().showEvent(event)
        self.timer.start(60)

    def closeEvent(self,event):
        try:
            self.jobs.cancel()
        except RuntimeError as exc:
            self.owner.error(f'Review cleanup incomplete; retry Close. {exc}')
            event.ignore()
            return
        self.timer.stop()
        event.accept()
