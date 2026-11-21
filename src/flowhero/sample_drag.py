"""Dragging samples from the sample list onto a histogram.

A drop gives that one histogram a fixed sample list: the samples it showed
before the drag plus the dropped ones. Fixed histograms no longer follow the
selection in the sample list until "Follow sample selection" is switched on
again. Nothing here changes gates, populations, bins or statistics.
"""
from copy import deepcopy
from dataclasses import replace
import json
from PySide6 import QtCore, QtGui, QtWidgets
from . import scales as scale_rules
from .model import new_id

MIME = 'application/x-flowhero-samples'


def encode(token, sample_ids):
    data = QtCore.QMimeData()
    data.setData(MIME, json.dumps({'window': token, 'sample_ids': list(sample_ids)}).encode('utf-8'))
    return data


def decode(mime, token, known_ids):
    """Sample identities of a drag that started in this window, or None.

    Anything else is refused: another window or program, malformed content,
    repeated or unknown identities.
    """
    if mime is None or not mime.hasFormat(MIME):
        return None
    try:
        content = json.loads(bytes(mime.data(MIME)).decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(content, dict) or set(content) != {'window', 'sample_ids'} or content['window'] != token:
        return None
    ids = content['sample_ids']
    if (not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids)
            or len(set(ids)) != len(ids) or not set(ids) <= set(known_ids)):
        return None
    return tuple(ids)


class SampleList(QtWidgets.QListWidget):
    """The sample list; its rows can be dragged onto a histogram."""

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setDragEnabled(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(QtCore.Qt.DropAction.CopyAction)

    def mousePressEvent(self, event):
        # Pressing an unselected row selects it before the drag begins. Remember
        # what every histogram showed, so the drop adds to that.
        if (event.button() == QtCore.Qt.MouseButton.LeftButton
                and self.itemAt(event.position().toPoint()) is not None):
            self.owner.remember_sample_press()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.owner.forget_sample_press()

    def startDrag(self, supported_actions):
        ids = self.owner.selected_sample_ids()
        if not ids:
            return
        drag = QtGui.QDrag(self)
        drag.setMimeData(encode(self.owner.drag_token, ids))
        try:
            drag.exec(QtCore.Qt.DropAction.CopyAction)
        finally:
            self.owner.finish_sample_drag()


class SampleDropActions:
    """Workbench methods for fixed sample lists of histograms."""

    def init_sample_drag(self):
        self.drag_token = new_id()
        self._drag_origin = None
        self._sample_drop_done = False

    def shown_sample_ids(self, spec):
        source = self.selected_source()
        return spec.sample_ids or ((source.id,) if source else ())

    def remember_sample_press(self):
        self._sample_drop_done = False
        self._drag_origin = {
            'current': self.samples.currentRow(),
            'selected': [i for i in range(self.samples.count()) if self.samples.item(i).isSelected()],
            'sources': tuple(s.id for s in self.project.sources),
            'shown': {p.id: self.shown_sample_ids(p) for p in self.project.plots if p.kind == 'histogram'}}

    def forget_sample_press(self):
        self._drag_origin = None

    def sample_drop_ids(self, plot_id, mime):
        """Identities a drop on this plot would add, or None when it is refused."""
        spec = next((p for p in self.project.plots if p.id == plot_id), None)
        if spec is None or spec.kind != 'histogram':
            return None
        return decode(mime, self.drag_token, [s.id for s in self.project.sources])

    def drop_samples(self, plot_id, sample_ids, base=None):
        """Add samples to one histogram and fix its sample list. Returns True when accepted."""
        spec = next((p for p in self.project.plots if p.id == plot_id), None)
        if spec is None or spec.kind != 'histogram':
            self.error('Samples can be dropped onto histograms only.')
            return False
        known = [s.id for s in self.project.sources]
        if base is None and self._drag_origin is not None:
            base = self._drag_origin['shown'].get(plot_id)
        if base is None:
            base = self.shown_sample_ids(spec)
        base = tuple(i for i in base if i in known)
        members = base + tuple(i for i in dict.fromkeys(sample_ids) if i in known and i not in base)
        if not members:
            return False
        added = len(members) - len(base)
        if spec.pinned and spec.sample_ids == members:
            self._sample_drop_done = True
            self.statusBar().showMessage('Those samples are already shown in this histogram.')
            return True
        limits = spec.x_limits
        if limits is None:
            # Overlaid samples share one axis and one set of bins: keep the range shown now.
            data = next((self.results[sid]['sheet_plots'][plot_id] for sid in members
                         if self.results.get(sid, {}).get('sheet_plots', {}).get(plot_id, {}).get('x_bounds')), None)
            if data is None:
                self.error('Wait until this histogram has been calculated, then drop the samples again.')
                return False
            limits = tuple(float(v) for v in scale_rules.inverse(data['x_bounds'], spec.x_scale))
        candidate = deepcopy(self.project)
        candidate.plots = [replace(p, sample_ids=members, pinned=True, x_limits=limits) if p.id == plot_id else p
                           for p in candidate.plots]
        if not self.changed(candidate):
            return False
        self._sample_drop_done = True
        self.notify((f'Added {added} sample(s); ' if added else '') +
                    f'this histogram now keeps a fixed list of {len(members)} sample(s). '
                    'Right-click it > Follow sample selection to return to the automatic behaviour. Undo reverses this.')
        return True

    def finish_sample_drag(self):
        """After a drop, put the list selection back to what it was before the drag."""
        origin, self._drag_origin = self._drag_origin, None
        done, self._sample_drop_done = self._sample_drop_done, False
        if not done or origin is None or origin['sources'] != tuple(s.id for s in self.project.sources):
            return
        moved = self.samples.currentRow() != origin['current']
        self.samples.blockSignals(True)
        self.samples.clearSelection()
        self.samples.setCurrentRow(origin['current'], QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)
        for row in origin['selected']:
            self.samples.item(row).setSelected(True)
        self.samples.blockSignals(False)
        if moved:
            self.sample_changed(origin['current'])
        self.selection_changed()

    def follow_selection(self, plot_id, follow):
        """Switch a histogram between the automatic and the fixed sample list."""
        if not follow:
            self.drop_samples(plot_id, (), base=self.shown_sample_ids(self.plot_spec(plot_id)))
            return
        ids = self.selected_sample_ids()
        self.edit_plot(plot_id, pinned=False, sample_ids=ids if len(ids) > 1 else ())
        self.notify('This histogram follows the selection in the sample list again.')
