"""Movable/resizable plot documents and coordinate-aware gate interaction."""
from pathlib import Path
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg
from .model import Ellipse, Polygon, RangeGate, Rectangle, new_id
from .histograms import NORMALIZATION_LABELS, histogram_values
from . import scales as scale_rules


# Screen colours, and the white-background set used only while writing images.
DARK = {'background': '#14202c', 'foreground': '#dde5ee', 'gate': '#ffc857', 'own_gate': '#ed8bcc',
        'region': (255, 200, 87, 35), 'points': (80, 185, 220, 160), 'contour': (235, 240, 245, 160),
        'highlight': (255, 90, 60, 230)}
LIGHT = {'background': '#ffffff', 'foreground': '#000000', 'gate': '#b3541e', 'own_gate': '#a0288c',
         'region': (179, 84, 30, 30), 'points': (25, 95, 160, 170), 'contour': (40, 40, 40, 170),
         'highlight': (215, 40, 20, 235)}
ELLIPSE_POINTS = 96
# More samples than this in one plot: the note below the plot gives totals, the tooltip every sample.
SUMMARY_LINES = 8
LEGEND_ROWS = 25


def series_color(index, count, alpha=255):
    """One colour per overlaid sample; up to eight samples keep the colours of earlier versions."""
    return pg.intColor(index, hues=max(8, count), alpha=alpha)


def ellipse_outline(center, axes, count=ELLIPSE_POINTS):
    angles = np.linspace(0, 2 * np.pi, count + 1)
    (ux, uy), (vx, vy) = axes
    return np.column_stack([center[0] + ux * np.cos(angles) + vx * np.sin(angles),
                            center[1] + uy * np.cos(angles) + vy * np.sin(angles)])


class ChannelAxis(pg.AxisItem):
    clicked = QtCore.Signal()

    def __init__(self, orientation):
        super().__init__(orientation)
        self.label.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        self.setAcceptHoverEvents(True)

    def mouseClickEvent(self, event):
        if (event.button() == QtCore.Qt.MouseButton.LeftButton
                and self.label.sceneBoundingRect().adjusted(-5, -5, 5, 5).contains(event.scenePos())):
            self.clicked.emit()
            event.accept()
        else:
            event.ignore()


class GateViewBox(pg.ViewBox):
    def __init__(self, document):
        super().__init__(enableMenu=False)
        self.document = document

    def mouseDragEvent(self, event, axis=None):
        document = self.document
        tool = document.owner.tool.currentData()
        if axis is None and event.button() == QtCore.Qt.MouseButton.LeftButton and tool in ('rectangle', 'range', 'ellipse'):
            if not document.ready():
                event.accept()
                return
            start = self.mapSceneToView(event.buttonDownScenePos())
            end = self.mapSceneToView(event.scenePos())
            document.points = [(start.x(), start.y()), (end.x(), end.y())]
            document.update_draft()
            if event.isFinish():
                document.finish()
            event.accept()
        else:
            super().mouseDragEvent(event, axis)


class AxisDialog(QtWidgets.QDialog):
    def __init__(self, document, axis):
        super().__init__(document)
        self.setWindowTitle(f'{axis.upper()} axis — channel, limits and scale')
        form = QtWidgets.QFormLayout(self)
        spec = document.spec
        self.channel = QtWidgets.QComboBox()
        count_axis = axis == 'y' and spec.kind == 'histogram'
        if count_axis:
            self.channel.addItem(NORMALIZATION_LABELS[spec.normalization], '')
            self.channel.setEnabled(False)
        else:
            for name in document.owner.channels:
                marker = document.owner.channel_labels.get(name, '')
                self.channel.addItem(name + (f' — {marker}' if marker else ''), name)
            self.channel.setCurrentIndex(self.channel.findData(getattr(spec, axis + '_channel')))
        self.scale = QtWidgets.QComboBox()
        self.scale.addItems(['linear', 'log'] if count_axis else ['linear', 'log', 'logicle'])
        current = getattr(spec, axis + '_scale')
        kind, parameters = scale_rules.parse(current)
        self.scale.setCurrentText(kind)
        self.parameters = parameters or scale_rules.LOGICLE_DEFAULTS
        self.top = QtWidgets.QLineEdit(format(self.parameters[0], 'g'))
        self.width = QtWidgets.QLineEdit(format(self.parameters[1], 'g'))
        limits = getattr(spec, axis + '_limits')
        self.auto = QtWidgets.QCheckBox('Automatic limits')
        self.auto.setChecked(limits is None)
        self.minimum = QtWidgets.QLineEdit('' if limits is None else str(limits[0]))
        self.maximum = QtWidgets.QLineEdit('' if limits is None else str(limits[1]))
        form.addRow('Channel / metric', self.channel)
        form.addRow('Scale', self.scale)
        if not count_axis:
            self.top_label = QtWidgets.QLabel('Logicle top of scale (T)')
            self.width_label = QtWidgets.QLabel('Logicle linear width (W, decades)')
            form.addRow(self.top_label, self.top)
            form.addRow(self.width_label, self.width)
            def show_logicle(text):
                for widget in (self.top, self.width, self.top_label, self.width_label):
                    widget.setVisible(text == 'logicle')
            self.scale.currentTextChanged.connect(show_logicle)
            self.scale.currentTextChanged.connect(lambda text: QtCore.QTimer.singleShot(0, self.adjustSize))
            show_logicle(kind)
        form.addRow(self.auto)
        form.addRow('Minimum (original units)', self.minimum)
        form.addRow('Maximum (original units)', self.maximum)
        help_text = QtWidgets.QLabel('Log means base 10. Nonpositive values are omitted from log plots and reported; a logicle axis shows them. Existing gates keep the scale they were drawn in, and statistics always use untransformed values.')
        help_text.setWordWrap(True)
        form.addRow(help_text)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def chosen_scale(self):
        if self.scale.currentText() != 'logicle':
            return self.scale.currentText()
        return scale_rules.logicle_scale(float(self.top.text()), float(self.width.text()),
                                         self.parameters[2], self.parameters[3])


def dismantle_item(item):
    """Break pyqtgraph's ROI reference cycles before the item is destroyed.

    An ROI, each of its handles and each PolyLineROI segment reference one
    another, and every Handle eagerly owns a context QMenu holding a child
    QAction. Left intact, that garbage is only reclaimable by Python's cyclic
    collector, which clears the members in arbitrary order. The reported crash
    and diagnostic controls are consistent with a Qt/Python destruction-order
    conflict (MAC-003: SIGSEGV under ``gc_collect_main``); the exact C++ destructor
    sequence has not been proved. Dropping the references here lets ordinary
    reference counting release the wrappers and leaves the C++ objects to the
    item's own ``deleteLater``.

    Items without these attributes, such as LinearRegionItem or PlotDataItem,
    are left untouched: they hold no such cycle.
    """
    def drop(owner, name, value):
        if hasattr(owner, name):
            setattr(owner, name, value)

    for owner in [item, *(getattr(item, 'segments', None) or [])]:
        for info in getattr(owner, 'handles', None) or []:
            handle = info.get('item') if isinstance(info, dict) else None
            if handle is None:
                continue
            drop(handle, 'rois', [])
            drop(handle, 'menu', None)
        drop(owner, 'handles', [])
        drop(owner, 'mouseDragHandler', None)
        drop(owner, 'menu', None)
    drop(item, 'segments', [])


class PlotDocument(QtWidgets.QMdiSubWindow):
    def __init__(self, owner, spec):
        super().__init__()
        self.owner, self.plot_id = owner, spec.id
        self.points = []
        self._rendering = False
        self._key = None
        self.gate_items = {}
        self.density_scale = None
        self.density_contours = []
        self.theme = getattr(owner, 'screen_theme', DARK)
        self._exporting = False
        self._source = None
        self.axes = {'bottom': ChannelAxis('bottom'), 'left': ChannelAxis('left')}
        self.graph = pg.PlotWidget(viewBox=GateViewBox(self), axisItems=self.axes)
        self.graph.sigRangeChanged.connect(self.update_open_rectangles)
        self.graph.sigRangeChanged.connect(self.update_ticks)
        self.graph.setMenuEnabled(False)
        self.graph.showGrid(x=True, y=True, alpha=.12)
        self.graph.addLegend(offset=(12, 12))
        self.axes['bottom'].clicked.connect(lambda: self.axis_dialog('x'))
        self.axes['left'].clicked.connect(lambda: self.axis_dialog('y'))
        self.graph.scene().sigMouseClicked.connect(self.clicked)
        self.summary = QtWidgets.QLabel('Select a sample to calculate.')
        self.summary.setWordWrap(True)
        self.summary.setMaximumHeight(70)
        body = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(body)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.addWidget(self.graph, 1)
        layout.addWidget(self.summary)
        self.setWidget(body)
        self.setMinimumSize(240, 180)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAcceptDrops(True)
        self.graph.viewport().installEventFilter(self)
        self.draft = None
        self.setGeometry(*spec.geometry)
        if self.theme is not DARK:
            self.apply_theme(self.theme)
        self.update_title()

    @property
    def spec(self):
        return self.owner.plot_spec(self.plot_id)

    # ---- samples dragged from the sample list -------------------------------
    def handle_sample_drag(self, event):
        """Accept sample rows of this window on a histogram; True when the event was a sample drag."""
        from .sample_drag import MIME
        if not event.mimeData().hasFormat(MIME):
            return False
        ids = self.owner.sample_drop_ids(self.plot_id, event.mimeData())
        if ids is None:
            event.ignore()
            return True
        if event.type() == QtCore.QEvent.Type.Drop and not self.owner.drop_samples(self.plot_id, ids):
            # Refused (for example while the histogram is still being calculated): the message is shown, nothing changed.
            event.ignore()
            return True
        event.setDropAction(QtCore.Qt.DropAction.CopyAction)
        event.accept()
        return True

    def eventFilter(self, watched, event):
        if (event.type() in (QtCore.QEvent.Type.DragEnter, QtCore.QEvent.Type.DragMove, QtCore.QEvent.Type.Drop)
                and watched is self.graph.viewport() and self.handle_sample_drag(event)):
            return True
        return super().eventFilter(watched, event)

    def dragEnterEvent(self, event):
        if not self.handle_sample_drag(event):
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if not self.handle_sample_drag(event):
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if not self.handle_sample_drag(event):
            super().dropEvent(event)

    def shown_source(self):
        """The selected sample, or the one an image export is rendering."""
        return self._source or self.owner.selected_source()

    def update_title(self):
        spec = self.spec
        population = next((g.name for g in self.owner.project.default_gates() if g.id == spec.population_id), 'All events')
        self.setWindowTitle(f'{spec.kind.title()} · {spec.x_channel}' + (f' / {spec.y_channel}' if spec.kind != 'histogram' else '') + f' · {population}'
                            + (f' · fixed list, {len(spec.sample_ids)} samples' if spec.pinned else ''))
        # The marker invites a click on screen; exported figures carry plain labels.
        marker = '' if self._exporting else '  ▾'
        self.axes['bottom'].setLabel(spec.x_channel + marker)
        self.axes['left'].setLabel((NORMALIZATION_LABELS[spec.normalization] if spec.kind == 'histogram' else spec.y_channel) + marker)
        # Tick modes are applied after the matching data/ranges in render().

    def ready(self):
        source = self.owner.selected_source()
        ready = (source is not None and self._key == (source.id, self.owner.project.revision)
                 and source.id not in getattr(self.owner, '_pending_sources', set())
                 and self.owner.results.get(source.id, {}).get('_verified', True))
        if not ready:
            self.owner.statusBar().showMessage('Wait for this sample and plot to finish calculating before gating.')
        return ready

    def discard_plot_items(self):
        """Dispose of replaced plot items deterministically instead of via the GC.

        Every re-render replaces the gate items. The outgoing ROIs then live
        only inside their own ROI<->Handle reference cycles, so Python's cyclic
        collector would free them in arbitrary order and Shiboken could call a
        Handle's C++ destructor after its parent ROI was already destroyed
        (SIGSEGV under ``gc_collect_main``). Disconnecting first also stops a
        drag that finishes on a replaced item from writing outdated geometry;
        ``deleteLater`` hands disposal to Qt's ordered destruction.
        """
        old = [item for value in self.gate_items.values()
               for item in (value if isinstance(value, tuple) else (value,))]
        if self.draft is not None:
            old.append(self.draft)
        old.extend(self.density_contours)
        for item in old:
            for name in ('sigRegionChangeFinished', 'sigPositionChangeFinished', 'sigPositionChanged'):
                signal = getattr(item, name, None)
                if signal is None or (name == 'sigPositionChanged' and not isinstance(item, pg.TargetItem)):
                    continue
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass
        self.hide_density_scale()
        self.graph.clear()
        for item in old:
            dismantle_item(item)
            if item.scene() is None:
                item.deleteLater()
        self.gate_items, self.draft, self.density_contours = {}, None, []

    def current_items(self, key, *items):
        """True only while these are the items this document currently shows.

        A gate signal can arrive after a re-render replaced its item; the
        replaced item's geometry is outdated and must never reach the project.
        """
        stored = self.gate_items.get(key)
        stored = stored if isinstance(stored, tuple) else (stored,)
        return len(stored) == len(items) and all(a is b for a, b in zip(stored, items))

    def clear_pending(self):
        self._key = None
        self.discard_plot_items()
        self.points = []
        self.summary.setText('Calculating current sample…')
        self.update_title()

    def render(self):
        self._rendering = True
        try:
            # A linked axis can update during clearing/layout. Never let log
            # ticks exponentiate the previous plot's raw acquisition ranges.
            for axis in self.axes.values():
                axis.setLogMode(False)
            self.discard_plot_items()
            # Auto-range preserves the prior span for constant/empty data.
            # Start in neutral coordinates so a change of units cannot carry
            # a large linear span into a logarithmic histogram.
            self.graph.setRange(xRange=(0, 1), yRange=(0, 1), padding=0)
            self.update_title()
            spec = self.spec
            source = self.shown_source()
            selected = self.owner.results.get(source.id) if source else None
            data = selected.get('sheet_plots', {}).get(spec.id) if selected else None
            self._key = None
            self.summary.setToolTip('')
            legend = self.graph.getPlotItem().legend
            overlaid = bool(spec.sample_ids) and (spec.kind == 'histogram' or (spec.kind == 'scatter' and spec.overlay))
            current = bool(data) and 'error' not in data
            problems, waiting = [], 0
            if overlaid:
                # Every sample of the list is drawn or named: one failed or
                # unfinished sample never hides the other curves.
                datasets = []
                failures = getattr(self.owner, 'failures', {})
                for number, sid in enumerate(spec.sample_ids, 1):
                    sample = next(s for s in self.owner.project.sources if s.id == sid)
                    item = self.owner.results.get(sid, {}).get('sheet_plots', {}).get(spec.id)
                    if item and 'error' not in item:
                        datasets.append((number, sample, item))
                    elif item or sid in failures:
                        problems.append(f"{number}: {Path(sample.path).name}: not shown — {item['error'] if item else failures[sid]}")
                    else:
                        waiting += 1
                if not datasets:
                    self.summary.setText('\n'.join(problems) or 'Waiting for sample results…')
                    return
                if not current:
                    data = datasets[0][2]
            elif not current:
                self.summary.setText(data.get('error', 'Waiting for sample results…') if data else 'Waiting for sample results…')
                return
            else:
                datasets = [(1, source, data)]
            total = len(spec.sample_ids) if overlaid else 1
            if legend is not None:
                legend.setVisible(spec.legend)
                legend.setColumnCount(max(1, -(-len(datasets) // LEGEND_ROWS)))
            notes = []
            # Offset layout: each overlaid histogram on its own baseline (display only).
            staggered = spec.kind == 'histogram' and spec.stagger and len(datasets) > 1 and spec.y_scale == 'linear'
            step = 0.0
            if staggered:
                step = max((float(histogram_values(item, spec.normalization)[0].max(initial=0)) for _, _, item in datasets),
                           default=0.0) or 1.0
            self.axes['left'].setStyle(showValues=not staggered)
            for i, (number, sample, item) in enumerate(datasets):
                label = f'{number}: {Path(sample.path).name}'
                if spec.kind == 'histogram':
                    counts, status = histogram_values(item, spec.normalization)
                    if spec.y_scale == 'log':
                        positive = counts > 0
                        y = np.full(len(counts), np.nan)
                        y[positive] = np.log10(counts[positive])
                    else:
                        y = counts + i * step
                    self.graph.plot(item['edges'], y, stepMode='center', connect='finite',
                                    pen=pg.mkPen(series_color(number - 1, total), width=2), name=label)
                elif spec.kind == 'density':
                    self.render_density(item, label)
                elif overlaid:
                    self.graph.addItem(pg.ScatterPlotItem(item['x'], item['y'], size=2, pen=None,
                                       brush=pg.mkBrush(series_color(number - 1, total, alpha=150)), name=label))
                else:
                    self.graph.addItem(pg.ScatterPlotItem(item['x'], item['y'], size=2,
                                       pen=None, brush=pg.mkBrush(*self.theme['points'])))
                notes.append(f"{label}: {item['event_count']:,} gated; nonfinite {item['nonfinite']}, log-excluded {item['nonpositive']}, outside {item['outside']}")
                if spec.kind == 'histogram' and status == 'empty':
                    notes[-1] += '; no events in displayed bins'
                if spec.kind == 'density':
                    notes[-1] += (f"; rendered {item['rendered_count']:,}/{item['eligible_count']:,} eligible events"
                                  f"; bins represented {item['represented_bins']:,}/{item['occupied_bins']:,}")
                    if spec.contours:
                        peak = max((max(line) for line in item['counts']), default=0)
                        notes[-1] += '; contours (events/bin): ' + ', '.join(f'{peak*f:.3g}' for f in (.25, .5, .75))
            self.graph.setXRange(*data['x_bounds'], padding=.02)
            if spec.kind != 'histogram':
                self.graph.setYRange(*data['y_bounds'], padding=.02)
            elif spec.y_limits:
                bounds = np.log10(spec.y_limits) if spec.y_scale == 'log' else spec.y_limits
                self.graph.setYRange(*bounds, padding=0)
            else:
                self.graph.enableAutoRange(axis='y', enable=True)
            if len(notes) > SUMMARY_LINES:
                # Many samples: totals below the plot, every sample in the tooltip.
                self.summary.setToolTip('\n'.join(notes))
                notes = [f"{len(datasets)} samples shown: {sum(item['event_count'] for _, _, item in datasets):,} gated events in total; "
                         f"nonfinite {sum(item['nonfinite'] for _, _, item in datasets)}, "
                         f"log-excluded {sum(item['nonpositive'] for _, _, item in datasets)}, "
                         f"outside {sum(item['outside'] for _, _, item in datasets)}. Hover here for each sample."]
            if waiting:
                notes.append(f'{waiting} sample(s) still calculating.')
            notes.extend(problems)
            if spec.pinned:
                notes.insert(0, f'Fixed list of {total} sample(s); right-click > Follow sample selection releases it.')
            # Without a result of the selected sample the curves are shown, but gating waits.
            if current:
                self._key = (source.id, self.owner.project.revision)
            highlight = selected.get('highlights', {}).get(spec.id) if current else None
            wanted = getattr(self.owner, 'highlight_id', None)
            if highlight and wanted and selected.get('highlight_id') == wanted and not overlaid:
                marked = pg.ScatterPlotItem(highlight['x'], highlight['y'], size=3, pen=None,
                                            brush=pg.mkBrush(*self.theme['highlight']))
                marked.setZValue(5)
                self.graph.addItem(marked)
                name = next((g.name for g in self.owner.project.default_gates() if g.id == wanted), 'population')
                notes.append(f"Highlighted: {name}, {highlight['event_count']:,} events of this sample")
            if staggered:
                notes.append('Offset layout: each curve has its own baseline; heights are not comparable to the axis.')
            self.summary.setText(('Density: full-population histogram2d; colour = log1p(events/bin). ' if spec.kind == 'density' else '') + '\n'.join(notes))
            if source is not None:
                self.draw_gates()
            if current and getattr(self.owner, 'gate_labels', False):
                self.draw_gate_labels(source, selected)
            # Histogram Y auto-range is deferred by pyqtgraph; settle it before
            # enabling log ticks, including constant and empty histograms.
            self.graph.getViewBox().updateAutoRange()
            for name, scale in (('bottom', spec.x_scale), ('left', spec.y_scale)):
                axis = self.axes[name]
                axis.setLogMode(scale == 'log')
                axis.enableAutoSIPrefix(scale_rules.kind(scale) != 'logicle')
                axis.updateAutoSIPrefix()
            self.update_ticks()
        finally:
            self._rendering = False

    def update_ticks(self, *args):
        """Logicle axes carry decade ticks in original units; other axes are automatic."""
        try:
            spec = self.spec
        except StopIteration:
            return
        view = self.graph.viewRange()
        for name, scale, (low, high) in (('bottom', spec.x_scale, view[0]), ('left', spec.y_scale, view[1])):
            axis = self.axes[name]
            if scale_rules.kind(scale) == 'logicle' and np.isfinite([low, high]).all() and high > low:
                axis.setTicks(list(scale_rules.logicle_ticks(scale, low, high)))
            else:
                axis.setTicks(None)

    def hide_density_scale(self):
        if self.density_scale is not None:
            # The plot layout owns this widget for the document's lifetime.
            # Reuse it rather than deleting widgets during pending Qt layouts.
            self.density_scale.hide()

    def render_density(self, data, label):
        counts = np.asarray(data['counts'], dtype=float)
        peak = float(counts.max()) if counts.size else 0
        maximum = float(np.log1p(peak)) if peak else 1.0
        colors = pg.colormap.get('viridis')
        density = np.asarray(data['density'], dtype=float)
        brushes = colors.map(np.log1p(density) / maximum, mode='qcolor')
        scatter = pg.ScatterPlotItem(data['x'], data['y'], size=3, pen=None,
                                     brush=brushes, name=label)
        self.graph.addItem(scatter)
        if self.density_scale is None:
            self.density_scale = pg.ColorBarItem(values=(0, maximum), width=12,
                                                colorMap=colors, label='log1p(events/bin)',
                                                interactive=False, colorMapMenu=False)
            self.graph.getPlotItem().layout.addItem(self.density_scale, 2, 5)
            self.density_scale.axis.setPen(self.theme['foreground'])
            self.density_scale.axis.setTextPen(self.theme['foreground'])
        else:
            self.density_scale.setLevels((0, maximum))
            self.density_scale.show()
        if self.spec.contours and peak:
            x_edges, y_edges = np.asarray(data['x_edges']), np.asarray(data['y_edges'])
            transform = QtGui.QTransform()
            transform.translate(float(x_edges[0]), float(y_edges[0]))
            transform.scale(float(x_edges[1] - x_edges[0]), float(y_edges[1] - y_edges[0]))
            for fraction in (.25, .5, .75):
                contour = pg.IsocurveItem(counts, level=peak * fraction,
                                         pen=pg.mkPen(self.theme['contour'], width=1),
                                         axisOrder='col-major')
                contour.setTransform(transform)
                contour.setZValue(-1)
                self.graph.addItem(contour)
                self.density_contours.append(contour)

    def convert_points(self, points, scales):
        points = np.asarray(points, dtype=float).copy()
        for i, (original, display) in enumerate(zip(scales, (self.spec.x_scale, self.spec.y_scale))):
            if original != display:
                points[:, i] = scale_rules.convert(points[:, i], original, display)
        points[~np.isfinite(points)] = np.nan
        return points

    def draw_gate_labels(self, source, result):
        """Name and % of parent beside each gate drawn on this plot (display only)."""
        spec = self.spec
        percent = {row['gate_id']: row.get('percent_parent') for row in result.get('rows', [])}
        def label(gate, x, y, anchor):
            value = percent.get(gate.id)
            text = gate.name + (f'  {value:.3g}%' if value is not None else '')
            backdrop = QtGui.QColor(self.theme['background'])
            backdrop.setAlpha(170)
            # Keep the label inside the axes when the gate reaches beyond them.
            (x0, x1), (y0, y1) = self.graph.viewRange()
            ax, ay = anchor
            if y >= y1:
                y, ay = y1, 0
            elif y <= y0:
                y, ay = y0, 1
            x = min(max(float(x), x0), x1)
            item = pg.TextItem(text, color=self.theme['foreground'], anchor=(ax, ay), fill=backdrop)
            item.setPos(float(x), float(y))
            item.setZValue(20)
            self.graph.addItem(item, ignoreBounds=True)
        for gate in self.owner.project.gates_for(source.id):
            if gate.parent_id != spec.population_id:
                continue
            if isinstance(gate, RangeGate):
                if spec.kind != 'histogram' or gate.channel != spec.x_channel:
                    continue
                low = self.convert_points([(gate.minimum,)], gate.scales)[0, 0]
                if np.isfinite(low):
                    low = max(low, self.graph.viewRange()[0][0])
                    label(gate, low, self.graph.viewRange()[1][1], (0, 0))
                continue
            if spec.kind == 'histogram' or gate.channels != (spec.x_channel, spec.y_channel):
                continue
            if isinstance(gate, (Polygon, Ellipse)):
                outline = gate.vertices if isinstance(gate, Polygon) else ellipse_outline(gate.center, gate.axes)
                points = self.convert_points(outline, gate.scales)
                points = points[np.isfinite(points).all(axis=1)]
                if len(points):
                    top = points[np.argmax(points[:, 1])]
                    label(gate, top[0], top[1], (.5, 1))
                continue
            corner = [lo if lo is not None else hi for lo, hi in zip(gate.minimum, gate.maximum)]
            point = self.convert_points([corner], gate.scales)[0]
            if not np.isfinite(point).all():
                continue
            if gate.group_id or any(v is None for v in gate.minimum + gate.maximum):
                # Open sides: sit in the corner that points into the region.
                anchor = (0 if gate.minimum[0] is not None else 1, 1 if gate.minimum[1] is not None else 0)
                label(gate, point[0], point[1], anchor)
            else:
                top = self.convert_points([(gate.minimum[0], gate.maximum[1])], gate.scales)[0]
                if np.isfinite(top).all():
                    label(gate, top[0], top[1], (0, 1))

    def draw_gates(self):
        spec = self.spec
        source = self.shown_source()
        groups = set()
        for gate in self.owner.project.gates_for(source.id):
            if gate.parent_id != spec.population_id:
                continue
            overridden = any(g.id == gate.id for g in self.owner.project.overrides.get(source.id, []))
            can_edit = not overridden or self.owner.scope.currentIndex() == 1
            pen = pg.mkPen(self.theme['own_gate' if overridden else 'gate'], width=2)
            if isinstance(gate, RangeGate):
                if spec.kind != 'histogram' or gate.channel != spec.x_channel:
                    continue
                bounds = self.convert_points([(gate.minimum,), (gate.maximum,)], gate.scales)[:, 0]
                if not np.isfinite(bounds).all():
                    continue
                item = pg.LinearRegionItem(bounds, brush=pg.mkBrush(*self.theme['region']), pen=pen)
                item.setMovable(can_edit and gate.scales[0] == spec.x_scale)
                self.graph.addItem(item)
                item.sigRegionChangeFinished.connect(lambda item, gate=gate: self.range_edited(gate, item))
            elif spec.kind != 'histogram' and gate.channels == (spec.x_channel, spec.y_channel):
                same_scale = gate.scales == (spec.x_scale, spec.y_scale)
                if isinstance(gate, Rectangle) and gate.group_id:
                    if gate.group_id in groups:
                        continue
                    groups.add(gate.group_id)
                    group = [g for g in self.owner.project.gates_for(source.id) if isinstance(g, Rectangle) and g.group_id == gate.group_id]
                    group_overrides = {g.id for g in self.owner.project.overrides.get(source.id, [])}
                    can_edit = self.owner.scope.currentIndex() == 1 or not any(g.id in group_overrides for g in group)
                    x = next(v for g in group for v in (g.minimum[0], g.maximum[0]) if v is not None)
                    y = next(v for g in group for v in (g.minimum[1], g.maximum[1]) if v is not None)
                    xy = self.convert_points([(x, y)], gate.scales)[0]
                    vertical = pg.InfiniteLine(xy[0], angle=90, movable=same_scale and can_edit, pen=pen)
                    horizontal = pg.InfiniteLine(xy[1], angle=0, movable=same_scale and can_edit, pen=pen)
                    self.graph.addItem(vertical); self.graph.addItem(horizontal)
                    for line in (vertical, horizontal):
                        line.sigPositionChangeFinished.connect(lambda *a, group_id=gate.group_id, items=(vertical, horizontal): self.quadrants_edited(group_id, items))
                    self.gate_items[gate.group_id] = (vertical, horizontal)
                    continue
                if isinstance(gate, Ellipse):
                    outline = self.convert_points(ellipse_outline(gate.center, gate.axes), gate.scales)
                    curve = pg.PlotCurveItem(outline[:, 0], outline[:, 1], pen=pen, connect='finite')
                    self.graph.addItem(curve)
                    if same_scale and can_edit:
                        self.gate_items[gate.id] = (curve,) + self.ellipse_handles(gate, curve, pen)
                    else:
                        self.gate_items[gate.id] = (curve,)
                    continue
                if isinstance(gate, Polygon):
                    if same_scale:
                        item = pg.PolyLineROI(gate.vertices, closed=True, pen=pen)
                        self.graph.addItem(item)
                        item.setEnabled(can_edit)
                        item.sigRegionChangeFinished.connect(lambda item, gate=gate: self.polygon_edited(gate, item))
                    else:
                        points = []
                        vertices = gate.vertices + gate.vertices[:1]
                        for a, b in zip(vertices, vertices[1:]):
                            points.extend(np.linspace(a, b, 64))
                        transformed = self.convert_points(points, gate.scales)
                        item = self.graph.plot(transformed[:, 0], transformed[:, 1], pen=pen, connect='finite')
                else:
                    if any(v is None for v in gate.minimum + gate.maximum):
                        item = pg.PlotDataItem(pen=pen, connect='finite')
                        self.graph.addItem(item, ignoreBounds=True)
                        self.gate_items[gate.id] = item
                        continue
                    points = self.convert_points([gate.minimum, gate.maximum], gate.scales)
                    if not np.isfinite(points).all():
                        continue
                    item = pg.RectROI(points[0], points[1] - points[0], pen=pen, movable=same_scale and can_edit, resizable=same_scale and can_edit)
                    self.graph.addItem(item)
                    if same_scale:
                        item.sigRegionChangeFinished.connect(lambda item, gate=gate: self.rectangle_edited(gate, item))
            else:
                continue
            self.gate_items[gate.id] = item
        self.update_open_rectangles()

    def update_open_rectangles(self, *args):
        source = self.shown_source()
        if source is None:
            return
        for gate in self.owner.project.gates_for(source.id):
            if not isinstance(gate, Rectangle) or gate.group_id or not any(v is None for v in gate.minimum + gate.maximum):
                continue
            item = self.gate_items.get(gate.id)
            if item is None:
                continue
            bounds = self.convert_points([gate.minimum, gate.maximum], gate.scales)
            if any(value is not None and not np.isfinite(bounds[row, axis])
                   for row, values in enumerate((gate.minimum, gate.maximum)) for axis, value in enumerate(values)):
                item.setData([], [])
                continue
            view = self.graph.viewRange()
            low = [view[axis][0] if value is None else bounds[0, axis] for axis, value in enumerate(gate.minimum)]
            high = [view[axis][1] if value is None else bounds[1, axis] for axis, value in enumerate(gate.maximum)]
            points = []
            for axis in (0, 1):
                other = 1-axis
                for values, side in ((gate.minimum, low), (gate.maximum, high)):
                    if values[axis] is None:
                        continue
                    a, b = [0., 0.], [0., 0.]
                    a[axis] = b[axis] = side[axis]
                    a[other], b[other] = low[other], high[other]
                    points.extend((a, b, [np.nan, np.nan]))
            if points:
                points = np.asarray(points)
                item.setData(points[:, 0], points[:, 1], connect='finite')
            else:
                item.setData([], [])

    def ellipse_handles(self, gate, curve, pen):
        """Centre handle moves the ellipse; the two rim handles shape and tilt it."""
        center = np.asarray(gate.center, dtype=float)
        ends = [center + np.asarray(axis, dtype=float) for axis in gate.axes]
        handles = []
        for position, symbol in ((center, '+'), (ends[0], 'o'), (ends[1], 'o')):
            handle = pg.TargetItem(pos=tuple(position), size=11, symbol=symbol, pen=pen,
                                   hoverPen=pg.mkPen(self.theme['foreground'], width=2), movable=True)
            handle.setZValue(15)
            self.graph.addItem(handle, ignoreBounds=True)
            handles.append(handle)
        middle, first, second = handles
        state = {'center': center.copy(), 'busy': False}

        def geometry():
            c = np.array([middle.pos().x(), middle.pos().y()])
            return c, tuple((h.pos().x() - c[0], h.pos().y() - c[1]) for h in (first, second))

        def redraw():
            c, axes = geometry()
            outline = ellipse_outline(c, axes)
            curve.setData(outline[:, 0], outline[:, 1])

        def centre_moved():
            if state['busy']:
                return
            state['busy'] = True
            try:
                c = np.array([middle.pos().x(), middle.pos().y()])
                shift = c - state['center']
                for h in (first, second):
                    h.setPos(h.pos().x() + shift[0], h.pos().y() + shift[1])
                state['center'] = c
                redraw()
            finally:
                state['busy'] = False

        def rim_moved():
            if not state['busy']:
                redraw()

        def finished(*args):
            self.ellipse_edited(gate, curve, *geometry())

        middle.sigPositionChanged.connect(centre_moved)
        for h in (first, second):
            h.sigPositionChanged.connect(rim_moved)
        for h in handles:
            h.sigPositionChangeFinished.connect(finished)
        return tuple(handles)

    def ellipse_edited(self, gate, curve, center, axes):
        stored = self.gate_items.get(gate.id)
        if self._rendering or not isinstance(stored, tuple) or stored[0] is not curve or not self.ready():
            return
        self.owner.edit_gate_values(gate, center=(float(center[0]), float(center[1])),
                                    axes=tuple((float(a), float(b)) for a, b in axes))

    def polygon_edited(self, gate, roi):
        if self._rendering or not self.current_items(gate.id, roi) or not self.ready():
            return
        vertices = tuple((float(p.x()), float(p.y())) for _, p in roi.getLocalHandlePositions() for p in [roi.mapToParent(p)])
        self.owner.edit_gate_values(gate, vertices=vertices)

    def rectangle_edited(self, gate, roi):
        if self._rendering or not self.current_items(gate.id, roi) or not self.ready():
            return
        a, b = roi.mapToParent(QtCore.QPointF(0, 0)), roi.mapToParent(roi.size())
        self.owner.edit_gate_values(gate, minimum=(min(a.x(), b.x()), min(a.y(), b.y())), maximum=(max(a.x(), b.x()), max(a.y(), b.y())))

    def range_edited(self, gate, item):
        if not self._rendering and self.current_items(gate.id, item) and self.ready():
            lo, hi = item.getRegion()
            self.owner.edit_gate_values(gate, minimum=float(lo), maximum=float(hi))

    def quadrants_edited(self, group_id, items):
        vertical, horizontal = items
        if not self._rendering and self.current_items(group_id, vertical, horizontal) and self.ready():
            self.owner.move_quadrants(group_id, vertical.value(), horizontal.value())

    def clicked(self, event):
        if not self.graph.getPlotItem().vb.sceneBoundingRect().contains(event.scenePos()):
            return
        tool = self.owner.tool.currentData()
        if event.button() == QtCore.Qt.MouseButton.RightButton:
            if tool == 'polygon' and self.points:
                self.points.pop(); self.update_draft()
            else:
                self.context_menu(event.screenPos().toPoint())
            event.accept()
            return
        if event.button() != QtCore.Qt.MouseButton.LeftButton or tool == 'navigate' or not self.ready():
            return
        self.owner.sheet.setActiveSubWindow(self)
        point = self.graph.getPlotItem().vb.mapSceneToView(event.scenePos())
        self.points.append((point.x(), point.y()))
        self.update_draft()
        if tool == 'quadrants' or (tool in ('rectangle', 'range', 'ellipse') and len(self.points) == 2):
            self.finish()
        event.accept()

    def update_draft(self):
        if self.draft is None:
            self.draft = pg.PlotDataItem(pen=pg.mkPen(self.theme['gate'], width=2), symbol='o', symbolSize=5)
            self.graph.addItem(self.draft)
        points = self.points
        tool = self.owner.tool.currentData()
        if len(points) == 2 and tool == 'rectangle':
            (x, y), (u, v) = points
            points = [(x, y), (u, y), (u, v), (x, v), (x, y)]
        elif len(points) == 2 and tool == 'ellipse':
            (x, y), (u, v) = points
            points = ellipse_outline(((x + u) / 2, (y + v) / 2), (((u - x) / 2, 0), (0, (v - y) / 2)))
            self.draft.setData(points[:, 0], points[:, 1], symbol=None)
            return
        self.draft.setData([p[0] for p in points], [p[1] for p in points], symbol='o')

    def finish(self):
        if not self.ready():
            return
        tool, spec = self.owner.tool.currentData(), self.spec
        try:
            name = self.owner.gate_name.text().strip() or f'Gate {len(self.owner.project.default_gates())+1}'
            if tool == 'range':
                if spec.kind != 'histogram' or len(self.points) != 2:
                    raise ValueError('Draw a range across a histogram with two clicks or a drag.')
                lo, hi = sorted(p[0] for p in self.points)
                gates = [RangeGate(new_id(), name, spec.population_id, spec.x_channel, lo, hi, (spec.x_scale,))]
            else:
                if spec.kind == 'histogram':
                    raise ValueError('Use the range tool on histograms.')
                axes, scales = (spec.x_channel, spec.y_channel), (spec.x_scale, spec.y_scale)
                if tool == 'polygon':
                    gates = [Polygon(new_id(), name, spec.population_id, axes, tuple(self.points), scales)]
                elif tool == 'rectangle':
                    if len(self.points) != 2:
                        raise ValueError('Click two opposite corners or drag a rectangle.')
                    a, b = self.points
                    gates = [Rectangle(new_id(), name, spec.population_id, axes,
                                       (min(a[0], b[0]), min(a[1], b[1])), (max(a[0], b[0]), max(a[1], b[1])), scales)]
                elif tool == 'ellipse':
                    if len(self.points) != 2:
                        raise ValueError('Drag a box around the population, or click two opposite corners.')
                    a, b = self.points
                    gates = [Ellipse(new_id(), name, spec.population_id, axes, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2),
                                     ((abs(b[0] - a[0]) / 2, 0.0), (0.0, abs(b[1] - a[1]) / 2)), scales)]
                elif tool == 'quadrants':
                    x, y = self.points[-1]
                    group = new_id()
                    gates = [Rectangle(new_id(), name + suffix, spec.population_id, axes, lo, hi, scales, group)
                             for suffix, lo, hi in [(' Q1', (x, y), (None, None)), (' Q2', (None, y), (x, None)),
                                                    (' Q3', (None, None), (x, y)), (' Q4', (x, None), (None, y))]]
                else:
                    return
            self.owner.add_gates(gates)
            self.points = []
            self.owner.tool.setCurrentIndex(0)
        except (ValueError, TypeError) as exc:
            self.owner.error(str(exc))

    def axis_dialog(self, axis):
        dialog = AxisDialog(self, axis)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            try:
                limits = None if dialog.auto.isChecked() else (float(dialog.minimum.text()), float(dialog.maximum.text()))
                changes = {axis + '_scale': dialog.chosen_scale(), axis + '_limits': limits}
                if not (axis == 'y' and self.spec.kind == 'histogram'):
                    changes[axis + '_channel'] = dialog.channel.currentData()
                self.owner.edit_plot(self.plot_id, **changes)
            except ValueError as exc:
                self.owner.error(str(exc))

    def context_menu(self, position):
        menu = QtWidgets.QMenu(self)
        self._context_menu = menu
        self._menu_children = []
        def add_submenu(title):
            child = QtWidgets.QMenu(title, menu)
            self._menu_children.append(child)
            menu.addMenu(child)
            return child
        menu.addAction('X axis: channel / scale / limits…', lambda: self.axis_dialog('x'))
        menu.addAction('Y axis: channel / scale / limits…', lambda: self.axis_dialog('y'))
        for axis in ('x', 'y'):
            submenu = add_submenu(axis.upper() + ' scale')
            choices = ['linear', 'log']
            if not (axis == 'y' and self.spec.kind == 'histogram'):
                current = getattr(self.spec, axis + '_scale')
                choices.append(current if scale_rules.kind(current) == 'logicle' else scale_rules.logicle_scale())
            for scale in choices:
                submenu.addAction(scale_rules.kind(scale), lambda checked=False, a=axis, s=scale: self.owner.edit_plot(self.plot_id, **{a+'_scale': s, a+'_limits': None}))
        menu.addAction('Automatic limits', lambda: self.owner.edit_plot(self.plot_id, x_limits=None, y_limits=None))
        populations = add_submenu('Population')
        populations.addAction('All events', lambda: self.owner.edit_plot(self.plot_id, population_id=None))
        for gate in self.owner.project.default_gates():
            populations.addAction(gate.name, lambda checked=False, gid=gate.id: self.owner.edit_plot(self.plot_id, population_id=gid))
        kinds = add_submenu('Plot type')
        for kind in ('histogram', 'scatter', 'density'):
            kinds.addAction(kind.title(), lambda checked=False, k=kind: self.owner.change_kind(self.plot_id, k))
        if self.spec.kind == 'density':
            contours = menu.addAction('Density contours')
            contours.setCheckable(True)
            contours.setChecked(self.spec.contours)
            contours.toggled.connect(lambda checked: self.owner.edit_plot(self.plot_id, contours=checked))
        if self.spec.kind == 'scatter':
            overlay = menu.addAction('Overlay selected samples')
            overlay.setCheckable(True)
            overlay.setChecked(self.spec.overlay)
            overlay.toggled.connect(lambda checked: self.owner.set_overlay(self.plot_id, checked))
        if self.spec.kind == 'histogram':
            normalization = add_submenu('Histogram normalization')
            for mode, label in NORMALIZATION_LABELS.items():
                action = normalization.addAction(label, lambda checked=False, m=mode: self.owner.edit_plot(self.plot_id, normalization=m, y_limits=None))
                action.setCheckable(True)
                action.setChecked(mode == self.spec.normalization)
            follow = menu.addAction('Follow sample selection')
            follow.setCheckable(True)
            follow.setChecked(not self.spec.pinned)
            follow.toggled.connect(lambda checked: self.owner.follow_selection(self.plot_id, checked))
            legend = menu.addAction('Show legend')
            legend.setCheckable(True)
            legend.setChecked(self.spec.legend)
            legend.toggled.connect(lambda checked: self.owner.edit_plot(self.plot_id, legend=checked))
            offset = menu.addAction('Offset overlaid histograms')
            offset.setCheckable(True)
            offset.setChecked(self.spec.stagger)
            offset.toggled.connect(lambda checked: self.owner.edit_plot(self.plot_id, stagger=checked))
            menu.addAction('Show histogram statistics…', lambda: self.owner.show_histogram_statistics(self.plot_id))
            menu.addAction('Set threshold from control…', lambda: self.owner.threshold_from_control(self.plot_id))
        menu.addAction('Export plot data…', lambda: self.owner.export_plot_data(self.plot_id))
        menu.addAction('Save plot image…', self.save_image)
        menu.addSeparator()
        menu.addAction('Duplicate plot', lambda: self.owner.duplicate_plot(self.plot_id))
        menu.addAction('Delete plot', self.close)
        menu.exec(position)

    def save_image(self):
        path, chosen = QtWidgets.QFileDialog.getSaveFileName(
            self, 'Save plot image (white background)', 'plot.png', 'PNG (*.png);;SVG (*.svg);;PDF (*.pdf)')
        if not path:
            return
        if not Path(path).suffix:
            path += '.' + chosen[:3].lower()
        try:
            if not self.export_image(path):
                self.owner.error('This plot has no current result to save.')
        except (OSError, ValueError) as exc:
            self.owner.error(f'Image not saved: {exc}')
        finally:
            self.render()

    def apply_theme(self, theme):
        self.theme = theme
        self.graph.setBackground(theme['background'])
        for axis in self.axes.values():
            axis.setPen(theme['foreground'])
            axis.setTextPen(theme['foreground'])
        legend = self.graph.getPlotItem().legend
        if legend is not None:
            legend.setLabelTextColor(theme['foreground'])
        if self.density_scale is not None:
            self.density_scale.axis.setPen(theme['foreground'])
            self.density_scale.axis.setTextPen(theme['foreground'])
            self.density_scale.axis.setLabel('log1p(events/bin)', color=theme['foreground'])

    def export_image(self, path, source=None):
        """Write this plot for one sample on a white background.

        Returns False when that sample has no result. The caller re-renders the
        screen afterwards; nothing here changes the project.
        """
        from pyqtgraph.exporters import ImageExporter, SVGExporter
        suffix = Path(path).suffix.lower()
        if suffix not in ('.png', '.svg', '.pdf'):
            raise ValueError('Choose a .png, .svg or .pdf file name')
        self._source = source
        self._exporting = True
        self.apply_theme(LIGHT)
        try:
            self.render()
            if self._key is None:
                return False
            QtWidgets.QApplication.processEvents()
            item = self.graph.getPlotItem()
            if suffix == '.png':
                exporter = ImageExporter(item)
                exporter.parameters()['width'] = max(1600, int(item.width()) * 2)
                exporter.export(path)
            elif suffix == '.svg':
                SVGExporter(item).export(path)
            else:
                bounds = item.sceneBoundingRect()
                writer = QtGui.QPdfWriter(path)
                writer.setPageSize(QtGui.QPageSize(QtCore.QSizeF(bounds.width(), bounds.height()),
                                                   QtGui.QPageSize.Unit.Point))
                writer.setPageMargins(QtCore.QMarginsF(0, 0, 0, 0))
                painter = QtGui.QPainter(writer)
                try:
                    self.graph.scene().render(painter, QtCore.QRectF(painter.viewport()), bounds)
                finally:
                    painter.end()
            if not Path(path).is_file() or not Path(path).stat().st_size:
                raise OSError(f'Nothing was written to {path}')
            return True
        finally:
            self._source = None
            self._exporting = False
            self.apply_theme(getattr(self.owner, 'screen_theme', DARK))

    def moveEvent(self, event):
        super().moveEvent(event)
        self.remember_geometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.remember_geometry()

    def remember_geometry(self):
        if hasattr(self, 'owner') and not self.owner.rebuilding and self.plot_id in self.owner.documents:
            geometry = self.geometry()
            self.owner.layout_plot(self.plot_id, (geometry.x(), geometry.y(), geometry.width(), geometry.height()))

    def closeEvent(self, event):
        # Qt destroys this document's whole item tree once the close is accepted.
        # Dispose of the gate items first, so their ROI<->Handle cycles never
        # reach the cyclic collector holding already-destroyed C++ objects
        # (rebuild_documents and remove_plot both close documents this way).
        self.discard_plot_items()
        if not self.owner.closing and not self.owner.rebuilding:
            self.owner.remove_plot(self.plot_id)
        event.accept()
