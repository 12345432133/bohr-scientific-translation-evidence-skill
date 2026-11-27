#!/usr/bin/env python3
"""Validate bilingual coverage, terminology, placeholders, and contract fields."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from assemble_result import (
    contains_term,
    glossary_readiness,
    locked_term_occurrences,
    restore_tokens,
)


STATUS_VALUES = {"success", "partial", "failure"}
CONTRACT_TYPES = {
    "bilingual_segments": list,
    "glossary": list,
    "evidence": list,
    "uncertainties": list,
    "status": str,
    "case_id": str,
    "target_language": str,
    "change_records": list,
    "source": (dict, str),
    "schema_version": str,
}
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


def load(path: Path) -> dict[str, Any]:
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


def validate_contract(
    contract: Any, result: dict[str, Any], errors: list[str], warnings: list[str]
) -> None:
    keys = contract_keys(contract)
    contract_output = result.get("contract_output")
    if not isinstance(contract_output, dict):
        errors.append("contract_output must be an object")
        contract_output = {}
    for key in keys:
        if key not in result:
            errors.append(f"output contract field {key!r} is missing at top level")
            continue
        if key not in contract_output:
            errors.append(f"output contract field {key!r} is missing from contract_output")
        elif contract_output[key] != result[key]:
            errors.append(
                f"contract_output[{key!r}] does not match the top-level value"
            )
        core_key = CONTRACT_ALIASES.get(normalize_key(key))
        expected_type = CONTRACT_TYPES.get(core_key or "")
        if expected_type and result[key] is not None and not isinstance(
            result[key], expected_type
        ):
            errors.append(
                f"output contract field {key!r} has type "
                f"{type(result[key]).__name__}"
            )
        if result[key] is None and result.get("status") == "success":
            errors.append(
                f"success status cannot contain null contract field {key!r}"
            )
    if not keys:
        warnings.append("work output_contract did not expose any fields")

    schema = None
    if isinstance(contract, dict):
        if (
            isinstance(contract.get("properties"), dict)
            or contract.get("type") == "object"
            or "$schema" in contract
        ):
            schema = contract
        else:
            properties = {
                key: value
                for key, value in contract.items()
                if isinstance(value, (dict, bool))
            }
            if properties:
                schema = {
                    "type": "object",
                    "properties": properties,
                    "required": keys,
                }

    if schema is not None:
        try:
            from jsonschema.validators import validator_for

            validator_type = validator_for(schema)
            validator_type.check_schema(schema)
            schema_errors = sorted(
                validator_type(schema).iter_errors(result),
                key=lambda error: list(error.path),
            )
            for error in schema_errors:
                errors.append(
                    f"output contract schema: {list(error.path)}: {error.message}"
                )
        except ImportError:
            errors.append("jsonschema unavailable; contract was not validated")
        except Exception as exc:
            errors.append(f"output contract schema validation failed: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    errors: list[str] = []
    warnings: list[str] = []
    glossary_issues: list[str] = []
    try:
        work = load(Path(args.work).expanduser().resolve())
        result = load(Path(args.result).expanduser().resolve())
        for owner, key in (
            (work, "segments"),
            (work, "glossary"),
            (work, "evidence"),
            (result, "bilingual_segments"),
            (result, "evidence"),
            (result, "change_records"),
        ):
            if not isinstance(owner.get(key), list):
                raise ValueError(f"{key} must be a list")
        glossary_issues = glossary_readiness(work["glossary"])
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "invalid",
                    "errors": [str(exc)],
                    "warnings": [],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    errors.extend(glossary_issues)

    work_segments: dict[str, dict[str, Any]] = {}
    for segment in work.get("segments") or []:
        if not isinstance(segment, dict):
            errors.append("work segments contains a non-object entry")
            continue
        segment_id = str(segment.get("segment_id") or "")
        if not segment_id:
            errors.append("work segment is missing segment_id")
            continue
        if segment_id in work_segments:
            errors.append(f"duplicate work segment_id: {segment_id}")
        work_segments[segment_id] = segment

    result_segments: dict[str, dict[str, Any]] = {}
    for segment in result.get("bilingual_segments") or []:
        if not isinstance(segment, dict):
            errors.append("bilingual_segments contains a non-object entry")
            continue
        segment_id = str(segment.get("segment_id") or "")
        if not segment_id:
            errors.append("bilingual segment is missing segment_id")
            continue
        if segment_id in result_segments:
            errors.append(f"duplicate bilingual segment_id: {segment_id}")
        result_segments[segment_id] = segment
    if set(work_segments) != set(result_segments):
        missing = sorted(set(work_segments) - set(result_segments))
        extra = sorted(set(result_segments) - set(work_segments))
        errors.append(f"segment coverage mismatch; missing={missing}, extra={extra}")
    if list(work_segments) != list(result_segments):
        errors.append("segment order differs from the worklist")

    work_evidence: dict[str, dict[str, Any]] = {}
    for item in work["evidence"]:
        if not isinstance(item, dict):
            errors.append("work evidence contains a non-object entry")
            continue
        evidence_id = item.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            errors.append("work evidence is missing evidence_id")
            continue
        if evidence_id in work_evidence:
            errors.append(f"duplicate work evidence_id: {evidence_id}")
        work_evidence[evidence_id] = item
    result_evidence: dict[str, dict[str, Any]] = {}
    for item in result["evidence"]:
        if not isinstance(item, dict):
            errors.append("result evidence contains a non-object entry")
            continue
        evidence_id = item.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            errors.append("result evidence is missing evidence_id")
            continue
        if evidence_id in result_evidence:
            errors.append(f"duplicate result evidence_id: {evidence_id}")
        result_evidence[evidence_id] = item
    if set(work_evidence) != set(result_evidence):
        errors.append(
            "result evidence IDs differ from the parser evidence: "
            f"missing={sorted(set(work_evidence) - set(result_evidence))}, "
            f"extra={sorted(set(result_evidence) - set(work_evidence))}"
        )
    for evidence_id in set(work_evidence) & set(result_evidence):
        for field, value in work_evidence[evidence_id].items():
            if result_evidence[evidence_id].get(field) != value:
                errors.append(
                    f"evidence {evidence_id}: parser field changed: {field}"
                )

    glossary = [
        entry for entry in (work.get("glossary") or []) if isinstance(entry, dict)
    ]
    missing_terms = [
        entry.get("term_id") or entry.get("source_term")
        for entry in glossary
        if entry.get("locked") and not str(entry.get("translation") or "").strip()
    ]
    if missing_terms:
        errors.append(f"locked glossary entries missing translation: {missing_terms}")

    for segment_id, source in work_segments.items():
        translated = result_segments.get(segment_id, {})
        refs = source.get("evidence_ids")
        if (
            not isinstance(refs, list)
            or not refs
            or not all(isinstance(ref, str) and ref for ref in refs)
        ):
            errors.append(f"{segment_id}: invalid or empty evidence_ids")
        else:
            for ref in refs:
                item = result_evidence.get(ref)
                if item is None:
                    errors.append(f"{segment_id}: absent evidence {ref}")
                    continue
                if item.get("page") != source.get("page"):
                    errors.append(
                        f"{segment_id}: evidence page mismatch: {ref}"
                    )
                bindings = item.get("segment_ids")
                if (
                    not isinstance(bindings, list)
                    or segment_id not in bindings
                ):
                    errors.append(
                        f"{segment_id}: missing evidence binding: {ref}"
                    )
        for field in ("source_text", "page", "kind", "evidence_ids", "bbox"):
            if translated.get(field) != source.get(field):
                errors.append(f"{segment_id}: source field changed: {field}")
        raw_target = translated.get("translated_text")
        if not isinstance(raw_target, str):
            errors.append(f"{segment_id}: translated_text must be a string")
            continue
        target = raw_target.strip()
        if not target:
            errors.append(f"{segment_id}: empty translation")
            continue
        tokens = source.get("protected_tokens") or []
        if not isinstance(tokens, list):
            errors.append(f"{segment_id}: protected_tokens is not a list")
            tokens = []
        protected_translation = source.get("translation")
        if not isinstance(protected_translation, str):
            errors.append(f"{segment_id}: work translation must be a string")
            continue
        restored, token_issues = restore_tokens(
            protected_translation.strip(), tokens
        )
        errors.extend(f"{segment_id}: {issue}" for issue in token_issues)
        if restored != target:
            errors.append(
                f"{segment_id}: result differs from one-pass token restoration"
            )
        for entry in locked_term_occurrences(
            str(source.get("source_text") or ""), glossary
        ):
            target_term = str(entry.get("translation") or "").strip()
            if target_term and not contains_term(target, target_term):
                errors.append(
                    f"{segment_id}: locked term "
                    f"{entry.get('source_term')!r} requires {target_term!r}"
                )
        if (
            translated.get("translation_status") == "translated"
            and not target
        ):
            errors.append(f"{segment_id}: translation_status is inconsistent")
        if (
            source.get("kind") == "heading"
            and len(target) > max(200, len(str(source.get("source_text"))) * 4)
        ):
            warnings.append(f"{segment_id}: heading translation unusually long")

    for evidence_id, item in result_evidence.items():
        bindings = item.get("segment_ids", [])
        if not isinstance(bindings, list):
            errors.append(f"{evidence_id}: segment_ids must be a list")
            continue
        for segment_id in bindings:
            segment = (
                result_segments.get(segment_id)
                if isinstance(segment_id, str)
                else None
            )
            refs = segment.get("evidence_ids") if segment else None
            if not isinstance(refs, list) or evidence_id not in refs:
                errors.append(
                    f"{evidence_id}: invalid reverse binding {segment_id!r}"
                )

    if result.get("case_id") != work.get("case_id"):
        errors.append("result case_id does not match the translation worklist")
    status = result.get("status")
    if not isinstance(status, str) or status not in STATUS_VALUES:
        errors.append(f"invalid status: {status!r}")
    parser_status = str(work.get("parser_status") or "partial")
    if result.get("source_parse_status") != work.get("parser_status"):
        errors.append("result source_parse_status does not match the worklist")
    uncertainties = result.get("uncertainties")
    if not isinstance(uncertainties, list):
        errors.append("uncertainties must be a list")
        uncertainties = []
    if status == "success" and (
        uncertainties
        or parser_status != "success"
        or work.get("parser_uncertainties")
    ):
        errors.append("success status conflicts with parser or translation uncertainties")
    if status == "failure" and parser_status != "failure" and work_segments:
        warnings.append("result is failure although the parser produced segments")
    if status == "partial" and not uncertainties:
        warnings.append("partial status has no explicit uncertainty")

    validate_contract(work.get("output_contract") or {}, result, errors, warnings)

    report = {
        "status": "valid" if not errors else "invalid",
        "segments": len(work_segments),
        "translated_segments": sum(
            1
            for segment in result_segments.values()
            if str(segment.get("translated_text") or "").strip()
        ),
        "glossary_entries": len(glossary),
        "errors": list(dict.fromkeys(errors)),
        "warnings": list(dict.fromkeys(warnings)),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
