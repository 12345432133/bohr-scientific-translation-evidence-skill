#!/usr/bin/env python3
"""Extract a translation worklist with paragraph-level evidence and protected tokens."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from parse_pdf import (
    build_failure_result,
    locate_case_and_task,
    parse_document,
    positive_int,
    write_artifacts,
    write_json,
)


TOKEN_PATTERNS = [
    ("literal_placeholder", re.compile(r"\[\[P\d+\]\]")),
    ("doi", re.compile(r"\b10\.\d{4,9}/[^\s,;)]+", re.IGNORECASE)),
    ("url", re.compile(r"https?://[^\s)>]+", re.IGNORECASE)),
    ("author_year", re.compile(r"\((?:[A-Z][A-Za-z'\-]+(?: et al\.)?,\s*)?\d{4}[a-z]?\)")),
    ("citation", re.compile(r"\[[0-9][0-9,\-–\s]*\]")),
    ("equation_number", re.compile(
        r"\bEq(?:uation)?\.?\s*(?:\(\d+[A-Za-z]?\)|\d+[A-Za-z]?)"
        r"|(?<!\w)\(\d+[a-z]?\)",
        re.IGNORECASE,
    )),
    ("figure_table_ref", re.compile(
        r"\b(?:Fig(?:ure)?s?\.?|Tables?|Tab\.?|Eq(?:uation)?s?\.?)"
        r"\s*\(?\d+[A-Za-z]?\)?(?:\([a-z]\))?",
        re.IGNORECASE,
    )),
    ("number_unit", re.compile(
        r"(?<![\w.])[+\-−]?(?:\d+(?:\.\d+)?|\.\d+)"
        r"(?:[eE][+\-−]?\d+)?\s*"
        r"(?:%|°C|°F|K|m|cm|mm|nm|µm|μm|kg|mg|g|s|ms|min|h|Hz|kHz|MHz|"
        r"eV|keV|MeV|GeV|T|V|A|W|Pa|kPa|MPa|ppm|ppb|mL|µL|μL|L|mol|mM|µM|μM|M)"
        r"(?:[¹²³⁰-⁹]|[+\-−]?\d+)?"
        r"(?!\w)",
        re.IGNORECASE,
    )),
    ("number", re.compile(
        r"(?<![\w.])[+\-−]?(?:\d+(?:\.\d+)?|\.\d+)"
        r"(?:[eE][+\-−]?\d+)?%?(?!\w|\.\d)"
    )),
    ("scientific_identifier", re.compile(
        r"\b(?:[A-Z]{2,}[A-Za-z0-9]*(?:[-/][A-Za-z0-9]+)+|[A-Z]{2,}[0-9]+[A-Za-z]?)\b"
    )),
    ("chemical_formula", re.compile(
        r"\b(?:[A-Z][a-z]?\d*){2,}(?:[+-])?(?:\([A-Za-z0-9]+\))?\b"
    )),
    ("unicode_script", re.compile(r"[₀-₉⁰-⁹¹²³⁺⁻]")),
    ("greek", re.compile(r"[α-ωΑ-Ω]")),
]


def dictionary(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def block_text(block: dict[str, Any]) -> tuple[str, str]:
    kind = str(block.get("kind", "body"))
    if kind == "paragraph":
        return "paragraph", str(block.get("text", "")).strip()
    if kind == "table":
        table = dictionary(block.get("data"))
        parts = [
            str(table.get("caption") or "").strip(),
            str(table.get("markdown") or "").strip(),
        ]
        return "table", "\n\n".join(part for part in parts if part)
    if kind == "figure":
        figure = dictionary(block.get("data"))
        return "figure", str(figure.get("caption", "")).strip()
    data = dictionary(block.get("data"))
    line_kind = str(data.get("kind") or kind)
    label = {
        "heading": "heading",
        "caption": "caption",
        "equation": "equation",
        "footnote": "footnote",
        "reference": "reference",
        "body": "paragraph",
    }.get(line_kind, line_kind)
    return label, str(data.get("text", "")).strip()


def block_evidence(block: dict[str, Any]) -> tuple[list[str], list[float] | None]:
    if block.get("kind") == "paragraph":
        return list(block.get("evidence_ids") or []), block.get("bbox")
    data = dictionary(block.get("data"))
    evidence_ids = [
        str(value)
        for value in (
            data.get("evidence_id"),
            data.get("global_id"),
            data.get("caption_evidence_id"),
        )
        if value
    ]
    bbox = data.get("bbox") or data.get("caption_bbox")
    return list(dict.fromkeys(evidence_ids)), bbox


def protect_segment_text(text: str, start_index: int) -> tuple[str, list[dict[str, Any]], int]:
    spans: list[tuple[int, int, str, str]] = []
    for kind, pattern in TOKEN_PATTERNS:
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end(), match.group(0), kind))
    if not spans:
        return text, [], start_index

    spans.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    selected: list[tuple[int, int, str, str]] = []
    last_end = -1
    for span in spans:
        if span[0] >= last_end:
            selected.append(span)
            last_end = span[1]

    tokens: list[dict[str, Any]] = []
    protected = text
    index = start_index
    reserved = set(re.findall(r"\[\[(P\d+)\]\]", text))
    for start, end, raw, kind in reversed(selected):
        token_id = f"P{index:04d}"
        while token_id in reserved:
            index += 1
            token_id = f"P{index:04d}"
        reserved.add(token_id)
        index += 1
        protected = protected[:start] + f"[[{token_id}]]" + protected[end:]
        tokens.append(
            {
                "token_id": token_id,
                "raw": raw,
                "kind": kind,
                "source_start": start,
                "source_end": end,
            }
        )
    tokens.reverse()
    return protected, tokens, index


def case_glossary(case_data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = (
        case_data.get("glossary")
        or case_data.get("terminology")
        or case_data.get("terms")
        or []
    )
    entries: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for source, value in raw.items():
            if isinstance(value, dict):
                entry = dict(value)
                entry.setdefault("source_term", str(source))
            elif value is None or isinstance(value, str):
                entry = {"source_term": str(source), "translation": value}
            else:
                raise ValueError(
                    f"Invalid glossary translation for {source!r}"
                )
            entries.append(entry)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                entries.append({"source_term": item, "translation": None})
            elif isinstance(item, dict):
                entry = dict(item)
                entry.setdefault("source_term", entry.get("source") or entry.get("term"))
                entries.append(entry)
    glossary: list[dict[str, Any]] = []
    for index, entry in enumerate(entries, start=1):
        source_term = str(entry.get("source_term") or "").strip()
        if not source_term:
            continue
        translation = entry.get("translation")
        if translation is None:
            translation = entry.get("target_term")
        locked = entry.get("locked", False)
        case_sensitive = entry.get("case_sensitive", True)
        required = entry.get("required", locked)
        aliases = entry.get("aliases", [])
        if not isinstance(locked, bool):
            raise ValueError(f"glossary.locked must be boolean for {source_term!r}")
        if not isinstance(required, bool):
            raise ValueError(f"glossary.required must be boolean for {source_term!r}")
        if not isinstance(case_sensitive, bool):
            raise ValueError(
                f"glossary.case_sensitive must be boolean for {source_term!r}"
            )
        if translation is not None and not isinstance(translation, str):
            raise ValueError(
                f"glossary.translation must be text for {source_term!r}"
            )
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) and alias.strip() for alias in aliases
        ):
            raise ValueError(
                f"glossary.aliases must contain non-empty strings for {source_term!r}"
            )
        if locked and not str(translation or "").strip():
            raise ValueError(f"Locked term has no translation: {source_term!r}")
        glossary.append(
            {
                "term_id": entry.get("term_id") or f"T{index:04d}",
                "source_term": source_term,
                "translation": translation,
                "locked": locked,
                "required": required,
                "case_sensitive": case_sensitive,
                "aliases": aliases,
                "notes": entry.get("notes"),
            }
        )
    return glossary


def infer_terms(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    pages: dict[str, set[int]] = {}
    acronym_re = re.compile(r"\b[A-Z][A-Z0-9\-]{1,9}\b")
    phrase_re = re.compile(r"\b[A-Z][A-Za-z\-]{2,}(?:\s+[A-Z][A-Za-z\-]{2,}){1,2}\b")
    ignored_phrases = {
        "the",
        "this",
        "that",
        "these",
        "those",
        "figure",
        "table",
        "page",
        "section",
    }
    for segment in segments:
        text = segment["source_text"]
        for term in acronym_re.findall(text):
            counts[term] += 1
            pages.setdefault(term, set()).add(segment["page"])
        for phrase in phrase_re.findall(text):
            if len(phrase) <= 60:
                counts[phrase] += 1
                pages.setdefault(phrase, set()).add(segment["page"])
    candidates = [
        (term, count)
        for term, count in counts.items()
        if count >= 2
        and term.casefold() not in ignored_phrases
        and not term.casefold().startswith(
            ("the ", "this ", "that ", "these ", "those ")
        )
    ]
    candidates.sort(key=lambda item: (-item[1], item[0].lower()))
    return [
        {
            "term_id": f"C{index:04d}",
            "source_term": term,
            "translation": None,
            "locked": False,
            "required": False,
            "case_sensitive": True,
            "aliases": [],
            "occurrences": count,
            "pages": sorted(pages[term]),
            "origin": "inferred_candidate",
        }
        for index, (term, count) in enumerate(candidates[:100], start=1)
    ]


def build_work(case_path: Path | None, task_path: Path | None, parsed: dict[str, Any]) -> dict[str, Any]:
    case_data = {}
    case_error = None
    task_error = None
    if case_path and case_path.is_file():
        try:
            case_data = json.loads(case_path.read_text(encoding="utf-8"))
            if not isinstance(case_data, dict):
                raise ValueError("case.json must contain a JSON object")
        except Exception as exc:
            case_data = {}
            case_error = f"Could not read case.json while building translation work: {exc}"
    task_text = ""
    if task_path:
        try:
            task_text = task_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            task_error = f"Could not read task.md while building translation work: {exc}"
    segments: list[dict[str, Any]] = []
    token_index = 1
    document = dictionary(parsed.get("document"))
    for page in document.get("pages") or []:
        if not isinstance(page, dict):
            continue
        for block in page.get("blocks", []):
            kind, text = block_text(block)
            if not text:
                continue
            evidence_ids, bbox = block_evidence(block)
            source_for_translation, tokens, token_index = protect_segment_text(
                text, token_index
            )
            segment_id = f"S{len(segments) + 1:05d}"
            segments.append(
                {
                    "segment_id": segment_id,
                    "page": page["page"],
                    "kind": kind,
                    "source_text": text,
                    "source_for_translation": source_for_translation,
                    "translation": "",
                    "protected_tokens": tokens,
                    "evidence_ids": evidence_ids,
                    "bbox": bbox,
                }
            )

    compact = lambda value: re.sub(r"\s+", "", value)
    for evidence in list_of_dicts(parsed.get("evidence")):
        if evidence.get("kind") != "rotated_text":
            continue
        text = str(evidence.get("text") or "").strip()
        if not text:
            continue
        if any(
            segment.get("page") == evidence.get("page")
            and compact(text) in compact(str(segment.get("source_text") or ""))
            for segment in segments
        ):
            continue
        protected, tokens, token_index = protect_segment_text(
            text, token_index
        )
        segments.append(
            {
                "segment_id": f"S{len(segments) + 1:05d}",
                "page": evidence.get("page"),
                "kind": "rotated_text",
                "source_text": text,
                "source_for_translation": protected,
                "translation": "",
                "protected_tokens": tokens,
                "evidence_ids": [evidence["evidence_id"]],
                "bbox": evidence.get("bbox"),
                "uncertainty": {
                    "scope": "reading_order",
                    "reason": (
                        "Rotated text was recovered separately; its placement "
                        "relative to the main reading order requires review."
                    ),
                    "impact": "Do not claim verified paragraph ordering.",
                },
            }
        )

    explicit_glossary = case_glossary(case_data)
    glossary_by_term: dict[str, dict[str, Any]] = {}
    for entry in explicit_glossary:
        key = entry["source_term"]
        if key in glossary_by_term and glossary_by_term[key] != entry:
            raise ValueError(f"Conflicting glossary entries: {key!r}")
        glossary_by_term[key] = entry
    for candidate in infer_terms(segments):
        key = candidate["source_term"]
        if key not in glossary_by_term:
            glossary_by_term[key] = candidate
    glossary = sorted(
        glossary_by_term.values(),
        key=lambda entry: (
            entry.get("translation") is None,
            -int(entry.get("occurrences", 0)),
            str(entry.get("source_term", "")).casefold(),
        ),
    )
    runtime = dictionary(parsed.get("runtime"))
    parser_status = str(
        runtime.get("extraction_status")
        or parsed.get("status")
        or "partial"
    )
    parser_uncertainties = [
        item
        for item in list_of_dicts(parsed.get("uncertainties"))
        if item.get("scope") != "output_contract"
    ]
    if case_error:
        parser_uncertainties.append(
            {
                "page": None,
                "scope": "case_input",
                "reason": case_error,
                "impact": "The output contract and supplied glossary may be incomplete.",
            }
        )
    if task_error:
        parser_uncertainties.append(
            {
                "page": None,
                "scope": "task_input",
                "reason": task_error,
                "impact": "Task-specific translation requirements may be missing.",
            }
        )
    for warning in runtime.get("warnings") or []:
        parser_uncertainties.append(
            {
                "page": None,
                "scope": "parser_runtime",
                "reason": str(warning),
                "impact": "Extraction coverage or reliability requires review.",
            }
        )
    if parser_uncertainties and parser_status == "success":
        parser_status = "partial"
    target_language = (
        case_data.get("target_language")
        or case_data.get("targetLanguage")
        or "Chinese"
    )
    if not isinstance(target_language, str) or not target_language.strip():
        raise ValueError("target language must be a non-empty string")
    work_status = (
        "failure"
        if parser_status == "failure" or not segments
        else "ready_for_translation"
    )

    return {
        "schema_version": "scientific-translation-work/v1",
        "case_id": parsed.get("case_id"),
        "source_file": parsed.get("input_identifier"),
        "task_markdown": task_text,
        "parser_status": parser_status,
        "parser_uncertainties": parser_uncertainties,
        "parser_change_records": list(parsed.get("change_records") or []),
        "evidence": list_of_dicts(parsed.get("evidence")),
        "target_language": target_language,
        "output_contract": case_data.get("output_contract")
        or case_data.get("outputContract")
        or case_data.get("output")
        or {},
        "glossary": glossary,
        "segments": segments,
        "instructions": {
            "translation_language": target_language,
            "preserve_placeholders": True,
            "preserve_paragraph_ids": True,
            "preserve_evidence_ids": True,
            "mark_uncertain": True,
        },
        "status": work_status,
    }


def build_input_failure_work(
    case_path: Path | None,
    task_path: Path | None,
    parsed: dict[str, Any],
    exc: Exception,
) -> dict[str, Any]:
    case_data: dict[str, Any] = {}
    if case_path and case_path.is_file():
        try:
            loaded = json.loads(case_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                case_data = loaded
        except Exception:
            pass
    task_text = ""
    if task_path:
        try:
            task_text = task_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    uncertainty = {
        "page": None,
        "scope": "translation_preparation",
        "reason": str(exc),
        "impact": "No translation worklist could be prepared.",
    }
    return {
        "schema_version": "scientific-translation-work/v1",
        "case_id": parsed.get("case_id"),
        "source_file": parsed.get("input_identifier"),
        "task_markdown": task_text,
        "parser_status": "failure",
        "parser_uncertainties": [
            *list_of_dicts(parsed.get("uncertainties")),
            uncertainty,
        ],
        "parser_change_records": list(parsed.get("change_records") or []),
        "evidence": list_of_dicts(parsed.get("evidence")),
        "target_language": case_data.get("target_language")
        or case_data.get("targetLanguage")
        or "Chinese",
        "output_contract": case_data.get("output_contract")
        or case_data.get("outputContract")
        or case_data.get("output")
        or {},
        "glossary": [],
        "segments": [],
        "instructions": {
            "translation_language": case_data.get("target_language")
            or case_data.get("targetLanguage")
            or "Chinese",
            "preserve_placeholders": True,
            "preserve_paragraph_ids": True,
            "preserve_evidence_ids": True,
            "mark_uncertain": True,
        },
        "status": "failure",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default="/input/case.json")
    parser.add_argument("--task", default="/input/task.md")
    parser.add_argument("--pdf")
    parser.add_argument("--work", default="/app/work/translation_work.json")
    parser.add_argument("--parsed", default="/app/work/parsed_document.json")
    parser.add_argument("--ocr", choices=("auto", "off", "required"), default="auto")
    parser.add_argument("--ocr-language", default="eng+chi_sim")
    parser.add_argument("--max-pages", type=positive_int)
    args = parser.parse_args()
    case_path = Path(args.case).expanduser().resolve()
    task_path = Path(args.task).expanduser().resolve()
    parse_args = argparse.Namespace(
        case=str(case_path),
        task=str(task_path),
        pdf=args.pdf,
        out=args.parsed,
        ocr=args.ocr,
        ocr_language=args.ocr_language,
        max_pages=args.max_pages,
    )
    parsed_path = Path(args.parsed).expanduser().resolve()
    try:
        parsed = parse_document(parse_args)
        write_artifacts(parsed, parsed_path)
    except Exception as exc:
        parsed = build_failure_result(parse_args, exc)
        write_artifacts(parsed, parsed_path)
    try:
        work = build_work(case_path, task_path, parsed)
    except Exception as exc:
        work = build_input_failure_work(
            case_path, task_path, parsed, exc
        )
    work_path = Path(args.work).expanduser().resolve()
    write_json(work_path, work)
    print(
        json.dumps(
            {
                "status": work["status"],
                "work": str(work_path),
                "parsed": str(parsed_path),
                "segments": len(work["segments"]),
                "glossary": len(work["glossary"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
