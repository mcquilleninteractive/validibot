# PDF Package Validator

The PDF Package Validator is an isolated, industry-neutral backend. It inspects
the PDF wrapper, records standardized package structures, and may extract an
exactly selected XML, JSON, or STEP Part 21 member. It does not render pages,
run scripts or media, verify signatures, or claim that an extracted payload is
valid for its business domain.

The public inventory contract is `validibot.pdf_inventory.v2`. Its typed models
live in `validibot-shared`; the backend and Django application import those
models instead of maintaining local copies.

## The file-port contract

Every file crossing the application/backend boundary has a required
`port_key`. The key comes from the validator catalog and is the file's sole
selection identity. `role`, `type`, filename, MIME type, and list position are
descriptive metadata and must never be used as substitutes.

The PDF backend has one input port:

| Direction | Port key | Cardinality | Carrier |
| --- | --- | --- | --- |
| Input | `pdf_document` | exactly one | `input_files`, PDF |

It can publish these fixed outputs:

| Port key | Cardinality | Meaning |
| --- | --- | --- |
| `pdf_inventory` | exactly one | Canonical `validibot.pdf_inventory.v2` JSON evidence. |
| `extracted_files_bundle` | zero or one | Deterministic ZIP of extraction-eligible members. |
| `xmp_metadata` | zero or one | Original safely readable document XMP packet. |
| `selected_xml` | zero or one | One selector-matched, carrier-preflighted XML member. |
| `selected_json` | zero or one | One selector-matched, carrier-preflighted JSON member. |
| `selected_step_p21` | zero or one | One selector-matched STEP Part 21 member. |

Later workflow steps bind to these output keys through the generic artifact
binding system. There is no PDF-specific cross-step transport and no implicit
fallback to the submitted PDF when a selected artifact is absent.

## Using `validibot-shared` in a backend

Pin the exact shared-contract version declared for the backend release. Import
the PDF envelope and output models from `validibot_shared.pdf`, and select the
input with the common port helper:

```python
from validibot_shared.pdf import PdfInputEnvelope
from validibot_shared.validations.file_ports import select_input_file


def pdf_document(envelope: PdfInputEnvelope):
    return select_input_file(
        envelope.input_files,
        port_key="pdf_document",
    )
```

`select_input_file()` fails if the required key is missing or appears more than
once. An optional resource port uses `select_resource_file(...,
required=False)`. Do not add a backend-local matcher, inspect `role` or `type`
to choose a file, or read `input_files[0]`.

When the wire contract changes, update `validibot-shared` first. Publish that
version, then update the backend dependency pin, every per-backend direct
requirement and lock, the application pin and lock, and all generated SBOMs.
The application, container, and evidence must all name the same contract
version.

## Exact payload selectors

Each selected output uses `PdfPayloadSelector`. A selector must contain at
least one exact match field; it never means “take the first member.” Multiple
fields are combined, so a member must satisfy all populated criteria.

Supported fields are:

- discovery kind;
- original filename;
- declared media type;
- detected media type;
- associated-file relationship;
- rich-media asset name;
- XML root qualified name; and
- one or more STEP `FILE_SCHEMA` identifiers.

No match is allowed for an optional selector and is an error for a required
selector. More than one match is always an error. XML and JSON selections also
receive a bounded carrier parse before publication. STEP selection confirms
the Part 21 carrier and reads its exact `FILE_SCHEMA` declarations; it does not
perform AP242 or other domain conformance validation.

## Staged artifacts and identity

The backend owns one caller-created workspace for an attempt. It streams each
eligible member to a staged file while calculating its byte count and SHA-256,
then builds inventory and bundle outputs from those staged files. Upload uses
the same observed size and digest, so the published artifact identity describes
the bytes that were inspected.

The inventory, XMP packet, deterministic ZIP, and selected payloads are staged
files too. The backend does not keep a second in-memory copy merely for upload.
All public artifacts use the normal output envelope and generic artifact
records; downstream validators see the fixed output port keys above.

## Resource and security boundary

`PdfProcessingLimits` is the shared, typed limit contract. It bounds input
bytes, pages, objects, traversal depth, member references, member and aggregate
decoded bytes, decode ratio, XMP, interactive-action entries, findings,
inventory bytes, bundle bytes, and execution time. The application clamps the
PDF deadline to at most 300 seconds, and the PDF service inventory advertises
the same 300-second domain ceiling.

The backend performs structural inspection only. It has no PDF renderer and
does not execute JavaScript, launch actions, RichMedia, 3D content, embedded
executables, or extracted members. Network and process isolation remain part of
the normal advanced-validator execution boundary.

## Where to change the contract

- Application catalog and fixed ports:
  `validibot/validations/validators/pdf/config.py`
- Workflow step configuration and authoring:
  `validibot/workflows/step_configs.py`, `validibot/workflows/forms.py`, and
  `validibot/workflows/views_helpers.py`
- Application envelope construction:
  `validibot/validations/services/cloud_run/envelope_builder.py`
- Shared input, inventory, selector, and output models:
  `validibot_shared/pdf/envelopes.py` in `validibot-shared`
- Isolated parser and artifact production:
  `validator_backends/pdf/engine.py` in `validibot-validator-backends`
- Backend runner and upload boundary:
  `validator_backends/pdf/runner.py` and `validator_backends/pdf/main.py`

The architecture decision and its complete acceptance boundary live in the
private project ADR `2026-08-07-pdf-package-validator.md`.
