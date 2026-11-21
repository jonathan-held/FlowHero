# Changelog

Version numbers before 1.0 mark development builds. "Project format" is the version of the saved `.flowhero.json` file; every version opens all earlier formats.

## 0.10.2 (2026-11-21)

- Right-click a plot > Duplicate plot adds an identical copy with all its settings, offset on the sheet. The copy is independent: changing it leaves the original unchanged. Undo removes it.

## 0.10.1 (2026-11-16)

First public version.

- Histogram statistics window (right-click a histogram > Show histogram statistics…): one row per sample; median, arithmetic mean and geometric mean are columns. The CSV exported from that window has the same layout. A statistic that cannot be calculated leaves its cell empty and is named in the Status column, for example `geometric_mean: nonpositive`.
- Fixed: a sample drop that FlowHero refuses, for example while the histogram is still being calculated, was reported to the system as accepted.
- Start scripts for macOS/Linux and Windows replace the earlier macOS-only installer.

## 0.10.0 (2026-11-16)

- Drag rows from the sample list onto a histogram to add those samples to that histogram only. Such a histogram keeps a fixed sample list; right-click > Follow sample selection switches back.
- No limit on the number of overlaid samples (was eight), in histograms and scatter overlays.
- Right-click a histogram > Show legend.
- Project format 10.

## 0.9.0 (2026-11-15)

- Compensation: off, from each file's spillover matrix, or one matrix for all samples.
- Logicle axis; gates keep the scale they were drawn in.
- Ellipse gates; combined gates (AND, OR, NOT); copy and paste of a gate with its descendants.
- Highlighting of a population in all 2D plots (backgating).
- Singlet gate proposal from area and height; acquisition-over-time view.
- Offset (ridge) layout for overlaid histograms; several samples overlaid in a scatter plot.
- Gate import from a FlowJo 10 workspace (`.wsp`) through FlowKit's parser.
- Recovery copy of unsaved work; light plot background.
- Project format 9.

## 0.8.0 (2026-11-15)

- Statistics export in long or wide layout with SD, CV, robust CV, percentiles, percentage of a chosen population and events per µL, with a provenance file; Markdown methods report.
- Event export of a plotted population; plot images as PNG, SVG or PDF.
- Threshold from a control sample; sample annotations from CSV; file information window.
- Gate names and percentages on plots; recent projects; drag and drop of files.
- Commands `flowhero export-statistics` and `flowhero methods-report`.
- Project format 8.

## 0.7.1 and earlier (November 2026)

Plot sheet with scatter, density and histogram plots; hierarchical polygon, rectangle, range and quadrant gates; shared gates with per-sample adjustments; Gating-ML and JSON gate import and export; templates; portable packages; analysis in background processes; experimental adaptive gate proposals (`flowhero analyze`).
