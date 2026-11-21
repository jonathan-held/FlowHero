"""Qt interaction and presentation. Scientific membership lives in analysis.py."""
from copy import deepcopy
from multiprocessing import freeze_support
from pathlib import Path
import sys
import numpy as np
from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg
from .jobs import Jobs, _bind
from .model import Histogram, Polygon, Project, RangeGate, Source, new_id
from .plot_sheet import dismantle_item
from .storage import export_csv, load_project, save_project

AXES = [('FSC-A', 'SSC-A'), ('SSC-A', 'SSC-H')]
NAMES = ['U937', 'Single cells']
STATISTIC_LABELS = {'median': 'Median', 'arithmetic_mean': 'Arithmetic mean',
                    'geometric_mean': 'Geometric mean'}


class Window(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('FlowHero — local cytometry')
        self.resize(1250, 850)
        self.project = Project()
        self.project_path = None
        self.dirty = False
        self.jobs = Jobs()
        self.rows = {}
        self.channels = []
        self._preview_key = None
        self._job_mode = None
        self._after_job = None
        self._drawing = None
        self._points = []
        self._rois = [None, None]
        self._rendering = False
        self._batch_errors = []
        self.histograms = {}
        self._histogram_suggestion = None
        self._build()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(70)

    def _build(self):
        toolbar = self.addToolBar('Project')
        for label, callback in [('Add FCS…', self.add_files), ('Open project…', self.open_project),
                                ('Save project', self.save), ('Save as…', lambda: self.save(True)),
                                ('Relink sample…', self.relink_selected), ('Export CSV…', self.export)]:
            toolbar.addAction(label, callback)
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)
        domain = QtWidgets.QLabel('Analysis: acquisition-preprocessed, uncompensated values • linear gate axes • full-event statistics')
        domain.setWordWrap(True)
        layout.addWidget(domain)
        splitter = QtWidgets.QSplitter()
        layout.addWidget(splitter, 1)
        left = QtWidgets.QWidget()
        panel = QtWidgets.QVBoxLayout(left)
        panel.addWidget(QtWidgets.QLabel('Samples'))
        self.samples = QtWidgets.QListWidget()
        self.samples.setMinimumHeight(100)
        self.samples.currentRowChanged.connect(self.sample_selected)
        panel.addWidget(self.samples, 1)
        self.sample_info = QtWidgets.QLabel('No sample loaded')
        self.sample_info.setWordWrap(True)
        panel.addWidget(self.sample_info)
        self.gate_summary = QtWidgets.QLabel('No gates yet')
        self.gate_summary.setWordWrap(True)
        panel.addWidget(self.gate_summary)
        panel.addWidget(QtWidgets.QLabel('Gate edits apply to'))
        self.edit_scope = QtWidgets.QComboBox()
        self.edit_scope.addItems(['Shared defaults', 'Selected sample only'])
        self.edit_scope.currentIndexChanged.connect(self.scope_changed)
        panel.addWidget(self.edit_scope)
        reset = QtWidgets.QPushButton('Use shared gates for this sample')
        reset.clicked.connect(self.reset_sample_gates)
        panel.addWidget(reset)
        for index, name in enumerate(NAMES):
            button = QtWidgets.QPushButton('Draw ' + name)
            button.clicked.connect(lambda checked=False, i=index: self.begin_gate(i))
            panel.addWidget(button)
        finish = QtWidgets.QPushButton('Finish polygon')
        finish.clicked.connect(self.finish_gate)
        panel.addWidget(finish)
        cancel_draft = QtWidgets.QPushButton('Discard drawing')
        cancel_draft.clicked.connect(self.discard_drawing)
        panel.addWidget(cancel_draft)
        help_text = QtWidgets.QLabel('Draw: click at least 3 vertices, then Finish polygon. Right-click removes the last vertex. Drag handles to edit; click a gate edge to add a handle. Child plot shows U937 events. Only plotted dots are sampled.')
        help_text.setWordWrap(True)
        panel.addWidget(help_text)
        viability_box = QtWidgets.QGroupBox('Optional viability gate')
        viability_form = QtWidgets.QFormLayout(viability_box)
        self.viability_channel = QtWidgets.QComboBox()
        self.viability_min = QtWidgets.QLineEdit('0')
        self.viability_max = QtWidgets.QLineEdit('10000')
        viability_form.addRow('Channel', self.viability_channel)
        viability_form.addRow('Minimum (included)', self.viability_min)
        viability_form.addRow('Maximum (excluded)', self.viability_max)
        apply_range = QtWidgets.QPushButton('Set viability range')
        apply_range.clicked.connect(self.apply_viability)
        viability_form.addRow(apply_range)
        remove_range = QtWidgets.QPushButton('Remove viability (all samples)')
        remove_range.clicked.connect(self.remove_viability)
        viability_form.addRow(remove_range)
        panel.addWidget(viability_box)
        panel.addWidget(QtWidgets.QLabel('Statistic channel'))
        self.channel = QtWidgets.QComboBox()
        self.channel.currentTextChanged.connect(self.channel_changed)
        panel.addWidget(self.channel)
        panel.addWidget(QtWidgets.QLabel('Statistic'))
        self.statistic = QtWidgets.QComboBox()
        for key, label in STATISTIC_LABELS.items():
            self.statistic.addItem(label, key)
        self.statistic.setToolTip('All selected events are used. Geometric mean requires every value to be positive; otherwise status is nonpositive. Nonfinite values are reported, never dropped.')
        self.statistic.currentIndexChanged.connect(self.statistic_changed)
        panel.addWidget(self.statistic)
        self.analyse_button = QtWidgets.QPushButton('Apply to all samples')
        self.analyse_button.clicked.connect(self.analyse_all)
        panel.addWidget(self.analyse_button)
        self.cancel_button = QtWidgets.QPushButton('Cancel computation')
        self.cancel_button.clicked.connect(self.cancel)
        panel.addWidget(self.cancel_button)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left)
        scroll.setMinimumWidth(280)
        splitter.addWidget(scroll)
        right = QtWidgets.QWidget()
        views = QtWidgets.QVBoxLayout(right)
        graph_row = QtWidgets.QHBoxLayout()
        self.plots = []
        self.scatters = []
        self.drafts = []
        for i, (x, y) in enumerate(AXES):
            plot = pg.PlotWidget(title=NAMES[i])
            plot.setLabel('bottom', x)
            plot.setLabel('left', y)
            plot.showGrid(x=True, y=True, alpha=0.15)
            plot.setMenuEnabled(False)
            scatter = pg.ScatterPlotItem(size=2, pen=None, brush=pg.mkBrush(74, 174, 207, 130))
            plot.addItem(scatter)
            draft = pg.PlotDataItem(pen=pg.mkPen('#ffc857', width=2), symbol='o', symbolSize=6)
            plot.addItem(draft)
            plot.scene().sigMouseClicked.connect(lambda event, i=i: self.plot_click(i, event))
            self.plots.append(plot)
            self.scatters.append(scatter)
            self.drafts.append(draft)
            graph_row.addWidget(plot)
        tabs = QtWidgets.QTabWidget()
        scatter_page = QtWidgets.QWidget()
        scatter_page.setLayout(graph_row)
        tabs.addTab(scatter_page, 'Scatter gates')
        histogram_page = QtWidgets.QWidget()
        histogram_layout = QtWidgets.QVBoxLayout(histogram_page)
        histogram_layout.addWidget(QtWidgets.QLabel('Check samples in the list, then compare. Uses the statistic channel and common linear bins.'))
        settings = QtWidgets.QHBoxLayout()
        self.histogram_gate = QtWidgets.QComboBox()
        self.histogram_min = QtWidgets.QLineEdit('0')
        self.histogram_max = QtWidgets.QLineEdit('10000')
        self.histogram_bins = QtWidgets.QSpinBox()
        self.histogram_bins.setRange(2, 512)
        self.histogram_bins.setValue(128)
        self.histogram_normalized = QtWidgets.QCheckBox('% of gated events')
        for label, widget in [('Gate', self.histogram_gate), ('Min', self.histogram_min),
                              ('Max', self.histogram_max), ('Bins', self.histogram_bins)]:
            settings.addWidget(QtWidgets.QLabel(label))
            settings.addWidget(widget)
        settings.addWidget(self.histogram_normalized)
        histogram_layout.addLayout(settings)
        actions = QtWidgets.QHBoxLayout()
        suggest = QtWidgets.QPushButton('Use selected sample range')
        suggest.clicked.connect(self.suggest_histogram_range)
        compare = QtWidgets.QPushButton('Compare checked samples')
        compare.clicked.connect(self.compare_samples)
        actions.addWidget(suggest)
        actions.addWidget(compare)
        histogram_layout.addLayout(actions)
        self.histogram_plot = pg.PlotWidget(title='Histogram overlays')
        self.histogram_plot.addLegend()
        self.histogram_plot.setMenuEnabled(False)
        histogram_layout.addWidget(self.histogram_plot, 1)
        self.histogram_info = QtWidgets.QLabel('No overlays calculated. Bin counts use full gated events; values outside the range and nonfinite values are reported below.')
        self.histogram_info.setWordWrap(True)
        histogram_layout.addWidget(self.histogram_info)
        tabs.addTab(histogram_page, 'Histogram overlays')
        views.addWidget(tabs, 2)
        self.table = QtWidgets.QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(['Sample', 'Gate', 'Channel', 'Events', 'Statistic', 'Value', 'Status', 'Gate definition'])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        views.addWidget(self.table, 1)
        self.errors = QtWidgets.QPlainTextEdit()
        self.errors.setReadOnly(True)
        self.errors.setMaximumHeight(100)
        self.errors.setPlaceholderText('Sample errors appear here; failed samples never become zero counts.')
        views.addWidget(self.errors)
        splitter.addWidget(right)
        splitter.setSizes([250, 1000])
        self.statusBar().showMessage('Add a small set of FCS files to begin.')

    def selected_source(self):
        i = self.samples.currentRow()
        return self.project.sources[i] if 0 <= i < len(self.project.sources) else None

    def error(self, message):
        self.errors.appendPlainText(str(message))
        self.statusBar().showMessage(str(message))

    def cancel(self):
        self._after_job = None
        self.jobs.cancel()
        self.statusBar().showMessage('Computation cancelled. Completed rows remain available.')

    def invalidate(self):
        self.cancel()
        self.project.edited()
        self._preview_key = None
        self.dirty = True
        self.rows.clear()
        self.histograms.clear()
        self.render_histograms()
        self._histogram_suggestion = None
        self.refresh_table()

    def add_files(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, 'Add FCS files', '', 'FCS files (*.fcs *.FCS)')
        if paths:
            self.add_paths(paths)

    def add_paths(self, paths):
        self.discard_drawing()
        self.invalidate()
        self._job_mode = 'bind'
        self._after_job = self.select_first
        self.jobs.start(deepcopy(self.project), paths, target=_bind)
        self.statusBar().showMessage('Loading and verifying FCS files…')

    def select_first(self):
        if self.project.sources:
            self.samples.blockSignals(True)
            self.samples.setCurrentRow(0)
            self.samples.blockSignals(False)
            self.preview()

    def sample_selected(self, row):
        self.discard_drawing()
        source = self.selected_source()
        if source and source.id in self.project.overrides:
            self.edit_scope.setCurrentIndex(1)
        self.preview()

    def preview(self):
        source = self.selected_source()
        if source is None:
            return
        self.cancel()
        self._job_mode = 'preview'
        self._preview_key = None
        self._histogram_suggestion = None
        self.sync_gate_controls()
        self.rows.pop(source.id, None)
        self.histograms.pop(source.id, None)
        self.render_histograms()
        self.refresh_table()
        for roi in self._rois:
            if roi is not None:
                roi.setEnabled(False)
        for scatter in self.scatters:
            scatter.setData([], [])
        self.sample_info.setText(f'{Path(source.path).name}: evaluating…')
        self.jobs.start(deepcopy(self.project), [source], True)
        self.statusBar().showMessage('Evaluating selected sample…')

    def analyse_all(self):
        if self._drawing is not None:
            self.error('Finish or discard the polygon before analysis.')
            return
        if len(self.project.gates) != 2 or not self.project.channel:
            self.error('Draw both gates and select a statistic channel first.')
            return
        self.cancel()
        self.rows.clear()
        self.histograms.clear()
        self.render_histograms()
        self.errors.clear()
        self.refresh_table()
        self._job_mode = 'batch'
        self._batch_errors = []
        self.jobs.start(deepcopy(self.project), self.project.sources)
        self.statusBar().showMessage('Applying gates sequentially to all samples…')

    def channel_changed(self, text):
        if text and text != self.project.channel:
            self.discard_drawing()
            self.project.channel = text
            self.invalidate()
            self.preview()

    def effective_gates(self):
        source = self.selected_source()
        return self.project.gates_for(source.id) if source else self.project.default_gates()

    def scope_changed(self, index):
        self.discard_drawing()
        self.render_gates()
        self.sync_gate_controls()

    def store_gate(self, index, gate):
        source = self.selected_source()
        if (self.edit_scope.currentIndex() == 0 and source
                and any(g.id == gate.id for g in self.project.overrides.get(source.id, []))):
            raise ValueError('This sample has an adjustment for this gate. Choose sample-only editing or reset it to shared gates first.')
        if self.edit_scope.currentIndex() == 1:
            if source is None or index >= len(self.project.default_gates()):
                raise ValueError('Create the shared gate before adjusting it for a sample.')
            default = self.project.default_gates()[index]
            if gate.channels != default.channels:
                raise ValueError('Sample adjustments keep the shared gate channels. Change only the range or polygon.')
            self.project.set_override(source.id, gate)
        elif isinstance(gate, RangeGate):
            if any(g.id == gate.id and g.channels != gate.channels
                   for gates in self.project.overrides.values() for g in gates):
                raise ValueError('Reset sample viability adjustments before changing its shared channel.')
            self.project.viability = gate
        elif index < len(self.project.gates):
            self.project.gates[index] = gate
        else:
            self.project.gates.append(gate)

    def reset_sample_gates(self):
        source = self.selected_source()
        if source and self.project.overrides.pop(source.id, None) is not None:
            self.discard_drawing()
            self.invalidate()
            self.preview()

    def apply_viability(self):
        if len(self.project.gates) != 2 or not self.selected_source():
            self.error('Draw both polygon gates and select a sample first.')
            return
        try:
            old = self.project.viability
            gate = RangeGate(old.id if old else new_id(), 'Viable cells', self.project.gates[1].id,
                             self.viability_channel.currentText(), float(self.viability_min.text()),
                             float(self.viability_max.text()))
            self.store_gate(2, gate)
        except ValueError as exc:
            self.error(exc)
            return
        self.discard_drawing()
        self.invalidate()
        self.preview()

    def remove_viability(self):
        gate = self.project.viability
        if gate is None:
            return
        for source_id in list(self.project.overrides):
            self.project.overrides[source_id] = [g for g in self.project.overrides[source_id] if g.id != gate.id]
            if not self.project.overrides[source_id]:
                del self.project.overrides[source_id]
        if self.project.histogram and self.project.histogram.gate_id == gate.id:
            self.project.histogram = None
        self.project.viability = None
        self.discard_drawing()
        self.invalidate()
        self.preview()

    def sync_gate_controls(self):
        gates = self.effective_gates()
        current_gate = self.histogram_gate.currentData()
        self.histogram_gate.clear()
        for gate in self.project.default_gates():
            self.histogram_gate.addItem(gate.name, gate.id)
        target = self.project.histogram.gate_id if self.project.histogram else current_gate
        index = self.histogram_gate.findData(target)
        self.histogram_gate.setCurrentIndex(index if index >= 0 else len(self.project.default_gates()) - 1)
        if self.project.viability:
            gate = self.project.viability if self.edit_scope.currentIndex() == 0 else gates[-1]
            self.viability_channel.setCurrentText(gate.channel)
            self.viability_min.setText(str(gate.minimum))
            self.viability_max.setText(str(gate.maximum))

    def suggest_histogram_range(self):
        source = self.selected_source()
        if source is None or self._preview_key != (source.id, self.project.revision) or not self._histogram_suggestion:
            self.error('Wait for a selected-sample preview with finite gated values.')
            return
        # Suggestion always comes from the final population, never plotted dots.
        self.histogram_gate.setCurrentIndex(len(self.project.default_gates()) - 1)
        self.histogram_min.setText(str(self._histogram_suggestion[0]))
        self.histogram_max.setText(str(self._histogram_suggestion[1]))

    def compare_samples(self):
        if self._drawing is not None:
            self.error('Finish or discard the polygon before comparing samples.')
            return
        selected = [source for i, source in enumerate(self.project.sources)
                    if self.samples.item(i).checkState() == QtCore.Qt.CheckState.Checked]
        try:
            if not selected:
                raise ValueError('Check at least one sample for an overlay.')
            settings = Histogram(self.histogram_gate.currentData(), float(self.histogram_min.text()),
                                 float(self.histogram_max.text()), self.histogram_bins.value(),
                                 self.histogram_normalized.isChecked(), tuple(s.id for s in selected))
            candidate = deepcopy(self.project)
            candidate.histogram = settings
            candidate.validate()
        except (ValueError, TypeError) as exc:
            self.error(exc)
            return
        self.project.histogram = settings
        self.invalidate()
        self._job_mode = 'overlay'
        self._after_job = self.preview
        self.jobs.start(deepcopy(self.project), selected)
        self.statusBar().showMessage('Calculating overlays from full gated events…')

    def render_histograms(self):
        self.histogram_plot.clear()
        settings = self.project.histogram
        if settings is None or not self.histograms:
            self.histogram_info.setText('No current overlays. Check samples and compare to calculate.')
            return
        self.histogram_plot.setLabel('bottom', self.project.channel)
        self.histogram_plot.setLabel('left', '% of gated events / bin' if settings.normalized else 'Events / bin')
        descriptions = []
        for i, sample_id in enumerate(settings.sample_ids):
            data = self.histograms.get(sample_id)
            if data is None:
                continue
            source = next(s for s in self.project.sources if s.id == sample_id)
            counts = np.asarray(data['counts'], dtype=float)
            if settings.normalized and data['event_count']:
                counts *= 100 / data['event_count']
            label = f'{i+1}: {Path(source.path).name}'
            self.histogram_plot.plot(data['edges'], counts, stepMode='center',
                                     pen=pg.mkPen(pg.intColor(i, hues=max(8, len(settings.sample_ids))), width=2), name=label)
            descriptions.append(f"{label}: {data['event_count']} gated; below={data['underflow']}, above={data['overflow']}, nonfinite={data['nonfinite']}")
        self.histogram_info.setText(f'{len(self.histograms)}/{len(settings.sample_ids)} overlays complete. '\
                                    'Last histogram bin includes its upper edge.\n' + '\n'.join(descriptions))

    def statistic_changed(self, index):
        statistic = self.statistic.itemData(index)
        if statistic and statistic != self.project.statistic:
            self.discard_drawing()
            self.project.statistic = statistic
            self.invalidate()
            self.preview()

    def poll(self):
        for message in self.jobs.poll():
            kind = message['kind']
            if kind == 'crash':
                self.error(message['message'])
                continue
            if message['revision'] != self.project.revision:
                continue
            if kind == 'bound':
                source = message['source']
                if self._job_mode == 'relink':
                    original = self._relink_source
                    if source.sha256 != original.sha256:
                        self.error('Relink rejected: selected file has different bytes. Add it as a new sample instead.')
                        continue
                    i = next(i for i, s in enumerate(self.project.sources) if s.id == original.id)
                    self.project.sources[i] = Source(original.id, source.path, original.sha256)
                    self.samples.item(i).setText(Path(source.path).name)
                    self.project.edited()
                    self.dirty = True
                    self.rows.clear()
                    self.histograms.clear()
                    self.render_histograms()
                    self.refresh_table()
                    continue
                if not any(s.path == source.path and s.sha256 == source.sha256 for s in self.project.sources):
                    self.project.sources.append(source)
                    self.samples.blockSignals(True)
                    self.add_sample_item(source)
                    self.samples.blockSignals(False)
                self.set_channels(message['channels'])
            elif kind == 'result':
                self.rows[message['source_id']] = message['data']['rows']
                settings = self.project.histogram
                if settings and message['source_id'] in settings.sample_ids and message['data'].get('histogram'):
                    self.histograms[message['source_id']] = message['data']['histogram']
                    self.render_histograms()
                self.refresh_table()
                if self._job_mode == 'preview':
                    source = self.selected_source()
                    if source and source.id == message['source_id']:
                        self.render(message['data'])
            elif kind == 'error':
                self.rows.pop(message['source_id'], None)
                self.histograms.pop(message['source_id'], None)
                self.render_histograms()
                self.refresh_table()
                self._batch_errors.append(message['source_id'])
                self.error(f"{message['source_id']}: {message['message']}")
            elif kind == 'done':
                self.statusBar().showMessage(f'Finished. Results for {len(self.rows)}/{len(self.project.sources)} samples; see errors below.')
        if not self.jobs.busy and self._after_job:
            callback, self._after_job = self._after_job, None
            callback()
        self.cancel_button.setEnabled(self.jobs.busy)

    def add_sample_item(self, source):
        item = QtWidgets.QListWidgetItem(Path(source.path).name)
        item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
        settings = self.project.histogram
        checked = source.id in settings.sample_ids if settings else self.samples.count() < 8
        item.setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)
        self.samples.addItem(item)

    def set_channels(self, channels):
        if self.channels == channels:
            return
        self.channels = channels
        self.channel.blockSignals(True)
        self.channel.clear()
        self.channel.addItems(channels)
        if self.project.channel in channels:
            self.channel.setCurrentText(self.project.channel)
        elif not self.project.channel and channels:
            self.project.channel = next((c for c in channels if c.endswith('-A') and not c.startswith(('FSC', 'SSC'))), channels[0])
            self.channel.setCurrentText(self.project.channel)
            self.dirty = True
        else:
            self.channel.setCurrentIndex(-1)
        self.channel.blockSignals(False)
        previous = self.viability_channel.currentText()
        self.viability_channel.clear()
        self.viability_channel.addItems(channels)
        target = self.project.viability.channel if self.project.viability else previous or self.project.channel
        self.viability_channel.setCurrentIndex(self.viability_channel.findText(target))

    def render(self, result):
        self._rendering = True
        try:
            self.set_channels(result['channels'])
            self.sample_info.setText(f"{result['event_count']:,} total events; up to 12,000 dots per plot")
            for i, key in enumerate(['population', 'singlets']):
                points = result['plots'].get(key, {'x': [], 'y': []})
                self.scatters[i].setData(points['x'], points['y'])
                self.plots[i].autoRange()
            source = self.selected_source()
            self._preview_key = (source.id, self.project.revision) if source else None
            self._histogram_suggestion = result.get('histogram_suggestion')
            self.render_gates()
            self.sync_gate_controls()
        finally:
            self._rendering = False

    def discard_gate_items(self):
        """Retire owned ROIs before another geometry callback can use them."""
        previous, self._rois = self._rois, [None, None]
        for plot, roi in zip(self.plots, previous):
            if roi is None:
                continue
            try:
                roi.sigRegionChangeFinished.disconnect()
            except (RuntimeError, TypeError):
                pass
            plot.removeItem(roi)
            dismantle_item(roi)
            roi.deleteLater()

    def render_gates(self):
        self._rendering = True
        try:
            self.discard_gate_items()
            source = self.selected_source()
            overridden = {g.id for g in self.project.overrides.get(source.id, [])} if source else set()
            for i, gate in enumerate(self.effective_gates()[:len(self.project.gates)]):
                roi = pg.PolyLineROI(gate.vertices, closed=True,
                                     pen=pg.mkPen('#e887cd' if gate.id in overridden else '#ffc857', width=2))
                self.plots[i].addItem(roi)
                roi.sigRegionChangeFinished.connect(lambda roi, i=i: self.roi_changed(i, roi))
                ready = source and self._preview_key == (source.id, self.project.revision)
                roi.setEnabled(bool(ready and (gate.id not in overridden or self.edit_scope.currentIndex() == 1)))
                self._rois[i] = roi
            summary = ' → '.join(g.name for g in self.effective_gates()) or 'No gates yet'
            if overridden:
                summary += '\nSample adjustments: ' + ', '.join(g.name for g in self.effective_gates() if g.id in overridden)
            self.gate_summary.setText(summary)
        finally:
            self._rendering = False

    def begin_gate(self, index):
        if not self.selected_source():
            self.error('Select a loaded sample first.')
            return
        if self._preview_key != (self.selected_source().id, self.project.revision):
            self.error('Wait for the current sample plot to finish loading before drawing.')
            return
        if index == 1 and not self.project.gates:
            self.error('Draw U937 before drawing its child singlet gate.')
            return
        if self.edit_scope.currentIndex() == 1 and index >= len(self.project.gates):
            self.error('Create the shared polygon before adjusting it for a sample.')
            return
        self.cancel()
        self.discard_drawing()
        self._drawing = index
        self._points = []
        self.statusBar().showMessage(f'Click vertices on {NAMES[index]}, then Finish polygon.')

    def plot_click(self, index, event):
        if self._drawing != index or not self.plots[index].getPlotItem().vb.sceneBoundingRect().contains(event.scenePos()):
            return
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            p = self.plots[index].getPlotItem().vb.mapSceneToView(event.scenePos())
            self._points.append((p.x(), p.y()))
        elif event.button() == QtCore.Qt.MouseButton.RightButton and self._points:
            self._points.pop()
        self.drafts[index].setData([p[0] for p in self._points], [p[1] for p in self._points])
        event.accept()

    def discard_drawing(self):
        self._drawing = None
        self._points = []
        for draft in self.drafts:
            draft.setData([], [])

    def finish_gate(self):
        if self._drawing is None:
            return
        i = self._drawing
        try:
            old = self.effective_gates()[i] if i < len(self.project.gates) else None
            gate = Polygon(old.id if old else new_id(), old.name if old else NAMES[i],
                           self.project.gates[0].id if i else None, AXES[i], tuple(self._points))
            self.store_gate(i, gate)
        except ValueError as exc:
            self.error(exc)
            return
        self.discard_drawing()
        self.invalidate()
        self.render_gates()
        self.preview()

    def roi_changed(self, index, roi):
        if self._rendering or not 0 <= index < len(self._rois) or self._rois[index] is not roi:
            return
        source = self.selected_source()
        if source is None or self._preview_key != (source.id, self.project.revision):
            return
        # Map handles through the item's transform; ROI-local positions alone lose translations.
        vertices = tuple((float(p.x()), float(p.y())) for _, p in roi.getLocalHandlePositions()
                         for p in [roi.mapToParent(p)])
        old = self.effective_gates()[index]
        try:
            self.store_gate(index, Polygon(old.id, old.name, old.parent_id, old.channels, vertices))
        except ValueError as exc:
            self.error(exc)
            self.render_gates()
            return
        self.invalidate()
        self.preview()

    def refresh_table(self):
        rows = self.all_rows()
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            values = [row['sample'], row['gate'], row['channel'], str(row['event_count']),
                      STATISTIC_LABELS[row['statistic']],
                      '' if row['value'] is None else f"{row['value']:.12g}", row['status'],
                      row.get('gate_scope', 'shared')]
            for j, value in enumerate(values):
                self.table.setItem(i, j, QtWidgets.QTableWidgetItem(value))

    def all_rows(self):
        return [row for source in self.project.sources for row in self.rows.get(source.id, [])]

    def confirm_replace(self):
        if not self.dirty:
            return True
        reply = QtWidgets.QMessageBox.question(self, 'Unsaved project', 'Discard unsaved project changes?',
                QtWidgets.QMessageBox.StandardButton.Discard | QtWidgets.QMessageBox.StandardButton.Cancel,
                QtWidgets.QMessageBox.StandardButton.Cancel)
        return reply == QtWidgets.QMessageBox.StandardButton.Discard

    def open_project(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Open project', '', 'FlowHero (*.flowhero.json *.flowpilot.json);;JSON (*.json)')
        if path and self.confirm_replace():
            try:
                project = load_project(path)
                if any(g.channels != AXES[i] for i, g in enumerate(project.gates)):
                    raise ValueError('This GUI version supports FSC-A/SSC-A then SSC-A/SSC-H polygons only.')
                self.cancel()
                self.discard_drawing()
                # Assign a fresh local revision to prevent any previous job result being reused.
                project.revision = max(project.revision, self.project.revision) + 1
                self.project = project
                self.edit_scope.setCurrentIndex(0)
                self.statistic.blockSignals(True)
                self.statistic.setCurrentIndex(self.statistic.findData(project.statistic))
                self.statistic.blockSignals(False)
                self.project_path = path
                self.dirty = False
                self.rows.clear()
                self.histograms.clear()
                self.render_histograms()
                self.refresh_table()
                self.samples.blockSignals(True)
                self.samples.clear()
                for source in project.sources:
                    self.add_sample_item(source)
                self.samples.blockSignals(False)
                self.channels = []
                if project.histogram:
                    h = project.histogram
                    self.histogram_min.setText(str(h.minimum))
                    self.histogram_max.setText(str(h.maximum))
                    self.histogram_bins.setValue(h.bins)
                    self.histogram_normalized.setChecked(h.normalized)
                self.render_gates()
                self.select_first()
            except (ValueError, OSError) as exc:
                self.error(exc)

    def save(self, save_as=False):
        if self._drawing is not None:
            self.error('Finish or discard the polygon before saving.')
            return False
        path = None if save_as else self.project_path
        if not path:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Save project', 'experiment.flowhero.json', 'FlowHero (*.flowhero.json *.flowpilot.json)')
        if path:
            try:
                save_project(self.project, path)
                self.project_path, self.dirty = path, False
                self.statusBar().showMessage(f'Saved definitions and source hashes: {path}')
                return True
            except (ValueError, OSError) as exc:
                self.error(exc)
        return False

    def relink_selected(self):
        source = self.selected_source()
        if source is None:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Relink original FCS', '', 'FCS (*.fcs *.FCS)')
        if path:
            try:
                # Hash verification belongs in a worker just like first-time binding.
                self.cancel()
                self._job_mode = 'relink'
                self._relink_source = source
                self._after_job = self.preview
                self.jobs.start(deepcopy(self.project), [path], target=_bind)
            except (ValueError, OSError) as exc:
                self.error(exc)

    def export(self):
        if self._drawing is not None:
            self.error('Finish or discard the polygon before export.')
            return
        rows = self.all_rows()
        if not rows:
            self.error('No current results. Draw gates and run analysis first.')
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, f'Export current results ({len(self.rows)}/{len(self.project.sources)} samples)', 'statistics.csv', 'CSV (*.csv)')
        if path:
            try:
                export_csv(rows, path)
                self.statusBar().showMessage(f'Exported {len(rows)} current rows to {path}')
            except OSError as exc:
                self.error(exc)

    def closeEvent(self, event):
        if not self.confirm_replace():
            event.ignore()
            return
        try:
            self.cancel()
        except RuntimeError as exc:
            self.error(exc)
            event.ignore()
            return
        self.timer.stop()
        self._preview_key = None
        self.discard_gate_items()
        event.accept()


def main():
    freeze_support()
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName('FlowHero')
    pg.setConfigOptions(antialias=False, background='#14202c', foreground='#dde5ee')
    window = Window()
    window.show()
    sys.exit(app.exec())
