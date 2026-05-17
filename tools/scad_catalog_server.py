#!/usr/bin/env python3
"""Serve the SCAD catalog and expose local preview/STL export endpoints."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import shlex
import shutil
import subprocess
import sys
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_CATALOG_DIR = ".catalog"
DEFAULT_OPENSCAD_BIN = "openscad-nightly"
DEFAULT_SLICER_BIN = ""
DEFAULT_CONFIG_PATH = "sources.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve the local SCAD catalog and export custom renders."
    )
    parser.add_argument(
        "--bind",
        default=DEFAULT_BIND,
        help=f"Address to bind the server to (default: {DEFAULT_BIND})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to bind the server to (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--workspace-root",
        default=".",
        help="Workspace root to serve from (default: current directory).",
    )
    parser.add_argument(
        "--catalog-dir",
        default=DEFAULT_CATALOG_DIR,
        help=f"Catalog directory relative to the workspace root (default: {DEFAULT_CATALOG_DIR})",
    )
    parser.add_argument(
        "--openscad-bin",
        default=None,
        help="OpenSCAD executable override.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout in seconds for OpenSCAD commands (default: 300).",
    )
    parser.add_argument(
        "--imgsize",
        default="1024,1024",
        help="Preview PNG size in WIDTH,HEIGHT form (default: 1024,1024).",
    )
    parser.add_argument(
        "--slicer-bin",
        default=None,
        help="Slicer executable override.",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Source config file to read and update (default: {DEFAULT_CONFIG_PATH})",
    )
    return parser.parse_args()


def compact_error(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stderr or result.stdout or "").strip()
    if not text:
        return f"OpenSCAD failed with exit code {result.returncode}"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[:6])


def openscad_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps("" if value is None else str(value))


def build_definition_args(parameters: dict[str, Any]) -> list[str]:
    definitions: list[str] = []
    for name, value in sorted(parameters.items()):
        definitions.extend(["-D", f"{name}={openscad_literal(value)}"])
    return definitions


def request_hash(source_path: str, parameters: dict[str, Any]) -> str:
    payload = json.dumps({"sourcePath": source_path, "parameters": parameters}, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def shell_command(
    command: list[str],
    workspace_root: Path,
    library_paths: list[str] | None = None,
) -> str:
    if library_paths:
        open_scad_path = os.pathsep.join(library_paths)
    else:
        open_scad_path = str(workspace_root)
    env_prefix = f"OPENSCADPATH={shlex.quote(open_scad_path)}"
    rendered = " ".join(shlex.quote(part) for part in command)
    return f"{env_prefix} {rendered}"


def build_openscad_env(workspace_root: Path, library_paths: list[str]) -> dict[str, str]:
    env = os.environ.copy()
    if library_paths:
        env["OPENSCADPATH"] = os.pathsep.join(library_paths)
    else:
        env["OPENSCADPATH"] = str(workspace_root)
    return env


def resolve_executable(command_text: str) -> str | None:
    expanded = os.path.expanduser(command_text)
    if os.path.isabs(expanded):
        return expanded if os.path.exists(expanded) else None
    return shutil.which(command_text)


def normalize_tool_text(value: Any, *, allow_empty: bool = False) -> str:
    if value is None and allow_empty:
        return ""
    if not isinstance(value, str):
        raise ValueError("Tool paths must be strings.")
    text = value.strip()
    if not text and not allow_empty:
        raise ValueError("Tool paths must be non-empty strings.")
    return text


class CatalogRequestHandler(SimpleHTTPRequestHandler):
    def __init__(
        self,
        *args: Any,
        builder_script_path: Path,
        workspace_root: Path,
        catalog_dir: Path,
        openscad_bin: str,
        slicer_bin: str,
        config_path: Path,
        timeout_seconds: int,
        imgsize: str,
        **kwargs: Any,
    ) -> None:
        self.builder_script_path = builder_script_path
        self.workspace_root = workspace_root
        self.catalog_dir = catalog_dir
        self.openscad_bin = openscad_bin
        self.slicer_bin = slicer_bin
        self.config_path = config_path
        self.timeout_seconds = timeout_seconds
        self.imgsize = imgsize
        super().__init__(*args, directory=str(workspace_root), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self.send_json(HTTPStatus.OK, {"ok": True})
            return
        if parsed.path == "/api/config":
            self.handle_get_config()
            return
        if parsed.path.startswith("/api/source-file/"):
            entry_id = parsed.path.removeprefix("/api/source-file/")
            self.handle_source_file(entry_id)
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/rescan":
            self.handle_rescan()
            return
        if parsed.path == "/api/config":
            self.handle_save_config()
            return
        if parsed.path not in {
            "/api/render-preview",
            "/api/export-stl",
            "/api/open-scad",
            "/api/open-in-slicer",
        }:
            self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Unknown API endpoint"})
            return

        length_header = self.headers.get("Content-Length")
        if not length_header:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing request body"})
            return

        try:
            content_length = int(length_header)
            payload = json.loads(self.rfile.read(content_length))
        except (ValueError, json.JSONDecodeError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid JSON body"})
            return

        entry_id = payload.get("entryId")
        parameters = payload.get("parameters", {})
        if not isinstance(entry_id, str) or not isinstance(parameters, dict):
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "entryId must be a string and parameters must be an object"},
            )
            return

        try:
            entry = self.resolve_entry(entry_id)
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return

        if parsed.path == "/api/render-preview":
            self.handle_render_preview(entry, parameters)
            return

        if parsed.path == "/api/open-scad":
            self.handle_open_scad(entry)
            return

        if parsed.path == "/api/open-in-slicer":
            self.handle_open_in_slicer(entry)
            return

        self.handle_export_stl(entry, parameters)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def config_payload(self) -> dict[str, Any]:
        if not self.config_path.exists():
            raise ValueError(f"Config file not found: {self.config_path}")
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Config file is invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Config file root must be a JSON object.")
        sources = payload.get("sources")
        if not isinstance(sources, list):
            raise ValueError("Config file must contain a 'sources' array.")
        return payload

    def effective_tools(self) -> dict[str, str]:
        payload = self.config_payload()
        raw_tools = payload.get("tools", {})
        if raw_tools is None:
            raw_tools = {}
        if not isinstance(raw_tools, dict):
            raise ValueError("Top-level 'tools' config must be an object.")

        openscad_bin = self.openscad_bin or raw_tools.get("openscadBin", DEFAULT_OPENSCAD_BIN)
        slicer_bin = self.slicer_bin
        if slicer_bin is None:
            slicer_bin = raw_tools.get("slicerBin", DEFAULT_SLICER_BIN)

        return {
            "openscadBin": normalize_tool_text(openscad_bin),
            "slicerBin": normalize_tool_text(slicer_bin, allow_empty=True),
        }

    def validate_config_payload(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValueError("Config payload must be a JSON object.")
        tools_config = payload.get("tools")
        if tools_config is not None:
            if not isinstance(tools_config, dict):
                raise ValueError("Top-level 'tools' config must be an object.")
            if "openscadBin" in tools_config and (
                not isinstance(tools_config["openscadBin"], str)
                or not tools_config["openscadBin"].strip()
            ):
                raise ValueError("Tools config field 'openscadBin' must be a non-empty string.")
            if "slicerBin" in tools_config and not isinstance(tools_config["slicerBin"], str):
                raise ValueError("Tools config field 'slicerBin' must be a string.")
        ai_config = payload.get("ai")
        if ai_config is not None:
            if not isinstance(ai_config, dict):
                raise ValueError("Top-level 'ai' config must be an object.")
            string_fields = ("provider", "baseUrl", "model")
            for field in string_fields:
                if field in ai_config and (
                    not isinstance(ai_config[field], str) or not ai_config[field].strip()
                ):
                    raise ValueError(f"AI config field '{field}' must be a non-empty string.")
            bool_fields = ("enabled", "includeScad", "includeStl")
            for field in bool_fields:
                if field in ai_config and not isinstance(ai_config[field], bool):
                    raise ValueError(f"AI config field '{field}' must be boolean.")
            int_fields = ("timeout", "maxSourceChars", "maxCommentChars")
            for field in int_fields:
                if field in ai_config and (
                    isinstance(ai_config[field], bool)
                    or not isinstance(ai_config[field], int)
                    or ai_config[field] <= 0
                ):
                    raise ValueError(f"AI config field '{field}' must be a positive integer.")
        sources = payload.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError("Config payload must contain a non-empty 'sources' array.")
        for index, source in enumerate(sources, start=1):
            if not isinstance(source, dict):
                raise ValueError(f"Source #{index} must be an object.")
            source_type = source.get("type", "scad")
            if source_type not in {"scad", "stl"}:
                raise ValueError(f"Source #{index} has unsupported type '{source_type}'.")
            if not isinstance(source.get("name"), str) or not source["name"].strip():
                raise ValueError(f"Source #{index} must have a non-empty 'name'.")
            if not isinstance(source.get("path"), str) or not source["path"].strip():
                raise ValueError(f"Source #{index} must have a non-empty 'path'.")
            library_paths = source.get("libraryPaths", [])
            if library_paths is None:
                source["libraryPaths"] = []
                library_paths = source["libraryPaths"]
            if not isinstance(library_paths, list) or any(
                not isinstance(item, str) for item in library_paths
            ):
                raise ValueError(f"Source #{index} has an invalid 'libraryPaths' array.")
            for field in ("includeHelpers", "includeInProgress", "includeDeprecated"):
                if field in source and not isinstance(source[field], bool):
                    raise ValueError(f"Source #{index} field '{field}' must be boolean.")

    def load_catalog_index(self) -> dict[str, dict[str, Any]]:
        catalog_path = self.catalog_dir / "catalog.json"
        if not catalog_path.exists():
            raise ValueError(f"Catalog file not found: {catalog_path}")
        try:
            payload = json.loads(catalog_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Catalog file is invalid JSON: {exc}") from exc

        entries = payload.get("entries", [])
        if not isinstance(entries, list):
            raise ValueError("Catalog file has an invalid 'entries' array.")
        index: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                index[entry["id"]] = entry
        return index

    def resolve_entry(self, entry_id: str) -> dict[str, Any]:
        index = self.load_catalog_index()
        entry = index.get(entry_id)
        if entry is None:
            raise ValueError(f"Unknown entryId: {entry_id}")

        source_path_text = entry.get("absoluteSourcePath")
        if not isinstance(source_path_text, str):
            raise ValueError(f"Entry '{entry_id}' is missing absoluteSourcePath")

        source_path = Path(source_path_text).expanduser().resolve()
        if not source_path.exists() or source_path.suffix.lower() not in {".scad", ".stl"}:
            raise ValueError(
                f"Entry '{entry_id}' does not resolve to a valid .scad or .stl file"
            )

        entry["_resolved_source_path"] = source_path
        return entry

    def artifact_url(self, artifact_path: Path) -> str:
        relative = artifact_path.relative_to(self.workspace_root)
        return "/" + relative.as_posix()

    def launch_slicer(self, artifact_path: Path, slicer_bin: str) -> tuple[bool, str | None]:
        if not slicer_bin:
            return False, "No slicer is configured."
        slicer_path = resolve_executable(slicer_bin)
        if not slicer_path:
            return False, f"Slicer not found: {slicer_bin}"
        if not os.access(slicer_path, os.X_OK):
            return False, f"Slicer is not executable: {slicer_path}"

        env = os.environ.copy()
        try:
            subprocess.Popen(
                [str(slicer_path), str(artifact_path)],
                cwd=self.workspace_root,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            return False, f"Failed to launch slicer: {exc}"
        return True, None

    def launch_openscad(
        self,
        source_path: Path,
        library_paths: list[str],
        openscad_bin: str,
    ) -> tuple[bool, str | None]:
        openscad_path = resolve_executable(openscad_bin)
        if not openscad_path:
            return False, f"OpenSCAD not found: {openscad_bin}"
        if not os.access(openscad_path, os.X_OK):
            return False, f"OpenSCAD is not executable: {openscad_path}"
        try:
            subprocess.Popen(
                [str(openscad_path), str(source_path)],
                cwd=self.workspace_root,
                env=build_openscad_env(self.workspace_root, library_paths),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            return False, f"Failed to launch OpenSCAD: {exc}"
        return True, None

    def run_openscad_for_entry(
        self,
        command: list[str],
        *,
        library_paths: list[str],
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=self.workspace_root,
            env=build_openscad_env(self.workspace_root, library_paths),
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )

    def handle_source_file(self, entry_id: str) -> None:
        try:
            entry = self.resolve_entry(entry_id)
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return

        source_path = entry["_resolved_source_path"]
        try:
            data = source_path.read_bytes()
        except OSError as exc:
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": f"Failed to read source file: {exc}"},
            )
            return

        mime_type, _ = mimetypes.guess_type(source_path.name)
        content_type = mime_type or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Disposition",
            f'inline; filename="{source_path.name}"',
        )
        self.end_headers()
        self.wfile.write(data)

    def handle_get_config(self) -> None:
        try:
            payload = self.config_payload()
            effective_tools = self.effective_tools()
        except ValueError as exc:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return
        self.send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "configPath": str(self.config_path),
                "config": payload,
                "effectiveTools": effective_tools,
            },
        )

    def handle_save_config(self) -> None:
        length_header = self.headers.get("Content-Length")
        if not length_header:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing request body"})
            return

        try:
            content_length = int(length_header)
            payload = json.loads(self.rfile.read(content_length))
        except (ValueError, json.JSONDecodeError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid JSON body"})
            return

        config = payload.get("config")
        try:
            self.validate_config_payload(config)
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return

        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(config, indent=2) + "\n",
            encoding="utf-8",
        )
        self.send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "configPath": str(self.config_path),
            },
        )

    def handle_rescan(self) -> None:
        force = False
        length_header = self.headers.get("Content-Length")
        if length_header:
            try:
                content_length = int(length_header)
                body = self.rfile.read(content_length) if content_length > 0 else b""
                if body:
                    payload = json.loads(body)
                    if isinstance(payload, dict):
                        force = bool(payload.get("force", False))
            except (ValueError, json.JSONDecodeError):
                self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid JSON body"})
                return

        try:
            tools = self.effective_tools()
        except ValueError as exc:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return
        command = [
            sys.executable,
            str(self.builder_script_path),
            "--config",
            str(self.config_path),
            "--output-dir",
            str(self.catalog_dir),
            "--openscad-bin",
            tools["openscadBin"],
            "--imgsize",
            self.imgsize,
            "--timeout",
            str(self.timeout_seconds),
        ]
        if force:
            command.append("--force")
        result = subprocess.run(
            command,
            cwd=self.workspace_root,
            capture_output=True,
            text=True,
            timeout=max(self.timeout_seconds * 10, self.timeout_seconds),
            check=False,
        )
        if result.returncode != 0:
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error": compact_error(result),
                    "command": " ".join(shlex.quote(part) for part in command),
                },
            )
            return
        try:
            catalog = self.load_catalog_index()
            source_count = len(self.config_payload().get("sources", []))
        except ValueError:
            catalog = {}
            source_count = 0
        self.send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "entryCount": len(catalog),
                "sourceCount": source_count,
                "forced": force,
                "command": " ".join(shlex.quote(part) for part in command),
            },
        )

    def handle_open_scad(self, entry: dict[str, Any]) -> None:
        if entry.get("entryType") != "scad":
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "Only SCAD entries can be opened in OpenSCAD."},
            )
            return
        source_path = entry["_resolved_source_path"]
        library_paths = entry.get("libraryPaths", [])
        try:
            tools = self.effective_tools()
        except ValueError as exc:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return
        launched, error = self.launch_openscad(source_path, library_paths, tools["openscadBin"])
        if not launched:
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error": error or "Failed to launch OpenSCAD",
                },
            )
            return
        self.send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "openedPath": str(source_path),
            },
        )

    def handle_open_in_slicer(self, entry: dict[str, Any]) -> None:
        source_path = entry["_resolved_source_path"]
        try:
            tools = self.effective_tools()
        except ValueError as exc:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return
        launched, error = self.launch_slicer(source_path, tools["slicerBin"])
        if not launched:
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error": error or "Failed to launch slicer",
                },
            )
            return
        self.send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "openedPath": str(source_path),
                "slicerPath": tools["slicerBin"],
            },
        )

    def handle_render_preview(
        self,
        entry: dict[str, Any],
        parameters: dict[str, Any],
    ) -> None:
        source_path = entry["absoluteSourcePath"]
        resolved_source = entry["_resolved_source_path"]
        library_paths = entry.get("libraryPaths", [])
        try:
            tools = self.effective_tools()
        except ValueError as exc:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return
        digest = request_hash(source_path, parameters)
        output_path = (
            self.catalog_dir
            / "custom"
            / "previews"
            / f"{resolved_source.stem}-{digest}.png"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        command = [
            tools["openscadBin"],
            "--autocenter",
            "--viewall",
            "--imgsize",
            self.imgsize,
            "--backend",
            "Manifold",
            "--render=true",
            *build_definition_args(parameters),
            "-o",
            str(output_path),
            str(resolved_source),
        ]
        result = self.run_openscad_for_entry(command, library_paths=library_paths)
        if result.returncode != 0 or not output_path.exists():
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error": compact_error(result) if result.returncode != 0 else "Preview file was not created",
                    "command": shell_command(command, self.workspace_root, library_paths),
                },
            )
            return

        self.send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "artifactPath": self.artifact_url(output_path),
                "command": shell_command(command, self.workspace_root, library_paths),
            },
        )

    def handle_export_stl(
        self,
        entry: dict[str, Any],
        parameters: dict[str, Any],
    ) -> None:
        source_path = entry["absoluteSourcePath"]
        resolved_source = entry["_resolved_source_path"]
        library_paths = entry.get("libraryPaths", [])
        try:
            tools = self.effective_tools()
        except ValueError as exc:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return
        digest = request_hash(source_path, parameters)
        output_path = self.catalog_dir / "custom" / "stl" / f"{resolved_source.stem}-{digest}.stl"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        command = [
            tools["openscadBin"],
            "--export-format",
            "binstl",
            *build_definition_args(parameters),
            "-o",
            str(output_path),
            str(resolved_source),
        ]
        result = self.run_openscad_for_entry(command, library_paths=library_paths)
        if result.returncode != 0 or not output_path.exists():
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error": compact_error(result) if result.returncode != 0 else "STL file was not created",
                    "command": shell_command(command, self.workspace_root, library_paths),
                },
            )
            return

        launched_slicer, slicer_error = self.launch_slicer(output_path, tools["slicerBin"])

        self.send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "artifactPath": self.artifact_url(output_path),
                "command": shell_command(command, self.workspace_root, library_paths),
                "launchedSlicer": launched_slicer,
                "slicerPath": tools["slicerBin"],
                "slicerError": slicer_error,
            },
        )


def main() -> int:
    args = parse_args()
    workspace_root = Path(args.workspace_root).resolve()
    catalog_dir = (workspace_root / args.catalog_dir).resolve()
    builder_script_path = (Path(__file__).resolve().parent / "scad_catalog.py").resolve()
    config_path = Path(args.config).expanduser()
    if not config_path.is_absolute():
        config_path = (workspace_root / config_path).resolve()
    if not catalog_dir.exists():
        raise SystemExit(f"Catalog directory does not exist: {catalog_dir}")

    handler = partial(
        CatalogRequestHandler,
        builder_script_path=builder_script_path,
        workspace_root=workspace_root,
        catalog_dir=catalog_dir,
        openscad_bin=args.openscad_bin,
        slicer_bin=args.slicer_bin,
        config_path=config_path,
        timeout_seconds=args.timeout,
        imgsize=args.imgsize,
    )
    server = ThreadingHTTPServer((args.bind, args.port), handler)

    url = f"http://{args.bind}:{args.port}/.catalog/index.html"
    print(f"Serving {workspace_root}")
    print(f"Catalog: {url}")
    print("Press Ctrl+C to stop.")
    try:
      server.serve_forever()
    except KeyboardInterrupt:
      pass
    finally:
      server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
