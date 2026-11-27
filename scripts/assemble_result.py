#!/usr/bin/env python3
"""Assemble translated segments into the final evidence-linked bilingual result."""

from __future__ import annotations

import argparse
import json
import re
import traceback
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PLACEHOLDER_RE = re.compile(r"\[\[(P\d+)\]\]")
CONTRACT_ALIASES = {
    "bilingual_segments": "bilingual_segments",
    "bilingual_segment": "bilingual_segments",
    "translation_segments": "bilingual_segments",
    "translation_segment": "bilingual_segments",
    "translations": "bilingual_segments",
    "双语翻译": "bilingual_segments",
    "双语段落": "bilingual_segments",
    "翻译段落": "bilingual_segments",
    "glossary": "glossary",
    "terminology": "glossary",
    "术语表": "glossary",
    "术语": "glossary",
    "evidence": "evidence",
    "source_evidence": "evidence",
    "证据": "evidence",
    "来源证据": "evidence",
    "uncertainties": "uncertainties",
    "uncertainty": "uncertainties",
    "limitations": "uncertainties",
    "不确定性": "uncertainties",
    "风险": "uncertainties",
    "status": "status",
    "状态": "status",
    "case_id": "case_id",
    "caseid": "case_id",
    "target_language": "target_language",
    "目标语言": "target_language",
    "change_records": "change_records",
    "变更记录": "change_records",
    "source": "source",
    "来源": "source",
    "schema_version": "schema_version",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def normalize_key(value: Any) -> str:
    return re.sub(
        r"[^a-z0-9\u3400-\u9fff]+", "_", str(value).casefold()
    ).strip("_")


def contract_keys(contract: Any) -> list[str]:
    if isinstance(contract, dict):
        if isinstance(contract.get("properties"), dict):
            keys = list(contract["properties"])
            required = contract.get("required")
            if isinstance(required, list):
                keys.extend(required)
            return list(dict.fromkeys(str(key) for key in keys))
        return [str(key) for key in contract]
    if isinstance(contract, list):
        keys: list[str] = []
        for item in contract:
            if isinstance(item, str):
                keys.append(item)
            elif isinstance(item, dict):
                for field in ("name", "field", "key", "id"):
                    if item.get(field):
                        keys.append(str(item[field]))
                        break
        return list(dict.fromkeys(keys))
    return []


def uncertainty_record(
    reason: Any,
    *,
    scope: str,
    segment_id: str | None = None,
    page: int | None = None,
    impact: str = "Final translation quality or coverage may be incomplete.",
) -> dict[str, Any]:
    return {
        "segment_id": segment_id,
        "page": page,
        "scope": scope,
        "reason": str(reason),
        "impact": impact,
    }


def normalize_uncertainties(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    results: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            result = dict(item)
            result.setdefault("scope", "unspecified")
            result.setdefault("reason", "Unspecified uncertainty.")
            result.setdefault("impact", "Review the source and translation.")
            results.append(result)
        elif str(item).strip():
            results.append(uncertainty_record(item, scope="unspecified"))
    return results


def contains_term(text: str, term: str) -> bool:
    if not term:
        return True
    pattern = re.escape(term)
    if term[0].isascii() and term[0].isalnum():
        pattern = r"(?<![A-Za-z0-9_])" + pattern
    if term[-1].isascii() and term[-1].isalnum():
        pattern += r"(?![A-Za-z0-9_])"
    return bool(re.search(pattern, text))


def glossary_readiness(glossary: Any) -> list[str]:
    if not isinstance(glossary, list):
        raise ValueError("glossary must be a list")
    issues: list[str] = []
    for entry in glossary:
        if not isinstance(entry, dict):
            raise ValueError("glossary entries must be objects")
        for field, default in (
            ("locked", False),
            ("required", False),
            ("case_sensitive", True),
        ):
            if not isinstance(entry.get(field, default), bool):
                raise ValueError(f"glossary.{field} must be boolean")
        translation = entry.get("translation")
        if translation is not None and not isinstance(translation, str):
            raise ValueError("glossary.translation must be text or null")
        name = entry.get("source_term")
        if entry.get("required") and not entry.get("locked"):
            issues.append(f"Required term {name!r} has not been locked")
        if (
            entry.get("required") or entry.get("locked")
        ) and not (
            isinstance(translation, str) and translation.strip()
        ):
            issues.append(
                f"Required/locked term {name!r} has no translation"
            )
    locked_term_occurrences("", glossary)
    return issues


def locked_term_occurrences(
    source: str, glossary: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    matches: list[tuple[int, int, int, dict[str, Any]]] = []
    for index, entry in enumerate(glossary):
        locked = entry.get("locked", False)
        if not isinstance(locked, bool):
            raise ValueError("glossary.locked must be boolean")
        if not entry.get("locked"):
            continue
        term = entry.get("source_term")
        aliases = entry.get("aliases", [])
        case_sensitive = entry.get("case_sensitive", True)
        if not isinstance(term, str) or not term.strip():
            raise ValueError("Locked glossary term is empty")
        if not isinstance(case_sensitive, bool):
            raise ValueError("glossary.case_sensitive must be boolean")
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) and alias.strip() for alias in aliases
        ):
            raise ValueError("glossary.aliases must contain non-empty strings")
        flags = 0 if case_sensitive else re.IGNORECASE
        for spelling in dict.fromkeys([term, *aliases]):
            pattern = re.escape(spelling)
            if spelling[0].isascii() and spelling[0].isalnum():
                pattern = r"(?<![A-Za-z0-9_])" + pattern
            if spelling[-1].isascii() and spelling[-1].isalnum():
                pattern += r"(?![A-Za-z0-9_])"
            for match in re.finditer(pattern, source, flags):
                matches.append(
                    (match.start(), match.end(), index, entry)
                )

    occupied: list[tuple[int, int]] = []
    selected: dict[int, dict[str, Any]] = {}
    for start, end, index, entry in sorted(
        matches,
        key=lambda item: (-(item[1] - item[0]), item[0], item[2]),
    ):
        if any(start < right and left < end for left, right in occupied):
            continue
        occupied.append((start, end))
        selected.setdefault(index, entry)
    return list(selected.values())


def restore_tokens(
    translation: str, tokens: list[dict[str, Any]]
) -> tuple[str, list[str]]:
    issues: list[str] = []
    expected: dict[str, dict[str, Any]] = {}
    for token in tokens:
        if not isinstance(token, dict) or not token.get("token_id"):
            issues.append("Malformed protected token record.")
            continue
        token_id = str(token["token_id"])
        if token_id in expected:
            issues.append(f"Duplicate protected-token definition: {token_id}.")
        expected[token_id] = token

    markers = PLACEHOLDER_RE.findall(translation)
    marker_counts = {token_id: markers.count(token_id) for token_id in expected}
    for token_id, count in marker_counts.items():
        if count == 0:
            issues.append(f"Missing placeholder: {token_id}.")
        elif count > 1:
            issues.append(
                f"Placeholder {token_id} appears {count} times; expected exactly once."
            )
    unknown = sorted(set(markers) - set(expected))
    if unknown:
        issues.append(f"Unknown placeholders remain: {unknown}.")

    restored = PLACEHOLDER_RE.sub(
        lambda match: (
            str(expected[match.group(1)].get("raw", ""))
            if match.group(1) in expected
            else match.group(0)
        ),
        translation,
    )
    return restored, issues


def add_evidence(
    evidence: list[dict[str, Any]],
    evidence_index: dict[str, dict[str, Any]],
    evidence_id: str,
    segment_id: str,
    page: int | None,
    bbox: Any,
    kind: Any,
) -> None:
    existing = evidence_index.get(evidence_id)
    if existing:
        if existing.get("page") != page:
            raise ValueError(
                f"Segment {segment_id} page {page} references evidence "
                f"{evidence_id} on page {existing.get('page')}"
            )
        existing.setdefault("segment_id", segment_id)
        existing["segment_ids"] = list(
            dict.fromkeys([*existing.get("segment_ids", []), segment_id])
        )
        return
    raise ValueError(
        f"Segment {segment_id} references absent parser evidence {evidence_id}"
    )


def assemble_work(work: dict[str, Any]) -> dict[str, Any]:
    raw_segments = work.get("segments")
    if not isinstance(raw_segments, list):
        raise ValueError("translation work must contain a segments list")
    glossary = deepcopy(work.get("glossary"))
    glossary_issues = glossary_readiness(glossary)
    uncertainties = normalize_uncertainties(work.get("parser_uncertainties"))
    uncertainties.extend(
        uncertainty_record(issue, scope="glossary")
        for issue in glossary_issues
    )
    bilingual: list[dict[str, Any]] = []
    raw_evidence = work.get("evidence")
    if not isinstance(raw_evidence, list):
        raise ValueError("work.evidence must contain original parser evidence")
    evidence = deepcopy(raw_evidence)
    evidence_index: dict[str, dict[str, Any]] = {}
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("Malformed parser evidence")
        evidence_id = item.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            raise ValueError("Parser evidence has no valid evidence_id")
        if evidence_id in evidence_index:
            raise ValueError(f"Duplicate parser evidence: {evidence_id}")
        evidence_index[evidence_id] = item
    translated_count = 0
    segment_count = len(raw_segments)

    for raw_segment in raw_segments:
        if not isinstance(raw_segment, dict):
            uncertainties.append(
                uncertainty_record(
                    "Malformed segment entry.",
                    scope="worklist",
                    impact="One segment could not be assembled.",
                )
            )
            continue
        segment_id = str(raw_segment.get("segment_id") or "unknown")
        source = str(raw_segment.get("source_text") or "")
        raw_translation = raw_segment.get("translation", "")
        translation = raw_translation.strip() if isinstance(raw_translation, str) else ""
        if raw_translation not in (None, "") and not isinstance(raw_translation, str):
            uncertainties.append(
                uncertainty_record(
                    f"Translation has invalid type {type(raw_translation).__name__}.",
                    scope="translation",
                    segment_id=segment_id,
                )
            )
        segment_issues: list[str] = []
        if not translation:
            segment_issues.append("Translation is empty.")
        else:
            translated_count += 1
        raw_tokens = raw_segment.get("protected_tokens") or []
        if not isinstance(raw_tokens, list):
            raw_tokens = []
            segment_issues.append("protected_tokens is not a list.")
        restored, token_issues = restore_tokens(translation, raw_tokens)
        segment_issues.extend(token_issues)
        for entry in locked_term_occurrences(source, glossary):
            target_term = str(entry.get("translation") or "").strip()
            if not target_term:
                segment_issues.append(
                    f"Locked term {entry.get('source_term')!r} has no translation."
                )
            elif not contains_term(restored, target_term):
                segment_issues.append(
                    f"Locked term {entry.get('source_term')!r} requires "
                    f"{target_term!r} in this segment."
                )
        for issue in segment_issues:
            uncertainties.append(
                uncertainty_record(
                    issue,
                    scope="translation",
                    segment_id=segment_id,
                )
            )
        segment_uncertainty = raw_segment.get("uncertainty")
        normalized_uncertainty: list[dict[str, Any]] = []
        if segment_uncertainty not in (None, "", [], {}):
            normalized_uncertainty = normalize_uncertainties(segment_uncertainty)
            for item in normalized_uncertainty:
                item.setdefault("segment_id", segment_id)
            uncertainties.extend(normalized_uncertainty)

        entry = {
            "segment_id": segment_id,
            "page": raw_segment.get("page"),
            "kind": raw_segment.get("kind"),
            "source_text": source,
            "translated_text": restored,
            "evidence_ids": [
                str(value)
                for value in (raw_segment.get("evidence_ids") or [])
                if value
            ],
            "bbox": raw_segment.get("bbox"),
            "translation_status": (
                "missing"
                if not translation
                else "partial"
                if segment_issues or normalized_uncertainty
                else "translated"
            ),
            "uncertainty": segment_uncertainty,
        }
        bilingual.append(entry)
        for evidence_id in entry["evidence_ids"]:
            add_evidence(
                evidence,
                evidence_index,
                evidence_id,
                segment_id,
                entry["page"],
                entry["bbox"],
                entry["kind"],
            )

    parser_status = str(work.get("parser_status") or "partial")
    if parser_status == "failure" or translated_count == 0:
        status = "failure"
    elif (
        parser_status != "success"
        or translated_count < segment_count
        or uncertainties
    ):
        status = "partial"
    else:
        status = "success"

    change_records = [
        record
        for record in (work.get("parser_change_records") or [])
        if isinstance(record, dict)
    ]
    change_records.append(
        {
            "at": utc_now(),
            "action": "assemble_translation",
            "detail": (
                f"Assembled {translated_count}/{segment_count} translated segments; "
                f"parser status was {parser_status}."
            ),
        }
    )
    core = {
        "schema_version": "evidence-preserving-scientific-translation/v1",
        "case_id": work.get("case_id"),
        "status": status,
        "target_language": work.get("target_language"),
        "source_parse_status": parser_status,
        "glossary": glossary,
        "bilingual_segments": bilingual,
        "evidence": evidence,
        "uncertainties": uncertainties,
        "change_records": change_records,
        "source": work.get("source_file"),
    }
    contract = work.get("output_contract") or {}
    keys = contract_keys(contract)
    contract_issues: list[str] = []
    for key in keys:
        core_key = CONTRACT_ALIASES.get(normalize_key(key))
        if not core_key or core_key not in core:
            contract_issues.append(
                f"Output contract field {key!r} has no exact mapping."
            )
    if not keys:
        contract_issues.append(
            "No output_contract keys were found; task-specific fields may be missing."
        )
    if contract_issues:
        for issue in contract_issues:
            uncertainties.append(
                uncertainty_record(
                    issue,
                    scope="output_contract",
                    impact="A required task-specific field may be incomplete.",
                )
            )
        if status == "success":
            status = "partial"
            core["status"] = status
    contract_fields: dict[str, Any] = {}
    for key in keys:
        core_key = CONTRACT_ALIASES.get(normalize_key(key))
        contract_fields[key] = (
            core.get(core_key) if core_key and core_key in core else None
        )

    result = {**contract_fields, **core}
    result["contract_output"] = {
        key: result.get(key) for key in keys
    }
    return result


def failure_result(work: dict[str, Any] | None, exc: Exception) -> dict[str, Any]:
    uncertainty = uncertainty_record(
        exc,
        scope="runtime",
        impact="No usable bilingual result was produced.",
    )
    keys = contract_keys(
        work.get("output_contract") if isinstance(work, dict) else {}
    )
    contract_fields = {key: None for key in keys}
    result = {
        **contract_fields,
        "schema_version": "evidence-preserving-scientific-translation/v1",
        "case_id": work.get("case_id") if isinstance(work, dict) else None,
        "status": "failure",
        "target_language": (
            work.get("target_language") if isinstance(work, dict) else None
        ),
        "source_parse_status": (
            work.get("parser_status") if isinstance(work, dict) else None
        ),
        "glossary": [],
        "bilingual_segments": [],
        "evidence": [],
        "uncertainties": [uncertainty],
        "change_records": [
            {
                "at": utc_now(),
                "action": "failure",
                "detail": str(exc),
            }
        ],
        "source": (
            work.get("source_file") if isinstance(work, dict) else None
        ),
        "contract_output": contract_fields,
        "runtime": {
            "traceback": traceback.format_exc(),
        },
    }
    for key in keys:
        core_key = CONTRACT_ALIASES.get(normalize_key(key))
        if core_key and core_key in result:
            result[key] = result[core_key]
    result["contract_output"] = {
        key: result.get(key) for key in keys
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", required=True)
    parser.add_argument("--out", default="/app/output/result.json")
    args = parser.parse_args()
    work: dict[str, Any] | None = None
    try:
        work = load_json(Path(args.work).expanduser().resolve())
        result = assemble_work(work)
        exit_code = 0 if result["status"] != "failure" else 2
    except Exception as exc:
        result = failure_result(work, exc)
        exit_code = 2
    output_path = Path(args.out).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "output": str(output_path),
                "segments": len(result["bilingual_segments"]),
                "translated": sum(
                    1
                    for segment in result["bilingual_segments"]
                    if segment.get("translated_text")
                ),
                "uncertainties": len(result["uncertainties"]),
            },
            ensure_ascii=False,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
