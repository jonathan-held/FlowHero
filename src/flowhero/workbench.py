"""Human-facing flow cytometry plot sheet and gate tree."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import csv
import os
import shutil
import tempfile
import sys
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg
from . import __version__
from .actions import Actions, install_exception_hook
from .gating_actions import AUTOSAVE_SECONDS, GatingActions
from .sample_drag import SampleDropActions, SampleList
from . import scales as scale_rules
from .jobs import Jobs, SessionJobs, _bind, sheet_worker
from .dependencies import retain_valid
from .model import PlotSpec, Project, Rectangle, new_id, ordered_gates
from .plot_sheet import DARK, LIGHT, PlotDocument
from .histograms import histogram_values, histogram_statistics_rows
from .result_windows import HistogramStatistics, QualityOverview
from .templates import save_template, apply_template
from .storage import atomic_write, export_gates, export_gatingml, load_project, save_project


class Workbench(Actions, GatingActions, SampleDropActions, QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f'FlowHero {__version__} — plot sheet')
        self.resize(1460, 960)
        self.project = Project()
        self.project_path = None
        self.dirty = False
        self.results = {}
        self.failures = {}
        self._pending_sources = set()
        self.channels = []
        self.channel_labels = {}
        self.jobs = SessionJobs()
        self.file_jobs = Jobs()
        self.task_jobs = Jobs()
        self._pending_images = None
        self.gate_labels = True
        self.highlight_id = None
        self._notice = None
        self.gate_clipboard = None
        self.screen_theme = DARK
        self._compensation_dialog = None
        self._autosave_path = self._autosave_lock = None
        self.setAcceptDrops(True)
        self.init_sample_drag()
        self._overlay_render_due = False
        self._overlay_timer = QtCore.QTimer(self)
        self._overlay_timer.setSingleShot(True)
        self._overlay_timer.timeout.connect(self.flush_overlay_render)
        self._file_task = None
        self._review_window = None
        self.documents = {}
        self.rebuilding = False
        self.closing = False
        self.undo_states = []
        self.redo_states = []
        self._binding = False
        self._relink = None
        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(lambda: self.request(reuse=True))
        self._build()
        self._autosave_timer = QtCore.QTimer(self)
        self._autosave_timer.timeout.connect(self.autosave)
        self._autosave_timer.start(AUTOSAVE_SECONDS * 1000)
        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.timeout.connect(self.poll)
        self._poll_timer.start(60)
        self.update_tree()

    def _build(self):
        self.build_menus()
        bar = self.addToolBar('Project')
        # Exports and rarer file actions live in the File menu, not here.
        for label, callback in [('Add FCS…', self.add_files), ('Open…', lambda: self.open_project()),
                                ('Save', lambda: self.save())]:
            bar.addAction(label, callback)
        templates = QtWidgets.QToolButton()
        templates.setText('Templates')
        templates.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        template_menu = QtWidgets.QMenu(templates)
        template_menu.addAction('Apply template…', self.choose_template)
        template_menu.addAction('Save shared strategy as template…', self.save_named_template)
        template_menu.addAction('Manage templates…', self.manage_templates)
        templates.setMenu(template_menu)
        bar.addWidget(templates)
        self.review_action = bar.addAction('Review proposed gates…', self.show_gate_review)
        self.review_action.setVisible(False)
        undo = QtGui.QAction('Undo', self); undo.setShortcut(QtGui.QKeySequence.StandardKey.Undo); undo.triggered.connect(self.undo)
        redo = QtGui.QAction('Redo', self); redo.setShortcut(QtGui.QKeySequence.StandardKey.Redo); redo.triggered.connect(self.redo)
        self.addAction(undo); self.addAction(redo)
        bar.addAction(undo); bar.addAction(redo)
        self.addToolBarBreak()
        drawing = self.addToolBar('Gating')
        drawing.addWidget(QtWidgets.QLabel('Gate tool  '))
        self.tool = QtWidgets.QComboBox()
        for label, value in [('Navigate', 'navigate'), ('Polygon', 'polygon'), ('Rectangle', 'rectangle'),
                             ('Ellipse', 'ellipse'), ('Histogram range', 'range'), ('Quadrants', 'quadrants')]:
            self.tool.addItem(label, value)
        self.tool.currentIndexChanged.connect(self.discard_drafts)
        drawing.addWidget(self.tool)
        drawing.addWidget(QtWidgets.QLabel('  Name  '))
        self.gate_name = QtWidgets.QLineEdit()
        self.gate_name.setPlaceholderText('New gate name (optional)')
        self.gate_name.setMaximumWidth(200)
        drawing.addWidget(self.gate_name)
        finish = drawing.addAction('Finish polygon', self.finish_gate)
        finish.setShortcut(QtGui.QKeySequence('Return'))
        drawing.addAction('Discard drawing', self.discard_drafts)
        drawing.addSeparator()
        drawing.addAction('Calculate all samples', lambda: self.request(True))
        drawing.addAction('Cancel', self.cancel_from_ui)
        drawing.addAction('Tile plots', self.tile)
        drawing.addAction('Cascade plots', self.sheet_cascade)
        container = QtWidgets.QWidget(); self.setCentralWidget(container)
        layout = QtWidgets.QVBoxLayout(container)
        self.domain_label = QtWidgets.QLabel()
        self.domain_label.setWordWrap(True); layout.addWidget(self.domain_label)
        self.domain_label.linkActivated.connect(self.edit_compensation)
        self.update_domain_label()
        horizontal = QtWidgets.QSplitter(); layout.addWidget(horizontal, 1)
        sidebar = QtWidgets.QWidget(); side = QtWidgets.QVBoxLayout(sidebar)
        sample_hint = QtWidgets.QLabel('Samples — Ctrl/Cmd or Shift selects histogram overlays; drag rows onto a histogram to keep them there; current row drives 2D plots')
        sample_hint.setWordWrap(True)
        side.addWidget(sample_hint)
        self.samples = SampleList(self); self.samples.setMinimumHeight(90)
        self.samples.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.samples.currentRowChanged.connect(self.sample_changed)
        self.samples.itemSelectionChanged.connect(self.selection_changed)
        self.samples.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.samples.customContextMenuRequested.connect(self.sample_menu)
        self._selection_timer = QtCore.QTimer(self)
        self._selection_timer.setSingleShot(True)
        self._selection_timer.timeout.connect(self.apply_sample_selection)
        side.addWidget(self.samples, 1)
        side.addWidget(QtWidgets.QLabel('Gate hierarchy — double-click to plot a population'))
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(['Population', 'Events', '% parent'])
        self.tree.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.gate_menu)
        self.tree.itemDoubleClicked.connect(lambda *a: self.add_plot('scatter'))
        side.addWidget(self.tree, 2)
        for kind in ('histogram', 'scatter', 'density'):
            button = QtWidgets.QPushButton('Add ' + kind + ' of selected population')
            button.clicked.connect(lambda checked=False, k=kind: self.add_plot(k))
            side.addWidget(button)
        side.addWidget(QtWidgets.QLabel('Gate edits'))
        self.scope = QtWidgets.QComboBox(); self.scope.addItems(['Shared strategy', 'Selected sample only'])
        self.scope.currentIndexChanged.connect(self.render_documents)
        side.addWidget(self.scope)
        reset = QtWidgets.QPushButton('Reset this sample to shared gates'); reset.clicked.connect(self.reset_overrides); side.addWidget(reset)
        side.addWidget(QtWidgets.QLabel('Additional statistics channel'))
        self.stat_channel = QtWidgets.QComboBox(); self.stat_channel.currentTextChanged.connect(self.metric_changed); side.addWidget(self.stat_channel)
        hint = QtWidgets.QLabel('Ranges/rectangles/ellipses: drag, or click two corners. Polygon: click vertices, then Finish. Quadrants: click the crossing. New gates are children of the plot population. Right-click a population to combine, copy or highlight it. Use Undo to recover changes.')
        hint.setWordWrap(True); side.addWidget(hint)
        horizontal.addWidget(sidebar)
        vertical = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.sheet = QtWidgets.QMdiArea()
        self.sheet.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.sheet.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        vertical.addWidget(self.sheet)
        self.table = QtWidgets.QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(['Sample', 'Population', 'Channel', 'Events', '% parent', 'Statistic', 'Value', 'Status', 'Geometry'])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        vertical.addWidget(self.table)
        self.table.hide()
        view = self.menuBar().addMenu('View')
        action = view.addAction('Show statistics table')
        action.setCheckable(True)
        action.toggled.connect(self.table.setVisible)
        labels = view.addAction('Show gate names and percentages on plots')
        labels.setCheckable(True)
        labels.setChecked(True)
        labels.toggled.connect(self.set_gate_labels)
        light = view.addAction('Light plot background')
        light.setCheckable(True)
        if self.read_settings().get('light_plots') is True:
            self.screen_theme = LIGHT
            light.setChecked(True)
        light.toggled.connect(self.set_light_plots)
        self.light_action = light
        for keys, callback in (('Escape', self.clear_highlight), (QtGui.QKeySequence.StandardKey.Copy, self.copy_gate),
                               (QtGui.QKeySequence.StandardKey.Paste, self.paste_gate)):
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(keys), self.tree)
            shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetShortcut if keys != 'Escape'
                                else QtCore.Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(callback)
        self.build_help_menu()
        self.errors = QtWidgets.QPlainTextEdit(); self.errors.setReadOnly(True); self.errors.setMaximumHeight(70)
        self.errors.setPlaceholderText('Errors appear here; failed samples do not become zero counts.')
        vertical.addWidget(self.errors)
        vertical.setSizes([670, 190, 60])
        horizontal.addWidget(vertical); horizontal.setSizes([310, 1150])
        self.quality_button = QtWidgets.QPushButton('Batch quality…')
        self.quality_button.clicked.connect(self.show_quality)
        self.statusBar().addPermanentWidget(self.quality_button)
        self.statusBar().showMessage('Add FCS files or open a project to begin. No original files are modified.')

    def set_gate_labels(self, shown):
        self.gate_labels = shown
        self.render_documents()

    def cancel_from_ui(self):
        try:
            self.cancel()
        except (OSError,RuntimeError) as exc:
            self.error(f'Cleanup incomplete; retry Cancel or Close. {exc}')

    def cancel_files(self):
        self.file_jobs.cancel()
        if self._file_task is not None:
            # Retain this owner/path on failed cleanup so Cancel can retry it.
            shutil.rmtree(self._file_task['workspace'])
            self._file_task = None

    def save_portable(self):
        from .portable import size_estimate
        try:
            estimate = size_estimate(self.project)
            title = f"Save portable package — {estimate['file_count']} FCS files, {estimate['total_input_bytes']/1048576:.1f} MiB before compression"
            path, _ = QtWidgets.QFileDialog.getSaveFileName(self, title, 'experiment.flowhero.zip', 'Portable FlowHero (*.flowhero.zip *.flowpilot.zip)')
            if path:
                self.start_file_task('create', path)
        except (OSError,ValueError) as exc:
            self.error(exc)

    def open_portable(self):
        archive, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Open portable package', '', 'Portable FlowHero (*.flowhero.zip *.flowpilot.zip)')
        if not archive or not self.confirm_replace():
            return
        parent = QtWidgets.QFileDialog.getExistingDirectory(self, 'Choose parent for a new local import folder')
        if not parent:
            return
        name, ok = QtWidgets.QInputDialog.getText(self, 'New import folder', 'New folder name', text=Path(archive).name.removesuffix('.flowhero.zip').removesuffix('.flowpilot.zip'))
        if ok:
            if not name or name in ('.','..') or any(c in name for c in '/\\\\'):
                self.error('Choose a new folder name without path separators.')
                return
            try:
                self.start_file_task('import', str(Path(parent)/name), archive)
            except (OSError,ValueError) as exc:
                self.error(exc)

    def start_file_task(self, operation, destination, archive=None):
        from .package_jobs import portable_worker
        if self.file_jobs.busy or self._file_task is not None:
            self.error('A portable operation is active; wait or Cancel first.')
            return
        destination = Path(destination).absolute()
        if os.path.lexists(destination):
            self.error('Choose a new destination; existing files/folders are preserved.')
            return
        workspace = Path(tempfile.mkdtemp(prefix='.flowhero-files-', dir=destination.parent))
        staged = workspace / ('package.zip' if operation=='create' else 'content')
        self._file_task = dict(operation=operation,destination=destination,workspace=workspace,staged=staged,archive=archive,context=deepcopy(self.project))
        try:
            self.file_jobs.start(deepcopy(self.project), [self._file_task], target=portable_worker)
            self.statusBar().showMessage('Verifying and writing portable data…')
        except Exception:
            self.cancel_files()
            raise

    def poll_files(self):
        messages = self.file_jobs.poll()
        if self._file_task is not None and self._file_task.get('blocked'):
            return
        for message in messages:
            if message['kind'] in ('error','crash'):
                self.error(message['message'])
        if self.file_jobs.busy or self._file_task is None:
            if self._file_task is not None:
                for message in messages:
                    if message['kind'] in ('package_ready','import_ready'):
                        self._file_task['ready']=message
            return
        ready = next((m for m in messages if m['kind'] in ('package_ready','import_ready')), None)
        # Small result messages can arrive before process exit. Preserve them
        # until the owned producer has completed and been reaped.
        ready = ready or self._file_task.pop('ready',None)
        if ready is None:
            try:
                self.cancel_files()
            except (OSError,RuntimeError) as exc:
                self.error(f'Cleanup incomplete; retry Cancel or Close. {exc}')
                self._file_task['blocked']=True
            return
        task = self._file_task
        destination, staged = task['destination'], task['staged']
        if os.path.lexists(destination):
            self.error(f'Destination appeared; verified staged data retained at {staged}. Choose Cancel to discard it.')
            task['ready']=ready
            task['blocked']=True
            return
        try:
            if ready['kind']=='import_ready':
                imported=ready['project']
                imported.sources=[replace(source,path=str(destination/Path(source.path).relative_to(staged))) for source in imported.sources]
                save_project(imported,staged/'project.json')
            os.rename(staged,destination)
            shutil.rmtree(task['workspace'])
            self._file_task=None
            if ready['kind']=='import_ready' and self.project != task['context']:
                self.statusBar().showMessage(f'Imported to {destination}; current edits retained. Open project.json there when ready to switch.')
                return
            if ready['kind']=='import_ready':
                self.stop_analysis()
                imported.revision=max(imported.revision,self.project.revision)+1
                self.close_gate_review()
                self.project=imported;self.project_path=str(destination/'project.json');self.dirty=False
                self.results.clear();self.undo_states.clear();self.redo_states.clear()
                self.channels=[];self.rebuild_samples();self.update_tree();self.refresh_table()
                self.rebuild_documents();self.review_action.setVisible(imported.analysis is not None)
                self._refresh_timer.start(20)
            self.statusBar().showMessage(f'Portable operation complete: {destination}')
        except (OSError,ValueError) as exc:
            self.error(exc)

    def close_gate_review(self):
        if self._review_window is not None:
            if not self._review_window.close():
                raise RuntimeError('Gate-review cleanup is incomplete; retry Close.')
            self._review_window.deleteLater()
            self._review_window=None

    def show_gate_review(self):
        if self.project.analysis is None:
            return
        from .gate_review import GateReview
        if self._review_window is None:
            self._review_window = GateReview(self)
        self._review_window.show()
        self._review_window.refresh()
        self._review_window.raise_()

    def template_directory(self):
        return Path(QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.StandardLocation.AppDataLocation)).with_name('FlowHero') / 'templates'

    def save_named_template(self):
        name, ok = QtWidgets.QInputDialog.getText(self, 'Save shared strategy as template',
                'Name (shared gates, channels and plots; sample adjustments are excluded)')
        if ok:
            try:
                path = save_template(self.project, self.template_directory(), name)
                self.statusBar().showMessage(f'Saved shared strategy: {path.name}; sample-specific adjustments were excluded.')
            except (OSError, ValueError) as exc:
                self.error(exc)

    def choose_template(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Apply template to current samples', str(self.template_directory()), 'FlowHero (*.flowhero.json *.flowpilot.json)')
        if path:
            try:
                candidate = apply_template(path, self.project.sources)
                candidate.annotations = deepcopy(self.project.annotations)
                self.changed(candidate, rebuild=True)
            except (OSError, ValueError) as exc:
                self.error(exc)

    def manage_templates(self):
        dialog = QtWidgets.QDialog(self); dialog.setWindowTitle('Manage analysis templates')
        layout = QtWidgets.QVBoxLayout(dialog)
        listing = QtWidgets.QListWidget(); layout.addWidget(listing)
        directory = self.template_directory()
        for path in sorted(set(directory.glob('*.flowhero.json')) | set(directory.glob('*.flowpilot.json'))):
            item = QtWidgets.QListWidgetItem(path.name.removesuffix('.flowhero.json').removesuffix('.flowpilot.json'))
            item.setData(QtCore.Qt.ItemDataRole.UserRole, str(path)); listing.addItem(item)
        remove = QtWidgets.QPushButton('Delete selected template')
        def delete():
            item = listing.currentItem()
            if item:
                try:
                    Path(item.data(QtCore.Qt.ItemDataRole.UserRole)).unlink()
                    listing.takeItem(listing.row(item))
                except OSError as exc:
                    self.error(exc)
        remove.clicked.connect(delete); layout.addWidget(remove)
        layout.addWidget(QtWidgets.QLabel('Save a new name from Templates to create another version. Existing templates are never overwritten.'))
        dialog.resize(500,300); dialog.exec()

    def selected_source(self):
        index = self.samples.currentRow()
        return self.project.sources[index] if 0 <= index < len(self.project.sources) else None

    def selected_population(self):
        item = self.tree.currentItem()
        return item.data(0, QtCore.Qt.ItemDataRole.UserRole) if item else None

    def plot_spec(self, plot_id):
        return next(p for p in self.project.plots if p.id == plot_id)

    def notify(self, text):
        """Status text that stays until the calculation it started has finished."""
        self._notice = text
        self.statusBar().showMessage(text)

    def error(self, text):
        self.errors.appendPlainText(str(text)); self.statusBar().showMessage(str(text))

    def cancel(self):
        """Explicit Cancel/Close: stop file operations, exports and analysis."""
        self.cancel_files()
        self.task_jobs.cancel()
        self._pending_images = None
        self.stop_analysis()

    def stop_analysis(self):
        """Stop only the calculation; a running save, import or export continues."""
        was_binding = self._binding
        self._refresh_timer.stop()
        self.jobs.cancel()
        self._pending_sources.clear()
        self._binding = False
        self._relink = None
        if was_binding:
            self.project.edited(); self.dirty = True; self.results.clear()
            self.rebuild_samples(); self.default_plots(); self.update_tree(); self.refresh_table()
        self.statusBar().showMessage('Calculation cancelled. Only completed current results can be exported.')

    def checkpoint(self):
        self.undo_states.append(deepcopy(self.project))
        self.undo_states = self.undo_states[-30:]
        self.redo_states.clear()

    def changed(self, candidate, select_gate=None, rebuild=False, review_decision=None, checkpoint=True):
        from .review import reconcile_edits
        reconcile_edits(self.project, candidate)
        if review_decision is not None:
            sid,gid,status=review_decision
            candidate.analysis['proposals'][sid][gid]['status']=status
        try:
            candidate.validate()
        except (ValueError, TypeError) as exc:
            self.error(exc)
            for document in self.documents.values(): document.render()
            return False
        self._refresh_timer.stop()
        if self._binding:
            self.stop_analysis()
        else:
            self.jobs.supersede()
        if checkpoint:
            self.checkpoint()
        previous = self.project
        candidate.revision = self.project.revision + 1
        self.project = candidate; self.dirty = True
        if self.highlight_id not in {g.id for g in candidate.default_gates()}:
            self.highlight_id = None
        self.update_domain_label()
        self.review_action.setVisible(candidate.analysis is not None)
        retained = {}
        for source in candidate.sources:
            if source.id in self.results:
                partial = retain_valid(self.results[source.id], previous, candidate, source)
                if partial is not None:
                    retained[source.id] = partial
        self.results = retained
        self.refresh_table(); self.update_tree(select_gate)
        if rebuild:
            self.rebuild_documents()
        self.render_documents()
        if candidate.analysis is None:
            self.close_gate_review()
        elif self._review_window is not None:
            self._review_window.refresh()
        self._refresh_timer.start(100)
        return True

    def undo(self):
        if not self.undo_states:
            return
        self.stop_analysis(); self.redo_states.append(deepcopy(self.project))
        previous = self.undo_states.pop(); previous.revision = self.project.revision + 1
        self.project = previous; self.after_restore()

    def redo(self):
        if not self.redo_states:
            return
        self.stop_analysis(); self.undo_states.append(deepcopy(self.project))
        following = self.redo_states.pop(); following.revision = self.project.revision + 1
        self.project = following; self.after_restore()

    def after_restore(self):
        self.dirty = True; self.results.clear()
        if self.highlight_id not in {g.id for g in self.project.default_gates()}:
            self.highlight_id = None
        self.update_domain_label()
        self.review_action.setVisible(self.project.analysis is not None)
        if self._review_window is not None: self._review_window.refresh()
        self.rebuild_samples(); self.update_tree(); self.refresh_table(); self.rebuild_documents()
        self._refresh_timer.start(100)

    def discard_drafts(self, *args):
        for document in self.documents.values():
            document.points = []
            if document.draft is not None:
                document.draft.setData([], [])

    def finish_gate(self):
        document = self.sheet.activeSubWindow()
        if isinstance(document, PlotDocument):
            document.finish()

    def add_gates(self, gates):
        candidate = deepcopy(self.project)
        candidate.gates.extend(gates)
        self.changed(candidate, gates[-1].id)

    def render_documents(self, *args):
        for document in self.documents.values(): document.render()

    def edit_gate_values(self, gate, **changes):
        try:
            self.replace_gate(replace(gate, **changes))
        except ValueError as exc:
            self.error(exc); self.render_documents()

    def replace_gate(self, gate):
        self.apply_gate_replacements([gate])

    def apply_gate_replacements(self, gates):
        candidate = deepcopy(self.project)
        source = self.selected_source()
        if source is None:
            return
        for gate in gates:
            if self.scope.currentIndex() == 1:
                candidate.set_override(source.id, gate)
            else:
                if any(g.id == gate.id for g in candidate.overrides.get(source.id, [])):
                    self.error('This sample has its own geometry. Choose Selected sample only, reset its adjustment, or select a sample using shared geometry.')
                    self.render_documents()
                    return
                if candidate.viability and candidate.viability.id == gate.id:
                    candidate.viability = gate
                else:
                    candidate.gates = [gate if old.id == gate.id else old for old in candidate.gates]
        self.changed(candidate, gates[-1].id)

    def move_quadrants(self, group_id, x, y):
        source = self.selected_source()
        if source is None:
            return
        gates = []
        for gate in self.project.gates_for(source.id):
            if isinstance(gate, Rectangle) and gate.group_id == group_id:
                lo = tuple(value if old is not None else None for value, old in zip((x, y), gate.minimum))
                hi = tuple(value if old is not None else None for value, old in zip((x, y), gate.maximum))
                gates.append(replace(gate, minimum=lo, maximum=hi))
        self.apply_gate_replacements(gates)

    def reset_overrides(self):
        source = self.selected_source()
        if source and source.id in self.project.overrides:
            candidate = deepcopy(self.project); del candidate.overrides[source.id]
            self.changed(candidate)

    def update_tree(self, selected=None):
        selected = selected or self.selected_population()
        self.tree.clear()
        source = self.selected_source()
        rows = self.results.get(source.id, {}).get('rows', []) if source and self.results.get(source.id, {}).get('_verified', True) else []
        by_id = {row['gate_id']: row for row in rows}
        root_row = by_id.get('root')
        root = QtWidgets.QTreeWidgetItem(['All events', str(root_row['event_count']) if root_row else '—', '100' if root_row and root_row['event_count'] else '—'])
        root.setData(0, QtCore.Qt.ItemDataRole.UserRole, None); self.tree.addTopLevelItem(root)
        items = {None: root}
        for gate in ordered_gates(self.project.default_gates()):
            row = by_id.get(gate.id)
            percent = row.get('percent_parent') if row else None
            custom = source and any(g.id == gate.id for g in self.project.overrides.get(source.id, []))
            item = QtWidgets.QTreeWidgetItem([gate.name + (' *' if custom else ''), str(row['event_count']) if row else '—', f'{percent:.3g}' if percent is not None else '—'])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, gate.id)
            items[gate.parent_id].addChild(item); items[gate.id] = item
        self.tree.setCurrentItem(items.get(selected, root)); self.tree.expandAll()
        self.tree.resizeColumnToContents(0)

    def gate_menu(self, position):
        item = self.tree.itemAt(position)
        if item is None:
            return
        self.tree.setCurrentItem(item)
        gate_id = self.selected_population()
        menu = QtWidgets.QMenu(self)
        for kind in ('histogram', 'scatter', 'density'):
            menu.addAction('Add ' + kind, lambda checked=False, k=kind: self.add_plot(k))
        if gate_id:
            menu.addSeparator()
            highlight = menu.addAction('Highlight in all plots')
            highlight.setCheckable(True)
            highlight.setChecked(self.highlight_id == gate_id)
            highlight.toggled.connect(lambda checked, gid=gate_id: self.set_highlight(gid if checked else None))
            menu.addAction('Combine gates…', lambda: self.combine_gates())
            menu.addAction('Copy gate', self.copy_gate)
        if self.gate_clipboard:
            menu.addAction('Paste gate here', self.paste_gate)
        if self.singlet_pairs() and self.selected_source() is not None:
            menu.addAction('Add singlet gate…', lambda: self.add_singlet_gate())
        if gate_id:
            menu.addSeparator()
            menu.addAction('Rename gate…', self.rename_gate)
            menu.addAction('Delete gate and descendants', self.delete_branch)
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def rename_gate(self):
        gate_id = self.selected_population()
        gate = next(g for g in self.project.default_gates() if g.id == gate_id)
        name, ok = QtWidgets.QInputDialog.getText(self, 'Rename gate', 'Name', text=gate.name)
        if ok and name.strip():
            candidate = deepcopy(self.project)
            candidate.gates = [replace(g, name=name.strip()) if g.id == gate_id else g for g in candidate.gates]
            if candidate.viability and candidate.viability.id == gate_id:
                candidate.viability = replace(candidate.viability, name=name.strip())
            candidate.overrides = {sid: [replace(g, name=name.strip()) if g.id == gate_id else g for g in gates] for sid, gates in candidate.overrides.items()}
            self.changed(candidate, gate_id)

    def delete_branch(self):
        gate_id = self.selected_population()
        if gate_id is None:
            return
        candidate = deepcopy(self.project)
        gates = {g.id: g for g in candidate.default_gates()}
        removed = {gate_id}
        while True:
            # A combined gate cannot outlive a gate it combines.
            following = removed | {g.id for g in gates.values() if g.parent_id in removed
                                   or any(o in removed for o in getattr(g, 'operands', ()))}
            if following == removed:
                break
            removed = following
        candidate.gates = [g for g in candidate.gates if g.id not in removed]
        if candidate.viability and candidate.viability.id in removed:
            candidate.viability = None
        candidate.overrides = {sid: [g for g in definitions if g.id not in removed] for sid, definitions in candidate.overrides.items()}
        candidate.overrides = {sid: definitions for sid, definitions in candidate.overrides.items() if definitions}
        parent = gates[gate_id].parent_id
        candidate.plots = [replace(p, population_id=parent) if p.population_id in removed else p for p in candidate.plots]
        if candidate.histogram and candidate.histogram.gate_id in removed:
            candidate.histogram = None
        self.changed(candidate, parent)

    def add_plot(self, kind):
        if not self.channels:
            self.error('Load an FCS sample before adding plots.'); return
        active = self.sheet.activeSubWindow()
        x = active.spec.x_channel if isinstance(active, PlotDocument) else self.project.channel or self.channels[0]
        y = next((c for c in self.channels if c != x), '')
        if kind != 'histogram' and not y:
            self.error('A 2D plot needs two distinct channels.'); return
        n = len(self.project.plots)
        spec = PlotSpec(new_id(), kind, x, y if kind != 'histogram' else '', self.selected_population(),
                        geometry=((n % 2)*520, (n // 2)*380, 520, 380), bins=64 if kind == 'density' else 128)
        candidate = deepcopy(self.project); candidate.plots.append(spec)
        if self.changed(candidate):
            self.add_document(spec)

    def duplicate_plot(self, plot_id):
        """Add an identical copy of a plot, offset so both stay visible.

        Every setting is copied (channels, population, scales, limits, bins,
        normalization, overlays and a fixed sample list); only the identity
        and position differ. Results are calculated for the copy like for any
        new plot, so nothing is shared between the two.
        """
        spec = self.plot_spec(plot_id)
        document = self.documents.get(plot_id)
        if document is not None:
            g = document.geometry()
            x, y, w, h = g.x(), g.y(), g.width(), g.height()
        else:
            x, y, w, h = spec.geometry
        copy = replace(spec, id=new_id(), geometry=(x + 30, y + 30, w, h))
        candidate = deepcopy(self.project); candidate.plots.append(copy)
        if self.changed(candidate):
            document = self.add_document(copy)
            self.sheet.setActiveSubWindow(document)
            return copy.id
        return None

    def add_document(self, spec):
        self.rebuilding = True
        try:
            document = PlotDocument(self, spec)
            self.documents[spec.id] = document; self.sheet.addSubWindow(document); document.show()
            document.setGeometry(*spec.geometry)
        finally:
            self.rebuilding = False
        return document

    def rebuild_documents(self):
        self.rebuilding = True
        try:
            for document in list(self.documents.values()):
                document.close()
            self.documents.clear()
        finally:
            self.rebuilding = False
        for spec in self.project.plots:
            self.add_document(spec)

    def layout_plot(self, plot_id, geometry):
        if geometry[2] < 240 or geometry[3] < 180:
            return
        self.project.plots = [replace(p, geometry=geometry) if p.id == plot_id else p for p in self.project.plots]
        self.dirty = True

    def remove_plot(self, plot_id):
        candidate = deepcopy(self.project); candidate.plots = [p for p in candidate.plots if p.id != plot_id]
        self.documents.pop(plot_id, None)
        self.changed(candidate)

    def edit_plot(self, plot_id, **changes):
        try:
            candidate = deepcopy(self.project)
            candidate.plots = [replace(p, **changes) if p.id == plot_id else p for p in candidate.plots]
            self.changed(candidate)
        except (ValueError, TypeError) as exc:
            self.error(exc)

    def change_kind(self, plot_id, kind):
        spec = self.plot_spec(plot_id)
        y = spec.y_channel or next((c for c in self.channels if c != spec.x_channel), '')
        self.edit_plot(plot_id, kind=kind, y_channel='' if kind == 'histogram' else y,
                       y_scale='linear', y_limits=None, sample_ids=(), overlay=False, stagger=False, pinned=False)

    def selected_sample_ids(self):
        return tuple(source.id for i, source in enumerate(self.project.sources)
                     if self.samples.item(i).isSelected())

    def selection_changed(self):
        self._selection_timer.start(120)

    def apply_sample_selection(self):
        ids = self.selected_sample_ids()
        candidate = deepcopy(self.project)
        changed = False
        for i, spec in enumerate(candidate.plots):
            if spec.pinned or not (spec.kind == 'histogram' or (spec.kind == 'scatter' and spec.overlay)):
                continue
            overlays = ids if len(ids) > 1 else ()
            if spec.sample_ids == overlays:
                continue
            limits, y_limits = spec.x_limits, spec.y_limits
            # Overlaid samples need common axes: fix them to the first available result.
            if overlays and (limits is None or (spec.kind == 'scatter' and y_limits is None)):
                data = next((self.results[sid]['sheet_plots'].get(spec.id) for sid in ids
                             if sid in self.results and self.results[sid]['sheet_plots'].get(spec.id, {}).get('x_bounds')), None)
                if data is None:
                    continue
                if limits is None:
                    limits = tuple(float(v) for v in scale_rules.inverse(data['x_bounds'], spec.x_scale))
                if spec.kind == 'scatter' and y_limits is None:
                    y_limits = tuple(float(v) for v in scale_rules.inverse(data['y_bounds'], spec.y_scale))
            candidate.plots[i] = replace(spec, sample_ids=overlays, x_limits=limits, y_limits=y_limits)
            changed = True
        if changed:
            # Choosing overlays is not an analysis edit: no undo step.
            self.changed(candidate, checkpoint=False)

    def tile(self):
        self.sheet.tileSubWindows()

    def sheet_cascade(self):
        self.sheet.cascadeSubWindows()

    def default_plots(self):
        if self.project.plots or not self.channels:
            return
        gates = self.project.default_gates()
        x = 'FSC-A' if 'FSC-A' in self.channels else self.channels[0]
        y = 'SSC-A' if 'SSC-A' in self.channels and x != 'SSC-A' else next((c for c in self.channels if c != x), '')
        if y:
            self.project.plots.append(PlotSpec(new_id(), 'scatter', x, y, geometry=(0, 0, 520, 380)))
        if len(gates) >= 2 and all(c in self.channels for c in gates[1].channels) and len(gates[1].channels) == 2:
            self.project.plots.append(PlotSpec(new_id(), 'scatter', *gates[1].channels, population_id=gates[1].parent_id,
                                               geometry=(520, 0, 520, 380)))
        self.project.plots.append(PlotSpec(new_id(), 'histogram', self.project.channel or x,
                                          population_id=gates[-1].id if gates else None,
                                          geometry=((len(self.project.plots) % 2)*520, (len(self.project.plots) // 2)*380, 520, 380)))
        self.dirty = True
        self.rebuild_documents()

    def set_channels(self, channels, labels=None):
        self.channels = channels; self.channel_labels.update(labels or {})
        self.stat_channel.blockSignals(True); self.stat_channel.clear(); self.stat_channel.addItems(channels)
        if not self.project.channel and channels:
            self.project.channel = next((c for c in channels if c.endswith('-A') and not c.startswith(('FSC', 'SSC'))), channels[0])
        self.stat_channel.setCurrentIndex(self.stat_channel.findText(self.project.channel)); self.stat_channel.blockSignals(False)

    def metric_changed(self, channel):
        if channel and channel != self.project.channel:
            candidate = deepcopy(self.project); candidate.channel = channel; self.changed(candidate)

    def add_files(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, 'Add FCS samples', '', 'FCS (*.fcs *.FCS)')
        if paths:
            self.add_paths(paths)

    def add_paths(self, paths):
        self.stop_analysis(); self.checkpoint(); self._binding = True
        self.jobs.start(deepcopy(self.project), paths, target=_bind)
        self.statusBar().showMessage('Verifying and reading FCS metadata…')

    def rebuild_samples(self, selected_id=None):
        source = self.selected_source(); selected_id = selected_id or (source.id if source else None)
        self.samples.blockSignals(True); self.samples.clear()
        for source in self.project.sources:
            item = QtWidgets.QListWidgetItem(self.sample_caption(source))
            item.setToolTip(self.sample_tooltip(source))
            self.samples.addItem(item)
        index = next((i for i, source in enumerate(self.project.sources) if source.id == selected_id), 0)
        if self.project.sources:
            self.samples.setCurrentRow(index)
        self.samples.blockSignals(False)

    def sample_changed(self, row):
        self._refresh_timer.stop(); self.jobs.supersede(); self.discard_drafts(); self.refresh_table(); self.update_tree()
        source = self.selected_source()
        if source: self.results.get(source.id, {}).pop('_dependencies', None)
        for document in self.documents.values(): document.clear_pending()
        self._refresh_timer.start(20)

    def request(self, all_samples=False, reuse=False):
        self._refresh_timer.stop()
        if self._binding:
            return
        source = self.selected_source()
        if source is None:
            return
        self.jobs.supersede()
        self._pending_sources.clear()
        if all_samples:
            sources = self.project.sources
        else:
            ids = {source.id} | {sid for p in self.project.plots for sid in p.sample_ids}
            sources = [s for s in self.project.sources if s.id in ids]
        sources = [s for s in sources if not reuse or all_samples or not self.results.get(s.id, {}).get('_verified', False) or self.results.get(s.id, {}).get('_dependencies') != self.current_key(s)]
        self.refresh_table(); self.update_tree()
        self.render_documents()
        if not sources:
            self.statusBar().showMessage('Reused current results; no scientific recalculation needed.')
            return
        self._pending_sources = {s.id for s in sources}
        for item in sources:
            if item.id in self.results:
                self.results[item.id]['_verified'] = False
        self.refresh_table(); self.update_tree()
        self.jobs.start(self.analysis_snapshot(), sources, target=sheet_worker)
        self.statusBar().showMessage(f'Updating {len(sources)} sample(s); unaffected results remain available…')

    def poll(self):
        self.poll_files()
        self.poll_tasks()
        messages = self.jobs.poll()
        for message in messages:
            kind = message['kind']
            if kind == 'crash':
                self._pending_sources.clear()
                self.error(message['message']); continue
            if message['revision'] != self.project.revision:
                continue
            if kind == 'bound':
                source = message['source']
                if self._relink:
                    old = self._relink
                    if old.sha256 != source.sha256:
                        self.error('Relink rejected: bytes differ from original source.'); continue
                    self.project.sources = [replace(old, path=source.path) if s.id == old.id else s for s in self.project.sources]
                elif not any(s.path == source.path and s.sha256 == source.sha256 for s in self.project.sources):
                    self.project.sources.append(source)
                if not self.channels:
                    self.set_channels(message['channels'], message.get('channel_labels'))
                self.dirty = True
            elif kind == 'result':
                self._pending_sources.discard(message['source_id'])
                self.results[message['source_id']] = {**message['data'], '_verified': True}
                self.failures.pop(message['source_id'], None)
                current = next(s for s in self.project.sources if s.id == message['source_id'])
                self.results[current.id]['_dependencies'] = self.current_key(current)
                source = self.selected_source()
                if source and source.id == message['source_id']:
                    self.set_channels(message['data']['channels'], message['data'].get('channel_labels'))
                self.update_tree(); self.refresh_table()
                if self._review_window is not None: self._review_window.refresh(preview=False)
                self.render_after_result(message['source_id'])
            elif kind == 'error':
                self._pending_sources.discard(message['source_id'])
                source = self.selected_source()
                if source and source.id == message['source_id'] and message.get('channels'):
                    self.set_channels(message['channels'], message.get('channel_labels'))
                self.results.pop(message['source_id'], None)
                self.failures[message['source_id']] = message['message']
                name = next((Path(s.path).name for s in self.project.sources if s.id == message['source_id']),
                            Path(str(message['source_id'])).name)
                self.error(f"{name}: {message['message']}")
                self.update_tree(); self.refresh_table()
                if self._review_window is not None: self._review_window.refresh(preview=False)
                self.render_after_result(message['source_id'])
        if not self.jobs.busy:
            self.flush_overlay_render()
            if self._binding:
                self._binding = False; self._relink = None
                self.project.edited(); self.results.clear(); self.rebuild_samples()
                self.default_plots(); self.update_tree(); self._refresh_timer.start(20)
            elif any(m['kind']=='done' and m.get('revision')==self.project.revision for m in messages):
                if self._pending_images is not None:
                    options, self._pending_images = self._pending_images, None
                    ready = [s for s in self.project.sources if s.id in options['sample_ids'] and s.id in self.results]
                    self.write_images(options, ready)
                    return
                # An explanation of the action that caused this calculation outlives it.
                notice, self._notice = self._notice, None
                self.statusBar().showMessage(notice or f'{len(self.results)}/{len(self.project.sources)} samples have current results. Errors, if any, are listed below.')

    def render_after_result(self, source_id):
        """Redraw for a new result; results of many overlaid samples share one redraw."""
        selected = self.selected_source()
        if selected is not None and selected.id == source_id:
            self._overlay_render_due = False
            for document in self.documents.values(): document.render()
            return
        # Another sample changes only plots that list it; with many samples the
        # results arrive in quick succession, so those plots are redrawn together.
        self._overlay_render_due = True
        if not self._overlay_timer.isActive():
            self._overlay_timer.start(250)

    def flush_overlay_render(self):
        if not self._overlay_render_due:
            return
        self._overlay_render_due = False
        self._overlay_timer.stop()
        for document in self.documents.values():
            if document.spec.sample_ids: document.render()

    def quality_entries(self):
        entries=[]
        for source in self.project.sources:
            result=self.results.get(source.id,{})
            reasons=sorted({row['gate'] + ': ' + row['status'] for row in result.get('rows',[])
                            if row['status'] == 'nonfinite'})
            reasons.extend(f'Plot {pid}: {data["error"]}' for pid,data in result.get('sheet_plots',{}).items() if 'error' in data)
            if self.project.analysis:
                records=self.project.analysis['proposals'].get(source.id,{})
                reasons.extend(f"{r['original']['name']}: gate review {r['status']}" for r in records.values()
                               if r['status'] not in ('accepted','reset','removed'))
            if source.id in self.failures:
                reasons.append(self.failures[source.id])
            current=result.get('_verified', False) and result.get('_dependencies')==self.current_key(source)
            state='Needs review' if reasons else 'Analyzed' if current else 'Pending'
            entries.append((source.id,Path(source.path).name,state,'; '.join(reasons) or ('Current computation' if current else 'Not calculated for current settings')))
        return entries

    def show_quality(self):
        self._quality_window=QualityOverview(self,self.quality_entries())
        self._quality_window.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self._quality_window.show()

    def refresh_table(self):
        if hasattr(self,'quality_button'):
            entries=self.quality_entries()
            complete=sum(e[2]=='Analyzed' for e in entries)
            flagged=sum(e[2]=='Needs review' for e in entries)
            self.quality_button.setText(f'{complete} analyzed · {flagged} need review…')
        source = self.selected_source()
        rows = self.results.get(source.id, {}).get('rows', []) if source and self.results.get(source.id, {}).get('_verified', True) else []
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            percent = row.get('percent_parent')
            values = [row['sample'], row['gate'], row['channel'], str(row['event_count']),
                      f'{percent:.5g}' if percent is not None else '', row['statistic'],
                      '' if row['value'] is None else f"{row['value']:.10g}", row['status'], row.get('gate_scope', 'shared')]
            for column, text in enumerate(values):
                self.table.setItem(index, column, QtWidgets.QTableWidgetItem(text))

    def all_rows(self):
        return [row for source in self.project.sources if source.id not in self._pending_sources
                and self.results.get(source.id, {}).get('_verified', True)
                for row in self.results.get(source.id, {}).get('rows', [])]

    def confirm_replace(self):
        if not self.dirty:
            return True
        buttons = QtWidgets.QMessageBox.StandardButton
        reply = QtWidgets.QMessageBox.question(self, 'Unsaved project', 'Save changes before continuing?', buttons.Save | buttons.Discard | buttons.Cancel, buttons.Save)
        return self.save() if reply == buttons.Save else reply == buttons.Discard

    def save(self, save_as=False):
        if any(document.points for document in self.documents.values()):
            self.error('Finish or discard the drawing before saving.'); return False
        path = None if save_as else self.project_path
        if not path:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Save project and plot sheet', 'experiment.flowhero.json', 'FlowHero (*.flowhero.json *.flowpilot.json)')
        if not path:
            return False
        try:
            save_project(self.project, path); self.project_path = path; self.dirty = False
            self.clear_autosave()
            self.remember_project(path)
            return True
        except (ValueError, OSError) as exc:
            self.error(exc); return False

    def open_project(self, path=None):
        if not path:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Open FlowHero project', '', 'FlowHero (*.flowhero.json *.flowpilot.json);;JSON (*.json)')
        if not path or not self.confirm_replace(): return
        try:
            project = load_project(path); self.stop_analysis(); self.close_gate_review()
            project.revision = max(project.revision, self.project.revision) + 1
            self.project = project; self.project_path = path; self.dirty = False
            self.highlight_id = None
            self.clear_autosave()
            self.update_domain_label()
            self.review_action.setVisible(project.analysis is not None)
            self.results.clear(); self.undo_states.clear(); self.redo_states.clear(); self.channels = []
            # Existing definitions supply initial channel choices until source metadata arrives.
            inferred = list(dict.fromkeys([c for g in project.default_gates() for c in g.channels] +
                                          [c for p in project.plots for c in (p.x_channel, p.y_channel) if c] +
                                          ([project.channel] if project.channel else [])))
            self.set_channels(inferred); self.rebuild_samples(); self.update_tree(); self.refresh_table()
            self.default_plots(); self.rebuild_documents(); self._refresh_timer.start(20)
            self.remember_project(path)
        except (ValueError, OSError) as exc:
            self.error(exc)

    def relink(self):
        source = self.selected_source()
        if source is None: return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Relink original FCS bytes', '', 'FCS (*.fcs *.FCS)')
        if path:
            self.stop_analysis(); self.checkpoint(); self._relink = source; self._binding = True
            self.jobs.start(deepcopy(self.project), [path], target=_bind)

    def show_histogram_statistics(self, plot_id):
        source = self.selected_source()
        ready_results = {sid: data for sid, data in self.results.items() if sid not in self._pending_sources and data.get('_verified', True)}
        rows = histogram_statistics_rows(self.plot_spec(plot_id), ready_results, source.id if source else None)
        if not rows:
            self.error('Wait for current histogram statistics before opening the table.')
            return
        dialog = HistogramStatistics(self, rows, self.documents[plot_id].windowTitle())
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        self._statistics_window = dialog

    def export_plot(self, plot_id):
        spec = self.plot_spec(plot_id); source = self.selected_source()
        ids = spec.sample_ids or ((source.id,) if source else ())
        rows = []
        for sid in ids:
            if sid in self._pending_sources or not self.results.get(sid, {}).get('_verified', True): continue
            data = self.results.get(sid, {}).get('sheet_plots', {}).get(plot_id)
            if not data or 'error' in data: continue
            sample = next(s for s in self.project.sources if s.id == sid)
            base = {'sample_id': sid, 'sample': Path(sample.path).name, 'source_sha256': sample.sha256,
                    'plot_id': plot_id, 'gate_id': spec.population_id or 'root', 'channel': spec.x_channel,
                    'x_scale': spec.x_scale, 'y_scale': spec.y_scale, 'domain': self.project.domain, 'population_count': data['event_count'],
                    'excluded_nonfinite': data['nonfinite'], 'excluded_nonpositive': data['nonpositive'],
                    'outside': data['outside']}
            raw = lambda value, scale: float(scale_rules.inverse([value], scale)[0])
            if spec.kind == 'histogram':
                normalized, normalization_status = histogram_values(data, spec.normalization)
                for i, count in enumerate(data['counts']):
                    rows.append({**base, 'record': 'bin', 'bin_low': raw(data['edges'][i], spec.x_scale),
                                 'bin_high': raw(data['edges'][i+1], spec.x_scale), 'upper_inclusive': i == len(data['counts'])-1, 'count': count,
                                 'normalization': spec.normalization, 'normalized_value': float(normalized[i]),
                                 'normalization_status': normalization_status})
            elif spec.kind == 'density':
                for x, line in enumerate(data['counts']):
                    for y, count in enumerate(line):
                        rows.append({**base, 'record': 'bin2d', 'bin_low': raw(data['x_edges'][x], spec.x_scale),
                                     'bin_high': raw(data['x_edges'][x+1], spec.x_scale),
                                     'y_low': raw(data['y_edges'][y], spec.y_scale), 'y_high': raw(data['y_edges'][y+1], spec.y_scale), 'count': count})
            for stat in ([] if spec.kind == 'histogram' else self.results[sid]['rows']):
                if stat['gate_id'] == (spec.population_id or 'root') and stat['channel'] in (spec.x_channel, spec.y_channel):
                    rows.append({**base, 'record': 'statistic', 'channel': stat['channel'], 'statistic': stat['statistic'], 'value': stat['value'], 'status': stat['status']})
        if not rows:
            self.error('Wait for current plot results before exporting.'); return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Export histogram bins' if spec.kind == 'histogram' else 'Export full-event bins and statistics', 'histogram-bins.csv' if spec.kind == 'histogram' else 'plot-data.csv', 'CSV (*.csv)')
        if path:
            def write(stream):
                fields = list(dict.fromkeys(key for row in rows for key in row))
                writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
            try: atomic_write(path, write)
            except OSError as exc: self.error(exc)

    def import_gate_file(self):
        from .gate_import import read_gate_import
        from .gate_import_dialog import GateImportDialog
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Import gate definitions', '',
            'Gate files (*.json *.xml *.gatingml *.wsp);;All files (*)')
        if not path:
            return
        dialog = None
        try:
            document = read_gate_import(path)
            previous, revision = self.project, self.project.revision
            dialog = GateImportDialog(previous, document, self.channels, self)
            if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                return
            if self.project is not previous or self.project.revision != revision:
                raise ValueError('The active project changed during import. Reopen gate import to review it against the current project.')
            if self.changed(dialog.candidate, rebuild=True):
                self.statusBar().showMessage(f'Imported {len(document.variants[dialog.variant.currentData()])} shared gates. Undo reverses this import.')
        except (OSError, ValueError, TypeError) as exc:
            self.error(f'Gate import failed: {exc}')
        finally:
            if dialog is not None:
                dialog.deleteLater()

    def export_gate_json(self):
        self.export_gate_file(False)

    def export_gate_ml(self):
        self.export_gate_file(True)

    def export_gate_file(self, xml):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Export gate definitions', 'gates.gatingml' if xml else 'gates.json', 'Gating-ML (*.gatingml)' if xml else 'JSON (*.json)')
        if path:
            source = self.selected_source()
            source_id = source.id if source and self.scope.currentIndex() == 1 else None
            try:
                if xml:
                    measured = self.results.get(source.id, {}).get('compensated_channels') if source else None
                    file_mode = self.project.compensation and self.project.compensation['source'] == 'file'
                    export_gatingml(self.project, path, source_id, (measured or None) if file_mode else None)
                else:
                    export_gates(self.project, path, source_id)
            except Exception as exc: self.error(f'Gate export failed: {exc}')

    def closeEvent(self, event):
        if not self.confirm_replace(): event.ignore(); return
        try:
            self.close_gate_review()
            self.cancel()
        except (OSError,RuntimeError) as exc:
            self.error(f'Cleanup incomplete; retry Close or Cancel. {exc}')
            event.ignore();return
        self._autosave_timer.stop(); self.clear_autosave()
        self._overlay_timer.stop(); self._selection_timer.stop(); self.closing = True; self._poll_timer.stop(); event.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName('FlowHero')
    pg.setConfigOptions(background='#14202c', foreground='#dde5ee', antialias=False)
    window = Workbench(); window.show()
    install_exception_hook(window)
    QtCore.QTimer.singleShot(300, window.offer_recovery)
    code = app.exec()
    # Let Qt destroy items queued for deletion before Python tears down.
    app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()
    sys.exit(code)
