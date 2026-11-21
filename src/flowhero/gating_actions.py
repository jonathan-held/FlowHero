"""Gate-tree, compensation, quality-view and session actions of the main window.

Second mixin of ``Workbench`` next to ``actions.Actions``. Everything here is
reached from an existing context menu, the View menu or the status line, so the
plot sheet itself gains no controls.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json
import os

from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg

from .model import (COMPENSATED_DOMAIN, DOMAIN, BooleanGate, PlotSpec, Polygon, Rectangle, new_id, ordered_gates,
                    validate_compensation)
from .plot_sheet import DARK, LIGHT
from .storage import atomic_write, load_project, save_project

AUTOSAVE_SECONDS = 120
OPERATIONS = [('In all of the ticked gates (AND)', 'and'), ('In any of the ticked gates (OR)', 'or'),
              ('Not in the gate (NOT)', 'not')]


class CombineDialog(QtWidgets.QDialog):
    def __init__(self, owner, gate, siblings):
        super().__init__(owner)
        self.setWindowTitle('Combine gates')
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel('The new population holds the events of the parent population that are:'))
        self.operation = QtWidgets.QComboBox()
        for label, value in OPERATIONS:
            self.operation.addItem(label, value)
        layout.addWidget(self.operation)
        self.gates = QtWidgets.QListWidget()
        for sibling in siblings:
            item = QtWidgets.QListWidgetItem(sibling.name)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, sibling.id)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked if sibling.id == gate.id else QtCore.Qt.CheckState.Unchecked)
            self.gates.addItem(item)
        layout.addWidget(self.gates)
        self.name = QtWidgets.QLineEdit()
        self.name.setPlaceholderText('Name (optional)')
        layout.addWidget(self.name)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                             | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choice(self):
        ids = [self.gates.item(i).data(QtCore.Qt.ItemDataRole.UserRole) for i in range(self.gates.count())
               if self.gates.item(i).checkState() == QtCore.Qt.CheckState.Checked]
        names = [self.gates.item(i).text() for i in range(self.gates.count())
                 if self.gates.item(i).checkState() == QtCore.Qt.CheckState.Checked]
        operation = self.operation.currentData()
        default = ('not ' + names[0]) if operation == 'not' and names else f' {operation} '.join(names)
        return operation, tuple(ids), self.name.text().strip() or default


class CompensationDialog(QtWidgets.QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle('Compensation')
        self.resize(720, 520)
        layout = QtWidgets.QVBoxLayout(self)
        self.off = QtWidgets.QRadioButton('Off — use the values as stored in the files')
        self.file = QtWidgets.QRadioButton('Use the spillover matrix stored in each file')
        self.matrix = QtWidgets.QRadioButton('Use one spillover matrix for all samples')
        for button in (self.off, self.file, self.matrix):
            layout.addWidget(button)
        row = QtWidgets.QHBoxLayout()
        for label, callback in (('Take from selected sample', self.load_selected), ('Import CSV…', self.import_csv),
                                ('Export CSV…', self.export_csv)):
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
        row.addStretch(1)
        self.buttons_row = row
        layout.addLayout(row)
        self.table = QtWidgets.QTableWidget(0, 0)
        layout.addWidget(self.table, 1)
        self.message = QtWidgets.QLabel(
            'Rows: fluorochrome; columns: detector; values as in the FCS $SPILLOVER keyword (1 on the diagonal). '
            'Compensated values replace the stored ones for gates, plots and statistics. Existing gates keep their '
            'coordinates, so check gates on fluorescence channels after changing this setting. Undo reverses it.')
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                             | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        current = owner.project.compensation
        (self.off if current is None else self.file if current['source'] == 'file' else self.matrix).setChecked(True)
        if current and current['source'] == 'matrix':
            self.show_matrix(current)
        self.result_value = current

    def show_matrix(self, compensation):
        names = compensation['channels']
        self.table.setRowCount(len(names))
        self.table.setColumnCount(len(names))
        self.table.setHorizontalHeaderLabels(names)
        self.table.setVerticalHeaderLabels(names)
        for r, row in enumerate(compensation['values']):
            for c, value in enumerate(row):
                self.table.setItem(r, c, QtWidgets.QTableWidgetItem(format(float(value), '.6g')))
        self.table.resizeColumnsToContents()
        self.matrix.setChecked(True)

    def table_matrix(self):
        size = self.table.rowCount()
        if size < 2:
            raise ValueError('Take a matrix from a sample or import one first.')
        names = [self.table.horizontalHeaderItem(i).text() for i in range(size)]
        try:
            values = [[float(self.table.item(r, c).text().replace(',', '.')) for c in range(size)] for r in range(size)]
        except (AttributeError, ValueError):
            raise ValueError('Every cell of the matrix needs a number.') from None
        compensation = {'source': 'matrix', 'channels': names, 'values': values}
        validate_compensation(compensation)
        return compensation

    def load_selected(self):
        source = self.owner.selected_source()
        if source is None:
            self.message.setText('Select a sample first.')
        elif self.owner.start_task({'kind': 'spillover', 'sample_id': source.id}, 'Reading the stored spillover matrix…'):
            self.message.setText('Reading the spillover matrix of ' + Path(source.path).name + '…')

    def spillover_ready(self, result):
        name = Path(result['sample']).name
        if result['spillover'] is None:
            self.message.setText(f'{name} stores no readable spillover matrix.')
        else:
            self.show_matrix(result['spillover'])
            self.message.setText(f'Matrix taken from {name}. Cells can be edited before you confirm.')

    def import_csv(self):
        from .export import read_spillover_csv
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Import spillover matrix', '', 'Tables (*.csv *.tsv *.txt);;All files (*)')
        if path:
            try:
                self.show_matrix(read_spillover_csv(path))
                self.message.setText('Matrix imported from ' + Path(path).name + '.')
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                self.message.setText(f'Import failed: {exc}')

    def export_csv(self):
        from .export import write_spillover_csv
        try:
            compensation = self.table_matrix()
            path, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Export spillover matrix', 'spillover.csv', 'CSV (*.csv)')
            if path:
                write_spillover_csv(path, compensation)
        except (OSError, ValueError) as exc:
            self.message.setText(str(exc))

    def accept(self):
        try:
            if self.off.isChecked():
                self.result_value = None
            elif self.file.isChecked():
                self.result_value = {'source': 'file'}
            else:
                self.result_value = self.table_matrix()
        except ValueError as exc:
            self.message.setText(str(exc))
            return
        super().accept()


class TimeCourseDialog(QtWidgets.QDialog):
    def __init__(self, owner, result):
        super().__init__(owner)
        import numpy as np
        name = Path(result['sample']).name
        self.setWindowTitle('Acquisition over time — ' + name)
        self.resize(760, 560)
        layout = QtWidgets.QVBoxLayout(self)
        theme = getattr(owner, 'screen_theme', DARK)
        edges, counts = np.asarray(result['edges']), np.asarray(result['counts'], dtype=float)
        rate = pg.PlotWidget()
        rate.setBackground(theme['background'])
        rate.setLabel('left', 'Events / time bin')
        rate.plot(edges, counts, stepMode='center', pen=pg.mkPen(theme['points'][:3], width=2))
        rate.addItem(pg.InfiniteLine(result['median_count'], angle=0, pen=pg.mkPen(theme['gate'], style=QtCore.Qt.PenStyle.DashLine)))
        for index, flagged in enumerate(result['flagged']):
            if flagged:
                region = pg.LinearRegionItem((edges[index], edges[index + 1]), movable=False,
                                             brush=pg.mkBrush(*theme['highlight'][:3], 70), pen=pg.mkPen(None))
                rate.addItem(region)
        layout.addWidget(rate, 1)
        self.plots = [rate]
        if result['signal_medians'] is not None:
            signal = pg.PlotWidget()
            signal.setBackground(theme['background'])
            signal.setLabel('left', f"Median {result['channel']} / bin")
            signal.setLabel('bottom', result['time_channel'])
            centres = (edges[:-1] + edges[1:]) / 2
            values = np.array([np.nan if v is None else v for v in result['signal_medians']], dtype=float)
            signal.plot(centres, values, pen=pg.mkPen(theme['points'][:3], width=2), connect='finite')
            signal.setXLink(rate)
            layout.addWidget(signal, 1)
            self.plots.append(signal)
        else:
            rate.setLabel('bottom', result['time_channel'])
        for plot in self.plots:
            for axis in ('left', 'bottom'):
                plot.getAxis(axis).setPen(theme['foreground'])
                plot.getAxis(axis).setTextPen(theme['foreground'])
        share = 100 * result['flagged_events'] / result['events'] if result['events'] else 0
        self.summary = QtWidgets.QLabel(
            f"{result['events']:,} events in {len(counts)} equal time bins; median {result['median_count']:.0f} events per bin. "
            f"{sum(result['flagged'])} bin(s) differ from the median by more than {result['limit']:g} robust SD "
            f"({result['robust_sd']:.1f} events) and are shaded; they hold {share:.2f} % of the events. "
            'This view only describes the acquisition. No event is removed; to exclude a period, plot the time '
            'channel as a histogram and draw a range gate.')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)


class GatingActions:
    """Mixin for ``Workbench``."""

    # ---- result identity ---------------------------------------------------
    def current_key(self, source):
        from .dependencies import result_key
        return (result_key(self.project, source), self.highlight_id)

    def analysis_snapshot(self):
        snapshot = deepcopy(self.project)
        snapshot._highlight = self.highlight_id
        return snapshot

    # ---- settings ----------------------------------------------------------
    def settings_file(self):
        return self.template_directory().parent / 'settings.json'

    def read_settings(self):
        try:
            data = json.loads(self.settings_file().read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def write_setting(self, name, value):
        data = self.read_settings()
        data[name] = value
        try:
            self.settings_file().parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.settings_file(), lambda stream: json.dump(data, stream))
        except OSError:
            pass  # A read-only settings folder must not block the session.

    def set_light_plots(self, light):
        self.screen_theme = LIGHT if light else DARK
        self.write_setting('light_plots', bool(light))
        for document in self.documents.values():
            document.apply_theme(self.screen_theme)
        self.render_documents()

    # ---- domain line -------------------------------------------------------
    def update_domain_label(self):
        compensation = self.project.compensation
        state = ('off' if compensation is None else 'from each file' if compensation['source'] == 'file'
                 else f"one matrix, {len(compensation['channels'])} channels")
        self.domain_label.setText(
            f'Compensation: <a href="#compensation">{state}</a> • full-event gating/statistics • '
            'click axis labels to choose channels • right-click plots for axes, limits and exports')

    def edit_compensation(self, *args):
        if self.project.viability is not None or self.project.histogram is not None:
            self.error('Compensation is not available for projects made in the first FlowHero window.')
            return
        dialog = CompensationDialog(self)
        self._compensation_dialog = dialog
        try:
            if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                return
            self.set_compensation(dialog.result_value)
        finally:
            self._compensation_dialog = None
            dialog.deleteLater()

    def set_compensation(self, compensation):
        if compensation == self.project.compensation:
            return False
        if self.project.analysis is not None:
            self.error('This project holds automatically proposed gate positions, which are defined for uncompensated '
                       'values. Finish or discard that review before changing compensation.')
            return False
        candidate = deepcopy(self.project)
        candidate.compensation = compensation
        candidate.domain = COMPENSATED_DOMAIN if compensation else DOMAIN
        if self.changed(candidate):
            self.notify('Compensation changed. Check gates on fluorescence channels; Undo reverses this.')
            return True
        return False

    def finish_spillover(self, task, result, revision):
        dialog = getattr(self, '_compensation_dialog', None)
        if dialog is not None:
            dialog.spillover_ready(result)

    # ---- combined gates ----------------------------------------------------
    def combine_gates(self, options=None):
        gate_id = self.selected_population()
        gates = {g.id: g for g in self.project.default_gates()}
        if gate_id not in gates:
            return
        gate = gates[gate_id]
        if options is None:
            siblings = [g for g in ordered_gates(self.project.default_gates()) if g.parent_id == gate.parent_id]
            dialog = CombineDialog(self, gate, siblings)
            try:
                if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                    return
                options = dialog.choice()
            finally:
                dialog.deleteLater()
        operation, operands, name = options
        try:
            combined = BooleanGate(new_id(), name, gate.parent_id, operation, tuple(operands))
        except ValueError as exc:
            self.error(exc)
            return
        self.add_gates([combined])

    # ---- copy and paste ----------------------------------------------------
    def copy_gate(self):
        gate_id = self.selected_population()
        gates = ordered_gates(self.project.default_gates())
        by_id = {g.id: g for g in gates}
        if gate_id not in by_id:
            return
        if isinstance(by_id[gate_id], BooleanGate):
            self.error('A combined gate refers to its neighbours. Copy the gates it combines, or their parent, instead.')
            return
        branch = {gate_id}
        for gate in gates:
            if gate.parent_id in branch and all(o in branch for o in getattr(gate, 'operands', ())):
                branch.add(gate.id)
        self.gate_clipboard = [g for g in gates if g.id in branch]
        self.statusBar().showMessage(f"Copied {by_id[gate_id].name} with {len(branch) - 1} descendant gate(s). "
                                     'Right-click a population and choose Paste.')

    def paste_gate(self):
        copied = getattr(self, 'gate_clipboard', None)
        if not copied:
            return
        target = self.selected_population()
        identities = {g.id: new_id() for g in copied}
        members = {}
        for gate in copied:
            if isinstance(gate, Rectangle) and gate.group_id:
                members.setdefault(gate.group_id, []).append(gate.id)
        # A quadrant set stays linked only when all four parts were copied.
        groups = {group: new_id() for group, ids in members.items() if len(ids) == 4}
        pasted = []
        for index, gate in enumerate(copied):
            changes = {'id': identities[gate.id], 'parent_id': target if index == 0 else identities[gate.parent_id]}
            if isinstance(gate, BooleanGate):
                changes['operands'] = tuple(identities[o] for o in gate.operands)
            if isinstance(gate, Rectangle) and gate.group_id:
                changes['group_id'] = groups.get(gate.group_id)
            pasted.append(replace(gate, **changes))
        candidate = deepcopy(self.project)
        candidate.gates.extend(pasted)
        if self.changed(candidate, pasted[0].id):
            self.notify(f'Pasted {len(pasted)} gate(s) with the shared geometry. Undo reverses this.')

    # ---- highlight (backgating) --------------------------------------------
    def set_highlight(self, gate_id):
        if gate_id == self.highlight_id:
            return
        self.highlight_id = gate_id
        self.request(reuse=True)
        if gate_id is None:
            self.notify('Highlight cleared.')
        else:
            name = next((g.name for g in self.project.default_gates() if g.id == gate_id), '')
            self.notify(f'{name} is highlighted in every 2D plot of the selected sample. Esc clears it.')

    def clear_highlight(self):
        self.set_highlight(None)

    # ---- singlet gate ------------------------------------------------------
    def singlet_pairs(self):
        pairs = []
        for name in self.channels:
            if name.upper().endswith('-A') and name[:-2] + '-H' in self.channels:
                pairs.append((name, name[:-2] + '-H'))
        # Forward scatter first, then side scatter, then anything else.
        return sorted(pairs, key=lambda p: (not p[0].upper().startswith('FSC'), not p[0].upper().startswith('SSC')))

    def add_singlet_gate(self, pair=None):
        source = self.selected_source()
        pairs = self.singlet_pairs()
        if source is None or not pairs:
            return
        if pair is None:
            pair = pairs[0]
            if len(pairs) > 1:
                labels = [f'{a} vs {h}' for a, h in pairs]
                label, ok = QtWidgets.QInputDialog.getItem(self, 'Add singlet gate', 'Area and height channels', labels, 0, False)
                if not ok:
                    return
                pair = pairs[labels.index(label)]
        self.start_task({'kind': 'singlets', 'sample_id': source.id, 'population_id': self.selected_population(),
                         'area': pair[0], 'height': pair[1]},
                        f'Fitting a singlet band to {Path(source.path).name}…')

    def finish_singlets(self, task, result, revision):
        if revision != self.project.revision:
            raise ValueError('The project changed while the singlet gate was calculated; run it again.')
        gate = Polygon(new_id(), 'Singlets', task['population_id'],
                       (task['area'], task['height']), tuple(tuple(float(v) for v in p) for p in result['vertices']))
        candidate = deepcopy(self.project)
        candidate.gates.append(gate)
        n = len(candidate.plots)
        spec = PlotSpec(new_id(), 'density', task['area'], task['height'], task['population_id'], bins=64,
                        geometry=((n % 2) * 520, (n // 2) * 380, 520, 380))
        candidate.plots.append(spec)
        if self.changed(candidate, gate.id):
            self.add_document(spec)
            d = result['diagnostics']
            self.notify(
                f"Singlet gate from the selected sample: height/area {d['median_ratio']:.3f} ± 3 robust SD "
                f"({d['robust_sd']:.3f}); {100 * d['fraction_inside']:.1f} % of {d['events']:,} events inside. "
                'It is an ordinary polygon shared by all samples: check it, drag it, or Undo.')

    # ---- time course -------------------------------------------------------
    def show_time_course(self):
        source = self.selected_source()
        if source is not None:
            self.start_task({'kind': 'time_qc', 'sample_id': source.id, 'channel': self.project.channel},
                            'Reading the acquisition over time…')

    def finish_time_qc(self, task, result, revision):
        dialog = TimeCourseDialog(self, result)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        self._time_window = dialog
        self.statusBar().showMessage('Time course calculated from every event of the file.')

    # ---- overlays ----------------------------------------------------------
    def set_overlay(self, plot_id, enabled):
        self.edit_plot(plot_id, overlay=bool(enabled), sample_ids=())
        if enabled:
            self.apply_sample_selection()

    # ---- autosave ----------------------------------------------------------
    def autosave_directory(self):
        return self.template_directory().parent / 'autosave'

    def autosave(self):
        """Keep a recovery copy of unsaved work. Never touches the project file."""
        if not self.dirty or not (self.project.sources or self.project.gates):
            return False
        try:
            directory = self.autosave_directory()
            directory.mkdir(parents=True, exist_ok=True)
            if self._autosave_path is None:
                path = directory / f'recovery-{new_id()[:12]}.flowhero.json'
                lock = QtCore.QLockFile(str(path) + '.lock')
                lock.setStaleLockTime(0)
                if not lock.tryLock(0):
                    return False
                self._autosave_path, self._autosave_lock = path, lock
            save_project(deepcopy(self.project), self._autosave_path)
            note = {'project_path': self.project_path,
                    'saved_utc': datetime.now(timezone.utc).isoformat(timespec='seconds')}
            atomic_write(str(self._autosave_path) + '.note', lambda stream: json.dump(note, stream))
            return True
        except (OSError, ValueError):
            return False  # A drawing in progress or a full disk must not interrupt work.

    def clear_autosave(self):
        path, lock = self._autosave_path, self._autosave_lock
        self._autosave_path = self._autosave_lock = None
        if path is not None:
            for name in (str(path), str(path) + '.note'):
                try:
                    os.unlink(name)
                except OSError:
                    pass
        if lock is not None:
            lock.unlock()

    def recovery_candidates(self):
        """Recovery copies whose FlowHero session is no longer running."""
        found = []
        directory = self.autosave_directory()
        if not directory.is_dir():
            return found
        for path in sorted(directory.glob('recovery-*.flowhero.json')):
            if path == self._autosave_path:
                continue
            lock = QtCore.QLockFile(str(path) + '.lock')
            lock.setStaleLockTime(0)
            if not lock.tryLock(0):
                continue  # Another running window owns this copy.
            try:
                note = json.loads(Path(str(path) + '.note').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                note = {}
            found.append((path, lock, note if isinstance(note, dict) else {}))
        return found

    def discard_recovery(self, path, lock):
        for name in (str(path), str(path) + '.note'):
            try:
                os.unlink(name)
            except OSError:
                pass
        lock.unlock()

    def restore_recovery(self, path, lock, note):
        project = load_project(path)
        self.stop_analysis()
        self.close_gate_review()
        project.revision = max(project.revision, self.project.revision) + 1
        self.project, self.project_path, self.dirty = project, None, True
        self.review_action.setVisible(project.analysis is not None)
        self.results.clear(); self.undo_states.clear(); self.redo_states.clear(); self.channels = []
        inferred = list(dict.fromkeys([c for g in project.default_gates() for c in g.channels] +
                                      [c for p in project.plots for c in (p.x_channel, p.y_channel) if c] +
                                      ([project.channel] if project.channel else [])))
        self.set_channels(inferred); self.rebuild_samples(); self.update_tree(); self.refresh_table()
        self.update_domain_label()
        self.rebuild_documents(); self._refresh_timer.start(20)
        self.discard_recovery(path, lock)
        origin = note.get('project_path')
        self.notify('Recovered unsaved work' + (f' of {Path(origin).name}' if origin else '')
                    + '. It is not saved yet: use Save project as… to keep it.')

    def offer_recovery(self):
        for path, lock, note in self.recovery_candidates():
            origin = note.get('project_path')
            when = note.get('saved_utc', 'an earlier session')
            box = QtWidgets.QMessageBox(self)
            box.setWindowTitle('Recover unsaved work')
            box.setText('FlowHero did not close normally last time.\n\n'
                        f"Unsaved work from {when}" + (f" (project {Path(origin).name})" if origin else '')
                        + ' can be restored. The project file itself was not changed.')
            restore = box.addButton('Restore', QtWidgets.QMessageBox.ButtonRole.AcceptRole)
            discard = box.addButton('Discard', QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
            box.addButton('Decide later', QtWidgets.QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is restore:
                try:
                    self.restore_recovery(path, lock, note)
                except (OSError, ValueError) as exc:
                    self.error(f'The recovery copy could not be read and was kept at {path}: {exc}')
                    lock.unlock()
                return
            if box.clickedButton() is discard:
                self.discard_recovery(path, lock)
            else:
                lock.unlock()
