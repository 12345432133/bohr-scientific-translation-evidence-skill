# Translation Rules

## Terminology

- Resolve all required and locked glossary entries before translating
  paragraphs. Review inferred candidates rather than automatically enforcing
  every candidate.
- Keep the same target term for a locked source term in titles, headings, body text, captions, tables, and conclusions.
- Respect each term's `case_sensitive` setting and explicit `aliases`; match
  overlapping source terms longest-first.
- An acronym may remain unchanged only when that form is clearer or explicitly required by the case.
- Record an ambiguous polysemous term instead of silently switching translations between paragraphs.

## Relations and Claims

- Preserve negation, scope, quantification, modality, condition, contrast, and causal direction.
- Do not turn correlation into causation or possibility into certainty.
- Keep comparison operators and thresholds attached to the values they modify.

## Protected Elements

- Restore citation markers and author-year citations verbatim.
- Keep numeric values, units, chemical formulas, equation numbers, Greek symbols, and figure/table references unchanged.
- Do not convert units unless the task explicitly asks for conversion.
- Keep URLs, DOIs, accession IDs, gene/protein names, and dataset identifiers unchanged.
- Protect literal `[[P...]]` text present in the source so generated
  placeholders cannot collide with it.

## Paragraph Quality

- One input segment produces one translated segment.
- Use source paragraphs and headings as semantic boundaries.
- Do not merge citations from separate paragraphs.
- Mark a segment uncertain if the source itself is corrupted or the translation requires a domain assumption.
