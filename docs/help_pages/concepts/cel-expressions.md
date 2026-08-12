# CEL Expressions

Validibot uses Common Expression Language (CEL) for advanced assertions. CEL is a simple, safe expression syntax for writing small, fast, readable conditions and rules over your data.

You can use CEL to perform simple assertion logic on your incoming data,
or data produced by your validator (e.g. after the FMU Validator runs a submission through a simulation and produces output).
Whenever you add an assertion to your workflow step, you can base it on a CEL expression.

For the architectural overview—scope, lifecycle, signals versus step ports,
constants, and the value/artifact boundary—start with
[How Data Flows Through a Workflow](/app/help/concepts/workflow-data/).

When the user submits data, each assertion runs. If the assertion has a CEL, and the CEL evaluates to false, the error message you provided in the
assertion will be added to the messages returned to the user.

## Data Namespaces

Every CEL expression runs in a context where your data is organized into clear namespaces. Each namespace gives you access to a different category of data, and you reference values using a short prefix.

| Short form | Long form | What it accesses |
|------------|-----------|------------------|
| `p.key` | `payload.key` | Raw submission data — the JSON, XML, or other payload the submitter sent |
| `s.name` | `signal.name` | The workflow's vocabulary — values you've named via signal mapping or promoted from a step |
| `i.name` | `input.name` | This step's inputs — values the validator has at the start of the step (parser facts, resolved bindings, template variables) |
| `o.name` | `output.name` | This step's outputs — values produced by the validator after it runs |
| `steps.step_key.input.name` / `steps.step_key.output.name` | | An earlier step's inputs or outputs |
| `c.name` | `const.name` | Workflow constants — fixed values defined by the workflow author |
| `submission.field` (e.g. `submission.metadata.deliverable`, `submission.uploaded_at`) | | The submission *envelope* — the submitter's metadata plus server-stamped facts (file type, size, upload time). Available for **any** file type, even when the file isn't JSON. Long-only (no short form). See [Using submission metadata](#using-submission-metadata). |

### The teaching analogy

Think of each step as a function in a program.

- **Inputs (`i.*`)** are the function's parameters — values handed to it at the start.
- **Outputs (`o.*`)** are what the function returns.
- **The workflow vocabulary (`s.*`)** is module-level state shared across functions.
- **Constants (`c.*`)** are named literals written into the workflow definition itself.
- **The submission file (`p.*`)** is the raw data the program started with, always available.
- **The submission envelope (`submission.*`)** is the metadata *about* that data — who/when/what-named/how-big, plus any key-value tags attached at upload. It sits beside the file, not inside it, so it works the same for every format.

Just like in a program, you can lift a function-local value-port input or output into module-level state when you want downstream functions to see it. In Validibot, that ceremony is called **promotion** — "Copy to Signal" gives a step-local `i.*` or `o.*` value an additional name in the workflow's `s.*` vocabulary. Artifacts cannot be promoted.

### Each namespace in detail

**`p.*` — the raw submission.** The logical content the submitter sent. If the submission contains `{"price": 20.00}`, you reference it as `p.price`. Opaque binary files are processed by their validator rather than exposed as a useful traversable object under `p.*`. For XML and other formats, see the format-specific sections later in this doc.

**`s.*` — the workflow's vocabulary.** Author-named CEL/JSON values. You create them two ways:
1. **Workflow signal mapping** (on the workflow's settings page) — pick a name like `target_eui`, point it at a path in the submission, and it's available everywhere as `s.target_eui`.
2. **Promotion from a step** — take a value-port input or output of a particular step, click "Copy to Signal", and give it a workflow-wide name. It becomes available as `s.<your_name>` after that stage completes, for downstream execution.

Use `s.*` for values you want to use in multiple steps, or values whose source might change and you don't want every assertion to know the details.

**`c.*` / `const.*` — workflow constants.** Fixed values the workflow author defines once on the workflow's **Constants** screen, next to **Signals**. Constants are best for thresholds, allow-lists, reference values, and other literals that should be part of the workflow contract: `c.energy_price`, `c.allowed_currencies`, `c.max_unmet_hours`.

Constants are known before any run starts. The workflow page can therefore show both the name and the actual value, unlike signals, which depend on each submitted file. Constant names are case-sensitive: a constant named `bubba` is `c.bubba`, not `c.Bubba`.

Basic structured assertions can use `c.<name>` as a target path. Comparing a payload value to a constant, or checking membership in a constant list, is CEL-only in this version:

```
p.currency in c.allowed_currencies
```

Assertion failure messages can also include constants:

```
Expected {{ c.energy_price }} but received {{ p.energy_price }}
```

**`i.*` — this step's inputs.** Values the validator can see at the start of this step, before its main work runs. For an EnergyPlus step this includes parser-extracted facts about the submitted IDF (`i.zone_count`, `i.idf_version`). For an FMU step it includes the resolved model input variables. For a step with author-supplied template variables, the resolved variable values appear here too.

`i.*` is **step-local** — `i.zone_count` in one step is unrelated to `i.zone_count` in another step (different submissions, different parses). If you want a value visible across steps, promote it to a signal.

**`o.*` — this step's outputs.** Values the validator produced after running. For an EnergyPlus step this is the simulation results (`o.site_eui_kwh_m2`, `o.unmet_heating_hours`). For a JSON Schema step there are usually no outputs — the validator just says pass/fail.

`o.*` is **step-local** too, and **temporally bound** — only available in *output-stage* assertions on the step that produced it. An input-stage assertion can't reference `o.*` because the validator hasn't run yet. Validibot's assertion editor enforces this: when you're editing an input-stage assertion, the autocomplete won't offer `o.*` references.

**`steps.<step_key>.input.*` / `steps.<step_key>.output.*`** — values from an earlier step in the workflow, by step key. Use this for ad-hoc cross-step access. For values you reference often across steps, promotion to `s.*` is cleaner.

**`submission.*` — the submission envelope.** Context *about* the submission that lives beside the file rather than inside it, so it resolves the same way no matter what was uploaded — JSON, XML, CSV, or an RDF `.ttl` graph. This is the one namespace you can rely on for a per-submission gate when the file itself isn't JSON. It has two kinds of value:

- **Submitter-provided** (treat as untrusted): `submission.name`, `submission.short_description`, and the free-form `submission.metadata.<key>` bag. Whoever launched the run set these.
- **Server-stamped** (trustworthy): `submission.file_type`, `submission.size` (bytes), `submission.uploaded_at` (a timestamp). Validibot sets these; a submitter can't forge them.

`submission` is long-only — there's no single-letter alias, because `s` already means signals. It is *not* a copy of the file: the file's contents stay at `p.*`/`payload.*`, and there is deliberately no `submission.payload`.

Artifacts such as FMUs, reports, logs, and transformed documents do not enter
these CEL namespaces. They travel through artifact ports and bindings. A small
fact about an artifact, such as `o.has_report`, can still be a normal value.

### Where do I find each kind of value?

| Question | Look in | Example |
|---|---|---|
| What did the user submit? | `p.*` | `p.metadata.client_id` |
| What named value does the workflow define? | `s.*` | `s.target_eui` |
| What fixed reference value did the author define? | `c.*` / `const.*` | `c.energy_price` |
| What can this step's validator see at the start? | `i.*` | `i.zone_count`, `i.idf_version` |
| What did this step's validator produce? | `o.*` | `o.site_eui_kwh_m2` |
| What did an earlier step produce? | `steps.<key>.output.*` | `steps.preflight.output.warning_count` |

### When do step inputs and step outputs exist?

A natural question: *"Why are `i.*` and `o.*` sometimes empty?"*

A step populates `i.*` or `o.*` only when its validator runs a **process**
that transforms data. If you're using a validator that just checks
structural rules (JSON Schema, XML Schema, Basic), both namespaces are
empty — you write your assertions entirely against `p.*` and `s.*`.

Three positions on the spectrum:

- **No process** (JSON Schema, XML Schema, Basic) — assertions use
  `p.*` (the payload) and optionally `s.*` (workflow signals). `i.*`
  and `o.*` are empty.
- **Process produces outputs only** (SHACL, THERM) — the validator
  parses or evaluates the payload and emits results. Assertions
  primarily use `o.*`. `i.*` is empty.
- **Process has discrete input and output stages** (EnergyPlus, FMU) —
  the validator extracts facts from the payload first (`i.*`), runs its
  main work, then emits results (`o.*`). Both stages are meaningful.

If you open a workflow step and the Inputs or Outputs panel is empty,
that's intentional — it accurately reflects what the chosen validator
does with your data.

### Why not just use JSON or XML Schemas?

Yes, it's true — you can use the Validibot JSON Schema validator or XML Schema validator for your workflow steps. In that case you don't define individual rules, you just attach one big schema to your validator. Schemas are great for structure: making sure fields exist, have the right type, follow enums, match patterns, etc. They're the first line of defense for data integrity.

CEL expressions, on the other hand, handle behavioural and cross-field rules that schemas either can't express cleanly or make horribly verbose — things like numeric relationships between fields, tolerances, conditional requirements, or checks on simulation outputs from an FMU Validator. In Validibot, schemas define "what the data looks like"; CEL assertions define "what must be true about this data for it to be acceptable." They're complementary, not competing.

You could create a workflow that has both a JSON schema validation and detailed CEL assertions.

## Using submission metadata

Sometimes the rule you want depends on *context about the submission* that isn't in the file at all — for example, "be strict for a final handover, lenient for a draft." That context lives in the `submission.*` namespace.

**Attaching metadata at upload.** There are three ways to send it, depending on how the run is launched:

- **Web launch form** — when the workflow enables it, the launch page shows a "Submission details" tab where the submitter can add a name, a short description, and submission metadata as JSON.
- **API** — include a `metadata` object (and optionally `filename`, `short_description`) in the start request.
- **CLI** — `validibot validate model.ttl -w my-workflow -o my-org --meta deliverable=handover --short-description "Final package"` (repeat `--meta` for more keys).

**Reading it in a rule.** Reference any field by its path:

```
submission.metadata.deliverable == "handover"
```

If a metadata key isn't a simple word — it has a hyphen, space, or dot — use bracket notation with quotes:

```
submission.metadata["deliverable-type"] == "handover"
```

**A worked example — the deliverable gate.** Suppose a workflow should apply a strict acceptance check only to final handovers. Put the strict rule behind a condition on the metadata:

```
submission.metadata.deliverable != "handover" || o.unmet_load_hours < 300
```

Read it as: "either this isn't a handover, or it must meet the strict limit." Drafts pass; handovers must clear the bar.

### A note on trust

`submission.metadata.*`, `submission.name`, and `submission.short_description` are **submitter-provided** — whoever launches the run chooses them. Treat them as *advisory* by default: a submitter could mark a real handover as a "draft" to dodge a strict branch. When a value must be **authoritative** (it truly gates acceptance), make sure only a trusted party sets it — for example, a CI pipeline holding an API token, or the workflow owner — rather than an anonymous web submitter.

The server-stamped facts are always safe to trust, because Validibot sets them and a submitter can't forge them:

- `submission.uploaded_at` — when Validibot received the file (a timestamp). A trustworthy freshness check looks like `now() - submission.uploaded_at < duration("720h")` (within 30 days).
- `submission.file_type` and `submission.size` — the detected type and byte count.

(One subtlety: `submission.original_filename` *looks* like a server fact but is really the submitter's own filename, only cleaned up for safety — so don't rely on it for gating.)

## Examples

Here are some examples of CEL expressions. The example names are highlighted in blue, while the rest of the CEL expression is in red.

### Core operators

- **Equality/inequality**: <code><span class="cel-reference">s.a</span></code> `==` <code><span class="cel-reference">s.b</span></code>, <code><span class="cel-reference">s.a</span></code> `!=` <code><span class="cel-reference">s.b</span></code>
- **Comparisons**: <code><span class="cel-reference">p.price</span></code> ` > 0`, <code><span class="cel-reference">p.score</span></code> `>= 90`, <code><span class="cel-reference">p.cost</span></code> `< 1000`
- **Boolean checks**: <code><span class="cel-reference">p.flag_active</span> == true</code>, <code><span class="cel-reference">p.is_valid</span> != false</code>
- **Logical**: `cond1 && cond2`, `cond1 || cond2`, `!cond`
- **Membership**: <code><span class="cel-reference">p.country</span></code> `in ['US', 'CA']`, <code><span class="cel-reference">p.role</span></code> `not in ['guest']`
- **Null/empty checks**: <code><span class="cel-reference">p.some_field</span></code> ` == null`, `size(`<code><span class="cel-reference">p.some_items</span></code>`) == 0`
- **String contains/starts/ends**: <code><span class="cel-reference">p.my_text</span></code>`.contains('error')`, <code><span class="cel-reference">p.my_text</span></code>`.startsWith('ID-')`, <code><span class="cel-reference">p.my_text</span></code>`.endsWith('.json')`
- **Regex**: <code><span class="cel-reference">p.my_text</span></code>`.matches('^ID-[0-9]+$')`
- **Length**: `size(` <code><span class="cel-reference">p.my_text</span></code>`) <= 140`, `size(` <code><span class="cel-reference">p.my_text</span></code>`) > 0`
- **Numeric tolerance**: `abs(` <code><span class="cel-reference">s.my_measured_value</span></code>`-` <code><span class="cel-reference">s.my_actual_value</span></code>`) < 0.01`

### Collections

- **Any/All**: <code><span class="cel-reference">p.my_items</span></code>`.exists(i, i.status == 'ok')`, <code><span class="cel-reference">p.my_items</span></code>`.all(i, i.score >= 80)`
- **Contains element**: `['value_A', 'value_B'].exists(f, f == ` <code><span class="cel-reference">p.my_value</span></code>`)`
- **Subset/superset**: `expected.all(e, e in provided)`

### Dates and numbers

- **Comparing timestamps**: <code><span class="cel-reference">p.my_time_value</span></code> `< timestamp('2024-12-31T23:59:59Z')`
- **Between**: <code><span class="cel-reference">p.my_value</span></code> `> 10 && ` <code><span class="cel-reference">p.my_value</span></code> `< 20`

### Examples by namespace

- **Payload check**: <code><span class="cel-reference">p.price</span></code> ` > 0`
- **Signal check**: <code><span class="cel-reference">s.target_eui</span></code> ` <= 60`
- **Constant threshold**: <code><span class="cel-reference">p.energy_price</span></code> ` <= ` <code><span class="cel-reference">c.energy_price</span></code>
- **Input-stage check (before validator runs)**: <code><span class="cel-reference">i.zone_count</span></code> ` >= 4 && ` <code><span class="cel-reference">i.idf_version</span></code> `.startsWith("25.")`
- **Output-stage check (after validator runs)**: <code><span class="cel-reference">o.site_eui_kwh_m2</span></code> ` <= ` <code><span class="cel-reference">s.target_eui</span></code>
- **Compare input to output**: `abs(` <code><span class="cel-reference">i.expected_floor_area</span></code> ` - ` <code><span class="cel-reference">o.floor_area_m2</span></code>`) < 5.0`
- **Cross-step reference**: <code><span class="cel-reference">steps.preflight.output.warning_count</span></code> ` < 10`

### Working with XML data

When your submission is XML, all element text values arrive in CEL as **strings** — even when they look numeric in the document. This is standard XML behaviour (XML has no native number type). To compare numerically, wrap the value with `double()` or `int()`:

- **Numeric comparison**: `double(`<code><span class="cel-reference">p.price</span></code>`) > 0` rather than <code><span class="cel-reference">p.price</span></code> `> 0`
- **Integer check**: `int(`<code><span class="cel-reference">p.count</span></code>`) >= 1`
- **Collection with cast**: <code><span class="cel-reference">p.items</span></code>`.all(i, double(i.value) > 0.0)`
- **String comparisons work directly**: <code><span class="cel-reference">p.status</span></code> `== "active"` (no cast needed)

**XML attributes** (like `<Material Conductivity="160.0">`) become `@`-prefixed keys in the data — `@Conductivity`, not `Conductivity`. Use bracket notation to access them:

- **Access an attribute**: <code><span class="cel-reference">p.Materials</span>.Material.all(m, double(m["@Conductivity"]) > 0.0)</code>
- **String attribute**: <code><span class="cel-reference">p.Materials</span>.Material.all(m, m["@Name"] != "")</code>

This is because XML distinguishes between child elements (`<Conductivity>160</Conductivity>`) and attributes (`Conductivity="160"`). The `@` prefix preserves that distinction so your expressions are unambiguous.

If an XML element name contains characters that aren't valid identifiers (hyphens, dots, etc.), access it via bracket notation on `payload`: `payload["THERM-XML"].Materials`.

### Working with named-element data (SysML v2, FHIR, etc.)

Some data formats store values in arrays of named objects rather than as simple key-value pairs. For example, a SysML v2 model might look like:

```json
{
  "ownedAttribute": [
    {"name": "emissivity", "defaultValue": 0.85},
    {"name": "mass", "defaultValue": 3.6}
  ]
}
```

You can't reference `emissivity` directly in a CEL expression because it's a **value**, not a key. The solution is to use **signal mapping** with filter expressions in the data path:

1. Create a signal named `emissivity` in the workflow's signal mapping
2. Set its data path to `ownedAttribute[?@.name=='emissivity'].defaultValue`
3. Write your CEL assertion as `s.emissivity > 0.0 && s.emissivity <= 1.0`

Validibot resolves the filter expression to find the right array element, then makes the value available under the signal name. Your CEL assertions stay clean and readable — the complexity of navigating the data structure is handled by the data path, not the expression.

See the [Signals](/app/help/validators/signals/) guide for a worked example, and the [Data Paths](/app/help/validators/data-paths/) guide for filter expression syntax.

## Tips

- Expressions run against the submission payload, workflow signals, step inputs, and step outputs.
- Keep them deterministic — no network or external state.
- Use step assertions to tighten a workflow. On JSON Schema, XML Schema, and SHACL steps, the built-in validation runs first and your assertions run afterward.
- Default assertions always run for the validator before step-level assertions.
- **Input vs. output assertions are different stages.** Input-stage assertions can reference `p.*`, `s.*`, `i.*`, and earlier steps via `steps.<key>.*`. They **cannot** reference this step's `o.*` because the validator hasn't run yet. Output-stage assertions can reference everything, including this step's `o.*` and `i.*`. The assertion editor's variable picker is filtered by stage to prevent confusion.
- **Use the namespace prefix** (`p.`, `s.`, `i.`, `o.`) to make it clear where your data comes from. In the UI we color the target portion to help you distinguish it from the rest of the expression.
- **Use constants** (`c.*`) for fixed thresholds and allow-lists that should be visible on the workflow contract. Define them from the workflow's Constants page.
- **Promote a value-port step input or output to a signal** if you want to reference it from multiple downstream steps. "Copy to Signal" works on value inputs (`i.*`) and outputs (`o.*`)—pick a workflow-wide name and the value becomes available as `s.<your_name>` after its source stage completes. Artifacts cannot be promoted.

For more syntax details, visit the CEL specification at <https://github.com/google/cel-spec>.

## Full CEL Expression List

The following CEL statements are supported in Validibot.

### Base CEL Syntax

| Syntax name                     | Description                                                  | Example                                                                               |
| ------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| Equality / inequality           | Compare two values for equality or difference.               | `p.my_status == "ready"`, `p.my_status != "ok"`                                       |
| Comparisons                     | Numerical comparisons with greater/less operators.           | `p.price > 0`, `p.score >= 90`, `p.cost < 1000`                                       |
| Arithmetic                      | Basic math operators over numbers.                           | `(o.kwh_total - s.kwh_baseline) / s.kwh_baseline < 0.1`                               |
| Logical                         | Combine boolean expressions.                                 | `cond1 && cond2`, `cond1 \|\| cond2`, `!cond1`                                        |
| Membership                      | Test whether a value is inside a list.                       | `p.my_status in ["draft", "approved"]`, `!(p.my_status in ["archived"])`              |
| Null / empty checks             | Detect missing or empty values.                              | `p.my_field == null`, `size(p.my_items) == 0`                                         |
| JSON-style path access          | Traverse objects and arrays with dot and `[index]` notation. | `p.device[0].id == "abc123"`                                                          |
| Length / size                   | Count characters or list elements.                           | `size(p.my_text) <= 140`, `size(p.my_items) > 0`                                      |
| String contains / starts / ends | String search helpers from CEL stdlib.                       | `p.my_text.contains("error")`, `p.my_text.startsWith("ID-")`, `p.my_text.endsWith(".json")` |
| Regex match                     | Match strings with a regular expression.                     | `p.my_text.matches("^ID-[0-9]+$")`                                                    |
| Collections (exists / all)      | Quantify over list elements.                                 | `p.my_items.exists(i, i.status == "ok")`, `p.my_items.all(i, i.score >= 80)`          |
| Subset / superset check         | Verify one list is contained in another.                     | `s.expected.all(e, e in s.provided)`                                                  |
| Ternary conditional             | Choose a value based on a condition.                         | `p.is_valid ? "pass" : "fail"`                                                        |
| Timestamp comparison            | Compare datetimes via CEL `timestamp()`.                     | `p.event_time < timestamp("2024-12-31T23:59:59Z")`                                    |
| Range / between                 | Combine comparisons to enforce bounds.                       | `p.my_value > 10 && p.my_value < 20`                                                  |

### Validibot Helpers

| Syntax name                   | Description                                              | Example                                 |
| ----------------------------- | -------------------------------------------------------- | --------------------------------------- |
| `has(value)`                  | True when the value is not null or empty.                | `has(my_description)`                   |
| `is_int(value)`               | True when the numeric value is an integer.               | `is_int(my_floor_area_m2)`              |
| `percentile(values, q)`       | q-quantile of a numeric list (ignores nulls).            | `percentile(my_values, 0.95) < 32.0`    |
| `mean(values)`                | Average of a numeric list (ignores nulls).               | `mean(my_values) <= 50000`              |
| `sum(values)`                 | Sum of a numeric list.                                   | `sum(my_values) > 0`                    |
| `max(values)`                 | Maximum value in a numeric list.                         | `max(my_values) < 75000`                |
| `min(values)`                 | Minimum value in a numeric list.                         | `min(my_values) >= 0`                   |
| `abs(value)`                  | Absolute value of a number.                              | `abs(my_measured - my_expected) < 0.05` |
| `round(value, digits)`        | Round a number to a set of decimal places.               | `round(my_eui_kbtu_ft2, 1) < 30.5`      |
| `duration(series, predicate)` | Count samples where a predicate over the series is true. | `duration(my_series, v > 0) > 100`      |

For `duration`, write the second argument as the condition that should hold for each sample in the series.
