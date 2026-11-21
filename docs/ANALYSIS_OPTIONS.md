# Compensation, scales, gate types and quality views (0.9.0)

Exact definitions of what was added in 0.9.0. Statistics are always calculated on every event of a population and never on transformed values.

## Compensation

Where: the line above the plots reads `Compensation: off`. Click the underlined word.

| Setting | What is applied |
|---|---|
| Off (default) | Values as FlowKit reads them after acquisition preprocessing. |
| Spillover matrix stored in each file | Each file's own `$SPILLOVER`, `$SPILL` or `$COMP` keyword. A file without a readable matrix fails with a message; it is not silently left uncompensated. |
| One matrix for all samples | The matrix shown in the dialog. Take it from the selected sample, import it from CSV, or edit cells. |

- Calculation: `compensated = events × inverse(spillover)` for the channels named in the matrix, by FlowKit (`Matrix.apply`, FlowUtils). Rows of the matrix are fluorochromes, columns are detectors, as in the FCS keyword.
- Scope: compensated values replace the stored ones for gates, plots, statistics and every export. Channel names stay the same. Exports state the domain (`flowkit-preprocessed-compensated`) and the provenance file and methods report contain the matrix.
- Existing gates keep their coordinates when the setting changes. A gate on a fluorescence channel then selects different events. Check such gates; Undo reverses the change.
- Scatter and time channels cannot be part of the matrix.
- Not available: per-sample matrices other than the one in each file, calculating a matrix from single-stain controls, spectral unmixing.
- CSV format: first row channel names, then one row per channel; rows may start with the channel name.

Gating-ML export writes a project matrix into the file. With "matrix stored in each file" it writes the standard reference `FCS` for the channels that were compensated in the selected sample.

## Axis scales

Click an axis label, or right-click a plot. Scales: linear, log (base 10), logicle.

- Logicle: the Gating-ML 2.0 function (Moore and Parks, Cytometry A 2012) as implemented in FlowUtils, with T (top of scale), W (width of the linear region in decades), M (decades) and A (additional negative decades). Defaults: T 262144, W 0.5, M 4.5, A 0. T and W can be set in the axis dialog. Set T to the top of your instrument's range if it differs.
- A logicle axis shows zero and negative values; a log axis leaves them out and reports how many.
- A gate stores the scale it was drawn in, including the logicle parameters, and is evaluated by FlowKit in exactly those coordinates. A polygon edge is straight on the axis it was drawn on.
- A gate shown on a plot with another scale is drawn converted and cannot be dragged there. Switch the plot to the gate's scale to edit it.
- Tick labels on a logicle axis are in original units.

## Gate types

- **Ellipse** (gate tool "Ellipse"): drag a box; the ellipse fills it. The centre handle moves it; the two rim handles change size and tilt. Stored as centre and two half-axes; an event is inside when its Mahalanobis distance is at most 1 for covariance = sum of the outer products of the half-axes (FlowKit `EllipsoidGate`, written to Gating-ML as such).
- **Combined gate** (right-click a gate > Combine gates…): events of the parent population that are in all (AND), in any (OR) or not in (NOT) the ticked gates. Only gates under the same parent can be combined. A combined gate has no outline and no per-sample position; it follows the gates it combines. Deleting one of those gates deletes the combined gate.
- **Copy / paste** (right-click, or Ctrl+C / Ctrl+V in the gate list): copies a gate with its descendants using the shared geometry; per-sample adjustments are not copied. A set of four quadrants stays linked only if all four are copied.

## Highlight a population in other plots (backgating)

Right-click a gate > Highlight in all plots. The events of that population in the selected sample are drawn in a second colour in every 2D plot. Display only: up to 4000 points per plot are drawn, the stated event count is exact. Esc clears it. It is not saved with the project.

## Overlays

- Histograms: select several samples; right-click > "Offset overlaid histograms" gives each curve its own baseline. Heights then no longer relate to the axis, so the axis numbers are hidden.
- Scatter plots: right-click > "Overlay selected samples" (off by default). One colour per sample, common axis limits fixed when the overlay starts. Density plots cannot be overlaid.
- There is no fixed limit on the number of overlaid samples (since 0.10.0). Every listed sample is calculated, drawn and exported. Beyond a few dozen curves the colours become hard to tell apart; the legend numbers and the exports identify each sample.

### Fixed sample list of a histogram (0.10.0)

A histogram shows its samples in one of two ways:

- **Automatic (default):** it follows the sample list. One selected row shows that sample; several selected rows are overlaid.
- **Fixed:** it keeps its own list, whatever is selected. The window title says "fixed list, N samples" and the note below the plot repeats it.

Drag one or several rows from the sample list onto a histogram to add them. The histogram then keeps the samples it showed before the drag plus the dropped ones, each once. Only that histogram changes; the selection in the list is put back to what it was before the drag. Right-click the histogram > "Follow sample selection" switches between the two ways: switching it off fixes what is shown now, switching it on returns to the automatic behaviour. Undo reverses a drop.

What stays the same: the population, the gates and each sample's own gate adjustments, compensation, channel, scale, bins and normalization. The X axis limits shown at the time of the first drop are kept as common limits for all curves; events outside are counted as "outside", not removed from statistics. Statistics are calculated per sample on every event; samples are never pooled.

A sample that cannot be shown (for example because it lacks the channel) is named with its reason in the note below the plot, and is absent from the curves and from the bin export. It never appears as zeros, and the other curves stay.

With more than eight curves the note below the plot gives totals; hover over it for the line of every sample. Right-click > "Show legend" hides or shows the legend; this affects the picture only.

Removing a sample from the project removes it from fixed lists. Templates carry no sample lists. Projects saved with 0.10.0 use format 10 and do not open in 0.9.0 or earlier; older projects open unchanged, with all histograms automatic.

## Singlet gate

Right-click a population > Add singlet gate… (offered when an area channel has a matching height channel, for example FSC-A and FSC-H).

Computed from every event of that population in the **selected sample**: ratio = height / area for events with both positive; centre = median ratio; robust SD = 1.4826 × median absolute deviation (at least 0.5 % of the centre); the gate keeps ratios within centre ± 3 robust SD, from the 0.1st to the 99.9th percentile of area, widened by 5 % of that span. The result is an ordinary polygon on linear axes shared by all samples, plus a density plot showing it. Check it on other samples and adjust or delete it. The rule has not been validated on real acquisitions.

## Acquisition over time

Right-click a sample > Acquisition over time…

All events of the file in 100 equal time bins: events per bin, and the median of the statistics channel per bin. A bin is shaded when its count differs from the median count by more than 4 robust SD, where robust SD is the larger of 1.4826 × MAD of the counts and the square root of the median count. The window describes the acquisition only. No event is removed and nothing is saved. To exclude a period, plot the time channel as a histogram and draw a range gate.

## Autosave and recovery

Every two minutes, unsaved work is written to a recovery copy in the settings folder, never to the project file. Saving, opening another project or closing normally deletes the copy. If FlowHero ends abnormally, the next start offers Restore, Discard or Decide later. A restored project is unsaved; use Save project as….

## Light plot background

View > Light plot background. Remembered between sessions. Exported images are on white in either setting.
