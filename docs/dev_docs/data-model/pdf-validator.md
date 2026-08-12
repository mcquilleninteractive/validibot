# PDF Package Validator

The PDF Package Validator is a deliberately narrow extraction boundary for
untrusted PDF packages. It always applies one policy,
`static_text_package_v1`. There is no inventory-only, permissive, or custom
policy mode.

The intended use is common in AEC delivery: a human-readable drawing or report
is the PDF wrapper, while an IFC/STEP model, an IDS requirements document, or a
JSON transmittal is attached for machine validation by later workflow steps.
The PDF step establishes only that the wrapper and attached bytes satisfy this
small carrier policy. It does not establish IFC, IDS, AP242, JSON Schema,
PDF/A, drawing, or design conformance.

> A pass is not a malware-free or safe-to-open guarantee. The submitted PDF,
> extracted members, and evidence ZIP remain untrusted data. The validator does
> not render the PDF or provide a safe viewer.

## The only policy: `static_text_package_v1`

The policy allows:

- an unencrypted PDF wrapper;
- one document-level XMP packet serialized as an explicit Metadata/XML stream
  containing XMP RDF;
- embedded or attached files whose bytes are detected and preflighted as XML,
  JSON, or STEP Part 21 text; and
- only the attachment routes listed below.

Everything else is rejected. In particular, the policy rejects scripts,
forms, XFA, launch and remote actions, automatic actions, multimedia,
RichMedia, 3D, PDF Collections, object-level XMP, external file
specifications, unsupported or ambiguous file routes, unsafe or duplicate
names, active XML vocabularies such as SVG/XHTML/XSLT, binary members, and
every encrypted PDF, including a PDF that opens with an empty user password.

Ordinary URI hyperlinks and digital signatures are outside this validator's
scope. URI targets are not followed, copied, or validated. Signature contents
and claims are not interpreted or validated. Their presence is not a PDF
package-policy result.

### Allowed attachment routes

An eligible member must be reachable through at least one of:

| PDF mechanism | Inventory discovery kind |
| --- | --- |
| Catalog `/Names/EmbeddedFiles` name tree | `embedded_files_name_tree` |
| Direct `/AF` array on the catalog, a page, or an annotation | `associated_file` |
| `/FS` on a `FileAttachment` annotation | `file_attachment_annotation` |

An embedded file specification found only somewhere else in the object graph
is an error. An `/AF` array on a structure element, XObject, marked-content
property, or other unlisted object is also an error. This allowlist is smaller
than the set of mechanisms PDF can technically express; that is intentional.

### Allowed stream encodings

Document XMP must have the standard Metadata/XML stream identity and contain an
XMP RDF packet; arbitrary XML does not become XMP merely because the catalog
references it as metadata. XMP and eligible member streams may be unfiltered or use one
`FlateDecode` filter without decode parameters. Other filters, filter chains,
or decode parameters are rejected before extraction. This restriction applies
to XMP and package-member streams, not to ordinary page content, fonts, or
images, because the validator does not decode those resources.

All decoded streams are processed through a short-lived, resource-limited
helper. The shared limits bound input bytes, pages, objects, graph depth,
member references, individual and aggregate decoded bytes, decode ratio, XMP,
findings, output sizes, and execution time.

### Atomic publication

`pdf_inventory` is always the evidence output when the file can be represented
as a domain result. XMP, selected members, and the extracted-files ZIP are a
single supplementary publication set:

- if every policy and selector check succeeds, configured supplementary
  artifacts may be published;
- if any error occurs anywhere, none of those artifacts is published; and
- the inventory records the failure and any safely observed member evidence.

This prevents a later workflow step from consuming one apparently good member
from a package that also contains a prohibited or ambiguous member.

## File-port contract

Every file crossing the application/backend boundary has a required
`port_key`. `role`, filename, MIME metadata, and list position are descriptive
only and never replace that identity.

The input is:

| Port key | Cardinality | Carrier |
| --- | --- | --- |
| `pdf_document` | exactly one | PDF |

The fixed outputs are:

| Port key | Cardinality | Meaning |
| --- | --- | --- |
| `pdf_inventory` | exactly one | Canonical `validibot.pdf_inventory.v2` JSON evidence. |
| `extracted_files_bundle` | zero or one | Deterministic ZIP of all eligible XML, JSON, and STEP members. |
| `xmp_metadata` | zero or one | Original document-level XMP bytes. |
| `selected_xml` | zero or one | One exact, carrier-preflighted XML member. |
| `selected_json` | zero or one | One exact, carrier-preflighted JSON member. |
| `selected_step_p21` | zero or one | One exact, carrier-preflighted STEP Part 21 member. |

Later workflow steps bind to these keys through the generic artifact-binding
system. There is no PDF-specific transport and no fallback to the submitted
PDF when a selected output is absent.

## Configuring selectors in the UI

The policy itself is not a field: every PDF step uses
`static_text_package_v1`. Authors only decide whether to produce an extraction
bundle and whether to expose one XML, JSON, or STEP member.

Each selector is an exact predicate. All populated fields must match the same
member. “First attachment” is never a selection rule. A required selector with
no match fails; an optional selector with no match emits no artifact; more than
one match always fails.

Available fields are:

- exact original filename;
- exact declared media type from PDF metadata;
- exact detected carrier type;
- exact Associated Files relationship;
- one or more required discovery kinds;
- XML root QName for XML; and
- one or more exact `FILE_SCHEMA` identifiers for STEP Part 21.

Use the smallest set that expresses a stable delivery contract. In most AEC
workflows, a controlled filename plus a content-derived identity is clearer
than a filename alone.

### Example: selecting `requirements.ids`

If the delivery specification requires every submission to contain exactly
one IDS document with that name, configure:

```text
Expose XML:                         yes
Fail when no matching XML exists:  yes
Exact original filename:           requirements.ids
Detected media type:               application/xml
XML root QName:                    {http://standards.buildingsmart.org/IDS}ids
```

Add a discovery kind or `/AFRelationship` only when the organization's
publishing contract genuinely requires that route or relationship. For
example, requiring `associated_file` means a file present only in the
EmbeddedFiles name tree will not match, even though the policy can safely read
it.

The filename is not magic to Validibot. The author is encoding the workflow's
delivery rule: “this package must contain exactly one file called
`requirements.ids`, and its bytes must be IDS-shaped XML.” A different
organization may allow any filename and select only by root QName, but that can
be ambiguous when a package contains several IDS documents.

### How XML root QName works

An XML qualified name combines the namespace URI and local element name. The
UI uses Clark notation:

```text
{namespace URI}local-name
```

For this XML:

```xml
<ids:ids xmlns:ids="http://standards.buildingsmart.org/IDS">
```

the root QName is:

```text
{http://standards.buildingsmart.org/IDS}ids
```

The prefix `ids` is not part of the identity. A document using `<x:ids>` with
the same namespace URI has the same QName. A document using `<ids>` with no
namespace has the QName `ids`. The backend parses only enough XML to identify
the root safely: DTDs, external entities, entity expansion, XInclude, and
network access are disabled or rejected. Matching the QName does not validate
the IDS schema; connect `selected_xml` to an XML Schema, Schematron, or future
IDS validator for that claim.

### STEP and IFC members are not XML

IFC commonly uses the ISO 10303-21 clear-text exchange syntax. The validator
detects the `ISO-10303-21;` carrier and bounded header/trailer structure, then
reads exact `FILE_SCHEMA` identifiers such as `IFC4`. Configure the STEP output
with, for example:

```text
Exact original filename:  coordination-model.ifc
Detected media type:      model/step
FILE_SCHEMA:              IFC4
```

This produces `selected_step_p21`; it does not attempt XML parsing and it does
not establish IFC model conformance.

## AEC authoring guidance

For reliable packages, publishers should:

- use one stable, unique filename per machine-readable deliverable;
- declare the correct MIME type and `/AFRelationship` consistently;
- use the EmbeddedFiles name tree and, when appropriate, a catalog/page/
  annotation `/AF` reference;
- avoid duplicate aliases and conflicting metadata for the same bytes;
- keep machine payloads as plain XML, JSON, or STEP Part 21 rather than ZIP or
  proprietary binary containers; and
- validate each selected payload in a later, domain-specific workflow step.

This model suits controlled exchanges such as design submissions, transmittal
packages, handover records, compliance evidence, and document-control gates.
It is deliberately unsuitable for interactive portfolios, embedded viewers,
3D PDF, signed-document trust decisions, encrypted deliverables, or arbitrary
office-file attachments.

## Isolation and residual risk

The backend does not render pages, repair or rewrite the PDF, execute embedded
content, or invoke extracted files. It runs without network access, with a
read-only root filesystem, reduced privileges and process limits, and a
non-executable scratch filesystem. Public/adversarial deployments can require
a stronger runtime such as gVisor or Kata.

These controls reduce attack surface and blast radius; they cannot prove that
qpdf, pikepdf, Python, or the container/kernel boundary has no unknown
vulnerability. Malware scanning and content-disarmed viewing copies are
separate controls and do not replace this package-policy check.

## Contract ownership

- App catalog and ports: `validibot/validations/validators/pdf/config.py`
- Authoring config: `validibot/workflows/step_configs.py`,
  `validibot/workflows/forms.py`, and `validibot/workflows/views_helpers.py`
- Envelope construction:
  `validibot/validations/services/cloud_run/envelope_builder.py`
- Shared models: `validibot_shared/pdf/envelopes.py` in `validibot-shared`
- Isolated inspection: `validator_backends/pdf/engine.py` in
  `validibot-validator-backends`

The architecture decision and threat analysis live in the private project ADR
`2026-08-07-pdf-package-validator.md` and
`docs/security/pdf-untrusted-input-hardening.md`.
