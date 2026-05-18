#!/usr/bin/env python3
"""Serve the SCAD catalog and expose local preview/STL export endpoints."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib import error as urllib_error
from urllib import request as urllib_request


DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_CATALOG_DIR = ".catalog"
DEFAULT_OPENSCAD_BIN = "openscad-nightly"
DEFAULT_SLICER_BIN = ""
DEFAULT_CONFIG_PATH = "sources.json"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen3:4b-instruct"
DEFAULT_AI_TIMEOUT = 30
SCAD_FILE_EXTENSION = ".scad"
BAKED_FILE_EXTENSIONS = {".stl", ".3mf"}
SUPPORTED_SOURCE_TYPES = {"scad", "stl", "mixed", "auto"}
ASSISTANT_MAX_CANDIDATES = 24
ASSISTANT_MAX_MESSAGES = 8
ASSISTANT_MAX_MESSAGE_CHARS = 600
SEARCH_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "for",
    "from",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "me",
    "my",
    "of",
    "on",
    "or",
    "that",
    "the",
    "them",
    "this",
    "to",
    "use",
    "using",
    "want",
    "what",
    "with",
}
QUERY_EXPANSIONS = {
    "hold": ["holder", "hook", "shelf", "tray", "mount", "bracket", "clamp", "channel"],
    "holding": ["holder", "hook", "shelf", "tray", "mount", "bracket", "clamp"],
    "mount": ["mount", "holder", "bracket", "hook", "shelf", "tray", "channel"],
    "mounted": ["mount", "holder", "bracket", "hook", "shelf", "tray"],
    "kvm": ["holder", "shelf", "tray", "mount", "device", "cable"],
    "cable": ["cable", "channel", "loop", "hook", "clip", "holder"],
    "desk": ["desk", "underware", "shelf", "mount"],
    "under": ["underware", "mount", "hook", "holder"],
}
GENERIC_ASSISTANT_PHRASES = (
    "structured summary",
    "comprehensive list",
    "product catalog",
    "documentation",
    "purchasing",
    "key components",
    "ecosystem components summary",
)
PARAMETER_REASON_HINTS = (
    ("width", "Adjust width for device size or side clearance."),
    ("length", "Adjust length for front-to-back support."),
    ("depth", "Adjust depth for front-to-back support."),
    ("height", "Adjust height or drop for clearance."),
    ("wall", "Increase wall size if you need more stiffness."),
    ("thickness", "Increase thickness if you need more stiffness."),
    ("cutout", "Tune cable cutouts for routing and access."),
    ("cable", "Tune cable-related openings or routing."),
    ("offset", "Adjust offset to fit around nearby surfaces."),
    ("connector", "Tune connector fit or attachment details."),
    ("hook", "Tune hook geometry for grip and clearance."),
    ("count", "Adjust the count to match your routing or attachment needs."),
)
RESCAN_STATUS_LOCK = threading.Lock()
RESCAN_STATUS: dict[str, Any] = {
    "active": False,
    "forced": False,
    "startedAt": None,
    "finishedAt": None,
    "current": 0,
    "total": 0,
    "lastLine": "",
    "error": None,
    "command": "",
    "sourceCount": 0,
    "entryCount": 0,
    "pid": None,
}


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


def collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_rescan_progress(line: str) -> tuple[int, int] | None:
    match = re.search(r"\[(\d+)/(\d+)\]", line)
    if not match:
        return None
    current = int(match.group(1))
    total = int(match.group(2))
    return current, total


def current_rescan_status() -> dict[str, Any]:
    with RESCAN_STATUS_LOCK:
        status = dict(RESCAN_STATUS)
    total = status.get("total", 0) or 0
    current = status.get("current", 0) or 0
    status["progressPercent"] = round((current / total) * 100, 1) if total > 0 else 0
    return status


def update_rescan_status(**updates: Any) -> None:
    with RESCAN_STATUS_LOCK:
        RESCAN_STATUS.update(updates)


def normalize_ai_text(value: Any, default: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        return default
    text = value.strip()
    if not text and not allow_empty:
        return default
    return text


def normalize_ai_timeout(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return default
    return value


def ollama_request(
    *,
    base_url: str,
    timeout_seconds: int,
    endpoint: str,
    payload: dict[str, Any] | None = None,
    method: str = "POST",
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib_request.Request(
        f"{base_url}{endpoint}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except urllib_error.URLError as exc:
        reason = str(exc.reason) if hasattr(exc, "reason") else str(exc)
        raise RuntimeError(reason) from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Ollama returned invalid JSON: {exc}") from exc
    if isinstance(parsed, dict) and isinstance(parsed.get("error"), str):
        raise RuntimeError(parsed["error"])
    if not isinstance(parsed, dict):
        raise RuntimeError("Ollama returned an unexpected response payload.")
    return parsed


def ollama_available_names(payload: dict[str, Any]) -> set[str]:
    models = payload.get("models", [])
    raw_names = {
        item.get("name")
        for item in models
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    raw_names.update(
        item.get("model")
        for item in models
        if isinstance(item, dict) and isinstance(item.get("model"), str)
    )
    available_names = {name for name in raw_names if isinstance(name, str) and name}
    for name in list(available_names):
        if name.endswith(":latest"):
            available_names.add(name[: -len(":latest")])
    return available_names


def assistant_messages_from_payload(payload: dict[str, Any]) -> list[dict[str, str]]:
    raw_messages = payload.get("messages", [])
    if not isinstance(raw_messages, list):
        raise ValueError("Assistant payload field 'messages' must be an array.")
    normalized: list[dict[str, str]] = []
    for item in raw_messages[-ASSISTANT_MAX_MESSAGES:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        text = collapse_whitespace(content)[:ASSISTANT_MAX_MESSAGE_CHARS]
        if not text:
            continue
        normalized.append({"role": role, "content": text})
    if not normalized:
        raise ValueError("Assistant request is missing any valid chat messages.")
    return normalized


def tokenize_search_text(text: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) >= 2]


def expanded_search_tokens(tokens: list[str]) -> list[tuple[str, float]]:
    weighted: list[tuple[str, float]] = []
    seen: set[tuple[str, float]] = set()
    for token in tokens:
        key = (token, 1.0)
        if key not in seen:
            weighted.append(key)
            seen.add(key)
        for extra in QUERY_EXPANSIONS.get(token, []):
            extra_key = (extra, 0.45)
            if extra_key not in seen:
                weighted.append(extra_key)
                seen.add(extra_key)
    return weighted


def build_assistant_candidate(entry: dict[str, Any]) -> dict[str, Any]:
    ai_payload = entry.get("ai") if isinstance(entry.get("ai"), dict) else {}
    parameter_hints = (
        ai_payload.get("parameterHints") if isinstance(ai_payload, dict) else {}
    )
    if not isinstance(parameter_hints, dict):
        parameter_hints = {}
    parameters: list[dict[str, Any]] = []
    raw_parameters = entry.get("parameters", [])
    if isinstance(raw_parameters, list):
        for parameter in raw_parameters[:16]:
            if not isinstance(parameter, dict):
                continue
            name = parameter.get("name")
            if not isinstance(name, str):
                continue
            hint = parameter_hints.get(name) if isinstance(parameter_hints.get(name), dict) else {}
            option_names: list[str] = []
            raw_options = parameter.get("options", [])
            if isinstance(raw_options, list):
                for option in raw_options[:8]:
                    if isinstance(option, dict) and isinstance(option.get("name"), str):
                        option_names.append(option["name"])
            parameters.append(
                {
                    "name": name,
                    "label": hint.get("label") if isinstance(hint.get("label"), str) else name,
                    "type": parameter.get("type"),
                    "caption": parameter.get("caption"),
                    "options": option_names,
                }
            )
    return {
        "id": entry.get("id"),
        "title": entry.get("title"),
        "entryType": entry.get("entryType"),
        "fileFormat": entry.get("fileFormat"),
        "sourceName": entry.get("sourceName"),
        "category": entry.get("category"),
        "relativePath": entry.get("relativePath"),
        "parameterCount": entry.get("parameterCount", 0),
        "groupNames": entry.get("groupNames", [])[:6],
        "parameterNames": entry.get("parameterNames", [])[:8],
        "summary": ai_payload.get("summary") if isinstance(ai_payload, dict) else None,
        "useCases": ai_payload.get("useCases", [])[:4] if isinstance(ai_payload, dict) else [],
        "searchTerms": ai_payload.get("searchTerms", [])[:8] if isinstance(ai_payload, dict) else [],
        "parameters": parameters,
    }


def candidate_score(entry: dict[str, Any], query: str, tokens: list[str], current_tab: str) -> float:
    search_text = str(entry.get("searchText", ""))
    title = str(entry.get("title", "")).lower()
    source_name = str(entry.get("sourceName", "")).lower()
    category = str(entry.get("category", "")).lower()
    relative_path = str(entry.get("relativePath", "")).lower()
    group_names = " ".join(str(item).lower() for item in entry.get("groupNames", []))
    parameter_names = " ".join(str(item).lower() for item in entry.get("parameterNames", []))
    score = 0.0
    lower_query = query.lower()
    if lower_query and lower_query in search_text:
        score += 12.0
    if lower_query and lower_query in title:
        score += 8.0
    for token, weight in expanded_search_tokens(tokens):
        if token in title:
            score += 4.0 * weight
        if token in source_name:
            score += 2.5 * weight
        if token in category:
            score += 2.0 * weight
        if token in relative_path:
            score += 1.5 * weight
        if token in group_names:
            score += 1.3 * weight
        if token in parameter_names:
            score += 1.1 * weight
        if token in search_text:
            score += 1.0 * weight
    if current_tab and entry.get("entryType") == current_tab:
        score += 6.0
    if entry.get("entryType") == "scad":
        score += 0.25
    return score


def normalize_assistant_response(
    raw: dict[str, Any],
    candidates_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    reply = collapse_whitespace(str(raw.get("reply", "")))[:900]
    follow_up = collapse_whitespace(str(raw.get("followUp", "")))[:220]
    matches: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in raw.get("matches", []):
        if not isinstance(item, dict):
            continue
        entry_id = item.get("id")
        if (
            not isinstance(entry_id, str)
            or entry_id not in candidates_by_id
            or entry_id in seen_ids
        ):
            continue
        seen_ids.add(entry_id)
        entry = candidates_by_id[entry_id]
        valid_names = {
            parameter.get("name")
            for parameter in entry.get("parameters", [])
            if isinstance(parameter, dict) and isinstance(parameter.get("name"), str)
        }
        suggested_parameters: list[dict[str, str]] = []
        for suggestion in item.get("suggestedParameters", []):
            if not isinstance(suggestion, dict):
                continue
            name = suggestion.get("name")
            if not isinstance(name, str) or name not in valid_names:
                continue
            reason = collapse_whitespace(str(suggestion.get("reason", "")))[:180]
            suggested_value = collapse_whitespace(str(suggestion.get("suggestedValue", "")))[:120]
            suggested_parameters.append(
                {
                    "name": name,
                    "reason": reason,
                    "suggestedValue": suggested_value,
                }
            )
        matches.append(
            {
                "id": entry_id,
                "reason": collapse_whitespace(str(item.get("reason", "")))[:220],
                "suggestedParameters": suggested_parameters[:4],
            }
        )
    return {
        "reply": reply,
        "followUp": follow_up,
        "matches": matches[:8],
    }


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
            status = current_rescan_status()
            self.send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "rescanActive": bool(status.get("active")),
                },
            )
            return
        if parsed.path == "/api/config":
            self.handle_get_config()
            return
        if parsed.path == "/api/rescan-status":
            self.handle_rescan_status()
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
        if parsed.path == "/api/assistant":
            self.handle_assistant()
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

    def effective_ai(self) -> dict[str, Any]:
        payload = self.config_payload()
        raw_ai = payload.get("ai", {})
        if raw_ai is None:
            raw_ai = {}
        if not isinstance(raw_ai, dict):
            raise ValueError("Top-level 'ai' config must be an object.")
        return {
            "enabled": bool(raw_ai.get("enabled", False)),
            "provider": normalize_ai_text(raw_ai.get("provider"), "ollama"),
            "baseUrl": normalize_ai_text(raw_ai.get("baseUrl"), DEFAULT_OLLAMA_URL),
            "model": normalize_ai_text(raw_ai.get("model"), DEFAULT_OLLAMA_MODEL),
            "modelfile": normalize_ai_text(raw_ai.get("modelfile"), "", allow_empty=True),
            "timeout": normalize_ai_timeout(raw_ai.get("timeout"), DEFAULT_AI_TIMEOUT),
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
            if "modelfile" in ai_config and not isinstance(ai_config["modelfile"], str):
                raise ValueError("AI config field 'modelfile' must be a string.")
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
            source_type = source.get("type", "mixed")
            if source_type not in SUPPORTED_SOURCE_TYPES:
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
        valid_suffixes = {SCAD_FILE_EXTENSION, *BAKED_FILE_EXTENSIONS}
        if not source_path.exists() or source_path.suffix.lower() not in valid_suffixes:
            raise ValueError(
                f"Entry '{entry_id}' does not resolve to a valid .scad, .stl, or .3mf file"
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

    def handle_rescan_status(self) -> None:
            self.send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "status": current_rescan_status(),
            },
        )

    def assistant_candidates(
        self,
        *,
        query: str,
        current_tab: str,
    ) -> list[dict[str, Any]]:
        entries = list(self.load_catalog_index().values())
        lower_query = query.lower()
        hinted_sources = {
            str(entry.get("sourceName", ""))
            for entry in entries
            if isinstance(entry.get("sourceName"), str)
            and entry.get("sourceName")
            and str(entry.get("sourceName")).lower() in lower_query
        }
        if hinted_sources:
            entries = [entry for entry in entries if entry.get("sourceName") in hinted_sources]
        source_tokens = {
            token
            for source_name in hinted_sources
            for token in tokenize_search_text(source_name)
        }
        tokens = [
            token
            for token in tokenize_search_text(query)
            if token not in SEARCH_STOP_WORDS and token not in source_tokens
        ]
        if not tokens:
            tokens = [
                token for token in tokenize_search_text(query) if token not in SEARCH_STOP_WORDS
            ]
        scored: list[tuple[float, dict[str, Any]]] = []
        for entry in entries:
            score = candidate_score(entry, query, tokens, current_tab)
            if score > 0:
                scored.append((score, entry))
        scored.sort(
            key=lambda item: (
                -item[0],
                str(item[1].get("title", "")),
                str(item[1].get("relativePath", "")),
            )
        )
        if not scored:
            scored = [(0.0, entry) for entry in entries[:ASSISTANT_MAX_CANDIDATES]]
        return [entry for _score, entry in scored[:ASSISTANT_MAX_CANDIDATES]]

    def fallback_assistant_reply(
        self,
        *,
        query: str,
        candidates: list[dict[str, Any]],
        ai_state: dict[str, Any],
        current_tab: str,
    ) -> dict[str, Any]:
        if current_tab == "scad":
            scad_candidates = [entry for entry in candidates if entry.get("entryType") == "scad"]
            if scad_candidates:
                baked_candidates = [entry for entry in candidates if entry.get("entryType") != "scad"]
                candidates = scad_candidates + baked_candidates
        top_matches = []
        for entry in candidates[:6]:
            suggested_parameters: list[dict[str, str]] = []
            for parameter in entry.get("parameters", []):
                if not isinstance(parameter, dict) or not isinstance(parameter.get("name"), str):
                    continue
                lower_name = parameter["name"].lower()
                for key, reason in PARAMETER_REASON_HINTS:
                    if key in lower_name:
                        suggested_parameters.append(
                            {
                                "name": parameter["name"],
                                "reason": reason,
                                "suggestedValue": "",
                            }
                        )
                        break
                if len(suggested_parameters) >= 4:
                    break
            reason_parts = [f"{entry.get('sourceName')} / {entry.get('category')}"]
            ai_payload = entry.get("ai")
            if isinstance(ai_payload, dict) and isinstance(ai_payload.get("summary"), str):
                reason_parts.append(ai_payload["summary"])
            elif entry.get("entryType") == "scad":
                reason_parts.append(
                    f"{entry.get('parameterCount', 0)} parameters across {len(entry.get('groupNames', []))} groups."
                )
            else:
                file_format = str(entry.get("fileFormat", "")).upper() or "baked"
                reason_parts.append(f"{file_format} baked object.")
            top_matches.append(
                {
                    "id": entry.get("id"),
                    "reason": " ".join(reason_parts)[:220],
                    "suggestedParameters": suggested_parameters,
                }
            )
        ai_note = (
            "Ollama search suggestions are unavailable right now."
            if not ai_state.get("enabled")
            else "These are grounded local catalog matches because Ollama did not return a trustworthy ranked answer."
        )
        return {
            "reply": (
                f"Here are the closest local catalog matches I could find for '{query}'. "
                f"{ai_note}"
            )[:900],
            "followUp": "Open one of the customizable entries to review parameters and adjust dimensions.",
            "matches": top_matches,
            "assistantUsed": False,
        }

    def resolve_ai_modelfile(self, modelfile: str) -> Path:
        path = Path(modelfile).expanduser()
        if not path.is_absolute():
            path = self.workspace_root / path
        return path.resolve()

    def ensure_ollama_model(self, ai_state: dict[str, Any]) -> None:
        payload = ollama_request(
            base_url=ai_state["baseUrl"],
            timeout_seconds=ai_state["timeout"],
            endpoint="/api/tags",
            method="GET",
        )
        available_names = ollama_available_names(payload)
        if ai_state["model"] in available_names:
            return

        modelfile_text = str(ai_state.get("modelfile") or "").strip()
        if not modelfile_text:
            raise RuntimeError(f"model '{ai_state['model']}' is not available in local Ollama")
        modelfile_path = self.resolve_ai_modelfile(modelfile_text)
        if not modelfile_path.exists():
            raise RuntimeError(f"Modelfile does not exist: {modelfile_path}")
        ollama_bin = shutil.which("ollama")
        if not ollama_bin:
            raise RuntimeError("Ollama CLI is not installed")
        timeout_seconds = max(1800, int(ai_state["timeout"]) * 60)
        try:
            result = subprocess.run(
                [ollama_bin, "create", ai_state["model"], "-f", str(modelfile_path)],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(
                collapse_whitespace(str(exc)) or "automatic Ollama model creation failed"
            ) from exc
        if result.returncode != 0:
            detail = collapse_whitespace(result.stderr or result.stdout or "")[:240]
            raise RuntimeError(detail or f"ollama create exited with code {result.returncode}")

        payload = ollama_request(
            base_url=ai_state["baseUrl"],
            timeout_seconds=ai_state["timeout"],
            endpoint="/api/tags",
            method="GET",
        )
        if ai_state["model"] not in ollama_available_names(payload):
            raise RuntimeError(
                f"model '{ai_state['model']}' is still unavailable after automatic creation"
            )

    def ollama_assistant_reply(
        self,
        *,
        messages: list[dict[str, str]],
        candidates: list[dict[str, Any]],
        ai_state: dict[str, Any],
    ) -> dict[str, Any]:
        compact_candidates = [build_assistant_candidate(entry) for entry in candidates]
        candidate_ids = [item["id"] for item in compact_candidates if isinstance(item.get("id"), str)]
        candidates_by_id = {
            entry["id"]: entry
            for entry in candidates
            if isinstance(entry.get("id"), str) and entry.get("id") in candidate_ids
        }
        schema = {
            "type": "object",
            "properties": {
                "reply": {"type": "string"},
                "followUp": {"type": "string"},
                "matches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "reason": {"type": "string"},
                            "suggestedParameters": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "suggestedValue": {"type": "string"},
                                        "reason": {"type": "string"},
                                    },
                                    "required": ["name", "suggestedValue", "reason"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["id", "reason", "suggestedParameters"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["reply", "followUp", "matches"],
            "additionalProperties": False,
        }
        system_prompt = (
            "You are a grounded assistant for a local OpenSCAD catalog. "
            "Use only the supplied catalog candidates and chat history. "
            "Do not invent files, dimensions, load ratings, or print guarantees. "
            "Answer the user's request directly instead of summarizing the dataset. "
            "Prefer concrete part suggestions over abstract categorization. "
            "If the user wants to hold or mount an object, prefer holders, shelves, hooks, trays, "
            "mounts, brackets, channels, or customizable device-support parts over generic variants. "
            "Recommend parameter names only if they already exist on a candidate entry. "
            "Suggested values must be framed as starting points, not facts."
        )
        prompt = (
            "Given the conversation and the candidate catalog entries, return concise JSON.\n"
            "Requirements:\n"
            "- reply: short helpful answer grounded in the candidates\n"
            "- followUp: one short next-step suggestion\n"
            "- matches: 3 to 8 best candidates\n"
            "- each match.reason should explain why it fits the request\n"
            "- suggestedParameters should only reference real parameter names from that entry\n"
            "- only suggest parameter changes when they seem genuinely relevant\n\n"
            f"Conversation:\n{json.dumps(messages, indent=2)}\n\n"
            f"Candidates:\n{json.dumps(compact_candidates, indent=2)}"
        )
        response = ollama_request(
            base_url=ai_state["baseUrl"],
            timeout_seconds=ai_state["timeout"],
            endpoint="/api/generate",
            payload={
                "model": ai_state["model"],
                "system": system_prompt,
                "prompt": prompt,
                "format": schema,
                "stream": False,
                "think": False,
                "options": {"temperature": 0.2},
            },
        )
        response_text = response.get("response")
        if not isinstance(response_text, str) or not response_text.strip():
            raise RuntimeError("Assistant returned an empty response.")
        try:
            raw = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Assistant returned invalid JSON: {exc}") from exc
        normalized = normalize_assistant_response(
            raw if isinstance(raw, dict) else {},
            candidates_by_id,
        )
        reply_lower = normalized["reply"].lower()
        if (
            not normalized["matches"]
            or any(phrase in reply_lower for phrase in GENERIC_ASSISTANT_PHRASES)
        ):
            raise RuntimeError("Assistant response was too generic to trust.")
        normalized["assistantUsed"] = True
        return normalized

    def handle_assistant(self) -> None:
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
        if not isinstance(payload, dict):
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "Assistant payload must be a JSON object."},
            )
            return
        try:
            messages = assistant_messages_from_payload(payload)
            ai_state = self.effective_ai()
            current_tab = str(payload.get("currentTab", "")).strip().lower()
            if current_tab not in {"scad", "baked"}:
                current_tab = ""
            latest_user_message = next(
                (item["content"] for item in reversed(messages) if item["role"] == "user"),
                "",
            )
            candidates = self.assistant_candidates(query=latest_user_message, current_tab=current_tab)
            if ai_state["enabled"] and ai_state["provider"] == "ollama":
                self.ensure_ollama_model(ai_state)
                assistant_payload = self.ollama_assistant_reply(
                    messages=messages,
                    candidates=candidates,
                    ai_state=ai_state,
                )
            else:
                assistant_payload = self.fallback_assistant_reply(
                    query=latest_user_message,
                    candidates=candidates,
                    ai_state=ai_state,
                    current_tab=current_tab,
                )
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        except RuntimeError as exc:
            try:
                ai_state = self.effective_ai()
                latest_user_message = next(
                    (item["content"] for item in reversed(messages) if item["role"] == "user"),
                    "",
                )
                current_tab = str(payload.get("currentTab", "")).strip().lower()
                if current_tab not in {"scad", "baked"}:
                    current_tab = ""
                candidates = self.assistant_candidates(query=latest_user_message, current_tab=current_tab)
                assistant_payload = self.fallback_assistant_reply(
                    query=latest_user_message,
                    candidates=candidates,
                    ai_state=ai_state,
                    current_tab=current_tab,
                )
                assistant_payload["reply"] = (
                    f"{assistant_payload['reply']} Ollama error: {collapse_whitespace(str(exc))[:220]}"
                )[:900]
            except Exception:
                self.send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"ok": False, "error": collapse_whitespace(str(exc))[:260]},
                )
                return
        self.send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                **assistant_payload,
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
        rendered_command = " ".join(shlex.quote(part) for part in command)
        current_status = current_rescan_status()
        if current_status.get("active"):
            self.send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "started": False,
                    "alreadyRunning": True,
                    "status": current_status,
                },
            )
            return
        try:
            source_count = len(self.config_payload().get("sources", []))
        except ValueError:
            source_count = 0
        update_rescan_status(
            active=True,
            forced=force,
            startedAt=utc_timestamp(),
            finishedAt=None,
            current=0,
            total=0,
            lastLine="Starting rescan...",
            error=None,
            command=rendered_command,
            sourceCount=source_count,
            entryCount=0,
            pid=None,
        )
        worker = threading.Thread(
            target=self.run_rescan_job,
            args=(command, force, rendered_command),
            daemon=True,
        )
        worker.start()
        self.send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "started": True,
                "alreadyRunning": False,
                "forced": force,
                "command": rendered_command,
                "status": current_rescan_status(),
            },
        )

    def run_rescan_job(self, command: list[str], force: bool, rendered_command: str) -> None:
        try:
            process = subprocess.Popen(
                command,
                cwd=self.workspace_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            update_rescan_status(
                active=False,
                forced=force,
                finishedAt=utc_timestamp(),
                current=0,
                total=0,
                lastLine="Rescan failed to start.",
                error=collapse_whitespace(str(exc)) or "Rescan failed to start.",
                command=rendered_command,
                pid=None,
            )
            return
        update_rescan_status(pid=process.pid)
        captured_lines: list[str] = []
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            captured_lines.append(line)
            progress = parse_rescan_progress(line)
            updates: dict[str, Any] = {"lastLine": line}
            if progress is not None:
                updates["current"], updates["total"] = progress
            update_rescan_status(**updates)
        return_code = process.wait()
        error_text = None
        entry_count = 0
        final_status = current_rescan_status()
        source_count = final_status.get("sourceCount", 0)
        last_line = captured_lines[-1] if captured_lines else ""
        if return_code != 0:
            error_text = collapse_whitespace(" | ".join(captured_lines[-8:]))[:400]
            if not error_text:
                error_text = f"Rescan failed with exit code {return_code}."
        else:
            try:
                catalog = self.load_catalog_index()
                entry_count = len(catalog)
                source_count = len(self.config_payload().get("sources", []))
            except ValueError:
                entry_count = 0
        completed_current = final_status.get("current", 0)
        completed_total = final_status.get("total", 0)
        if return_code == 0 and completed_total > 0:
            completed_current = completed_total
        update_rescan_status(
            active=False,
            forced=force,
            finishedAt=utc_timestamp(),
            current=completed_current,
            total=completed_total,
            lastLine=last_line or ("Rescan complete." if return_code == 0 else "Rescan failed."),
            error=error_text,
            command=rendered_command,
            sourceCount=source_count,
            entryCount=entry_count,
            pid=None,
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
        if entry.get("entryType") != "scad":
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "Only SCAD entries support parameterized preview rendering."},
            )
            return
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
        if entry.get("entryType") != "scad":
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "Only SCAD entries support STL export."},
            )
            return
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
