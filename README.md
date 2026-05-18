# scad-library

A local-first browser catalogue for OpenSCAD, STL, and 3MF libraries.

Built for people who want to keep their printable model libraries on their own disk,
preview and customize OpenSCAD files locally, and open finished models directly in
their slicer without depending on hosted customizer platforms.

It can:

- scan one or more OpenSCAD source folders
- render preview images
- expose OpenSCAD Customizer parameters
- export binary STL files
- open source `.scad` files in OpenSCAD
- open baked `.stl` and `.3mf` files directly in a slicer
- manage source folders from the browser
- optionally enrich entries with local Ollama-generated summaries, tags, and friendlier parameter labels

This repository is the tool only. It does not contain any model libraries.

## Screenshots

Catalog overview:

![SCAD Catalog overview](docs/screenshots/catalog-overview.png)

Customizer modal:

![SCAD Catalog customizer modal](docs/screenshots/customizer-modal.png)

## License

This project is licensed under the `GNU Affero General Public License v3.0 or later` (`AGPL-3.0-or-later`).

See [LICENSE](LICENSE) for the full text.

## Requirements

- `python3`
- `openscad-nightly`
- optionally a slicer AppImage or executable if you want direct slicer launch

## Repository layout

- `tools/scad_catalog.py`
  - builds the catalog from configured sources
- `tools/scad_catalog_server.py`
  - serves the browser UI and handles preview/export/open actions
- `sources.json`
  - source library configuration
- `CATALOG.md`
  - extra usage notes

## Configure sources

Edit `sources.json`.

Example:

```json
{
  "tools": {
    "openscadBin": "openscad-nightly",
    "slicerBin": ""
  },
  "ai": {
    "enabled": false,
    "provider": "ollama",
    "baseUrl": "http://127.0.0.1:11434",
    "model": "scad-customizer",
    "modelfile": "models/Modelfile.scad-customizer",
    "timeout": 30,
    "includeScad": true,
    "includeStl": false,
    "maxSourceChars": 12000,
    "maxCommentChars": 3000
  },
  "sources": [
    {
      "id": "my-scad-library",
      "name": "My SCAD Library",
      "path": "/path/to/scad/library",
      "libraryPaths": ["/path/to/openscad/libs"],
      "includeHelpers": false,
      "includeInProgress": false,
      "includeDeprecated": false
    },
    {
      "id": "my-baked-library",
      "name": "My Baked Library",
      "path": "/path/to/baked/folder",
      "libraryPaths": []
    }
  ]
}
```

Source fields:

- `id`: stable internal identifier
- `name`: display label in the UI
- `path`: folder to scan
- `libraryPaths`: paths added to `OPENSCADPATH` for that source
- `includeHelpers`: SCAD files only
- `includeInProgress`: SCAD files only
- `includeDeprecated`: SCAD files only

Each configured source folder is scanned for both customizable `.scad` files and baked object files such as `.stl` and `.3mf`.
Legacy `type` values are still accepted in old configs, but scanning is now file-driven.

Tool fields:

- `openscadBin`: full path or command name for OpenSCAD
- `slicerBin`: optional full path or command name for OrcaSlicer or another slicer

AI fields:

- `enabled`: turn local AI enrichment on or off
- `provider`: currently `ollama`
- `baseUrl`: Ollama API base URL
- `model`: local model name such as `scad-customizer`
- `modelfile`: optional Ollama Modelfile path used to auto-create the named model if it is missing
- `timeout`: request timeout in seconds
- `includeScad`: allow AI summaries/tags/parameter labels for SCAD entries
- `includeStl`: allow AI summaries/tags for baked object entries such as STL and 3MF
- `maxSourceChars`: max source excerpt sent to Ollama per SCAD file
- `maxCommentChars`: max leading comment text sent to Ollama per SCAD file

If AI is enabled but Ollama is not running or the model is unavailable, catalog generation falls back to the current non-AI behavior. If `modelfile` is set and Ollama is installed, the tool will try to create the missing model automatically with `ollama create`.

## Build the catalog

From the repo root:

```bash
python3 tools/scad_catalog.py
```

This writes:

- `.catalog/catalog.json`
- `.catalog/index.html`
- `.catalog/previews/`
- `.catalog/metadata/`
- `.catalog/wrappers/` for baked object preview wrappers

Useful variants:

```bash
python3 tools/scad_catalog.py --force
python3 tools/scad_catalog.py --limit 10
python3 tools/scad_catalog.py --skip-previews
python3 tools/scad_catalog.py --config /path/to/other-sources.json
python3 tools/scad_catalog.py --ai --ollama-model qwen3:8b
python3 tools/scad_catalog.py --no-ai
```

## Run the local app

Start the server:

```bash
python3 tools/scad_catalog_server.py
```

Then open:

```text
http://127.0.0.1:8765/.catalog/index.html
```

## UI features

The app currently supports:

- a `Customizable SCAD` tab
- a `Baked Object` tab
- source filtering
- rescan from the browser
- force a full rebuild from the browser
- folder/source editing from the browser
- OpenSCAD launch for `.scad` entries
- slicer launch for baked `.stl` and `.3mf` entries

## Notes

- SCAD previews are rendered by `openscad-nightly`
- baked object previews are also rendered by `openscad-nightly` through generated wrapper `.scad` files
- AI enrichment is optional and cached under `.catalog/ai/`
- AI enrichment never edits or rewrites your SCAD files
- the server defaults to `openscad-nightly`
- OpenSCAD and slicer paths can be configured in `sources.json` or overridden on the server command line
