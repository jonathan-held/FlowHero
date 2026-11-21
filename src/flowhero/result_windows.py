"""Optional result windows keep the plot sheet uncluttered."""
import csv
from PySide6 import QtWidgets
from .histograms import STATISTIC_COLUMNS, TABLE_FIELDS, histogram_statistics_table
from .storage import atomic_write


class HistogramStatistics(QtWidgets.QDialog):
    """One row per sample; median, arithmetic mean and geometric mean side by side."""
    def __init__(self, owner, rows, title):
        super().__init__(owner)
        self.rows = histogram_statistics_table(rows)
        self.setWindowTitle(title + ' — statistics snapshot')
        self.resize(900, 400)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel('Full-event statistics for the displayed samples, population and channel at opening, one row per sample. '
                                          'Export saves these rows with unrounded values.'))
        headers = ['Sample', 'Population', 'Channel', 'Events'] + [label for _, label in STATISTIC_COLUMNS] + ['Status', 'Gate review']
        fields = ['sample', 'gate', 'channel', 'event_count'] + [name for name, _ in STATISTIC_COLUMNS] + ['status', 'analysis_status']
        numeric = {name for name, _ in STATISTIC_COLUMNS}
        self.table = QtWidgets.QTableWidget(len(self.rows), len(fields))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, row in enumerate(self.rows):
            for j, field in enumerate(fields):
                value = row.get(field, 'manual' if field == 'analysis_status' else None)
                if field in numeric and value is not None:
                    item = QtWidgets.QTableWidgetItem(f'{value:.6g}')
                    item.setToolTip(repr(value))
                else:
                    item = QtWidgets.QTableWidgetItem('' if value is None else str(value))
                self.table.setItem(i, j, item)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table)
        export = QtWidgets.QPushButton('Export statistics CSV…')
        export.clicked.connect(self.export)
        layout.addWidget(export)

    def export(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Export displayed histogram statistics', 'histogram-statistics.csv', 'CSV (*.csv)')
        if path:
            def write(stream):
                writer = csv.DictWriter(stream, fieldnames=TABLE_FIELDS, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(self.rows)
            try:
                atomic_write(path, write)
            except OSError as exc:
                self.parent().error(exc)


class QualityOverview(QtWidgets.QDialog):
    def __init__(self, owner, entries):
        super().__init__(owner)
        self.setWindowTitle('Batch quality — operational checks')
        self.resize(850,400)
        layout=QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel('Double-click a sample to inspect it. These are operational flags, not biological acceptance judgments.'))
        table=QtWidgets.QTableWidget(len(entries),3)
        table.setHorizontalHeaderLabels(['Sample','State','Reason'])
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, entry in enumerate(entries):
            for j, text in enumerate(entry[1:]):
                table.setItem(i,j,QtWidgets.QTableWidgetItem(text))
        table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        table.cellDoubleClicked.connect(lambda row,column: owner.samples.setCurrentRow(next(i for i,s in enumerate(owner.project.sources) if s.id==entries[row][0])))
        layout.addWidget(table)
