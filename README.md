# wheels-middleware

FastAPI middleware for the Wheels Contract Intelligence platform: PDF parsing + OCR,
LLM extraction of billing terms with confidence + citations, the approval-lifecycle state
machine, and the invoice-audit engine. Deploys to AWS Lambda (ap-south-2) behind API Gateway.

## Phase 1 (built): extraction core + golden suite

```
app/
  config.py                 settings (regions, models, thresholds)
  ocr/                      pluggable document parsing (engine-agnostic normalized geometry)
    base.py                 BBox / Word / PageParse / DocumentParse / OCREngine
    pymupdf_engine.py       native-text: text + word bboxes + page PNG render ($0, no GPU)
    citations.py            deterministic quote -> normalized highlight rects
  extraction/
    schema.py               Pydantic contract = LLM tool schema = stored/API shape
    prompts.py              system prompt + page-delimited user content
    service.py              parse -> LLM tool-use -> resolve citations -> flag review
  llm/
    base.py                 provider-neutral forced-tool-use interface
    bedrock_provider.py     primary: Bedrock Converse (in-region, IAM, no key)
    anthropic_provider.py   fallback/dev: Anthropic API (ANTHROPIC_API_KEY)
    tools.py                builds the tool schema from the Pydantic model
tests/
  golden/ground_truth.py    section-4 ground truth: full ContractExtraction objects + expectations
  test_schema_golden.py     schema expresses every pricing shape; self-consistency
  test_pymupdf_engine.py    parses the real PDFs (native, page counts, normalized bboxes)
  test_citations.py         highlight feature on the real $610 / $465 Schedule B anchors
  test_extraction_live.py   @pytest.mark.llm  live eval vs golden + token/cost report
```

## Run

```bash
make install        # venv + deps
make test           # deterministic suite (no LLM, $0)  -> 29 passed
make lint

# Live extraction eval (needs a credential):
LLM_PROVIDER=bedrock   make test-llm     # requires Bedrock model access in ap-south-2
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-... make test-llm
```

## Design notes

- **Citations are resolved deterministically**, not trusted from the model: the LLM returns a
  verbatim `quote`; we locate it in the parsed word geometry and compute the true normalized
  bbox. Matching is whitespace/punctuation-insensitive so table dot-leaders don't defeat it.
- **One tool-use call per document** extracts all 13 service lines + lease terms; output is
  validated against `ContractExtraction`.
- **OCR is pluggable**: PyMuPDF (native, launch) today; Textract (scanned, cross-region
  ap-south-1) and Unlimited-OCR (GPU, future) slot behind the same `OCREngine` + normalized
  coordinate contract, so the UI highlight code never changes.
