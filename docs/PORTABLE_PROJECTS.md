# Projects and portable packages

A project JSON saves gates, plot settings, source identities/hashes and any import/adaptation provenance. It references FCS paths; it does not contain event data.

Use File → Save portable package… to bundle the active FCS inputs and current project. File → Open portable package… extracts and verifies into a NEW local folder, retaining identities and rebinding paths. Do not overwrite an existing folder. The original files are not modified. Cancellation and intervening edits are handled without replacing current edits unexpectedly.

Historical gate-import export files and adaptation reference/template acquisitions are not bundled unless they are active inputs. Their source hashes, original geometry and interpretation/proposal metadata remain retained. Active results can be replayed without the old active-input paths; running adaptation again still needs its historical reference acquisition.

Privacy: a project file and a portable package record the full folder path of every FCS file as it was on your computer, and the path of imported gate files. Check this before sharing them.

The earlier suffixes `.flowpilot.json` / `.flowpilot.zip` still open. A newer FlowHero reads all earlier project formats, but an older FlowHero cannot open files saved by a newer one. Save a copy before testing a newer version.
