# Evidence-Preserving Scientific Translation

This Skill turns a scientific PDF into an evidence-linked bilingual translation worklist, then assembles and validates the final result after the Agent translates the segments.

The worklist carries parser uncertainties and table captions into the final
stage. Assembly preserves the parser status, all output-contract fields, and
evidence, while validation rejects missing or duplicated placeholders,
inconsistent locked terms, incomplete coverage, and contract type/value
mismatches.

## Commands

```bash
python scripts/prepare_translation.py \
  --case /input/case.json \
  --task /input/task.md \
  --work /app/work/translation_work.json \
  --parsed /app/work/parsed_document.json

# Complete glossary translations and segment translations in translation_work.json.

python scripts/assemble_result.py \
  --work /app/work/translation_work.json \
  --out /app/output/result.json

python scripts/validate_translation.py \
  --work /app/work/translation_work.json \
  --result /app/output/result.json
```

## Files

- `scripts/parse_pdf.py`: evidence-linked PDF extraction
- `scripts/prepare_translation.py`: segmentation, glossary candidates, protected tokens
- `scripts/assemble_result.py`: status-preserving bilingual and contract assembly
- `scripts/validate_translation.py`: coverage, term, placeholder, and contract checks
- `references/translation-rules.md`: scientific translation invariants
- `references/workflow-example.md`: illustrative segment schema

The Skill translates the actual supplied paper. It does not include prepared answers or hidden-case outputs.
