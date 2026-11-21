# Exports, statistics and annotations

All exports recalculate from the FCS files after checking their SHA-256 checksum. They do not depend on which plots are open, and they never overwrite an existing file silently (the system file dialog asks). A sample that cannot be calculated is left out and named; it is never written as zeros.

## Statistics table

File > Export > Statistics…

- **Channels**: ticked by default are the statistics channel and the channels of the open plots. Tick any others. With no channel ticked, the table holds counts and percentages only.
- **Statistics**: median, mean and geometric mean by default. "More options" adds SD, CV, robust CV, percentiles and a percentage of a chosen population.
- **Layout**: long or wide.

| Layout | Shape | Use |
|---|---|---|
| Long | One row per sample × population × channel × statistic | R, Python, pivot tables |
| Wide | One row per sample; one column per population and measure | Prism, Excel |

Wide column names read `Population path | measure`, for example `Cells/CD4+ | % of parent` or `Cells/CD4+ | PE-A median`. A population path joins gate names from the top with `/`. If two gates would get the same path, the later one gets ` [2]`.

Sample annotations appear as columns directly after the sample name in both layouts.

### Definitions

| Name | Definition | No value when |
|---|---|---|
| `median` | Median | population empty |
| `arithmetic_mean` | Arithmetic mean | population empty |
| `geometric_mean` | exp(mean(ln x)) | any value ≤ 0 (`nonpositive`) |
| `sd` | Sample standard deviation, n − 1 denominator | fewer than 2 events (`insufficient`) |
| `cv` | 100 × SD / mean | mean is 0 (`undefined`) |
| `robust_cv` | 100 × 0.5 × (P84.13 − P15.87) / median | median is 0 (`undefined`) |
| `pN` | N-th percentile, linear interpolation (NumPy default) | population empty |
| `percent_parent` | 100 × events / events of the parent population | parent empty |
| `percent_total` | 100 × events / all events in the file | file empty |
| `percent_of_ancestor` | 100 × events / events of the chosen population; blank for populations outside it | — |
| `events_per_ul` | events / (`$VOL` / 1000) | file has no `$VOL` keyword (column absent) |

All statistics use the untransformed values of every event in the population. A population holding a non-finite value reports `nonfinite` instead of a number. The `status` column of the long layout carries these words; `ok` means a number was calculated.

`$VOL` is the acquired volume in nanolitres as defined by FCS 3.1. Only volumetric instruments write it. Bead-based counting is not implemented.

### Provenance file

### Statistics of one histogram

Right-click a histogram > `Show histogram statistics…` lists the samples that histogram shows, for its population and channel: one row per sample with event count, median, arithmetic mean and geometric mean. `Export statistics CSV…` writes these rows with the columns `sample_id, sample, source_path, source_sha256, gate_id, gate, channel, event_count, median, arithmetic_mean, geometric_mean, status, domain, gate_scope, parent_id, percent_parent, percent_total, analysis_status`. `status` is `ok`, or names what could not be calculated (`geometric_mean: nonpositive`, `empty`, `nonfinite`); that cell is empty. This file has no provenance file; use File > Export > Statistics for a table to publish.

Next to every statistics table, `<name>.csv.provenance.json` records the software versions, the data domain, each file's path and SHA-256, all gate coordinates (shared and per sample), the requested statistics with their definitions, and any sample left out with the reason.

## Methods report

File > Export > Methods report… writes the same record as a Markdown document with a draft methods paragraph, a sample table and a gate list. It is meant as supplementary material; edit the draft paragraph.

## Plot data

Right-click a plot > Export plot data…

| Plot | Output |
|---|---|
| Histogram | One row per bin and sample: bin limits in original units, count, normalised value |
| Dot plot | One row per event of the shown population: sample, x channel, y channel (untransformed) |
| Density plot | Choice of events (as dot plot) or 2D bin counts |

The event export contains every event of the population, not the subset drawn on screen.

## Plot images

- Right-click a plot > Save plot image… writes that plot for the current sample.
- File > Export > Plot images… writes every open plot for all or the selected samples into a folder. Samples without a current result are calculated first.

Images use a white background and dark axes whatever the screen shows. PNG is at least 1600 pixels wide; SVG and PDF are vector files. Files are named `<sample>__<plot title>`; existing files are kept and a number is appended. A histogram that overlays several samples is written once, as `overlay__…`, with every sample of its list whatever their number.

## Threshold from a control

Right-click a histogram > Set threshold from control…

1. Choose the control sample and a percentile (default 99).
2. FlowHero takes that percentile of the histogram's channel over every event of the control sample's plotted population. On a log axis only positive values count.
3. A range gate from the threshold upwards is added to the shared strategy, named `<channel>+` unless a name is typed in the gate name box.

The result is an ordinary range gate: it can be dragged, renamed, adjusted per sample or undone. Its upper limit is 1e12 (12 on a log axis), which stands for "open" and is noted in provenance files. The threshold is not recalculated when the control changes.

## Sample annotations

File > Import > Sample annotations (CSV)…

The table needs a header row. The first column holds the FCS file name (with or without `.fcs`, case ignored); every further column becomes an annotation.

```text
file,Genotype,Dose_nM
A1.fcs,WT,0
A2.fcs,WT,10
```

Comma, semicolon and tab separators are recognised. Rows that match no file are listed in the message area. Importing again replaces the annotations of the matched samples. Annotations are saved in the project, shown in brackets after the sample name, and added to every statistics table and methods report. They are not copied into templates.
