---
name: evidence-preserving-scientific-translation
description: Translate scientific papers into evidence-linked bilingual paragraphs. Use for terminology-controlled literature translation, long-document continuation, citation-safe bilingual output, or paragraph-level translation QA.
---

# Evidence-Preserving Scientific Translation

Translate the actual supplied paper, not a summary. Preserve paragraph identity, terminology, citations, numbers, units, equation symbols, and the evidence needed to revisit every source segment.

## Workflow

1. Read `/input/case.json` and `/input/task.md`. Identify the target language, requested paragraph range, output contract, and any supplied glossary.
2. Build the evidence and translation worklist:

   ```bash
   python scripts/prepare_translation.py \
     --case /input/case.json \
     --task /input/task.md \
     --work /app/work/translation_work.json \
     --parsed /app/work/parsed_document.json
   ```

   The worklist records the parser status, non-contract extraction
   uncertainties, evidence IDs, protected tokens, and attached table captions.
   Do not discard those fields while editing the worklist.

3. Complete the glossary before translating body paragraphs. Resolve entries
   marked `required` or `locked`; review inferred candidates and lock only the
   terms that should be enforced. For each selected term, provide one
   target-language equivalent and set `"locked": true`. Use
   `case_sensitive` and explicit `aliases` where needed; do not merge
   scientifically distinct case variants.
4. Translate each `segments[].source_for_translation` into `segments[].translation`.
   - Preserve every placeholder such as `[[P0001]]` exactly.
   - Preserve paragraph IDs and evidence IDs.
   - Keep headings, captions, footnotes, and references in their own segment kinds.
   - Translate separately recovered rotated text as its own segment and retain
     the reading-order uncertainty attached to it.
   - Use adjacent segments only as context. Do not merge, duplicate, or reorder segments.
   - Keep citation markers, numbers, units, formula symbols, figure/table identifiers, and negation exactly aligned with the source.
   - Add `uncertainty` to a segment when a term or relation is genuinely ambiguous.
5. Assemble the final result:

   ```bash
   python scripts/assemble_result.py \
     --work /app/work/translation_work.json \
     --out /app/output/result.json
   ```

6. Validate coverage and protected tokens:

   ```bash
   python scripts/validate_translation.py \
     --work /app/work/translation_work.json \
     --result /app/output/result.json
   ```

7. If validation reports missing segments, inconsistent locked terms, malformed
   placeholders, or contract mismatches, fix the source segment and rerun both
   steps. Do not remove the error or edit the result to force `success`.

## Translation Rules

- Translate the source meaning, not the surface word order. Keep Chinese scientific prose precise and readable.
- Distinguish `not`, `only`, `except`, `unless`, `may`, `suggest`, `associated with`, and causal claims. Never strengthen a claim.
- Preserve author-year citations, numbered citations, equation numbers, chemical notation, Greek letters, superscripts, subscripts, and units.
- Keep acronyms when the task or glossary requires them; on first use, translate the full term and place the acronym in parentheses.
- Do not invent a translation for an illegible source span. Keep the protected source marker or record the uncertainty.
- Do not claim that a scanned page, formula, or table was translated when OCR or extraction failed.
- Do not translate the same source paragraph twice under different segment IDs.
- Keep each table caption with its table segment instead of translating the
  caption as an unrelated paragraph.
- Preserve parser evidence records unchanged; later stages may link segments
  to them but must not replace their source text, locator, precision, or bbox.

## Output Contract

`assemble_result.py` creates the top level of `result.json` with:

- exact `case_id`
- every original output-contract field
- `status`: `success`, `partial`, or `failure`
- `glossary`
- `bilingual_segments`
- `evidence`
- `uncertainties`
- `change_records`
- `source`

Each bilingual segment retains page, kind, source text, translated text, evidence IDs, bbox, and translation status.
The final status cannot be `success` when parser extraction, OCR, table
structure, coverage, placeholders, locked terms, or the output contract is
partial. Unknown contract fields remain present as `null` and are reported.
Runtime warnings such as page truncation are preserved separately from
translation-contract completion and cannot be hidden by rewriting the final
status.

## Failure Handling

- Missing or unreadable PDF: preserve the runtime error and return failure.
- Incomplete OCR: translate recovered text and mark the page partial.
- Ambiguous term: choose the most defensible translation, lock it, and add an uncertainty note.
- Broken protected token: restore the original source span or leave the segment partial; never silently guess.
- Output contract field whose meaning is unknown: keep the field present as `null` and record that it needs task-specific completion.

Read [references/translation-rules.md](references/translation-rules.md) for terminology and relation checks. Read [references/workflow-example.md](references/workflow-example.md) for the worklist shape.

## Dependencies

Python 3.10+ with `pdfplumber`, `pypdf`, `pypdfium2`, and `jsonschema`. The scientific PDF is the factual authority.
