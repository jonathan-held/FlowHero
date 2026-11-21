"""Review exported gate geometry and explicit channel mappings before applying it."""
from pathlib import Path
from PySide6 import QtWidgets
from .gate_import import prepare_gate_import


class GateImportDialog(QtWidgets.QDialog):
    def __init__(self, project, document, channels, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Import gate definitions')
        self.resize(780, 620)
        self.project, self.document, self.available = project, document, tuple(channels)
        self.candidate = None
        layout = QtWidgets.QVBoxLayout(self)
        info = QtWidgets.QLabel(f'{Path(document.source_path).name} • {document.source_format}\n'
            f'Add gates alongside {len(project.default_gates())} existing gates. Existing gates and sample adjustments are retained.\n'
            'Imported gates will be shared by all samples. Undo reverses the whole import.')
        info.setWordWrap(True); layout.addWidget(info)
        self.use_compensation = None
        self.variant = QtWidgets.QComboBox()
        prefix = 'Workspace sample: ' if document.source_format == 'flowjo-wsp' else 'Sample adjustment: '
        if None in document.variants:
            self.variant.addItem('Exported gate set', None)
        for identity in document.variants:
            if identity is not None:
                self.variant.addItem(prefix + identity, identity)
        if len(document.variants) > 1:
            layout.addWidget(self.variant)
        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(['Gate', 'Parent', 'Type', 'Channels', 'Scales', 'Geometry'])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table)
        layout.addWidget(QtWidgets.QLabel('Map exported channels to acquisition channel names:'))
        self.mapping_layout = QtWidgets.QFormLayout(); layout.addLayout(self.mapping_layout)
        self.mapping = {}
        self.open_plot = QtWidgets.QCheckBox('Open a plot for the first imported gate')
        self.open_plot.setChecked(True); layout.addWidget(self.open_plot)
        self.use_compensation = QtWidgets.QCheckBox("Also use this workspace sample's compensation matrix for the project "
                                                    '(replaces the current compensation setting)')
        layout.addWidget(self.use_compensation)
        shown = list(document.notes[:10])
        if len(document.notes) > 10:
            shown.append(f'… and {len(document.notes) - 10} more notes, all saved with the project.')
        self.notes = QtWidgets.QLabel('\n'.join(shown) + '\n'
            'The file path, SHA-256, original gates and channel/identity mappings will be saved with the project.\n'
            + ('Channel mappings are checked against the selected sample. Other samples are checked during calculation.' if channels
               else 'No sample is loaded. Load matching FCS data to calculate counts after importing.'))
        self.notes.setWordWrap(True); layout.addWidget(self.notes)
        self.error_label = QtWidgets.QLabel(); self.error_label.setWordWrap(True); layout.addWidget(self.error_label)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText('Import gates')
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.variant.currentIndexChanged.connect(self.refresh)
        self.refresh()

    def refresh(self):
        gates = self.document.variants[self.variant.currentData()]
        names = {gate.id: gate.name for gate in gates}
        self.table.setRowCount(len(gates))
        for row, gate in enumerate(gates):
            geometry = (f'{len(gate.vertices)} vertices' if hasattr(gate, 'vertices')
                        else f'{gate.operation}: ' + ', '.join(names.get(o, o) for o in gate.operands) if hasattr(gate, 'operands')
                        else f'centre {gate.center}' if hasattr(gate, 'center') else f'{gate.minimum} → {gate.maximum}')
            values = [gate.name, names.get(gate.parent_id, 'All events'), type(gate).__name__,
                      ', '.join(gate.channels), ', '.join(gate.scales), geometry]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QtWidgets.QTableWidgetItem(value))
        previous = {channel: combo.currentText() for channel, combo in self.mapping.items()}
        while self.mapping_layout.rowCount():
            self.mapping_layout.removeRow(0)
        self.mapping = {}
        for channel in sorted({channel for gate in gates for channel in gate.channels}):
            combo = QtWidgets.QComboBox(); combo.setEditable(True)
            combo.addItems(self.available or (channel,))
            combo.setCurrentText(previous.get(channel, channel))
            self.mapping[channel] = combo
            self.mapping_layout.addRow(channel, combo)
        available = bool((self.document.compensation or {}).get(self.variant.currentData()))
        self.use_compensation.setVisible(available)
        self.use_compensation.setChecked(available)
        self.error_label.clear()

    def accept(self):
        try:
            mapping = {channel: combo.currentText() for channel, combo in self.mapping.items()}
            missing = sorted(set(mapping.values()) - set(self.available)) if self.available else []
            if missing:
                raise ValueError('Selected sample is missing channel(s): ' + ', '.join(missing)
                    + '. Choose matching channel names, or load the matching sample before importing.')
            self.candidate = prepare_gate_import(self.project, self.document, variant=self.variant.currentData(),
                channel_map=mapping, open_plot=self.open_plot.isChecked(),
                use_compensation=self.use_compensation.isVisibleTo(self) and self.use_compensation.isChecked())
        except (TypeError, ValueError) as exc:
            self.candidate = None; self.error_label.setText(str(exc)); return
        super().accept()
