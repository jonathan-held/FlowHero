"""Export, import and sample actions of the main window.

Kept apart from ``workbench`` so that every action here lives in a menu and the
plot sheet itself stays unchanged. Work that needs event data runs in
``task_jobs.task_worker``; this module only collects choices and shows results.
"""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json
import re

from PySide6 import QtCore, QtWidgets

from . import __version__
from .export import (BASIC_STATISTICS, OPEN_BOUND, SPREAD_STATISTICS, methods_report, package_version,
                     provenance, read_annotations, valid_statistic)
from .model import RangeGate, new_id
from .storage import atomic_write

STATISTIC_LABELS = {'median': 'Median', 'arithmetic_mean': 'Mean', 'geometric_mean': 'Geometric mean',
                    'sd': 'SD', 'cv': 'CV (%)', 'robust_cv': 'Robust CV (%)'}
RECENT_LIMIT = 8


def safe_name(text):
    return re.sub(r'[^A-Za-z0-9._+-]+', '_', text).strip('_') or 'plot'


class StatisticsExportDialog(QtWidgets.QDialog):
    """Defaults reproduce the previous export; rarer choices sit under More options."""

    def __init__(self, owner, default_channels):
        super().__init__(owner)
        self.setWindowTitle('Export statistics')
        layout = QtWidgets.QVBoxLayout(self)
        count = len(owner.project.sources)
        layout.addWidget(QtWidgets.QLabel(
            f'All {count} sample(s) are recalculated from the files, independent of the open plots.'))
        self.long = QtWidgets.QRadioButton('Long — one row per sample, population, channel and statistic (R, Python)')
        self.wide = QtWidgets.QRadioButton('Wide — one row per sample, one column per measure (Prism, Excel)')
        self.long.setChecked(True)
        layout.addWidget(self.long)
        layout.addWidget(self.wide)
        layout.addWidget(QtWidgets.QLabel('Channels'))
        self.channels = QtWidgets.QListWidget()
        self.channels.setMaximumHeight(150)
        for name in owner.channels:
            marker = owner.channel_labels.get(name, '')
            item = QtWidgets.QListWidgetItem(name + (f' — {marker}' if marker else ''))
            item.setData(QtCore.Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked if name in default_channels
                               else QtCore.Qt.CheckState.Unchecked)
            self.channels.addItem(item)
        layout.addWidget(self.channels)
        row = QtWidgets.QHBoxLayout()
        self.statistics = {}
        for name in BASIC_STATISTICS:
            box = QtWidgets.QCheckBox(STATISTIC_LABELS[name])
            box.setChecked(True)
            self.statistics[name] = box
            row.addWidget(box)
        layout.addLayout(row)
        self.more = QtWidgets.QGroupBox('More options')
        self.more.setCheckable(True)
        self.more.setChecked(False)
        body = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(body)
        spread = QtWidgets.QHBoxLayout()
        for name in SPREAD_STATISTICS:
            box = QtWidgets.QCheckBox(STATISTIC_LABELS[name])
            self.statistics[name] = box
            spread.addWidget(box)
        form.addRow('Spread', spread)
        self.percentiles = QtWidgets.QLineEdit()
        self.percentiles.setPlaceholderText('for example 5, 25, 75, 95')
        form.addRow('Percentiles', self.percentiles)
        self.ancestor = QtWidgets.QComboBox()
        self.ancestor.addItem('None', None)
        for gate in owner.project.default_gates():
            self.ancestor.addItem(gate.name, gate.id)
        form.addRow('Also % of population', self.ancestor)
        outer = QtWidgets.QVBoxLayout(self.more)
        outer.addWidget(body)
        body.setVisible(False)
        self.more.toggled.connect(body.setVisible)
        self.more.toggled.connect(lambda: self.adjustSize())
        layout.addWidget(self.more)
        layout.addWidget(QtWidgets.QLabel(
            'A .provenance.json file with versions, file checksums, gates and definitions is written next to the table.'))
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                             | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def options(self):
        channels = [self.channels.item(i).data(QtCore.Qt.ItemDataRole.UserRole)
                    for i in range(self.channels.count())
                    if self.channels.item(i).checkState() == QtCore.Qt.CheckState.Checked]
        statistics = [name for name, box in self.statistics.items() if box.isChecked()
                      and (self.more.isChecked() or name in BASIC_STATISTICS)]
        ancestor = None
        if self.more.isChecked():
            for part in re.split(r'[,;\s]+', self.percentiles.text().strip()):
                if part:
                    name = 'p' + part
                    if not valid_statistic(name) or not 0 < float(part) < 100:
                        raise ValueError(f'Not a percentile between 0 and 100: {part}')
                    statistics.append(name)
            ancestor = self.ancestor.currentData()
        if channels and not statistics:
            raise ValueError('Choose at least one statistic, or untick all channels to export counts only.')
        return {'layout': 'wide' if self.wide.isChecked() else 'long', 'channels': channels,
                'statistics': list(dict.fromkeys(statistics)), 'ancestor_id': ancestor}


class InformationDialog(QtWidgets.QDialog):
    def __init__(self, owner, information):
        super().__init__(owner)
        self.setWindowTitle('File information — ' + information['file'])
        self.resize(760, 520)
        layout = QtWidgets.QVBoxLayout(self)
        summary = QtWidgets.QLabel(f"{information['path']}\n{information['event_count']:,} events · SHA-256 {information['sha256']}")
        summary.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        summary.setWordWrap(True)
        layout.addWidget(summary)
        tabs = QtWidgets.QTabWidget()
        layout.addWidget(tabs)
        fields = ['index', 'name', 'marker', 'voltage', 'gain', 'range', 'amplification']
        tabs.addTab(self.table(['#', 'Channel', 'Marker', 'Voltage', 'Gain', 'Range', 'Amplification'],
                               [[str(c[f]) for f in fields] for c in information['channels']]), 'Channels')
        tabs.addTab(self.table(['Keyword', 'Value'], sorted(information['keywords'].items())), 'Keywords')
        spill = information['spillover']
        if spill:
            widget = self.table([''] + spill['channels'], [[name] + row for name, row in zip(spill['channels'], spill['values'])])
        else:
            widget = QtWidgets.QLabel('This file contains no readable spillover matrix.')
            widget.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        tabs.addTab(widget, 'Spillover (stored in file, not applied)')

    def table(self, headers, rows):
        table = QtWidgets.QTableWidget(len(rows), len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                table.setItem(i, j, QtWidgets.QTableWidgetItem(str(value)))
        table.resizeColumnsToContents()
        return table


class Actions:
    """Mixin for ``Workbench``; expects its project, results, jobs and widgets."""

    # ---- menus -------------------------------------------------------------
    def build_menus(self):
        file_menu = self.menuBar().addMenu('File')
        file_menu.addAction('Add FCS files…', self.add_files)
        file_menu.addAction('Open project…', lambda: self.open_project())
        self.recent_menu = file_menu.addMenu('Open recent')
        self.recent_menu.aboutToShow.connect(self.fill_recent_menu)
        file_menu.addAction('Save project', lambda: self.save())
        file_menu.addAction('Save project as…', lambda: self.save(True))
        file_menu.addSeparator()
        file_menu.addAction('Save portable package…', self.save_portable)
        file_menu.addAction('Open portable package…', self.open_portable)
        file_menu.addAction('Relink sample…', self.relink)
        file_menu.addSeparator()
        imports = file_menu.addMenu('Import')
        imports.addAction('Gate definitions…', self.import_gate_file)
        imports.addAction('Sample annotations (CSV)…', self.import_annotations)
        exports = file_menu.addMenu('Export')
        # Qt passes a 'checked' flag to slots that accept an argument.
        exports.addAction('Statistics…', lambda: self.export_statistics())
        exports.addAction('Plot images…', lambda: self.export_images())
        exports.addAction('Methods report…', self.export_methods_report)
        exports.addSeparator()
        exports.addAction('Gates as JSON…', self.export_gate_json)
        exports.addAction('Gates as Gating-ML…', self.export_gate_ml)
        self._menus = [file_menu, imports, exports]

    def build_help_menu(self):
        menu = self.menuBar().addMenu('Help')
        menu.addAction('About FlowHero', self.show_about)
        self._menus.append(menu)

    def show_about(self):
        QtWidgets.QMessageBox.about(self, 'About FlowHero', (
            f'<b>FlowHero {__version__}</b><br>Gating and statistics for flow cytometry files.<br><br>'
            f'FlowKit {package_version("flowkit")} · NumPy {package_version("numpy")} · '
            f'PySide6 {package_version("PySide6")} · pyqtgraph {package_version("pyqtgraph")}<br><br>'
            'Values are read as acquisition-preprocessed and untransformed; compensation is applied only '
            'when switched on in the line above the plots. '
            'Statistics always use every event of a population.'))

    # ---- recent projects ---------------------------------------------------
    def recent_file(self):
        return self.template_directory().parent / 'recent-projects.json'

    def recent_projects(self):
        try:
            items = json.loads(self.recent_file().read_text(encoding='utf-8'))
            return [p for p in items if isinstance(p, str)][:RECENT_LIMIT] if isinstance(items, list) else []
        except (OSError, ValueError):
            return []

    def remember_project(self, path):
        path = str(Path(path).absolute())
        items = [path] + [p for p in self.recent_projects() if p != path]
        try:
            self.recent_file().parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.recent_file(), lambda stream: json.dump(items[:RECENT_LIMIT], stream))
        except OSError:
            pass  # A read-only settings folder must never block saving a project.

    def fill_recent_menu(self):
        self.recent_menu.clear()
        items = self.recent_projects()
        for path in items:
            self.recent_menu.addAction(Path(path).name, lambda checked=False, p=path: self.open_project(p)).setToolTip(path)
        if not items:
            self.recent_menu.addAction('No recent projects').setEnabled(False)

    # ---- drag and drop -----------------------------------------------------
    def dropped_paths(self, event):
        return [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]

    def dragEnterEvent(self, event):
        paths = self.dropped_paths(event)
        if paths and all(p.lower().endswith(('.fcs', '.flowhero.json', '.flowpilot.json')) for p in paths):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = self.dropped_paths(event)
        projects = [p for p in paths if p.lower().endswith(('.flowhero.json', '.flowpilot.json'))]
        samples = [p for p in paths if p.lower().endswith('.fcs')]
        if projects and (samples or len(projects) > 1):
            self.error('Drop either one project file or FCS files, not both.')
        elif projects:
            self.open_project(projects[0])
        elif samples:
            self.add_paths(samples)
        event.acceptProposedAction()

    # ---- background tasks --------------------------------------------------
    def start_task(self, task, message):
        if self.task_jobs.busy:
            self.error('Another export or file look-up is still running; wait for it to finish.')
            return False
        if any(document.points for document in self.documents.values()):
            self.error('Finish or discard the drawing first.')
            return False
        try:
            self.project.validate()
            self.task_jobs.start(deepcopy(self.project), [task], target=self.task_target)
        except (ValueError, OSError, RuntimeError) as exc:
            self.error(exc)
            return False
        self.statusBar().showMessage(message)
        return True

    @property
    def task_target(self):
        from .task_jobs import task_worker
        return task_worker

    def poll_tasks(self):
        for message in self.task_jobs.poll():
            if message['kind'] in ('error', 'crash'):
                self.error(message['message'])
            elif message['kind'] == 'task_ready':
                task, result = message['task'], message['result']
                try:
                    getattr(self, 'finish_' + task['kind'])(task, result, message['revision'])
                except (ValueError, OSError, StopIteration) as exc:
                    self.error(exc)

    def finish_statistics(self, task, result, revision):
        text = f"Exported {result['rows']} rows to {Path(result['path']).name}"
        if result['failures']:
            names = ', '.join(f['sample'] for f in result['failures'])
            self.error(f"Statistics export left out {len(result['failures'])} sample(s) without results: {names}. "
                       'The reasons are in the provenance file.')
        else:
            self.statusBar().showMessage(text + ' with its provenance file.')

    def finish_events(self, task, result, revision):
        self.statusBar().showMessage(f"Exported {result['rows']:,} events to {Path(result['path']).name}.")

    def finish_information(self, task, result, revision):
        dialog = InformationDialog(self, result)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        self._information_window = dialog
        self.statusBar().showMessage('File information read.')

    def finish_threshold(self, task, result, revision):
        if revision != self.project.revision:
            raise ValueError('The project changed while the threshold was calculated; run it again.')
        from . import scales as scale_rules
        upper = float(scale_rules.forward([OPEN_BOUND], task['scale'])[0])
        value = result['value']
        gate = RangeGate(new_id(), task['name'], task['population_id'], task['channel'], value, upper, (task['scale'],))
        self.add_gates([gate])
        shown = float(scale_rules.inverse([value], task['scale'])[0])
        self.notify(
            f"Threshold {shown:.6g} = {task['percentile']:g}th percentile of {result['events']:,} control events. "
            'It is an ordinary range gate; drag it or use Undo.')

    # ---- statistics --------------------------------------------------------
    def default_statistic_channels(self):
        names = [self.project.channel] + [c for p in self.project.plots for c in (p.x_channel, p.y_channel)]
        return [c for c in dict.fromkeys(names) if c in self.channels]

    def export_statistics(self, options=None):
        if not self.project.sources:
            self.error('Add FCS files before exporting statistics.')
            return
        if options is None:
            dialog = StatisticsExportDialog(self, self.default_statistic_channels())
            try:
                if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                    return
                options = dialog.options()
            except ValueError as exc:
                self.error(exc)
                return
            finally:
                dialog.deleteLater()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Export statistics', 'statistics.csv', 'CSV (*.csv)')
        if path:
            self.start_task({'kind': 'statistics', 'path': path, 'project_path': self.project_path, **options},
                            f'Calculating statistics for {len(self.project.sources)} sample(s)…')

    def export_methods_report(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Export methods report', 'analysis-record.md', 'Markdown (*.md)')
        if not path:
            return
        try:
            self.project.validate()
            record = provenance(self.project, project_path=self.project_path,
                                channels=self.default_statistic_channels(), statistics=BASIC_STATISTICS)
            atomic_write(path, lambda stream: stream.write(methods_report(record)))
            self.statusBar().showMessage(f'Methods report written: {Path(path).name}')
        except (ValueError, OSError) as exc:
            self.error(exc)

    # ---- plot data ---------------------------------------------------------
    def export_plot_data(self, plot_id):
        spec = self.plot_spec(plot_id)
        if spec.kind == 'histogram':
            self.export_plot(plot_id)
            return
        source = self.selected_source()
        if source is None:
            return
        choice = 'Events'
        if spec.kind == 'density':
            choice, ok = QtWidgets.QInputDialog.getItem(
                self, 'Export plot data', 'Data to export',
                ['Events (one row per event, both channels)', '2D bin counts and statistics'], 0, False)
            if not ok:
                return
        if choice.startswith('2D'):
            self.export_plot(plot_id)
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Export events of the shown population', 'plot-events.csv', 'CSV (*.csv)')
        if path:
            self.start_task({'kind': 'events', 'path': path, 'sample_ids': [source.id],
                             'population_id': spec.population_id, 'channels': [spec.x_channel, spec.y_channel]},
                            'Writing untransformed events of the shown population…')

    # ---- images ------------------------------------------------------------
    def export_images(self, options=None):
        if not self.documents or not self.project.sources:
            self.error('Open at least one plot and one sample before exporting images.')
            return
        if options is None:
            selected = self.selected_sample_ids()
            labels = [f'All {len(self.project.sources)} samples', f'Selected sample(s) ({len(selected)})']
            scope, ok = QtWidgets.QInputDialog.getItem(self, 'Export plot images', 'Samples', labels, 0, False)
            if not ok:
                return
            kind, ok = QtWidgets.QInputDialog.getItem(self, 'Export plot images', 'Format (white background)',
                                                      ['PNG', 'SVG', 'PDF'], 0, False)
            if not ok:
                return
            folder = QtWidgets.QFileDialog.getExistingDirectory(self, 'Folder for the plot images')
            if not folder:
                return
            options = {'folder': folder, 'format': kind.lower(),
                       'sample_ids': list(selected) if scope == labels[1] else [s.id for s in self.project.sources]}
        sources = [s for s in self.project.sources if s.id in options['sample_ids']]
        current = all(self.results.get(s.id, {}).get('_verified', False)
                      and self.results[s.id].get('_dependencies') == self.current_key(s) for s in sources)
        if not current:
            self._pending_images = options
            self.request(True)
            self.statusBar().showMessage('Calculating all samples before writing images…')
            return
        self.write_images(options, sources)

    def write_images(self, options, sources):
        folder, written, skipped = Path(options['folder']), 0, []
        overlays_done = set()
        try:
            for source in sources:
                for document in self.documents.values():
                    spec = document.spec
                    shared = bool(spec.sample_ids)
                    if shared and document.plot_id in overlays_done:
                        continue
                    title = safe_name(document.windowTitle())
                    stem = ('overlay' if shared else safe_name(Path(source.path).stem)) + '__' + title
                    target = folder / f"{stem}.{options['format']}"
                    number = 2
                    while target.exists():
                        target = folder / f"{stem}_{number}.{options['format']}"
                        number += 1
                    if document.export_image(str(target), source=source):
                        written += 1
                        if shared:
                            overlays_done.add(document.plot_id)
                    else:
                        skipped.append(f'{Path(source.path).name}: {document.windowTitle()}')
        except OSError as exc:
            self.error(exc)
        finally:
            self.render_documents()
        if skipped:
            self.error(f'{len(skipped)} image(s) had no result and were not written: ' + '; '.join(skipped[:5]))
        self.statusBar().showMessage(f'Wrote {written} image(s) to {folder}. Existing files were kept.')

    # ---- control threshold -------------------------------------------------
    def threshold_from_control(self, plot_id, options=None):
        spec = self.plot_spec(plot_id)
        if spec.kind != 'histogram' or not self.project.sources:
            return
        if options is None:
            names = [Path(s.path).name for s in self.project.sources]
            current = self.samples.currentRow()
            name, ok = QtWidgets.QInputDialog.getItem(self, 'Set threshold from control', 'Control sample',
                                                      names, max(current, 0), False)
            if not ok:
                return
            percentile, ok = QtWidgets.QInputDialog.getDouble(
                self, 'Set threshold from control',
                f'Percentile of {spec.x_channel} in the control\n(events above it become the new population)',
                99.0, 0.01, 99.99, 2)
            if not ok:
                return
            options = {'sample_id': self.project.sources[names.index(name)].id, 'percentile': percentile}
        task = {'kind': 'threshold', 'population_id': spec.population_id, 'channel': spec.x_channel,
                'scale': spec.x_scale, 'name': self.gate_name.text().strip() or f'{spec.x_channel}+', **options}
        self.start_task(task, 'Calculating the threshold from every event of the control population…')

    # ---- samples -----------------------------------------------------------
    def sample_menu(self, position):
        item = self.samples.itemAt(position)
        if item is None:
            return
        self.samples.setCurrentItem(item)
        menu = QtWidgets.QMenu(self)
        self._sample_menu = menu
        menu.addAction('File information…', self.show_file_information)
        menu.addAction('Acquisition over time…', self.show_time_course)
        menu.addAction('Relink sample…', self.relink)
        menu.addSeparator()
        menu.addAction('Remove sample from project', self.remove_sample)
        menu.exec(self.samples.viewport().mapToGlobal(position))

    def show_file_information(self):
        source = self.selected_source()
        if source is not None:
            self.start_task({'kind': 'information', 'sample_id': source.id}, 'Reading file information…')

    def remove_sample(self):
        source = self.selected_source()
        if source is None:
            return
        if self.project.analysis is not None:
            self.error('This project holds proposed gate positions per sample; finish or discard that review before removing samples.')
            return
        candidate = deepcopy(self.project)
        candidate.sources = [s for s in candidate.sources if s.id != source.id]
        candidate.overrides.pop(source.id, None)
        candidate.annotations.pop(source.id, None)
        remaining = lambda p: tuple(i for i in p.sample_ids if i != source.id)
        candidate.plots = [replace(p, sample_ids=remaining(p), pinned=p.pinned and bool(remaining(p)))
                           for p in candidate.plots]
        if candidate.histogram:
            candidate.histogram = replace(candidate.histogram,
                                          sample_ids=tuple(i for i in candidate.histogram.sample_ids if i != source.id))
        if self.changed(candidate, rebuild=True):
            self.failures.pop(source.id, None)
            self.rebuild_samples()
            self.statusBar().showMessage(f'Removed {Path(source.path).name} from the project. The file itself is untouched; Undo restores it.')

    def import_annotations(self):
        if not self.project.sources:
            self.error('Add FCS files before importing annotations.')
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, 'Import sample annotations — first column: file name; further columns: your labels', '',
            'Tables (*.csv *.tsv *.txt);;All files (*)')
        if not path:
            return
        try:
            annotations, unmatched = read_annotations(path, self.project.sources)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            self.error(f'Annotation import failed: {exc}')
            return
        candidate = deepcopy(self.project)
        candidate.annotations.update(annotations)
        if self.changed(candidate):
            self.rebuild_samples()
            text = f'Annotated {len(annotations)} of {len(self.project.sources)} sample(s); annotations are added to every export.'
            if unmatched:
                self.error(text + f' {len(unmatched)} row(s) matched no file name: ' + ', '.join(unmatched[:5]))
            else:
                self.statusBar().showMessage(text)

    def sample_caption(self, source):
        values = [v for v in self.project.annotations.get(source.id, {}).values() if v]
        return Path(source.path).name + (f"   [{' · '.join(values)}]" if values else '')

    def sample_tooltip(self, source):
        notes = self.project.annotations.get(source.id, {})
        return source.path + ''.join(f'\n{key}: {value}' for key, value in notes.items())


def install_exception_hook(window):
    """Report unexpected errors in the window and a log file instead of losing them."""
    import sys
    import traceback
    previous = sys.excepthook

    def report(kind, value, trace):
        text = ''.join(traceback.format_exception(kind, value, trace))
        try:
            log = window.template_directory().parent / 'flowhero-errors.log'
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open('a', encoding='utf-8') as stream:
                stream.write(f'--- FlowHero {__version__} ---\n{text}\n')
            window.error(f'Unexpected error: {kind.__name__}: {value}. Your project is unchanged; details were saved to {log}.')
        except Exception:
            previous(kind, value, trace)

    sys.excepthook = report
    return report
