# Worklist Example

This is an illustrative shape, not a formal test answer.

```json
{
  "case_id": "example-translation-001",
  "parser_status": "success",
  "parser_uncertainties": [],
  "evidence": [
    {
      "evidence_id": "p001-l0001",
      "page": 1,
      "bbox": [45.0, 120.0, 540.0, 131.0],
      "kind": "body",
      "text": "The model uses an attention mechanism [12].",
      "coordinate_precision": "line"
    }
  ],
  "target_language": "Chinese",
  "glossary": [
    {
      "term_id": "T0001",
      "source_term": "attention mechanism",
      "translation": "注意力机制",
      "locked": true
    }
  ],
  "segments": [
    {
      "segment_id": "S00001",
      "page": 1,
      "kind": "paragraph",
      "source_text": "The model uses an attention mechanism [12].",
      "source_for_translation": "The model uses an attention mechanism [[P0001]].",
      "translation": "该模型使用注意力机制 [[P0001]]。",
      "protected_tokens": [
        {
          "token_id": "P0001",
          "raw": "[12]",
          "kind": "citation"
        }
      ],
      "evidence_ids": ["p001-l0001"]
    }
  ]
}
```

After translation, `assemble_result.py` restores `[[P0001]]` to `[12]` and retains the evidence ID.
If `parser_status` is `partial` or any coverage, terminology, placeholder, or
contract check fails, the assembled result must remain `partial` or `failure`.
