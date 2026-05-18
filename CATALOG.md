# SCAD Catalog Notes

This tool is driven by `sources.json`.

An optional top-level `tools` block can configure the OpenSCAD executable and slicer executable used by the local server and by config-driven rescans.

Each configured source is one library folder. Every source is scanned for:

- customizable OpenSCAD `.scad` files
- baked object files such as `.stl` and `.3mf`

Legacy source `type` fields are still accepted in existing configs, but scan classification now happens per file.

An optional top-level `ai` block can enable local Ollama enrichment during indexing. When enabled and available, the indexer can add:

- short descriptions
- search terms
- friendly parameter labels with the original SCAD variable names preserved in the UI

If Ollama is disabled, offline, or missing the configured model, the catalog still builds normally without AI metadata. If `ai.modelfile` is configured and Ollama is installed, the tool will try to create the missing model automatically with `ollama create`.

## Rendering

Preview generation is intentionally consistent across source types:

- SCAD previews are rendered directly with `openscad-nightly`
- baked object previews are rendered with `openscad-nightly` through generated wrapper `.scad` files

Generated wrapper files live in:

```text
.catalog/wrappers/
```

## Browser actions

SCAD entries support:

- open source
- open in OpenSCAD
- render custom preview
- export binary STL or export straight to a configured slicer
- copy command

Baked object entries support:

- open/download source
- open in slicer

## Rebuild behavior

Running the catalog builder again is incremental by default:

- changed source files are rerendered
- unchanged source files reuse cached preview/metadata artifacts
- unchanged AI enrichments reuse cached `.catalog/ai/` artifacts when the input and model are unchanged
- `--force` rebuilds everything

## Server

The server can also:

- rescan libraries
- force a full rebuild, equivalent to `--force`
- read and write `sources.json`
- relaunch the browser UI after a rescan by reloading the page
