#!/usr/bin/env python3
"""Build a local searchable catalog for one or more OpenSCAD libraries."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request


DEFAULT_CONFIG_PATH = "sources.json"
DEFAULT_OUTPUT_DIR = ".catalog"
DEFAULT_OPENSCAD_BIN = "openscad-nightly"
DEFAULT_SLICER_BIN = ""
HELPER_DIRS = {"Modules"}
IN_PROGRESS_DIRS = {"InProgress"}
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen3:4b-instruct"
DEFAULT_AI_TIMEOUT = 30
DEFAULT_AI_MAX_SOURCE_CHARS = 12000
DEFAULT_AI_MAX_COMMENT_CHARS = 3000
AI_PROMPT_VERSION = 1
SCAD_FILE_EXTENSION = ".scad"
BAKED_FILE_EXTENSIONS = {".stl", ".3mf"}
SUPPORTED_SOURCE_TYPES = {"scad", "stl", "mixed", "auto"}


@dataclass
class SourceConfig:
    id: str
    name: str
    source_type: str
    source_root: Path
    relative_root: str
    library_paths: list[Path]
    include_helpers: bool
    include_in_progress: bool
    include_deprecated: bool


@dataclass
class AIConfig:
    enabled: bool
    provider: str
    base_url: str
    model: str
    timeout_seconds: int
    include_scad: bool
    include_stl: bool
    max_source_chars: int
    max_comment_chars: int


@dataclass
class AIState:
    enabled: bool
    available: bool
    provider: str
    base_url: str
    model: str
    reason: str | None = None


@dataclass
class ToolConfig:
    openscad_bin: str
    slicer_bin: str


@dataclass
class Entry:
    id: str
    entry_type: str
    file_format: str
    source_id: str
    source_name: str
    source_path: Path
    relative_path: str
    category: str
    title: str
    parameters: list[dict[str, Any]]
    library_paths: list[str]
    tags: list[str]
    parameter_count: int
    group_names: list[str]
    parameter_names: list[str]
    option_values: list[str]
    metadata_path: str | None
    preview_path: str | None
    metadata_error: str | None
    preview_error: str | None
    ai_summary: str | None
    ai_use_cases: list[str]
    ai_search_terms: list[str]
    ai_parameter_hints: dict[str, dict[str, str]]
    ai_error: str | None

    def to_index_record(self) -> dict[str, Any]:
        ai_labels = [
            hint.get("label", "")
            for hint in self.ai_parameter_hints.values()
            if isinstance(hint, dict)
        ]
        ai_descriptions = [
            hint.get("description", "")
            for hint in self.ai_parameter_hints.values()
            if isinstance(hint, dict)
        ]
        search_parts = [
            self.source_name,
            self.title,
            self.relative_path,
            self.category,
            self.file_format,
            *self.tags,
            *self.group_names,
            *self.parameter_names,
            *self.option_values,
            self.ai_summary or "",
            *self.ai_use_cases,
            *self.ai_search_terms,
            *ai_labels,
            *ai_descriptions,
        ]
        ai_payload = None
        if (
            self.ai_summary
            or self.ai_use_cases
            or self.ai_search_terms
            or self.ai_parameter_hints
            or self.ai_error
        ):
            ai_payload = {
                "summary": self.ai_summary,
                "useCases": self.ai_use_cases,
                "searchTerms": self.ai_search_terms,
                "parameterHints": self.ai_parameter_hints,
                "error": self.ai_error,
            }
        return {
            "id": self.id,
            "entryType": self.entry_type,
            "fileFormat": self.file_format,
            "sourceId": self.source_id,
            "sourceName": self.source_name,
            "title": self.title,
            "relativePath": self.relative_path,
            "category": self.category,
            "parameters": self.parameters,
            "libraryPaths": self.library_paths,
            "tags": self.tags,
            "parameterCount": self.parameter_count,
            "groupNames": self.group_names,
            "parameterNames": self.parameter_names[:10],
            "metadataPath": self.metadata_path,
            "previewPath": self.preview_path,
            "metadataError": self.metadata_error,
            "previewError": self.preview_error,
            "ai": ai_payload,
            "searchText": " ".join(part for part in search_parts if part).lower(),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a searchable HTML catalog of OpenSCAD models."
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"JSON config file listing sources to scan (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to write catalog files into (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--openscad-bin",
        default=None,
        help="OpenSCAD executable override for metadata and preview export.",
    )
    parser.add_argument(
        "--imgsize",
        default="512,512",
        help="Preview PNG size in WIDTH,HEIGHT form (default: 512,512).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N discovered files.",
    )
    parser.add_argument(
        "--include-helpers",
        action="store_true",
        help="Include helper files under Modules/.",
    )
    parser.add_argument(
        "--include-in-progress",
        action="store_true",
        help="Include files in InProgress/ directories.",
    )
    parser.add_argument(
        "--include-deprecated",
        action="store_true",
        help="Include files whose names contain DEPRECATED.",
    )
    parser.add_argument(
        "--skip-previews",
        action="store_true",
        help="Only export metadata and HTML; do not render preview PNGs.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate metadata and previews even if cached files exist.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Timeout in seconds for each OpenSCAD invocation (default: 180).",
    )
    parser.add_argument(
        "--ai",
        action="store_true",
        help="Enable optional AI metadata enrichment during indexing.",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Disable AI metadata enrichment even if enabled in the config file.",
    )
    parser.add_argument(
        "--ollama-url",
        default=None,
        help=f"Ollama base URL override (default from config or {DEFAULT_OLLAMA_URL}).",
    )
    parser.add_argument(
        "--ollama-model",
        default=None,
        help=f"Ollama model override (default from config or {DEFAULT_OLLAMA_MODEL}).",
    )
    parser.add_argument(
        "--ai-timeout",
        type=int,
        default=None,
        help=f"AI request timeout in seconds (default from config or {DEFAULT_AI_TIMEOUT}).",
    )
    return parser.parse_args()


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().replace(os.sep, "__"))
    cleaned = cleaned.strip("-._") or "model"
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned}-{digest}"


def resolve_config_path(path_text: str, workspace_root: Path) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = (workspace_root / path).resolve()
    return path


def resolve_path(path_text: str, workspace_root: Path) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = (workspace_root / path).resolve()
    return path


def dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(path.resolve())
    return unique


def load_config_payload(config_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Source config is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("Source config root must be a JSON object.")
    return payload


def parse_positive_int(
    value: Any,
    *,
    field_name: str,
    default: int,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SystemExit(f"AI config field '{field_name}' must be a positive integer.")
    return value


def parse_bool(value: Any, *, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise SystemExit(f"AI config field '{field_name}' must be boolean.")
    return value


def load_ai_config(payload: dict[str, Any], args: argparse.Namespace) -> AIConfig:
    raw_ai = payload.get("ai", {})
    if raw_ai is None:
        raw_ai = {}
    if not isinstance(raw_ai, dict):
        raise SystemExit("Top-level 'ai' config must be an object when present.")

    provider = raw_ai.get("provider", "ollama")
    if not isinstance(provider, str) or not provider.strip():
        raise SystemExit("AI config field 'provider' must be a non-empty string.")
    provider = provider.strip().lower()

    base_url = raw_ai.get("baseUrl", DEFAULT_OLLAMA_URL)
    if args.ollama_url is not None:
        base_url = args.ollama_url
    if not isinstance(base_url, str) or not base_url.strip():
        raise SystemExit("AI config field 'baseUrl' must be a non-empty string.")

    model = raw_ai.get("model", DEFAULT_OLLAMA_MODEL)
    if args.ollama_model is not None:
        model = args.ollama_model
    if not isinstance(model, str) or not model.strip():
        raise SystemExit("AI config field 'model' must be a non-empty string.")

    timeout_seconds = parse_positive_int(
        args.ai_timeout if args.ai_timeout is not None else raw_ai.get("timeout"),
        field_name="timeout",
        default=DEFAULT_AI_TIMEOUT,
    )
    max_source_chars = parse_positive_int(
        raw_ai.get("maxSourceChars"),
        field_name="maxSourceChars",
        default=DEFAULT_AI_MAX_SOURCE_CHARS,
    )
    max_comment_chars = parse_positive_int(
        raw_ai.get("maxCommentChars"),
        field_name="maxCommentChars",
        default=DEFAULT_AI_MAX_COMMENT_CHARS,
    )

    enabled = parse_bool(raw_ai.get("enabled"), field_name="enabled", default=False)
    if args.ai:
        enabled = True
    if args.no_ai:
        enabled = False

    return AIConfig(
        enabled=enabled,
        provider=provider,
        base_url=base_url.rstrip("/"),
        model=model.strip(),
        timeout_seconds=timeout_seconds,
        include_scad=parse_bool(raw_ai.get("includeScad"), field_name="includeScad", default=True),
        include_stl=parse_bool(raw_ai.get("includeStl"), field_name="includeStl", default=False),
        max_source_chars=max_source_chars,
        max_comment_chars=max_comment_chars,
    )


def load_tool_config(payload: dict[str, Any], args: argparse.Namespace) -> ToolConfig:
    raw_tools = payload.get("tools", {})
    if raw_tools is None:
        raw_tools = {}
    if not isinstance(raw_tools, dict):
        raise SystemExit("Top-level 'tools' config must be an object when present.")

    openscad_bin = args.openscad_bin or raw_tools.get("openscadBin", DEFAULT_OPENSCAD_BIN)
    slicer_bin = raw_tools.get("slicerBin", DEFAULT_SLICER_BIN)

    if not isinstance(openscad_bin, str) or not openscad_bin.strip():
        raise SystemExit("Tools config field 'openscadBin' must be a non-empty string.")
    if slicer_bin is None:
        slicer_bin = ""
    if not isinstance(slicer_bin, str):
        raise SystemExit("Tools config field 'slicerBin' must be a string.")

    return ToolConfig(openscad_bin=openscad_bin.strip(), slicer_bin=slicer_bin.strip())


def load_sources(
    args: argparse.Namespace,
    workspace_root: Path,
) -> tuple[list[SourceConfig], AIConfig, ToolConfig]:
    config_path = resolve_config_path(args.config, workspace_root)
    if not config_path.exists():
        raise SystemExit(f"Source config does not exist: {config_path}")

    payload = load_config_payload(config_path)

    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise SystemExit("Source config must contain a non-empty 'sources' array.")

    sources: list[SourceConfig] = []
    for index, raw in enumerate(raw_sources, start=1):
        if not isinstance(raw, dict):
            raise SystemExit(f"Source #{index} must be an object.")

        name = raw.get("name") or raw.get("id")
        path_text = raw.get("path")
        if not isinstance(name, str) or not name.strip():
            raise SystemExit(f"Source #{index} is missing a valid 'name'.")
        if not isinstance(path_text, str) or not path_text.strip():
            raise SystemExit(f"Source '{name}' is missing a valid 'path'.")

        source_root = resolve_path(path_text, workspace_root)
        if not source_root.exists():
            raise SystemExit(f"Source '{name}' does not exist: {source_root}")
        if not source_root.is_dir():
            raise SystemExit(f"Source '{name}' is not a directory: {source_root}")

        source_type = raw.get("type", "mixed")
        if source_type not in SUPPORTED_SOURCE_TYPES:
            raise SystemExit(
                f"Source '{name}' has unsupported type '{source_type}'. "
                "Use one of: scad, stl, mixed, auto."
            )

        source_id = raw.get("id")
        if not isinstance(source_id, str) or not source_id.strip():
            source_id = slugify(name)[:40]

        raw_library_paths = raw.get("libraryPaths", [])
        if raw_library_paths is None:
            raw_library_paths = []
        if not isinstance(raw_library_paths, list) or any(
            not isinstance(item, str) for item in raw_library_paths
        ):
            raise SystemExit(f"Source '{name}' has an invalid 'libraryPaths' array.")

        library_paths = dedupe_paths(
            [resolve_path(item, workspace_root) for item in raw_library_paths]
        )
        relative_root = (
            str(source_root.relative_to(workspace_root))
            if source_root.is_relative_to(workspace_root)
            else str(source_root)
        )

        sources.append(
            SourceConfig(
                id=source_id,
                name=name,
                source_type="mixed",
                source_root=source_root,
                relative_root=relative_root,
                library_paths=library_paths,
                include_helpers=bool(raw.get("includeHelpers", args.include_helpers)),
                include_in_progress=bool(
                    raw.get("includeInProgress", args.include_in_progress)
                ),
                include_deprecated=bool(
                    raw.get("includeDeprecated", args.include_deprecated)
                ),
            )
        )

    return sources, load_ai_config(payload, args), load_tool_config(payload, args)


def classify_tags(path: Path) -> list[str]:
    tags: list[str] = []
    upper_name = path.name.upper()
    if any(part in HELPER_DIRS for part in path.parts):
        tags.append("helper")
    if any(part in IN_PROGRESS_DIRS or "inprogress" in part.lower() for part in path.parts):
        tags.append("in-progress")
    if "DEPRECATED" in upper_name:
        tags.append("deprecated")
    return tags


def should_include(
    path: Path,
    *,
    include_helpers: bool,
    include_in_progress: bool,
    include_deprecated: bool,
) -> bool:
    tags = set(classify_tags(path))
    if "helper" in tags and not include_helpers:
        return False
    if "in-progress" in tags and not include_in_progress:
        return False
    if "deprecated" in tags and not include_deprecated:
        return False
    return True


def path_entry_type(path: Path) -> str:
    return "scad" if path.suffix.lower() == SCAD_FILE_EXTENSION else "baked"


def path_file_format(path: Path) -> str:
    return path.suffix.lower().removeprefix(".")


def is_scad_path(path: Path) -> bool:
    return path.suffix.lower() == SCAD_FILE_EXTENSION


def is_baked_path(path: Path) -> bool:
    return path.suffix.lower() in BAKED_FILE_EXTENSIONS


def discover_files(source: SourceConfig, args: argparse.Namespace) -> list[Path]:
    scad_files = [
        path
        for path in sorted(source.source_root.rglob("*.scad"))
        if should_include(
            path.relative_to(source.source_root),
            include_helpers=source.include_helpers,
            include_in_progress=source.include_in_progress,
            include_deprecated=source.include_deprecated,
        )
    ]
    baked_files: list[Path] = []
    for extension in sorted(BAKED_FILE_EXTENSIONS):
        baked_files.extend(sorted(source.source_root.rglob(f"*{extension}")))
    return sorted(scad_files + baked_files)


def collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_leading_comment(source_text: str, max_chars: int) -> str:
    match = re.search(r"\A\s*(/\*.*?\*/|(?://[^\n]*\n)+)", source_text, re.DOTALL)
    if not match:
        return ""
    comment = match.group(1)
    comment = re.sub(r"^\s*//\s?", "", comment, flags=re.MULTILINE)
    comment = comment.replace("/*", " ").replace("*/", " ")
    return collapse_whitespace(comment)[:max_chars]


def read_text_excerpt(source_path: Path, max_chars: int) -> str:
    try:
        text = source_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    return text[:max_chars]


def ollama_request(
    ai_config: AIConfig,
    endpoint: str,
    payload: dict[str, Any] | None = None,
    *,
    method: str = "POST",
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib_request.Request(
        f"{ai_config.base_url}{endpoint}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib_request.urlopen(request, timeout=ai_config.timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except urllib_error.URLError as exc:
        raise RuntimeError(str(exc.reason) if hasattr(exc, "reason") else str(exc)) from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Ollama returned invalid JSON: {exc}") from exc

    if isinstance(parsed, dict) and isinstance(parsed.get("error"), str):
        raise RuntimeError(parsed["error"])
    if not isinstance(parsed, dict):
        raise RuntimeError("Ollama returned an unexpected response payload.")
    return parsed


def probe_ai(ai_config: AIConfig) -> AIState:
    if not ai_config.enabled:
        return AIState(
            enabled=False,
            available=False,
            provider=ai_config.provider,
            base_url=ai_config.base_url,
            model=ai_config.model,
            reason="disabled",
        )
    if ai_config.provider != "ollama":
        return AIState(
            enabled=True,
            available=False,
            provider=ai_config.provider,
            base_url=ai_config.base_url,
            model=ai_config.model,
            reason=f"unsupported AI provider '{ai_config.provider}'",
        )

    try:
        payload = ollama_request(ai_config, "/api/tags", method="GET")
    except RuntimeError as exc:
        return AIState(
            enabled=True,
            available=False,
            provider=ai_config.provider,
            base_url=ai_config.base_url,
            model=ai_config.model,
            reason=f"Ollama unavailable at {ai_config.base_url}: {exc}",
        )

    models = payload.get("models", [])
    available_names = {
        item.get("name")
        for item in models
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    available_names.update(
        item.get("model")
        for item in models
        if isinstance(item, dict) and isinstance(item.get("model"), str)
    )
    if ai_config.model not in available_names:
        return AIState(
            enabled=True,
            available=False,
            provider=ai_config.provider,
            base_url=ai_config.base_url,
            model=ai_config.model,
            reason=f"model '{ai_config.model}' is not available in local Ollama",
        )

    return AIState(
        enabled=True,
        available=True,
        provider=ai_config.provider,
        base_url=ai_config.base_url,
        model=ai_config.model,
    )


def build_ai_context(
    source_path: Path,
    source: SourceConfig,
    *,
    title: str,
    category: str,
    tags: list[str],
    parameters: list[dict[str, Any]],
    ai_config: AIConfig,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "sourceType": path_entry_type(source_path),
        "fileFormat": path_file_format(source_path),
        "sourceName": source.name,
        "relativePath": str(source_path.relative_to(source.source_root)),
        "title": title,
        "category": category,
        "tags": tags,
    }
    if is_scad_path(source_path):
        source_excerpt = read_text_excerpt(source_path, ai_config.max_source_chars)
        context["leadingComment"] = extract_leading_comment(
            source_excerpt,
            ai_config.max_comment_chars,
        )
        context["sourceExcerpt"] = source_excerpt
        context["parameters"] = [
            {
                "name": parameter.get("name"),
                "group": parameter.get("group"),
                "type": parameter.get("type"),
                "caption": parameter.get("caption"),
                "initial": parameter.get("initial"),
                "options": [
                    {
                        "name": option.get("name"),
                        "value": option.get("value"),
                    }
                    for option in parameter.get("options", [])
                    if isinstance(option, dict)
                ],
            }
            for parameter in parameters
            if isinstance(parameter, dict) and isinstance(parameter.get("name"), str)
        ]
    return context


def normalize_ai_enrichment(
    raw: dict[str, Any],
    *,
    parameter_names: set[str],
) -> dict[str, Any]:
    summary = collapse_whitespace(str(raw.get("summary", "")))[:320]

    use_cases: list[str] = []
    for value in raw.get("useCases", []):
        text = collapse_whitespace(str(value))[:100]
        if text and text not in use_cases:
            use_cases.append(text)

    search_terms: list[str] = []
    for value in raw.get("searchTerms", []):
        text = collapse_whitespace(str(value)).lower()[:64]
        if text and text not in search_terms:
            search_terms.append(text)

    parameter_hints: dict[str, dict[str, str]] = {}
    for item in raw.get("parameterHints", []):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or name not in parameter_names:
            continue
        label = collapse_whitespace(str(item.get("label", "")))[:80]
        description = collapse_whitespace(str(item.get("description", "")))[:180]
        if not label:
            continue
        parameter_hints[name] = {"label": label, "description": description}

    return {
        "summary": summary or None,
        "useCases": use_cases[:4],
        "searchTerms": search_terms[:10],
        "parameterHints": parameter_hints,
    }


def enrich_with_ai(
    *,
    ai_dir: Path,
    entry_id: str,
    source_path: Path,
    source: SourceConfig,
    title: str,
    category: str,
    tags: list[str],
    parameters: list[dict[str, Any]],
    ai_config: AIConfig,
    ai_state: AIState,
    force: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    if not ai_state.available:
        return None, None
    if is_scad_path(source_path) and not ai_config.include_scad:
        return None, None
    if is_baked_path(source_path) and not ai_config.include_stl:
        return None, None

    context = build_ai_context(
        source_path,
        source,
        title=title,
        category=category,
        tags=tags,
        parameters=parameters,
        ai_config=ai_config,
    )
    input_hash = hashlib.sha1(
        json.dumps(context, sort_keys=True).encode("utf-8")
    ).hexdigest()
    cache_path = ai_dir / f"{entry_id}.json"

    if not force and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cached = None
        if isinstance(cached, dict):
            meta = cached.get("_meta", {})
            enrichment = cached.get("enrichment")
            if (
                isinstance(meta, dict)
                and meta.get("inputHash") == input_hash
                and meta.get("model") == ai_state.model
                and meta.get("provider") == ai_state.provider
                and meta.get("promptVersion") == AI_PROMPT_VERSION
                and isinstance(enrichment, dict)
            ):
                return enrichment, None

    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "useCases": {"type": "array", "items": {"type": "string"}},
            "searchTerms": {"type": "array", "items": {"type": "string"}},
            "parameterHints": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "label": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["name", "label", "description"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["summary", "useCases", "searchTerms", "parameterHints"],
        "additionalProperties": False,
    }
    system_prompt = (
        "You generate cautious catalog metadata for local CAD files. "
        "Do not suggest editing source files. "
        "Use only the supplied context. "
        "If the purpose is unclear, stay generic rather than guessing. "
        "Friendly parameter labels must refer only to existing parameter names."
    )
    prompt = (
        "Return concise JSON metadata for this catalog entry.\n"
        "Requirements:\n"
        "- summary: 1 or 2 short sentences describing what the model appears to be for\n"
        "- useCases: up to 4 short practical uses\n"
        "- searchTerms: up to 10 lower-case search terms or synonyms\n"
        "- parameterHints: only for real parameters already present in the input\n"
        "- parameterHints.label should be friendlier than the raw variable name\n"
        "- parameterHints.description should explain the user-facing meaning in plain language\n"
        "- never invent manufacturing claims or dimensions not present in the input\n\n"
        f"Input:\n{json.dumps(context, indent=2, sort_keys=True)}"
    )

    try:
        response = ollama_request(
            ai_config,
            "/api/generate",
            {
                "model": ai_state.model,
                "system": system_prompt,
                "prompt": prompt,
                "format": schema,
                "stream": False,
                "think": False,
                "options": {"temperature": 0.2},
            },
        )
    except RuntimeError as exc:
        return None, f"AI enrichment failed: {exc}"

    response_text = response.get("response")
    if not isinstance(response_text, str) or not response_text.strip():
        return None, "AI enrichment returned an empty response"

    try:
        raw_enrichment = json.loads(response_text)
    except json.JSONDecodeError as exc:
        return None, f"AI enrichment returned invalid JSON: {exc}"

    enrichment = normalize_ai_enrichment(
        raw_enrichment if isinstance(raw_enrichment, dict) else {},
        parameter_names={
            parameter["name"]
            for parameter in parameters
            if isinstance(parameter, dict) and isinstance(parameter.get("name"), str)
        },
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "_meta": {
                    "generatedAt": datetime.now(timezone.utc).isoformat(),
                    "provider": ai_state.provider,
                    "model": ai_state.model,
                    "inputHash": input_hash,
                    "promptVersion": AI_PROMPT_VERSION,
                },
                "enrichment": enrichment,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return enrichment, None


def run_openscad(
    command: list[str],
    workspace_root: Path,
    timeout_seconds: int,
    library_paths: list[Path],
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if library_paths:
        env["OPENSCADPATH"] = os.pathsep.join(str(path) for path in library_paths)
    return subprocess.run(
        command,
        cwd=workspace_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )


def is_cache_valid(output_path: Path, source_path: Path) -> bool:
    if not output_path.exists():
        return False
    return output_path.stat().st_mtime >= source_path.stat().st_mtime


def export_metadata(
    source_path: Path,
    output_path: Path,
    openscad_bin: str,
    workspace_root: Path,
    force: bool,
    timeout_seconds: int,
    library_paths: list[Path],
) -> tuple[dict[str, Any] | None, str | None]:
    if not force and is_cache_valid(output_path, source_path):
        try:
            return json.loads(output_path.read_text(encoding="utf-8")), None
        except json.JSONDecodeError as exc:
            return None, f"cached metadata parse error: {exc}"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = run_openscad(
        [
            openscad_bin,
            "--export-format",
            "param",
            "-o",
            str(output_path),
            str(source_path),
        ],
        workspace_root,
        timeout_seconds,
        library_paths,
    )
    if result.returncode != 0:
        return None, compact_error(result)

    try:
        return json.loads(output_path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "metadata file was not created"
    except json.JSONDecodeError as exc:
        return None, f"metadata parse error: {exc}"


def render_preview(
    source_path: Path,
    output_path: Path,
    openscad_bin: str,
    workspace_root: Path,
    imgsize: str,
    force: bool,
    timeout_seconds: int,
    library_paths: list[Path],
    cache_source_path: Path | None = None,
) -> str | None:
    cache_path = cache_source_path or source_path
    if not force and is_cache_valid(output_path, cache_path):
        return None

    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = run_openscad(
        [
            openscad_bin,
            "--autocenter",
            "--viewall",
            "--imgsize",
            imgsize,
            "--backend",
            "Manifold",
            "--render=true",
            "-o",
            str(output_path),
            str(source_path),
        ],
        workspace_root,
        timeout_seconds,
        library_paths,
    )
    if result.returncode != 0:
        return compact_error(result)
    if not output_path.exists():
        return "preview file was not created"
    return None


def compact_error(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stderr or result.stdout or "").strip()
    if not text:
        return f"OpenSCAD failed with exit code {result.returncode}"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[:4])


def openscad_string_literal(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def ensure_baked_wrapper(wrapper_path: Path, baked_path: Path) -> None:
    wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    wrapper_path.write_text(
        "// Auto-generated for baked object preview rendering.\n"
        f'import("{openscad_string_literal(baked_path.resolve())}");\n',
        encoding="utf-8",
    )


def decode_scad_string_literal(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value


def collect_string_parameter_comments(source_text: str) -> dict[str, list[str]]:
    option_map: dict[str, list[str]] = {}
    assignment_pattern = re.compile(
        r'^\s*([A-Za-z_]\w*)\s*=\s*"((?:\\.|[^"\\])*)"\s*;?\s*(?://\s*(\[[^\n]*\]))?\s*$'
    )
    for line in source_text.splitlines():
        match = assignment_pattern.match(line)
        if not match:
            continue
        name, initial_text, comment_json = match.groups()
        if not comment_json:
            continue
        options: list[str] = []
        try:
            parsed = json.loads(comment_json)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            options.extend(item for item in parsed if item)
        initial_value = decode_scad_string_literal(initial_text)
        if initial_value and initial_value not in options:
            options.insert(0, initial_value)
        if options:
            option_map[name] = options
    return option_map


def collect_string_parameter_comparisons(source_text: str, parameter_name: str) -> list[str]:
    patterns = [
        re.compile(
            rf'\b{re.escape(parameter_name)}\b\s*(?:==|!=)\s*"((?:\\.|[^"\\])*)"'
        ),
        re.compile(
            rf'"((?:\\.|[^"\\])*)"\s*(?:==|!=)\s*\b{re.escape(parameter_name)}\b'
        ),
    ]
    values: list[str] = []
    for pattern in patterns:
        for match in pattern.finditer(source_text):
            value = decode_scad_string_literal(match.group(1))
            if value and value not in values:
                values.append(value)
    return values


def normalize_string_parameter_options(
    source_path: Path,
    parameters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not parameters:
        return parameters
    try:
        source_text = source_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return parameters

    explicit_comment_options = collect_string_parameter_comments(source_text)
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        if parameter.get("type") != "string":
            continue
        if parameter.get("options"):
            continue
        name = parameter.get("name")
        if not isinstance(name, str) or not name:
            continue

        option_values = explicit_comment_options.get(name, [])
        if not option_values:
            option_values = collect_string_parameter_comparisons(source_text, name)
            initial_value = parameter.get("initial")
            if isinstance(initial_value, str) and initial_value and initial_value not in option_values:
                option_values.insert(0, initial_value)
            if len(option_values) < 2:
                continue

        parameter["options"] = [
            {"name": option_value, "value": option_value}
            for option_value in option_values
        ]
    return parameters


def build_entry(
    source_path: Path,
    source: SourceConfig,
    output_dir: Path,
    metadata_dir: Path,
    preview_dir: Path,
    ai_dir: Path,
    args: argparse.Namespace,
    workspace_root: Path,
    ai_config: AIConfig,
    ai_state: AIState,
) -> Entry:
    if is_baked_path(source_path):
        return build_baked_entry(
            source_path=source_path,
            source=source,
            output_dir=output_dir,
            preview_dir=preview_dir,
            ai_dir=ai_dir,
            args=args,
            workspace_root=workspace_root,
            ai_config=ai_config,
            ai_state=ai_state,
        )

    return build_scad_entry(
        source_path=source_path,
        source=source,
        output_dir=output_dir,
        metadata_dir=metadata_dir,
        preview_dir=preview_dir,
        ai_dir=ai_dir,
        args=args,
        workspace_root=workspace_root,
        ai_config=ai_config,
        ai_state=ai_state,
    )


def build_scad_entry(
    source_path: Path,
    source: SourceConfig,
    output_dir: Path,
    metadata_dir: Path,
    preview_dir: Path,
    ai_dir: Path,
    args: argparse.Namespace,
    workspace_root: Path,
    ai_config: AIConfig,
    ai_state: AIState,
) -> Entry:
    rel_from_source_root = source_path.relative_to(source.source_root)
    category = rel_from_source_root.parts[0] if rel_from_source_root.parts else source.name
    tags = classify_tags(rel_from_source_root)
    entry_id = slugify(f"{source.id}:{rel_from_source_root.with_suffix('')}")
    slug = entry_id
    metadata_file = metadata_dir / f"{slug}.json"
    preview_file = preview_dir / f"{slug}.png"

    metadata, metadata_error = export_metadata(
        source_path=source_path,
        output_path=metadata_file,
        openscad_bin=args.openscad_bin,
        workspace_root=workspace_root,
        force=args.force,
        timeout_seconds=args.timeout,
        library_paths=source.library_paths,
    )

    title = metadata.get("title") if metadata else source_path.stem
    parameters = metadata.get("parameters", []) if metadata else []
    if isinstance(parameters, list):
        parameters = normalize_string_parameter_options(source_path, parameters)
    group_names = sorted(
        {param.get("group", "Ungrouped") for param in parameters if isinstance(param, dict)}
    )
    parameter_names = [
        param["name"]
        for param in parameters
        if isinstance(param, dict) and isinstance(param.get("name"), str)
    ]
    option_values = []
    for param in parameters:
        if not isinstance(param, dict):
            continue
        for option in param.get("options", []):
            if isinstance(option, dict):
                option_name = option.get("name")
                if isinstance(option_name, str):
                    option_values.append(option_name)

    ai_enrichment, ai_error = enrich_with_ai(
        ai_dir=ai_dir,
        entry_id=entry_id,
        source_path=source_path,
        source=source,
        title=title,
        category=category,
        tags=tags,
        parameters=parameters,
        ai_config=ai_config,
        ai_state=ai_state,
        force=args.force,
    )

    preview_error = None
    if not args.skip_previews:
        preview_error = render_preview(
            source_path=source_path,
            output_path=preview_file,
            openscad_bin=args.openscad_bin,
            workspace_root=workspace_root,
            imgsize=args.imgsize,
            force=args.force,
            timeout_seconds=args.timeout,
            library_paths=source.library_paths,
        )

    metadata_path = (
        os.path.relpath(metadata_file, output_dir) if metadata_file.exists() else None
    )
    preview_path = (
        os.path.relpath(preview_file, output_dir) if preview_file.exists() else None
    )

    return Entry(
        id=entry_id,
        entry_type="scad",
        file_format=path_file_format(source_path),
        source_id=source.id,
        source_name=source.name,
        source_path=source_path,
        relative_path=str(rel_from_source_root),
        category=category,
        title=title,
        parameters=parameters,
        library_paths=[str(path) for path in source.library_paths],
        tags=tags,
        parameter_count=len(parameter_names),
        group_names=group_names,
        parameter_names=parameter_names,
        option_values=option_values,
        metadata_path=metadata_path,
        preview_path=preview_path,
        metadata_error=metadata_error,
        preview_error=preview_error,
        ai_summary=ai_enrichment.get("summary") if ai_enrichment else None,
        ai_use_cases=ai_enrichment.get("useCases", []) if ai_enrichment else [],
        ai_search_terms=ai_enrichment.get("searchTerms", []) if ai_enrichment else [],
        ai_parameter_hints=ai_enrichment.get("parameterHints", {}) if ai_enrichment else {},
        ai_error=ai_error,
    )


def build_baked_entry(
    source_path: Path,
    source: SourceConfig,
    output_dir: Path,
    preview_dir: Path,
    ai_dir: Path,
    args: argparse.Namespace,
    workspace_root: Path,
    ai_config: AIConfig,
    ai_state: AIState,
) -> Entry:
    rel_from_source_root = source_path.relative_to(source.source_root)
    category = rel_from_source_root.parts[0] if rel_from_source_root.parts else source.name
    entry_id = slugify(f"{source.id}:{rel_from_source_root.with_suffix('')}")
    preview_file = preview_dir / f"{entry_id}.png"
    wrapper_file = output_dir / "wrappers" / f"{entry_id}.scad"
    file_format = path_file_format(source_path)

    preview_error = None
    if not args.skip_previews:
        ensure_baked_wrapper(wrapper_file, source_path)
        preview_error = render_preview(
            source_path=wrapper_file,
            output_path=preview_file,
            openscad_bin=args.openscad_bin,
            workspace_root=workspace_root,
            imgsize=args.imgsize,
            force=args.force,
            timeout_seconds=args.timeout,
            library_paths=[],
            cache_source_path=source_path,
        )

    preview_path = (
        os.path.relpath(preview_file, output_dir) if preview_file.exists() else None
    )
    ai_enrichment, ai_error = enrich_with_ai(
        ai_dir=ai_dir,
        entry_id=entry_id,
        source_path=source_path,
        source=source,
        title=source_path.stem,
        category=category,
        tags=[],
        parameters=[],
        ai_config=ai_config,
        ai_state=ai_state,
        force=args.force,
    )

    return Entry(
        id=entry_id,
        entry_type="baked",
        file_format=file_format,
        source_id=source.id,
        source_name=source.name,
        source_path=source_path,
        relative_path=str(rel_from_source_root),
        category=category,
        title=source_path.stem,
        parameters=[],
        library_paths=[str(path) for path in source.library_paths],
        tags=[file_format],
        parameter_count=0,
        group_names=[],
        parameter_names=[],
        option_values=[],
        metadata_path=None,
        preview_path=preview_path,
        metadata_error=None,
        preview_error=preview_error,
        ai_summary=ai_enrichment.get("summary") if ai_enrichment else None,
        ai_use_cases=ai_enrichment.get("useCases", []) if ai_enrichment else [],
        ai_search_terms=ai_enrichment.get("searchTerms", []) if ai_enrichment else [],
        ai_parameter_hints=ai_enrichment.get("parameterHints", {}) if ai_enrichment else {},
        ai_error=ai_error,
    )


def write_catalog_json(output_dir: Path, payload: dict[str, Any]) -> None:
    (output_dir / "catalog.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def html_template(payload: dict[str, Any]) -> str:
    data_json = json.dumps(payload, separators=(",", ":"))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SCAD Library Catalog</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4efe7;
      --panel: #fffaf2;
      --panel-strong: #f2e7d7;
      --border: #d4c4ae;
      --text: #1f1d19;
      --muted: #63594b;
      --accent: #0d6f63;
      --accent-2: #c85d2f;
      --accent-3: #17487a;
      --shadow: 0 18px 48px rgba(49, 36, 18, 0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(13, 111, 99, 0.16), transparent 28rem),
        radial-gradient(circle at top right, rgba(200, 93, 47, 0.14), transparent 24rem),
        linear-gradient(180deg, #f6f1ea 0%, #efe5d8 100%);
    }}
    main {{
      width: min(1400px, calc(100vw - 2rem));
      margin: 0 auto;
      padding: 1.5rem 0 3rem;
    }}
    .hero {{
      background: linear-gradient(135deg, rgba(255,255,255,0.8), rgba(247,236,219,0.92));
      border: 1px solid rgba(212, 196, 174, 0.9);
      border-radius: 1.25rem;
      box-shadow: var(--shadow);
      padding: 1.5rem;
      margin-bottom: 1rem;
      backdrop-filter: blur(10px);
    }}
    h1 {{
      margin: 0 0 0.5rem;
      font-size: clamp(2rem, 5vw, 3.2rem);
      line-height: 0.95;
      letter-spacing: -0.04em;
    }}
    .lede {{
      margin: 0;
      max-width: 60rem;
      color: var(--muted);
      line-height: 1.5;
    }}
    .toolbar {{
      display: grid;
      grid-template-columns: minmax(14rem, 2fr) minmax(10rem, 1fr) minmax(10rem, 1fr);
      gap: 0.75rem;
      margin: 1rem 0 0;
    }}
    input, select {{
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 0.9rem;
      padding: 0.95rem 1rem;
      font: inherit;
      background: rgba(255,255,255,0.9);
      color: var(--text);
    }}
    .summary {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.6rem;
      margin: 1rem 0 1.25rem;
    }}
    .top-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      align-items: center;
      margin-bottom: 1rem;
    }}
    .tabs {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      margin-bottom: 1rem;
    }}
    .chip {{
      padding: 0.45rem 0.7rem;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.75);
      color: var(--muted);
      font-size: 0.92rem;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 1rem;
    }}
    .card {{
      display: flex;
      flex-direction: column;
      overflow: hidden;
      min-height: 100%;
      background: var(--panel);
      border: 1px solid rgba(212, 196, 174, 0.9);
      border-radius: 1.1rem;
      box-shadow: var(--shadow);
    }}
    .preview {{
      aspect-ratio: 1 / 1;
      display: grid;
      place-items: center;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.2), rgba(0,0,0,0)),
        linear-gradient(135deg, #ecf5f4 0%, #efe3d4 100%);
      border-bottom: 1px solid rgba(212, 196, 174, 0.9);
    }}
    .preview img {{
      width: 100%;
      height: 100%;
      object-fit: contain;
    }}
    .preview-fallback {{
      padding: 1rem;
      text-align: center;
      color: var(--muted);
      line-height: 1.45;
    }}
    .content {{
      display: flex;
      flex: 1;
      flex-direction: column;
      gap: 0.8rem;
      padding: 1rem;
    }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      color: var(--accent);
      font-size: 0.85rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 700;
    }}
    .title {{
      margin: 0;
      font-size: 1.25rem;
      line-height: 1.1;
    }}
    .path {{
      margin: 0;
      color: var(--muted);
      font-family: "IBM Plex Mono", "Consolas", monospace;
      font-size: 0.85rem;
      word-break: break-word;
    }}
    .description {{
      margin: 0;
      color: var(--muted);
      line-height: 1.45;
    }}
    .stats {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
    }}
    .tag-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.45rem;
    }}
    .tag {{
      padding: 0.28rem 0.55rem;
      border-radius: 999px;
      background: var(--panel-strong);
      color: var(--text);
      font-size: 0.82rem;
    }}
    .issues {{
      color: var(--accent-2);
      font-size: 0.88rem;
      line-height: 1.4;
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      margin-top: auto;
      padding-top: 0.2rem;
    }}
    button {{
      border: 1px solid var(--border);
      border-radius: 0.9rem;
      padding: 0.8rem 1rem;
      font: inherit;
      background: rgba(255,255,255,0.9);
      color: var(--text);
      cursor: pointer;
    }}
    button.primary {{
      background: var(--accent);
      border-color: var(--accent);
      color: white;
    }}
    button.secondary {{
      background: rgba(13, 111, 99, 0.08);
      border-color: rgba(13, 111, 99, 0.28);
      color: var(--accent);
    }}
    button.ghost {{
      background: transparent;
    }}
    button.tab-active {{
      background: var(--accent-3);
      border-color: var(--accent-3);
      color: white;
    }}
    button:disabled {{
      opacity: 0.55;
      cursor: not-allowed;
    }}
    a {{
      color: var(--accent);
      text-decoration: none;
      font-weight: 600;
    }}
    a:hover {{
      text-decoration: underline;
    }}
    .empty {{
      display: none;
      background: rgba(255,255,255,0.72);
      border: 1px dashed var(--border);
      border-radius: 1rem;
      padding: 1.5rem;
      color: var(--muted);
    }}
    .footer {{
      margin-top: 1rem;
      color: var(--muted);
      font-size: 0.9rem;
    }}
    dialog {{
      width: min(1100px, calc(100vw - 1rem));
      max-width: 1100px;
      border: none;
      border-radius: 1.2rem;
      padding: 0;
      background: transparent;
    }}
    dialog::backdrop {{
      background: rgba(16, 12, 7, 0.45);
      backdrop-filter: blur(4px);
    }}
    .modal {{
      display: grid;
      grid-template-columns: minmax(260px, 380px) minmax(0, 1fr);
      min-height: min(88vh, 900px);
      overflow: hidden;
      background: #fcf8f1;
      border: 1px solid rgba(212, 196, 174, 0.9);
      border-radius: 1.2rem;
      box-shadow: var(--shadow);
    }}
    .modal-pane {{
      padding: 1rem;
      overflow: auto;
    }}
    .modal-pane.preview-pane {{
      background:
        radial-gradient(circle at top left, rgba(13, 111, 99, 0.16), transparent 20rem),
        linear-gradient(180deg, #f3ece0 0%, #e9dcc8 100%);
      border-right: 1px solid rgba(212, 196, 174, 0.9);
    }}
    .modal-toolbar {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.6rem;
      margin: 0.75rem 0 0;
    }}
    .modal-preview {{
      display: grid;
      place-items: center;
      width: 100%;
      aspect-ratio: 1 / 1;
      background: rgba(255,255,255,0.72);
      border: 1px solid rgba(212, 196, 174, 0.9);
      border-radius: 1rem;
      overflow: hidden;
    }}
    .modal-preview img {{
      width: 100%;
      height: 100%;
      object-fit: contain;
    }}
    .modal-title {{
      margin: 0.2rem 0 0;
      font-size: 1.65rem;
      line-height: 1.02;
      letter-spacing: -0.03em;
    }}
    .modal-subtitle {{
      margin: 0.35rem 0 0;
      color: var(--muted);
      font-family: "IBM Plex Mono", "Consolas", monospace;
      font-size: 0.88rem;
      word-break: break-word;
    }}
    .server-status {{
      margin: 0.8rem 0 0;
      color: var(--muted);
      font-size: 0.92rem;
      line-height: 1.45;
    }}
    .server-status.online {{
      color: var(--accent);
    }}
    .server-status.offline {{
      color: var(--accent-2);
    }}
    .param-groups {{
      display: grid;
      gap: 1rem;
    }}
    .group {{
      border: 1px solid rgba(212, 196, 174, 0.9);
      border-radius: 1rem;
      padding: 0.9rem;
      background: rgba(255,255,255,0.7);
    }}
    .group h3 {{
      margin: 0 0 0.65rem;
      font-size: 1rem;
    }}
    .fields {{
      display: grid;
      gap: 0.75rem;
    }}
    .field {{
      display: grid;
      gap: 0.35rem;
    }}
    .field label {{
      font-weight: 600;
      font-size: 0.94rem;
    }}
    .field-help {{
      color: var(--muted);
      font-size: 0.84rem;
      line-height: 1.35;
    }}
    .field-meta {{
      color: var(--muted);
      font-size: 0.8rem;
      font-family: "IBM Plex Mono", "Consolas", monospace;
    }}
    .checkbox-row {{
      display: flex;
      align-items: center;
      gap: 0.7rem;
      padding: 0.8rem 0.9rem;
      border: 1px solid var(--border);
      border-radius: 0.9rem;
      background: rgba(255,255,255,0.85);
    }}
    .checkbox-row input {{
      width: auto;
      margin: 0;
    }}
    .command-box {{
      margin: 0;
      padding: 0.9rem;
      white-space: pre-wrap;
      word-break: break-word;
      border: 1px solid rgba(212, 196, 174, 0.9);
      border-radius: 1rem;
      background: #1f241f;
      color: #f4f1e7;
      font-family: "IBM Plex Mono", "Consolas", monospace;
      font-size: 0.84rem;
      line-height: 1.45;
    }}
    .status-box {{
      min-height: 1.5rem;
      color: var(--muted);
      font-size: 0.92rem;
      line-height: 1.45;
    }}
    .status-box.error {{
      color: var(--accent-2);
    }}
    .status-box.success {{
      color: var(--accent-3);
    }}
    .settings-list {{
      display: grid;
      gap: 1rem;
    }}
    .settings-card {{
      border: 1px solid rgba(212, 196, 174, 0.9);
      border-radius: 1rem;
      padding: 1rem;
      background: rgba(255,255,255,0.74);
    }}
    .settings-card h3 {{
      margin: 0 0 0.75rem;
      font-size: 1rem;
    }}
    .settings-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      margin-top: 1rem;
    }}
    .small-note {{
      color: var(--muted);
      font-size: 0.88rem;
      line-height: 1.45;
    }}
    .scan-indicator {{
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .scan-dot {{
      width: 0.7rem;
      height: 0.7rem;
      border-radius: 999px;
      background: var(--accent-2);
      box-shadow: 0 0 0 rgba(200, 93, 47, 0.4);
      animation: pulse 1.4s ease-in-out infinite;
    }}
    @keyframes pulse {{
      0%, 100% {{ box-shadow: 0 0 0 0 rgba(200, 93, 47, 0.35); }}
      50% {{ box-shadow: 0 0 0 0.5rem rgba(200, 93, 47, 0); }}
    }}
    .assistant-modal {{
      grid-template-columns: minmax(0, 1.4fr) minmax(280px, 0.9fr);
      min-height: min(86vh, 860px);
    }}
    .assistant-pane {{
      display: grid;
      grid-template-rows: auto auto minmax(0, 1fr) auto;
      gap: 0.9rem;
    }}
    .assistant-results-pane {{
      border-left: 1px solid rgba(212, 196, 174, 0.9);
      background: rgba(255,255,255,0.55);
    }}
    .assistant-thread {{
      display: grid;
      gap: 0.75rem;
      align-content: start;
      min-height: 0;
      overflow: auto;
      padding-right: 0.2rem;
    }}
    .assistant-message {{
      max-width: min(90%, 42rem);
      border-radius: 1rem;
      padding: 0.85rem 1rem;
      line-height: 1.5;
      border: 1px solid rgba(212, 196, 174, 0.9);
      background: rgba(255,255,255,0.86);
    }}
    .assistant-message.user {{
      margin-left: auto;
      background: rgba(13, 111, 99, 0.12);
      border-color: rgba(13, 111, 99, 0.24);
    }}
    .assistant-message.assistant {{
      margin-right: auto;
    }}
    .assistant-composer {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 0.75rem;
      align-items: start;
    }}
    .assistant-composer textarea {{
      width: 100%;
      min-height: 6rem;
      resize: vertical;
      border: 1px solid var(--border);
      border-radius: 1rem;
      padding: 0.95rem 1rem;
      font: inherit;
      background: rgba(255,255,255,0.92);
      color: var(--text);
    }}
    .assistant-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.6rem;
    }}
    .assistant-results {{
      display: grid;
      gap: 0.85rem;
      align-content: start;
    }}
    .assistant-result-card {{
      border: 1px solid rgba(212, 196, 174, 0.9);
      border-radius: 1rem;
      padding: 0.9rem;
      background: rgba(255,255,255,0.78);
      display: grid;
      gap: 0.55rem;
    }}
    .assistant-result-card h3 {{
      margin: 0;
      font-size: 1rem;
      line-height: 1.15;
    }}
    .assistant-result-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.55rem;
    }}
    .assistant-suggestions {{
      display: grid;
      gap: 0.45rem;
    }}
    .assistant-suggestion {{
      color: var(--muted);
      font-size: 0.86rem;
      line-height: 1.4;
    }}
    .assistant-empty {{
      color: var(--muted);
      font-size: 0.92rem;
      line-height: 1.45;
      padding: 0.2rem 0;
    }}
    @media (max-width: 720px) {{
      .toolbar {{
        grid-template-columns: 1fr;
      }}
      dialog {{
        width: calc(100vw - 0.5rem);
      }}
      .modal {{
        grid-template-columns: 1fr;
      }}
      .modal-pane.preview-pane {{
        border-right: none;
        border-bottom: 1px solid rgba(212, 196, 174, 0.9);
      }}
      .assistant-results-pane {{
        border-left: none;
        border-top: 1px solid rgba(212, 196, 174, 0.9);
      }}
      .assistant-composer {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <h1>SCAD Library Catalog</h1>
      <p class="lede">
        Search one or more local OpenSCAD libraries by source, folder, parameter group, parameter name,
        and option label. Point it at your own folders, Git clones, or any mix you want.
      </p>
      <div class="toolbar">
        <input id="search" type="search" placeholder="Search: bin, shelf, hook, Multiconnect, Board_Width, GOEWS...">
        <select id="source">
          <option value="">All sources</option>
        </select>
        <select id="category">
          <option value="">All categories</option>
        </select>
      </div>
      <div class="summary" id="summary"></div>
    </section>
    <div class="top-actions">
      <button class="secondary" id="assistant-btn" type="button">Assistant</button>
      <button class="ghost" id="clear-assistant-filter-btn" type="button" hidden>Clear Assistant Filter</button>
      <button class="secondary" id="settings-btn" type="button">Configure / Scan</button>
      <div class="scan-indicator" id="scan-indicator" hidden>
        <span class="scan-dot"></span>
        <span id="scan-indicator-text">Scan running...</span>
      </div>
    </div>
    <div class="tabs">
      <button class="secondary tab-active" id="tab-scad-btn" type="button">Customizable SCAD</button>
      <button class="secondary" id="tab-baked-btn" type="button">Baked Object</button>
    </div>
    <section class="grid" id="grid"></section>
    <section class="empty" id="empty">
      No models matched the current filter. Try a broader term, or add more source folders from
      the configuration dialog.
    </section>
    <p class="footer" id="footer"></p>
  </main>
  <dialog id="customizer">
    <div class="modal">
      <section class="modal-pane preview-pane">
        <div class="eyebrow" id="modal-category"></div>
        <h2 class="modal-title" id="modal-title"></h2>
        <p class="modal-subtitle" id="modal-path"></p>
        <div class="modal-preview" id="modal-preview"></div>
        <div class="server-status" id="server-status"></div>
        <div class="modal-toolbar">
          <button class="primary" id="render-preview-btn" type="button">Render Preview</button>
          <button class="secondary" id="export-stl-btn" type="button">Export Binary STL</button>
          <button class="secondary" id="open-scad-btn" type="button">Open In OpenSCAD</button>
          <button class="ghost" id="copy-command-btn" type="button">Copy Command</button>
          <button class="ghost" id="reset-params-btn" type="button">Reset</button>
          <button class="ghost" id="close-modal-btn" type="button">Close</button>
        </div>
      </section>
      <section class="modal-pane">
        <div class="status-box" id="action-status"></div>
        <div class="param-groups" id="param-groups"></div>
        <h3>OpenSCAD Command</h3>
        <pre class="command-box" id="command-box"></pre>
      </section>
    </div>
  </dialog>
  <dialog id="settings-dialog">
    <div class="modal">
      <section class="modal-pane preview-pane">
        <div class="eyebrow">Library Config</div>
        <h2 class="modal-title">Configure / Scan</h2>
        <p class="small-note" id="config-path-note"></p>
        <div class="status-box" id="settings-status"></div>
        <div class="settings-actions">
          <button class="primary" id="save-settings-btn" type="button">Save Sources</button>
          <button class="secondary" id="save-rescan-btn" type="button">Save And Rescan</button>
          <button class="secondary" id="save-force-rescan-btn" type="button">Save And Force Rebuild</button>
          <button class="ghost" id="add-source-btn" type="button">Add Source</button>
          <button class="ghost" id="close-settings-btn" type="button">Close</button>
        </div>
        <p class="small-note" id="rescan-note">
          Once started, rescans continue even if you close this window or reload the page.
        </p>
      </section>
      <section class="modal-pane">
        <div class="settings-list" id="settings-list"></div>
      </section>
    </div>
  </dialog>
  <dialog id="assistant-dialog">
    <div class="modal assistant-modal">
      <section class="modal-pane assistant-pane">
        <div class="eyebrow">Assistant</div>
        <h2 class="modal-title">Catalog Assistant</h2>
        <div class="status-box" id="assistant-status"></div>
        <div class="assistant-thread" id="assistant-thread"></div>
        <div class="assistant-composer">
          <textarea id="assistant-input" placeholder="Ask for parts, use cases, or starting customizations."></textarea>
          <div class="assistant-actions">
            <button class="primary" id="assistant-send-btn" type="button">Send</button>
            <button class="ghost" id="assistant-close-btn" type="button">Close</button>
          </div>
        </div>
      </section>
      <section class="modal-pane assistant-results-pane">
        <div class="eyebrow">Matches</div>
        <div class="assistant-results" id="assistant-results"></div>
      </section>
    </div>
  </dialog>
  <script id="catalog-data" type="application/json">{data_json}</script>
  <script>
    const payload = JSON.parse(document.getElementById("catalog-data").textContent);
    const entries = payload.entries;
    const searchInput = document.getElementById("search");
    const sourceSelect = document.getElementById("source");
    const categorySelect = document.getElementById("category");
    const grid = document.getElementById("grid");
    const summary = document.getElementById("summary");
    const empty = document.getElementById("empty");
    const footer = document.getElementById("footer");
    const customizer = document.getElementById("customizer");
    const settingsDialog = document.getElementById("settings-dialog");
    const assistantDialog = document.getElementById("assistant-dialog");
    const modalCategory = document.getElementById("modal-category");
    const modalTitle = document.getElementById("modal-title");
    const modalPath = document.getElementById("modal-path");
    const modalPreview = document.getElementById("modal-preview");
    const paramGroups = document.getElementById("param-groups");
    const commandBox = document.getElementById("command-box");
    const actionStatus = document.getElementById("action-status");
    const serverStatus = document.getElementById("server-status");
    const renderPreviewBtn = document.getElementById("render-preview-btn");
    const exportStlBtn = document.getElementById("export-stl-btn");
    const openScadBtn = document.getElementById("open-scad-btn");
    const copyCommandBtn = document.getElementById("copy-command-btn");
    const resetParamsBtn = document.getElementById("reset-params-btn");
    const closeModalBtn = document.getElementById("close-modal-btn");
    const assistantBtn = document.getElementById("assistant-btn");
    const clearAssistantFilterBtn = document.getElementById("clear-assistant-filter-btn");
    const settingsBtn = document.getElementById("settings-btn");
    const saveSettingsBtn = document.getElementById("save-settings-btn");
    const saveRescanBtn = document.getElementById("save-rescan-btn");
    const saveForceRescanBtn = document.getElementById("save-force-rescan-btn");
    const addSourceBtn = document.getElementById("add-source-btn");
    const closeSettingsBtn = document.getElementById("close-settings-btn");
    const settingsList = document.getElementById("settings-list");
    const settingsStatus = document.getElementById("settings-status");
    const configPathNote = document.getElementById("config-path-note");
    const scanIndicator = document.getElementById("scan-indicator");
    const scanIndicatorText = document.getElementById("scan-indicator-text");
    const tabScadBtn = document.getElementById("tab-scad-btn");
    const tabBakedBtn = document.getElementById("tab-baked-btn");
    const assistantStatus = document.getElementById("assistant-status");
    const assistantThread = document.getElementById("assistant-thread");
    const assistantResults = document.getElementById("assistant-results");
    const assistantInput = document.getElementById("assistant-input");
    const assistantSendBtn = document.getElementById("assistant-send-btn");
    const assistantCloseBtn = document.getElementById("assistant-close-btn");
    const aiMetadata = payload.ai || null;

    let currentEntry = null;
    let currentValues = {{}};
    let serverAvailable = false;
    let editableConfig = null;
    let configPath = "";
    let activeTab = "scad";
    let assistantMessages = [];
    let assistantMatches = [];
    let assistantFilterIds = null;
    let assistantBusy = false;
    let rescanStatus = null;
    let rescanPollTimer = null;
    let rescanReloadOnComplete = false;
    const entryById = new Map(entries.map((entry) => [entry.id, entry]));

    const sourceNames = [...new Set(entries.map((entry) => entry.sourceName))].sort();
    for (const sourceName of sourceNames) {{
      const option = document.createElement("option");
      option.value = sourceName;
      option.textContent = sourceName;
      sourceSelect.appendChild(option);
    }}

    const categories = [...new Set(entries.map((entry) => entry.category))].sort();
    for (const category of categories) {{
      const option = document.createElement("option");
      option.value = category;
      option.textContent = category;
      categorySelect.appendChild(option);
    }}

    function renderSummary(filtered) {{
      const previewCount = filtered.filter((entry) => entry.previewPath).length;
      const errorCount = filtered.filter((entry) => entry.metadataError || entry.previewError).length;
      const aiCount = filtered.filter((entry) => entry.ai && (entry.ai.summary || entry.ai.searchTerms?.length)).length;
      const scadCount = entries.filter((entry) => entry.entryType === "scad").length;
      const bakedCount = entries.filter((entry) => entry.entryType === "baked").length;
      const stlCount = entries.filter((entry) => entry.fileFormat === "stl").length;
      const threeMfCount = entries.filter((entry) => entry.fileFormat === "3mf").length;
      summary.innerHTML = "";
      const chips = [
        activeTab === "scad" ? "Customizable SCAD" : "Baked Object",
        `${{filtered.length}} shown`,
        `${{entries.length}} indexed`,
        `${{sourceNames.length}} sources`,
        `${{scadCount}} scad`,
        `${{bakedCount}} baked`,
        `${{stlCount}} stl`,
        `${{threeMfCount}} 3mf`,
        `${{previewCount}} with previews`,
        `${{aiCount}} AI-tagged`,
        `${{errorCount}} with export issues`,
      ];
      if (assistantFilterIds?.size) {{
        chips.splice(1, 0, `${{assistantFilterIds.size}} assistant picks`);
      }}
      for (const label of chips) {{
        const span = document.createElement("span");
        span.className = "chip";
        span.textContent = label;
        summary.appendChild(span);
      }}
    }}

    function makeTag(text) {{
      const span = document.createElement("span");
      span.className = "tag";
      span.textContent = text;
      return span;
    }}

    function fileFormatLabel(entry) {{
      return String(entry.fileFormat || "").toUpperCase();
    }}

    function bakedDownloadLabel(entry) {{
      const formatLabel = fileFormatLabel(entry);
      return formatLabel ? `Download ${{formatLabel}}` : "Download file";
    }}

    function makeCard(entry) {{
      const card = document.createElement("article");
      card.className = "card";

      const preview = document.createElement("div");
      preview.className = "preview";
      if (entry.previewPath) {{
        const img = document.createElement("img");
        img.src = entry.previewPath;
        img.alt = `${{entry.title}} preview`;
        preview.appendChild(img);
      }} else {{
        const fallback = document.createElement("div");
        fallback.className = "preview-fallback";
        fallback.textContent = entry.previewError || "No preview generated.";
        preview.appendChild(fallback);
      }}
      card.appendChild(preview);

      const content = document.createElement("div");
      content.className = "content";

      const eyebrow = document.createElement("div");
      eyebrow.className = "eyebrow";
      eyebrow.textContent = `${{entry.sourceName}} / ${{entry.category}}`;
      content.appendChild(eyebrow);

      const title = document.createElement("h2");
      title.className = "title";
      title.textContent = entry.title;
      content.appendChild(title);

      const path = document.createElement("p");
      path.className = "path";
      path.textContent = entry.relativePath;
      content.appendChild(path);

      if (entry.ai && entry.ai.summary) {{
        const description = document.createElement("p");
        description.className = "description";
        description.textContent = entry.ai.summary;
        content.appendChild(description);
      }}

      const stats = document.createElement("div");
      stats.className = "stats";
      if (entry.entryType === "scad") {{
        stats.appendChild(makeTag(`${{entry.parameterCount}} parameters`));
        stats.appendChild(makeTag(`${{entry.groupNames.length}} groups`));
      }} else {{
        stats.appendChild(makeTag(`${{fileFormatLabel(entry)}} baked file`));
      }}
      content.appendChild(stats);

      const tags = document.createElement("div");
      tags.className = "tag-list";
      const visibleTags = [...entry.tags, ...entry.groupNames.slice(0, 4)];
      for (const tag of visibleTags) {{
        tags.appendChild(makeTag(tag));
      }}
      if (entry.parameterNames.length) {{
        tags.appendChild(makeTag(`e.g. ${{entry.parameterNames.slice(0, 3).join(", ")}}`));
      }}
      content.appendChild(tags);

      if (entry.metadataError || entry.previewError) {{
        const issues = document.createElement("div");
        issues.className = "issues";
        const messages = [entry.metadataError, entry.previewError].filter(Boolean);
        issues.textContent = messages.join(" | ");
        content.appendChild(issues);
      }}

      const actions = document.createElement("div");
      actions.className = "actions";

      const sourceLink = document.createElement("a");
      sourceLink.href = `${{payload.serverBasePath}}/source-file/${{entry.id}}`;
      sourceLink.textContent = entry.entryType === "scad" ? "Open source" : bakedDownloadLabel(entry);
      actions.appendChild(sourceLink);

      if (entry.entryType === "scad") {{
        const customizeButton = document.createElement("button");
        customizeButton.className = "secondary";
        customizeButton.type = "button";
        customizeButton.textContent = "Customize";
        customizeButton.addEventListener("click", () => openCustomizer(entry));
        actions.appendChild(customizeButton);
      }} else {{
        const slicerButton = document.createElement("button");
        slicerButton.className = "secondary";
        slicerButton.type = "button";
        slicerButton.textContent = stlOpenButtonLabel();
        slicerButton.disabled = !serverAvailable;
        slicerButton.addEventListener("click", () => openStlInSlicer(entry));
        actions.appendChild(slicerButton);
      }}

      if (entry.metadataPath) {{
        const metadataLink = document.createElement("a");
        metadataLink.href = entry.metadataPath;
        metadataLink.textContent = "Customizer JSON";
        actions.appendChild(metadataLink);
      }}

      content.appendChild(actions);
      card.appendChild(content);
      return card;
    }}

    function applyFilters() {{
      const needle = searchInput.value.trim().toLowerCase();
      const source = sourceSelect.value;
      const category = categorySelect.value;
      const filtered = entries.filter((entry) => {{
        if (entry.entryType !== activeTab) {{
          return false;
        }}
        if (source && entry.sourceName !== source) {{
          return false;
        }}
        if (category && entry.category !== category) {{
          return false;
        }}
        if (assistantFilterIds && !assistantFilterIds.has(entry.id)) {{
          return false;
        }}
        if (!needle) {{
          return true;
        }}
        return entry.searchText.includes(needle);
      }});

      grid.innerHTML = "";
      for (const entry of filtered) {{
        grid.appendChild(makeCard(entry));
      }}

      empty.style.display = filtered.length ? "none" : "block";
      renderSummary(filtered);
      clearAssistantFilterBtn.hidden = !assistantFilterIds?.size;
    }}

    function shellQuote(text) {{
      return `'${{String(text).replace(/'/g, `'\"'\"'`)}}'`;
    }}

    function openscadLiteral(value) {{
      if (typeof value === "boolean") {{
        return value ? "true" : "false";
      }}
      if (typeof value === "number") {{
        return Number.isInteger(value) ? String(value) : String(value);
      }}
      return JSON.stringify(String(value));
    }}

    function readInitialValue(parameter) {{
      if (Object.prototype.hasOwnProperty.call(parameter, "initial")) {{
        return parameter.initial;
      }}
      if (parameter.type === "boolean") {{
        return false;
      }}
      return "";
    }}

    function cloneInitialValues(parameters) {{
      const values = {{}};
      for (const parameter of parameters) {{
        values[parameter.name] = readInitialValue(parameter);
      }}
      return values;
    }}

    function groupedParameters(parameters) {{
      const groups = new Map();
      for (const parameter of parameters) {{
        const groupName = parameter.group || "Ungrouped";
        if (!groups.has(groupName)) {{
          groups.set(groupName, []);
        }}
        groups.get(groupName).push(parameter);
      }}
      return groups;
    }}

    function parameterHintFor(parameter) {{
      return currentEntry?.ai?.parameterHints?.[parameter.name] || null;
    }}

    function renderPreviewImage(src, alt) {{
      modalPreview.innerHTML = "";
      const img = document.createElement("img");
      img.src = src;
      img.alt = alt;
      modalPreview.appendChild(img);
    }}

    function updateStatus(message = "", tone = "") {{
      actionStatus.textContent = message;
      actionStatus.className = tone ? `status-box ${{tone}}` : "status-box";
    }}

    function refreshCommandBox() {{
      if (!currentEntry) {{
        commandBox.textContent = "";
        return;
      }}
      const outputStem =
        `${{currentEntry.sourceName}}-${{currentEntry.title}}`.replace(/[^A-Za-z0-9._-]+/g, "-") || "model";
      const outputName = `${{outputStem}}.stl`;
      const outputPath = `${{payload.catalogDir}}/custom/stl/${{outputName}}`;
      const openScadPath = currentEntry.libraryPaths.length
        ? currentEntry.libraryPaths.join(payload.pathSeparator)
        : payload.workspaceRoot;
      const defs = currentEntry.parameters.map((parameter) => {{
        const literal = openscadLiteral(currentValues[parameter.name]);
        return `-D ${{shellQuote(`${{parameter.name}}=${{literal}}`)}}`;
      }});
      commandBox.textContent = [
        `OPENSCADPATH=${{shellQuote(openScadPath)}}`,
        `${{shellQuote(effectiveToolsConfig().openscadBin)}} --export-format binstl -o ${{shellQuote(outputPath)}}`,
        ...defs,
        shellQuote(currentEntry.absoluteSourcePath),
      ].join(" ");
    }}

    function makeInput(parameter) {{
      const hint = parameterHintFor(parameter);
      const displayLabel = hint?.label || parameter.name;
      if (parameter.type === "boolean") {{
        const field = document.createElement("div");
        field.className = "field";
        const wrapper = document.createElement("label");
        wrapper.className = "checkbox-row";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = Boolean(currentValues[parameter.name]);
        input.addEventListener("change", () => {{
          currentValues[parameter.name] = input.checked;
          refreshCommandBox();
        }});
        const text = document.createElement("span");
        text.textContent = displayLabel;
        wrapper.appendChild(input);
        wrapper.appendChild(text);
        field.appendChild(wrapper);
        const meta = document.createElement("div");
        meta.className = "field-meta";
        meta.textContent = `SCAD variable: ${{parameter.name}}`;
        field.appendChild(meta);
        if (hint?.description) {{
          const aiHelp = document.createElement("div");
          aiHelp.className = "field-help";
          aiHelp.textContent = hint.description;
          field.appendChild(aiHelp);
        }}
        if (parameter.caption) {{
          const help = document.createElement("div");
          help.className = "field-help";
          help.textContent = parameter.caption;
          field.appendChild(help);
        }}
        return field;
      }}

      const wrapper = document.createElement("div");
      wrapper.className = "field";

      const label = document.createElement("label");
      label.textContent = displayLabel;
      wrapper.appendChild(label);

      if (displayLabel !== parameter.name) {{
        const meta = document.createElement("div");
        meta.className = "field-meta";
        meta.textContent = `SCAD variable: ${{parameter.name}}`;
        wrapper.appendChild(meta);
      }}

      let input;
      if (parameter.options && parameter.options.length) {{
        input = document.createElement("select");
        for (const option of parameter.options) {{
          const opt = document.createElement("option");
          opt.value = String(option.value);
          opt.textContent = option.name;
          if (String(currentValues[parameter.name]) === String(option.value)) {{
            opt.selected = true;
          }}
          input.appendChild(opt);
        }}
      }} else {{
        input = document.createElement("input");
        input.type = parameter.type === "number" ? "number" : "text";
        if (parameter.type === "number") {{
          input.step = parameter.step || "any";
        }}
        input.value = String(currentValues[parameter.name] ?? "");
      }}

      input.addEventListener("input", () => {{
        if (parameter.type === "number") {{
          currentValues[parameter.name] = input.value === "" ? "" : Number(input.value);
        }} else {{
          currentValues[parameter.name] = input.value;
        }}
        refreshCommandBox();
      }});
      input.addEventListener("change", () => {{
        if (parameter.type === "number") {{
          currentValues[parameter.name] = input.value === "" ? "" : Number(input.value);
        }} else {{
          currentValues[parameter.name] = input.value;
        }}
        refreshCommandBox();
      }});

      wrapper.appendChild(input);
      if (hint?.description) {{
        const aiHelp = document.createElement("div");
        aiHelp.className = "field-help";
        aiHelp.textContent = hint.description;
        wrapper.appendChild(aiHelp);
      }}
      if (parameter.caption) {{
        const help = document.createElement("div");
        help.className = "field-help";
        help.textContent = parameter.caption;
        wrapper.appendChild(help);
      }}
      return wrapper;
    }}

    function renderParameterGroups() {{
      paramGroups.innerHTML = "";
      const groups = groupedParameters(currentEntry.parameters);
      for (const [groupName, parameters] of groups.entries()) {{
        const section = document.createElement("section");
        section.className = "group";
        const heading = document.createElement("h3");
        heading.textContent = groupName;
        section.appendChild(heading);

        const fields = document.createElement("div");
        fields.className = "fields";
        for (const parameter of parameters) {{
          fields.appendChild(makeInput(parameter));
        }}
        section.appendChild(fields);
        paramGroups.appendChild(section);
      }}
    }}

    function updateServerStatus() {{
      serverStatus.className = serverAvailable ? "server-status online" : "server-status offline";
      serverStatus.textContent = serverAvailable
        ? "Local server detected. Preview render, STL export, OpenSCAD launch, and rescans are enabled."
        : "Open this catalog through the local server to render custom previews, export STL files, launch OpenSCAD, or rescan libraries. Copy Command still works offline.";
      renderPreviewBtn.disabled = !serverAvailable;
      exportStlBtn.disabled = !serverAvailable;
      openScadBtn.disabled = !serverAvailable;
      settingsBtn.disabled = !serverAvailable;
      assistantBtn.disabled = !serverAvailable;
    }}

    function updateAssistantStatus(message = "", tone = "") {{
      assistantStatus.textContent = message;
      assistantStatus.className = tone ? `status-box ${{tone}}` : "status-box";
    }}

    function ensureAssistantWelcome() {{
      if (assistantMessages.length) {{
        return;
      }}
      assistantMessages = [
        {{
          role: "assistant",
          content:
            "Ask what you want to build or mount, and I’ll suggest matching parts from this local catalog plus likely starting customizations.",
        }},
      ];
    }}

    function renderAssistantThread() {{
      assistantThread.innerHTML = "";
      for (const message of assistantMessages) {{
        const bubble = document.createElement("div");
        bubble.className = `assistant-message ${{message.role}}`;
        bubble.textContent = message.content;
        assistantThread.appendChild(bubble);
      }}
      assistantThread.scrollTop = assistantThread.scrollHeight;
    }}

    function entryForAssistantMatch(match) {{
      if (!match || typeof match.id !== "string") {{
        return null;
      }}
      return entryById.get(match.id) || null;
    }}

    function showAssistantEntry(match, openAfter = false) {{
      const entry = entryForAssistantMatch(match);
      if (!entry) {{
        return;
      }}
      assistantFilterIds = new Set([entry.id]);
      sourceSelect.value = entry.sourceName || "";
      categorySelect.value = entry.category || "";
      searchInput.value = "";
      setActiveTab(entry.entryType === "baked" ? "baked" : "scad");
      assistantDialog.close();
      grid.scrollIntoView({{ behavior: "smooth", block: "start" }});
      if (openAfter && entry.entryType === "scad") {{
        window.setTimeout(() => openCustomizer(entry), 150);
      }}
    }}

    function applyAssistantFilter(matches) {{
      const ids = matches
        .map((match) => match.id)
        .filter((value) => typeof value === "string" && entryById.has(value));
      assistantMatches = matches;
      assistantFilterIds = ids.length ? new Set(ids) : null;
      if (!assistantFilterIds) {{
        applyFilters();
        return;
      }}
      const scadMatches = ids.filter((id) => entryById.get(id)?.entryType === "scad").length;
      const bakedMatches = ids.length - scadMatches;
      sourceSelect.value = "";
      categorySelect.value = "";
      searchInput.value = "";
      setActiveTab(scadMatches >= bakedMatches ? "scad" : "baked");
    }}

    function renderAssistantResults() {{
      assistantResults.innerHTML = "";
      if (!assistantMatches.length) {{
        const emptyState = document.createElement("div");
        emptyState.className = "assistant-empty";
        emptyState.textContent =
          "Best matches and starting customization ideas will appear here after you ask the assistant something.";
        assistantResults.appendChild(emptyState);
        return;
      }}
      for (const match of assistantMatches) {{
        const entry = entryForAssistantMatch(match);
        if (!entry) {{
          continue;
        }}
        const card = document.createElement("section");
        card.className = "assistant-result-card";

        const eyebrow = document.createElement("div");
        eyebrow.className = "eyebrow";
        eyebrow.textContent = `${{entry.sourceName}} / ${{entry.category}}`;
        card.appendChild(eyebrow);

        const title = document.createElement("h3");
        title.textContent = entry.title;
        card.appendChild(title);

        const chips = document.createElement("div");
        chips.className = "tag-list";
        chips.appendChild(makeTag(entry.entryType === "scad" ? "customizable" : fileFormatLabel(entry)));
        if (entry.entryType === "scad") {{
          chips.appendChild(makeTag(`${{entry.parameterCount}} parameters`));
        }}
        card.appendChild(chips);

        if (match.reason) {{
          const reason = document.createElement("div");
          reason.className = "small-note";
          reason.textContent = match.reason;
          card.appendChild(reason);
        }}

        if (match.suggestedParameters?.length) {{
          const suggestions = document.createElement("div");
          suggestions.className = "assistant-suggestions";
          for (const suggestion of match.suggestedParameters) {{
            const row = document.createElement("div");
            row.className = "assistant-suggestion";
            const parameter = entry.parameters.find((item) => item.name === suggestion.name);
            const label =
              currentEntry?.id === entry.id
                ? parameterHintFor(parameter || {{ name: suggestion.name }})?.label || suggestion.name
                : entry.ai?.parameterHints?.[suggestion.name]?.label || suggestion.name;
            const valueText = suggestion.suggestedValue ? `: ${{suggestion.suggestedValue}}` : "";
            row.textContent = `${{label}}${{valueText}}${{suggestion.reason ? ` - ${{suggestion.reason}}` : ""}}`;
            suggestions.appendChild(row);
          }}
          card.appendChild(suggestions);
        }}

        const actions = document.createElement("div");
        actions.className = "assistant-result-actions";

        const showBtn = document.createElement("button");
        showBtn.className = "secondary";
        showBtn.type = "button";
        showBtn.textContent = "Show In Catalog";
        showBtn.addEventListener("click", () => showAssistantEntry(match, false));
        actions.appendChild(showBtn);

        if (entry.entryType === "scad") {{
          const openBtn = document.createElement("button");
          openBtn.className = "ghost";
          openBtn.type = "button";
          openBtn.textContent = "Open Customizer";
          openBtn.addEventListener("click", () => showAssistantEntry(match, true));
          actions.appendChild(openBtn);
        }}

        card.appendChild(actions);
        assistantResults.appendChild(card);
      }}
    }}

    async function sendAssistantMessage() {{
      const text = assistantInput.value.trim();
      if (!text || !serverAvailable || assistantBusy) {{
        return;
      }}
      ensureAssistantWelcome();
      assistantBusy = true;
      assistantMessages.push({{ role: "user", content: text }});
      assistantInput.value = "";
      renderAssistantThread();
      updateAssistantStatus("Assistant is reviewing the local catalog...", "");
      assistantSendBtn.disabled = true;
      try {{
        const data = await postJson(`${{payload.serverBasePath}}/assistant`, {{
          currentTab: activeTab,
          messages: assistantMessages,
        }});
        assistantMessages.push({{
          role: "assistant",
          content: data.reply || "I found some grounded matches in the local catalog.",
        }});
        if (data.followUp) {{
          assistantMessages.push({{
            role: "assistant",
            content: `Next step: ${{data.followUp}}`,
          }});
        }}
        assistantMatches = Array.isArray(data.matches) ? data.matches : [];
        applyAssistantFilter(assistantMatches);
        renderAssistantResults();
        renderAssistantThread();
        updateAssistantStatus(
          data.assistantUsed
            ? "Assistant suggestions are grounded in the local catalog and Ollama."
            : "Assistant fell back to local catalog matching without Ollama ranking.",
          data.assistantUsed ? "success" : ""
        );
      }} catch (error) {{
        updateAssistantStatus(error.message, "error");
      }} finally {{
        assistantBusy = false;
        assistantSendBtn.disabled = false;
      }}
    }}

    function openAssistant() {{
      ensureAssistantWelcome();
      renderAssistantThread();
      renderAssistantResults();
      updateAssistantStatus(
        serverAvailable
          ? "Ask about use cases, matching parts, or likely starting parameters."
          : "Open this catalog through the local server to use Assistant.",
        ""
      );
      assistantDialog.showModal();
      assistantInput.focus();
    }}

    function setActiveTab(tab) {{
      activeTab = tab;
      tabScadBtn.classList.toggle("tab-active", tab === "scad");
      tabBakedBtn.classList.toggle("tab-active", tab === "baked");
      applyFilters();
    }}

    function openCustomizer(entry) {{
      currentEntry = entry;
      currentValues = cloneInitialValues(entry.parameters);
      refreshConfiguredLabels(editableConfig);
      modalCategory.textContent = `${{entry.sourceName}} / ${{entry.category}}`;
      modalTitle.textContent = entry.title;
      modalPath.textContent = entry.relativePath;
      renderPreviewImage(entry.previewPath || "", `${{entry.title}} preview`);
      renderParameterGroups();
      refreshCommandBox();
      updateStatus("");
      updateServerStatus();
      customizer.showModal();
    }}

    async function postJson(url, payload) {{
      const response = await fetch(url, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(payload),
      }});
      const data = await response.json();
      if (!response.ok || !data.ok) {{
        throw new Error(data.error || `Request failed with ${{response.status}}`);
      }}
      return data;
    }}

    function rescanActivityLabel(status) {{
      return status?.forced ? "Force rebuild" : "Rescan";
    }}

    function rescanProgressText(status) {{
      if (!status?.active) {{
        return "";
      }}
      const label = rescanActivityLabel(status);
      if ((status.total || 0) > 0) {{
        return `${{label}} running: ${{status.current || 0}} / ${{status.total}} (${{status.progressPercent || 0}}%)`;
      }}
      return `${{label}} running...`;
    }}

    function rescanDetailText(status) {{
      const detail = String(status?.lastLine || "").trim();
      if (!detail) {{
        return "";
      }}
      const lowerDetail = detail.toLowerCase();
      const lowerLabel = rescanActivityLabel(status).toLowerCase();
      if (lowerDetail.includes(lowerLabel) && lowerDetail.includes("running")) {{
        return "";
      }}
      return detail;
    }}

    function rescanCompletionText(status) {{
      const label = rescanActivityLabel(status);
      const entryCount = Number(status?.entryCount || 0);
      const sourceCount = Number(status?.sourceCount || 0);
      return `${{label}} complete: ${{entryCount}} entries across ${{sourceCount}} sources.`;
    }}

    function rescanFailureText(status) {{
      const label = rescanActivityLabel(status);
      const detail = String(status?.error || status?.lastLine || "Rescan failed.").trim();
      return `${{label}} failed: ${{detail}}`;
    }}

    function renderScanIndicator(status = rescanStatus) {{
      const active = Boolean(status?.active);
      scanIndicator.hidden = !active;
      scanIndicatorText.textContent = active ? rescanProgressText(status) : "Scan running...";
      saveRescanBtn.disabled = active;
      saveForceRescanBtn.disabled = active;
    }}

    function updateSettingsForRescan(status, phase = "active") {{
      if (!status) {{
        return;
      }}
      if (phase === "error") {{
        updateSettingsStatus(rescanFailureText(status), "error");
        return;
      }}
      if (phase === "complete") {{
        updateSettingsStatus(rescanCompletionText(status), "success");
        return;
      }}
      const progress = rescanProgressText(status);
      const detail = rescanDetailText(status);
      updateSettingsStatus(detail ? `${{progress}} ${{detail}}` : progress, "");
    }}

    async function fetchRescanStatus() {{
      const response = await fetch(`${{payload.serverBasePath}}/rescan-status`);
      const data = await response.json();
      if (!response.ok || !data.ok) {{
        throw new Error(data.error || "Failed to read rescan status");
      }}
      const previousActive = Boolean(rescanStatus?.active);
      rescanStatus = data.status || null;
      renderScanIndicator(rescanStatus);
      if (rescanStatus?.active) {{
        updateSettingsForRescan(rescanStatus, "active");
      }} else if (previousActive) {{
        if (rescanStatus?.error) {{
          updateSettingsForRescan(rescanStatus, "error");
          rescanReloadOnComplete = false;
        }} else {{
          updateSettingsForRescan(rescanStatus, "complete");
          if (rescanReloadOnComplete) {{
            rescanReloadOnComplete = false;
            window.setTimeout(() => window.location.reload(), 700);
          }}
        }}
      }}
      return rescanStatus;
    }}

    function stopRescanPolling() {{
      if (rescanPollTimer) {{
        window.clearTimeout(rescanPollTimer);
        rescanPollTimer = null;
      }}
    }}

    async function pollRescanStatus() {{
      try {{
        await fetchRescanStatus();
      }} catch (_error) {{
        stopRescanPolling();
        return;
      }}
      if (rescanStatus?.active) {{
        rescanPollTimer = window.setTimeout(pollRescanStatus, 1200);
      }} else {{
        rescanPollTimer = null;
      }}
    }}

    function startRescanPolling() {{
      if (rescanPollTimer) {{
        return;
      }}
      rescanPollTimer = window.setTimeout(pollRescanStatus, 0);
    }}

    async function detectServer() {{
      try {{
        const response = await fetch(`${{payload.serverBasePath}}/health`);
        if (!response.ok) {{
          throw new Error("offline");
        }}
        const data = await response.json();
        serverAvailable = Boolean(data.ok);
        if (serverAvailable) {{
          try {{
            await fetchRescanStatus();
          }} catch (_error) {{
            rescanStatus = null;
            renderScanIndicator(null);
          }}
          if (data.rescanActive || rescanStatus?.active) {{
            startRescanPolling();
          }}
        }}
      }} catch (_error) {{
        serverAvailable = false;
        rescanStatus = null;
        stopRescanPolling();
        renderScanIndicator(null);
      }}
      updateServerStatus();
    }}

    async function renderCustomPreview() {{
      if (!currentEntry || !serverAvailable) {{
        return;
      }}
      updateStatus("Rendering preview...", "");
      try {{
        const data = await postJson(`${{payload.serverBasePath}}/render-preview`, {{
          entryId: currentEntry.id,
          parameters: currentValues,
        }});
        renderPreviewImage(data.artifactPath, `${{currentEntry.title}} custom preview`);
        updateStatus(`Preview written to ${{data.artifactPath}}`, "success");
      }} catch (error) {{
        updateStatus(error.message, "error");
      }}
    }}

    async function exportBinaryStl() {{
      if (!currentEntry || !serverAvailable) {{
        return;
      }}
      updateStatus("Exporting binary STL...", "");
      try {{
        const data = await postJson(`${{payload.serverBasePath}}/export-stl`, {{
          entryId: currentEntry.id,
          parameters: currentValues,
        }});
        const slicerName = slicerDisplayName(data.slicerPath || "");
        if (data.launchedSlicer) {{
          updateStatus(`Binary STL written to ${{data.artifactPath}} and opened in ${{slicerName}}.`, "success");
        }} else if (data.artifactPath) {{
          updateStatus(
            data.slicerError
              ? `Binary STL written to ${{data.artifactPath}}. ${{slicerName}} was not launched: ${{data.slicerError}}`
              : `Binary STL written to ${{data.artifactPath}}`,
            data.slicerError ? "error" : "success"
          );
          window.open(data.artifactPath, "_blank", "noopener");
        }}
      }} catch (error) {{
        updateStatus(error.message, "error");
      }}
    }}

    async function openInOpenScad() {{
      if (!currentEntry || !serverAvailable) {{
        return;
      }}
      updateStatus("Launching OpenSCAD...", "");
      try {{
        const data = await postJson(`${{payload.serverBasePath}}/open-scad`, {{
          entryId: currentEntry.id,
          parameters: currentValues,
        }});
        updateStatus(`Opened ${{data.openedPath}} in OpenSCAD.`, "success");
      }} catch (error) {{
        updateStatus(error.message, "error");
      }}
    }}

    async function openStlInSlicer(entry) {{
      if (!serverAvailable) {{
        return;
      }}
      try {{
        const data = await postJson(`${{payload.serverBasePath}}/open-in-slicer`, {{
          entryId: entry.id,
          parameters: {{}},
        }});
        updateStatus(`Opened ${{data.openedPath}} in ${{slicerDisplayName(data.slicerPath || "")}}.`, "success");
      }} catch (error) {{
        updateStatus(error.message, "error");
      }}
    }}

    async function copyCommand() {{
      try {{
        await navigator.clipboard.writeText(commandBox.textContent);
        updateStatus("OpenSCAD command copied to clipboard.", "success");
      }} catch (_error) {{
        updateStatus("Clipboard copy failed. Copy the command manually from the box below.", "error");
      }}
    }}

    function updateSettingsStatus(message = "", tone = "") {{
      settingsStatus.textContent = message;
      settingsStatus.className = tone ? `status-box ${{tone}}` : "status-box";
    }}

    function clearAssistantFilter() {{
      assistantFilterIds = null;
      applyFilters();
      updateAssistantStatus("Assistant filter cleared.", "");
    }}

    function blankSource() {{
      return {{
        id: "",
        name: "",
        path: "",
        libraryPaths: [],
        includeHelpers: false,
        includeInProgress: false,
        includeDeprecated: false,
      }};
    }}

    function blankToolsConfig() {{
      return {{
        openscadBin: "openscad-nightly",
        slicerBin: "",
      }};
    }}

    function blankAiConfig() {{
      return {{
        enabled: false,
        provider: "ollama",
        baseUrl: "http://127.0.0.1:11434",
        model: "qwen3:4b-instruct",
        timeout: 30,
        includeScad: true,
        includeStl: false,
        maxSourceChars: 12000,
        maxCommentChars: 3000,
      }};
    }}

    function effectiveToolsConfig() {{
      return {{
        ...blankToolsConfig(),
        ...(payload.tools || {{}}),
        ...(editableConfig?.tools || {{}}),
      }};
    }}

    function hasConfiguredSlicer(toolsConfig = effectiveToolsConfig()) {{
      return Boolean((toolsConfig.slicerBin || "").trim());
    }}

    function isOrcaConfigured(toolsConfig = effectiveToolsConfig()) {{
      return /orca/i.test(toolsConfig.slicerBin || "");
    }}

    function exportButtonLabel(toolsConfig = effectiveToolsConfig()) {{
      if (isOrcaConfigured(toolsConfig)) {{
        return "Export To OrcaSlicer";
      }}
      if (hasConfiguredSlicer(toolsConfig)) {{
        return "Export To Slicer";
      }}
      return "Export Binary STL";
    }}

    function stlOpenButtonLabel(toolsConfig = effectiveToolsConfig()) {{
      return isOrcaConfigured(toolsConfig) ? "Open In OrcaSlicer" : "Open In Slicer";
    }}

    function slicerDisplayName(slicerPath = effectiveToolsConfig().slicerBin || "") {{
      if (!slicerPath) {{
        return "the slicer";
      }}
      const parts = slicerPath.split(/[\\\\/]/).filter(Boolean);
      const fileName = parts.length ? parts[parts.length - 1] : slicerPath;
      return /orca/i.test(fileName) ? "OrcaSlicer" : fileName;
    }}

    function saveRescanButtonLabel(config = editableConfig || {{ ai: payload.ai || {{}} }}) {{
      return config?.ai?.enabled ? "Save And Rescan + AI" : "Save And Rescan";
    }}

    function saveForceRescanButtonLabel(config = editableConfig || {{ ai: payload.ai || {{}} }}) {{
      return config?.ai?.enabled ? "Save And Force Rebuild + AI" : "Save And Force Rebuild";
    }}

    function refreshConfiguredLabels(config = editableConfig) {{
      const toolsConfig = config?.tools || effectiveToolsConfig();
      exportStlBtn.textContent = exportButtonLabel(toolsConfig);
      saveRescanBtn.textContent = saveRescanButtonLabel(config || {{ ai: payload.ai || {{}} }});
      saveForceRescanBtn.textContent = saveForceRescanButtonLabel(config || {{ ai: payload.ai || {{}} }});
    }}

    function splitPaths(text) {{
      return text
        .split(/\\n|,/)
        .map((item) => item.trim())
        .filter(Boolean);
    }}

    function renderSettingsList() {{
      settingsList.innerHTML = "";
      editableConfig.tools ||= blankToolsConfig();
      const toolsCard = document.createElement("section");
      toolsCard.className = "settings-card";
      const toolsHeading = document.createElement("h3");
      toolsHeading.textContent = "Tool Paths";
      toolsCard.appendChild(toolsHeading);
      const toolsFields = document.createElement("div");
      toolsFields.className = "fields";
      const toolDefinitions = [
        [
          "openscadBin",
          "OpenSCAD Executable",
          editableConfig.tools.openscadBin || "openscad-nightly",
          "Full path or command name used for metadata export, preview rendering, STL export, and OpenSCAD launch.",
        ],
        [
          "slicerBin",
          "Slicer Executable",
          editableConfig.tools.slicerBin || "",
          "Optional full path or command name for OrcaSlicer or another slicer. Leave blank to disable slicer launch.",
        ],
      ];
      for (const [key, labelText, value, helpText] of toolDefinitions) {{
        const field = document.createElement("div");
        field.className = "field";
        const label = document.createElement("label");
        label.textContent = labelText;
        field.appendChild(label);
        const input = document.createElement("input");
        input.type = "text";
        input.value = value;
        input.addEventListener("input", () => {{
          editableConfig.tools[key] = input.value;
          refreshConfiguredLabels(editableConfig);
        }});
        field.appendChild(input);
        const help = document.createElement("div");
        help.className = "field-help";
        help.textContent = helpText;
        field.appendChild(help);
        toolsFields.appendChild(field);
      }}
      toolsCard.appendChild(toolsFields);
      settingsList.appendChild(toolsCard);

      const aiCard = document.createElement("section");
      aiCard.className = "settings-card";
      const aiHeading = document.createElement("h3");
      aiHeading.textContent = "Optional AI Enrichment";
      aiCard.appendChild(aiHeading);
      const aiFields = document.createElement("div");
      aiFields.className = "fields";
      editableConfig.ai ||= blankAiConfig();

      const aiTextFields = [
        ["provider", "Provider", editableConfig.ai.provider || "ollama"],
        ["baseUrl", "Base URL", editableConfig.ai.baseUrl || "http://127.0.0.1:11434"],
        ["model", "Model", editableConfig.ai.model || "qwen3:4b-instruct"],
        ["timeout", "Timeout Seconds", String(editableConfig.ai.timeout ?? 30)],
        ["maxSourceChars", "Max Source Chars", String(editableConfig.ai.maxSourceChars ?? 12000)],
        ["maxCommentChars", "Max Comment Chars", String(editableConfig.ai.maxCommentChars ?? 3000)],
      ];
      for (const [key, labelText, value] of aiTextFields) {{
        const field = document.createElement("div");
        field.className = "field";
        const label = document.createElement("label");
        label.textContent = labelText;
        field.appendChild(label);
        const input = document.createElement("input");
        input.type = key === "timeout" || key === "maxSourceChars" || key === "maxCommentChars" ? "number" : "text";
        if (input.type === "number") {{
          input.min = "1";
        }}
        input.value = value;
        input.addEventListener("input", () => {{
          if (input.type === "number") {{
            editableConfig.ai[key] = input.value === "" ? "" : Number(input.value);
          }} else {{
            editableConfig.ai[key] = input.value;
          }}
          refreshConfiguredLabels(editableConfig);
        }});
        field.appendChild(input);
        aiFields.appendChild(field);
      }}

      const aiToggles = [
        ["enabled", "Enable AI enrichment at index time"],
        ["includeScad", "Allow AI enrichment for SCAD entries"],
        ["includeStl", "Allow AI enrichment for baked object entries (.stl, .3mf)"],
      ];
      for (const [key, labelText] of aiToggles) {{
        const wrapper = document.createElement("label");
        wrapper.className = "checkbox-row";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = Boolean(editableConfig.ai[key]);
        input.addEventListener("change", () => {{
          editableConfig.ai[key] = input.checked;
          refreshConfiguredLabels(editableConfig);
        }});
        const span = document.createElement("span");
        span.textContent = labelText;
        wrapper.appendChild(input);
        wrapper.appendChild(span);
        aiFields.appendChild(wrapper);
      }}
      const aiHelp = document.createElement("div");
      aiHelp.className = "small-note";
      aiHelp.textContent =
        "AI enrichment is optional. If Ollama is disabled, missing, or the model is unavailable, indexing falls back to the normal non-AI behavior.";
      aiFields.appendChild(aiHelp);
      aiCard.appendChild(aiFields);
      settingsList.appendChild(aiCard);
      refreshConfiguredLabels(editableConfig);

      const sources = editableConfig?.sources || [];
      sources.forEach((source, index) => {{
        const card = document.createElement("section");
        card.className = "settings-card";

        const heading = document.createElement("h3");
        heading.textContent = source.name || `Source ${{index + 1}}`;
        card.appendChild(heading);

        const fields = document.createElement("div");
        fields.className = "fields";

        const definitions = [
          ["id", "Source ID", "text", source.id || ""],
          ["name", "Display Name", "text", source.name || ""],
          ["path", "Folder Path", "text", source.path || ""],
          ["libraryPaths", "Library Paths", "text", (source.libraryPaths || []).join(", ")],
        ];

        for (const [key, labelText, type, value] of definitions) {{
          const field = document.createElement("div");
          field.className = "field";
          const label = document.createElement("label");
          label.textContent = labelText;
          field.appendChild(label);
          const input = document.createElement("input");
          input.type = type;
          input.value = value;
          input.addEventListener("input", () => {{
            if (key === "libraryPaths") {{
              source[key] = splitPaths(input.value);
            }} else {{
              source[key] = input.value;
            }}
            if (key === "name") {{
              heading.textContent = input.value || `Source ${{index + 1}}`;
            }}
          }});
          field.appendChild(input);
          if (key === "libraryPaths") {{
            const help = document.createElement("div");
            help.className = "field-help";
            help.textContent = "Comma-separated or newline-separated paths added to OPENSCADPATH for this source.";
            field.appendChild(help);
          }}
          fields.appendChild(field);
        }}

        const scanHelp = document.createElement("div");
        scanHelp.className = "small-note";
        scanHelp.textContent =
          "Each source folder is scanned for customizable .scad files and baked object files such as .stl and .3mf.";
        fields.appendChild(scanHelp);

        const toggles = [
          ["includeHelpers", "Include helper files"],
          ["includeInProgress", "Include in-progress files"],
          ["includeDeprecated", "Include deprecated files"],
        ];
        for (const [key, labelText] of toggles) {{
          const wrapper = document.createElement("label");
          wrapper.className = "checkbox-row";
          const input = document.createElement("input");
          input.type = "checkbox";
          input.checked = Boolean(source[key]);
          input.addEventListener("change", () => {{
            source[key] = input.checked;
          }});
          const span = document.createElement("span");
          span.textContent = labelText;
          wrapper.appendChild(input);
          wrapper.appendChild(span);
          fields.appendChild(wrapper);
        }}

        card.appendChild(fields);

        const actions = document.createElement("div");
        actions.className = "settings-actions";
        const removeBtn = document.createElement("button");
        removeBtn.className = "ghost";
        removeBtn.type = "button";
        removeBtn.textContent = "Remove Source";
        removeBtn.addEventListener("click", () => {{
          editableConfig.sources.splice(index, 1);
          renderSettingsList();
        }});
        actions.appendChild(removeBtn);
        card.appendChild(actions);

        settingsList.appendChild(card);
      }});
    }}

    async function loadConfig() {{
      const response = await fetch(`${{payload.serverBasePath}}/config`);
      const data = await response.json();
      if (!response.ok || !data.ok) {{
        throw new Error(data.error || "Failed to load config");
      }}
      editableConfig = structuredClone(data.config);
      editableConfig.tools = {{
        ...blankToolsConfig(),
        ...(data.effectiveTools || {{}}),
        ...(editableConfig.tools || {{}}),
      }};
      editableConfig.ai ||= blankAiConfig();
      configPath = data.configPath;
      configPathNote.textContent = `Editing ${{configPath}}`;
      renderSettingsList();
    }}

    async function openSettings() {{
      if (!serverAvailable) {{
        return;
      }}
      updateSettingsStatus("Loading source configuration...", "");
      try {{
        await loadConfig();
        if (rescanStatus?.active) {{
          updateSettingsForRescan(rescanStatus, "active");
          startRescanPolling();
        }} else if (rescanStatus?.error) {{
          updateSettingsForRescan(rescanStatus, "error");
        }} else {{
          updateSettingsStatus("", "");
        }}
        settingsDialog.showModal();
      }} catch (error) {{
        updateSettingsStatus(error.message, "error");
        settingsDialog.showModal();
      }}
    }}

    async function saveConfig() {{
      if (!editableConfig) {{
        return;
      }}
      updateSettingsStatus("Saving source configuration...", "");
      const data = await postJson(`${{payload.serverBasePath}}/config`, {{
        config: editableConfig,
      }});
      updateSettingsStatus(`Saved ${{data.configPath}}.`, "success");
      return data;
    }}

    async function triggerRescan(force = false) {{
      updateSettingsStatus(
        force ? "Starting background force rebuild..." : "Starting background rescan...",
        ""
      );
      const data = await postJson(`${{payload.serverBasePath}}/rescan`, {{ force }});
      rescanReloadOnComplete = true;
      rescanStatus = data.status || rescanStatus;
      renderScanIndicator(rescanStatus);
      if (data.alreadyRunning) {{
        updateSettingsStatus("A rescan is already running. Tracking progress below.", "");
      }} else if (rescanStatus?.active) {{
        updateSettingsForRescan(rescanStatus, "active");
      }}
      startRescanPolling();
      return data;
    }}

    searchInput.addEventListener("input", applyFilters);
    sourceSelect.addEventListener("change", applyFilters);
    categorySelect.addEventListener("change", applyFilters);
    tabScadBtn.addEventListener("click", () => setActiveTab("scad"));
    tabBakedBtn.addEventListener("click", () => setActiveTab("baked"));
    renderPreviewBtn.addEventListener("click", renderCustomPreview);
    exportStlBtn.addEventListener("click", exportBinaryStl);
    openScadBtn.addEventListener("click", openInOpenScad);
    copyCommandBtn.addEventListener("click", copyCommand);
    assistantBtn.addEventListener("click", openAssistant);
    assistantSendBtn.addEventListener("click", sendAssistantMessage);
    assistantCloseBtn.addEventListener("click", () => assistantDialog.close());
    clearAssistantFilterBtn.addEventListener("click", clearAssistantFilter);
    settingsBtn.addEventListener("click", openSettings);
    assistantInput.addEventListener("keydown", (event) => {{
      if (event.key === "Enter" && !event.shiftKey) {{
        event.preventDefault();
        sendAssistantMessage();
      }}
    }});
    addSourceBtn.addEventListener("click", () => {{
      if (!editableConfig) {{
        editableConfig = {{ tools: blankToolsConfig(), ai: blankAiConfig(), sources: [] }};
      }}
      editableConfig.sources.push(blankSource());
      renderSettingsList();
    }});
    saveSettingsBtn.addEventListener("click", async () => {{
      try {{
        await saveConfig();
        refreshConfiguredLabels(editableConfig);
      }} catch (error) {{
        updateSettingsStatus(error.message, "error");
      }}
    }});
    saveRescanBtn.addEventListener("click", async () => {{
      try {{
        await saveConfig();
        await triggerRescan(false);
      }} catch (error) {{
        updateSettingsStatus(error.message, "error");
      }}
    }});
    saveForceRescanBtn.addEventListener("click", async () => {{
      try {{
        await saveConfig();
        await triggerRescan(true);
      }} catch (error) {{
        updateSettingsStatus(error.message, "error");
      }}
    }});
    closeSettingsBtn.addEventListener("click", () => settingsDialog.close());
    resetParamsBtn.addEventListener("click", () => {{
      if (!currentEntry) {{
        return;
      }}
      currentValues = cloneInitialValues(currentEntry.parameters);
      renderParameterGroups();
      refreshCommandBox();
      updateStatus("Parameters reset to OpenSCAD defaults.", "");
    }});
    closeModalBtn.addEventListener("click", () => customizer.close());
    customizer.addEventListener("click", (event) => {{
      const rect = customizer.getBoundingClientRect();
      const withinDialog =
        rect.top <= event.clientY &&
        event.clientY <= rect.top + rect.height &&
        rect.left <= event.clientX &&
        event.clientX <= rect.left + rect.width;
      if (!withinDialog) {{
        customizer.close();
      }}
    }});
    settingsDialog.addEventListener("click", (event) => {{
      const rect = settingsDialog.getBoundingClientRect();
      const withinDialog =
        rect.top <= event.clientY &&
        event.clientY <= rect.top + rect.height &&
        rect.left <= event.clientX &&
        event.clientX <= rect.left + rect.width;
      if (!withinDialog) {{
        settingsDialog.close();
      }}
    }});
    assistantDialog.addEventListener("click", (event) => {{
      const rect = assistantDialog.getBoundingClientRect();
      const withinDialog =
        rect.top <= event.clientY &&
        event.clientY <= rect.top + rect.height &&
        rect.left <= event.clientX &&
        event.clientX <= rect.left + rect.width;
      if (!withinDialog) {{
        assistantDialog.close();
      }}
    }});

    footer.textContent =
      `Generated ${{payload.generatedAt}} using ${{payload.openscadBin}} across ${{payload.sources.length}} configured source libraries.` +
      (aiMetadata?.enabled
        ? aiMetadata.available
          ? ` AI enrichments came from ${{aiMetadata.provider}}:${{aiMetadata.model}}.`
          : ` AI was enabled but skipped: ${{aiMetadata.reason}}.`
        : "") +
      ` Run the local server for custom preview renders and binary STL exports.`;

    refreshConfiguredLabels();
    ensureAssistantWelcome();
    renderAssistantResults();
    setActiveTab("scad");
    detectServer();
  </script>
</body>
</html>
"""


def write_catalog_html(output_dir: Path, payload: dict[str, Any]) -> None:
    (output_dir / "index.html").write_text(html_template(payload), encoding="utf-8")


def build_payload(
    entries: list[Entry],
    args: argparse.Namespace,
    sources: list[SourceConfig],
    workspace_root: Path,
    ai_state: AIState,
    tool_config: ToolConfig,
) -> dict[str, Any]:
    index_records = []
    for entry in entries:
        record = entry.to_index_record()
        record["sourcePath"] = str(entry.source_path)
        record["absoluteSourcePath"] = str(entry.source_path)
        index_records.append(record)

    return {
        "generatedAt": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "openscadBin": tool_config.openscad_bin,
        "workspaceRoot": str(workspace_root),
        "catalogDir": args.output_dir,
        "pathSeparator": os.pathsep,
        "serverBasePath": "/api",
        "tools": {
            "openscadBin": tool_config.openscad_bin,
            "slicerBin": tool_config.slicer_bin,
            "hasSlicer": bool(tool_config.slicer_bin),
        },
        "ai": {
            "enabled": ai_state.enabled,
            "available": ai_state.available,
            "provider": ai_state.provider,
            "baseUrl": ai_state.base_url,
            "model": ai_state.model,
            "reason": ai_state.reason,
        },
        "sources": [
            {
                "id": source.id,
                "name": source.name,
                "type": source.source_type,
                "path": str(source.source_root),
                "relativeRoot": source.relative_root,
                "libraryPaths": [str(path) for path in source.library_paths],
            }
            for source in sources
        ],
        "entries": index_records,
    }


def main() -> int:
    args = parse_args()
    workspace_root = Path.cwd().resolve()
    output_dir = (workspace_root / args.output_dir).resolve()
    sources, ai_config, tool_config = load_sources(args, workspace_root)
    args.openscad_bin = tool_config.openscad_bin

    metadata_dir = output_dir / "metadata"
    preview_dir = output_dir / "previews"
    ai_dir = output_dir / "ai"
    output_dir.mkdir(parents=True, exist_ok=True)
    ai_state = probe_ai(ai_config)
    if ai_state.enabled and ai_state.available:
        print(
            f"AI enrichment enabled via {ai_state.provider} model {ai_state.model} at {ai_state.base_url}",
            file=sys.stderr,
        )
    elif ai_state.enabled and ai_state.reason:
        print(f"AI enrichment skipped: {ai_state.reason}", file=sys.stderr)

    source_file_pairs = [
        (source, source_path)
        for source in sources
        for source_path in discover_files(source, args)
    ]
    if args.limit is not None and args.limit >= 0:
        source_file_pairs = source_file_pairs[: args.limit]

    total_files = len(source_file_pairs)
    if total_files == 0:
        print("No source files matched the current filters.", file=sys.stderr)
        return 1

    entries: list[Entry] = []
    for index, (source, source_path) in enumerate(source_file_pairs, start=1):
        rel = source_path.relative_to(source.source_root)
        print(f"[{index}/{total_files}] {source.name}: {rel}")
        entry = build_entry(
            source_path=source_path,
            source=source,
            output_dir=output_dir,
            metadata_dir=metadata_dir,
            preview_dir=preview_dir,
            ai_dir=ai_dir,
            args=args,
            workspace_root=workspace_root,
            ai_config=ai_config,
            ai_state=ai_state,
        )
        entries.append(entry)

    payload = build_payload(entries, args, sources, workspace_root, ai_state, tool_config)
    write_catalog_json(output_dir, payload)
    write_catalog_html(output_dir, payload)

    print(f"\nCatalog written to {output_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
