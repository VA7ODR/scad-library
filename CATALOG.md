# SCAD Catalog Notes

This tool is driven by `sources.json`.

Each configured source is one library folder, and each source has a `type`:

- `scad`
  - customizable OpenSCAD source files
- `stl`
  - baked STL folders

## Rendering

Preview generation is intentionally consistent across source types:

- SCAD previews are rendered directly with `openscad-nightly`
- STL previews are rendered with `openscad-nightly` through generated wrapper `.scad` files

Generated wrapper files live in:

```text
.catalog/wrappers/
```

## Browser actions

SCAD entries support:

- open source
- open in OpenSCAD
- render custom preview
- export binary STL
- copy command

STL entries support:

- open/download source
- open in slicer

## Rebuild behavior

Running the catalog builder again is incremental by default:

- changed source files are rerendered
- unchanged source files reuse cached preview/metadata artifacts
- `--force` rebuilds everything

## Server

The server can also:

- rescan libraries
- read and write `sources.json`
- relaunch the browser UI after a rescan by reloading the page
