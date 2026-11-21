# Import exported gate definitions

FlowHero imports standalone **FlowHero gate JSON**, a supported subset of **Gating-ML 2.0 XML** and the gates of a **FlowJo 10 workspace (`.wsp`)**. Importing gates does not import FCS events; load matching FCS files separately. Other workspace formats are not supported.

## Desktop workflow

1. Load a small set of matching FCS files and select a sample. You can also import before loading samples; counts are then unavailable until matching data is loaded.
2. Choose **File → Import → Gate definitions…**, then your `.json`, `.xml`, `.gatingml` or `.wsp` file.
3. Review gate names, parents, type, channels, scales and geometry. Map exported channel names to acquisition channel names explicitly. No automatic stain-name guessing or compensation substitution is performed. With a loaded sample, every mapped channel must exist in that selected sample. Other samples are checked during calculation and may report missing-channel failures.
4. For JSON containing sample adjustments, choose the exported gate set or the named source sample's adjusted geometry. The selected complete gate set becomes **new shared gates for all current samples**; adjustments are not assigned automatically to current sample IDs.
5. Choose **Import gates**. Existing gates, settings and sample adjustments remain. Imported parent links are preserved; colliding active or historical IDs get new identities. **Undo** reverses the complete import and **Redo** restores it. By default, one plot opens for the first imported gate; use the gate tree to add plots for other populations.
6. Verify the imported geometry, full-event counts and statistics against an export from the same source analysis state, then save a new project copy. A vendor export alone does not establish biological or vendor-equivalent results.

Cancel, malformed/unsupported input or an active-project change while reviewing leaves the current strategy intact. The source export and original FCS bytes are never edited.

## Supported representations

| Input | Supported | Explicit limitations |
|---|---|---|
| FlowHero `flowpilot-gates`, version1 | Polygon, rectangle, bounded range, linear/log10 scales, open quadrant rectangles, hierarchy, display names and explicit sample-adjusted geometry selection | Requires the known uncompensated coordinate/validity policy. Quadrant groups must have coherent channels/parent/scales/crossing. No automatic assignment of foreign sample IDs. |
| Gating-ML 2.0 `PolygonGate` | Two dimensions, finite valid vertices, parent hierarchy | IDs become display names. Identity-linear, exact log10 or supported logicle transforms; no clipped polygon dimensions or complements. |
| Gating-ML 2.0 `RectangleGate` | Two-dimensional closed/open bounds, or a one-dimensional range with both limits | Native `QuadrantGate` XML is not supported. Export quadrants as rectangle gates. Open rectangles have read-only boundary outlines; group editing metadata is not encoded in Gating-ML. |
| Standard polygon export | `U937` / `Single Cells`, identity `flin T=1 A=0`, exact channels and parents; IDs with spaces retained | XML-safe IDs are used internally for schema parsing and then restored. No vendor numerical-parity claim. |

Transforms must be absent, identity `flin T=1 A=0`, exact log10 `flog T=10 M=1`, or `logicle` with valid T, W, M, A. Nonidentity scaling, other logarithms, hyperlog/asinh, ratios and derived dimensions reject the **whole file**. Polygon vertices are never silently converted through a nonlinear transform.

Compensation must be absent/uncompensated. The supplied Gating-ML `FCS` references are accepted only when **every dimension in that gate is FSC-/SSC- scatter**, because the pinned engine compensates fluorescence channels only. This normalization is recorded in the preview/provenance and is covered by a real nonidentity-spillover test. `FCS` references on fluorescence or mixed scatter/fluorescence gates and named matrices reject. Export uncompensated gates instead; FlowHero has a compensation dialog for analysis, but this Gating-ML path accepts only uncompensated definitions. Imported gates need a matching data domain; the project compensation setting controls subsequent statistics.

Two-dimensional ellipsoid gates are imported as ellipses. Unknown elements/attributes, Boolean gates, ellipsoids with more than two dimensions, missing parents, duplicate IDs, cycles, nonfinite/degenerate geometry and unresolved transform references reject instead of partially importing. XML uses the engine's local schema; no DTD/entity/network resolution is allowed. JSON arrays are parsed as arrays and are not reconstructed from strings/objects.

## Saved provenance and portability

Project format 9 stores `gate_imports`: selected file path/SHA-256, format and sample-geometry choice, original canonical gates, source→current gate/channel mappings and interpretation notes. History survives Undo/Redo, save/reopen, templates and portable packaging. Deleting a gate does not erase its import history; reimporting reserves old gate/review identities. Historical export files are **not bundled** into portable projects; original canonical geometry and provenance remain available even if those paths are unavailable later.

Format 9 reads project formats 1-8. Earlier versions cannot read new saves; preserve the old project and save a new copy. Runtime pins are unchanged.

## Headless workflow

    python -m flowhero import-gates current.flowhero.json gates.xml \
      --output imported.flowhero.json

    python -m flowhero import-gates current.flowhero.json gates.json \
      --variant EXPORTED_SAMPLE_ID --channel "Exported channel" "Acquisition channel" \
      --output imported.flowhero.json

The output must be new. Channel mappings are optional explicit pairs; unmapped channels retain their exported names. Headless import validates definitions and writes the candidate, but does **not** calculate counts or require available FCS paths. Open the project or run analysis with matching inputs to calculate. `inspect` returns compact import summaries; `inspect --details` includes original gates and full mappings.


## Version 2 gate JSON (0.9.0)

A gate export is written as version 1 whenever it contains only what 0.8.0 could read. It is written as version 2 when it contains an ellipse, a combined (AND/OR/NOT) gate, a logicle scale, or when the project is compensated. Version 2 files carry the domain they were drawn in; importing gates drawn on compensated values adds a note asking you to check the project's compensation setting. Version 2 does not open in 0.8.0 or earlier.

## FlowJo 10 workspaces (`.wsp`)

The workspace is parsed by FlowKit (`flowkit.parse_wsp`); FlowHero then maps each workspace sample's gates onto its own gate types. The import dialog lists the workspace samples; the gates of the one you choose are added as new shared gates.

| In the workspace | Imported as | Exact? |
|---|---|---|
| Polygon, rectangle, range on linear, log or logicle axes | Polygon, rectangle, range on the same scale | Yes |
| Gate on a FlowJo "biex" or ArcSinh axis | Polygon/rectangle on a logicle axis (T 262144, W 0.5, M 4.5, A 0); polygon edges subdivided into 16 parts each | Rectangle and range bounds: yes. Polygon outline: follows the original closely, but events very close to an edge can fall on the other side |
| Ellipse | Polygon with 128 vertices (FlowKit's conversion, as FlowJo itself evaluates ellipses) | Approximation by FlowKit |
| AND / OR / NOT gate of gates under the same parent | Combined gate | Yes |
| One-sided range | Range with the open side at 1e12 (1e-12 on a log axis) | Yes for real data |
| Inverted gates, combined gates across different parents or with inverted members, derived parameters | Left out together with their descendants, and named in the notes | - |

Compensation: when the workspace sample carries a spillover matrix, the dialog offers "Also use this workspace sample's compensation matrix for the project". Ticked (the default), the matrix becomes the project's compensation setting in the same undo step, replacing the current setting. Unticked, the gates are added as drawn and you must set compensation yourself; gates on compensated channels are then in the wrong place.

Not imported: statistics, plots, layouts, keywords, sample groups, and per-sample differences other than the sample you chose.

Tested with workspaces written by FlowKit's own FlowJo exporter and with FlowKit transform objects. **No workspace saved by FlowJo itself was available for testing.** Compare event counts of a few populations with FlowJo before relying on an imported strategy.
