# Wiki Contract Specification

This file defines the format of `wiki_contract.yaml` — a machine-checkable
schema that the framework validates against after every tick. The agent
generates this file on its first tick by reading the CLAUDE.md and
translating the entity definitions into this format.

## Purpose

The contract bridges CLAUDE.md (human-readable, LLM-readable) and the
per-tick validator (Python code). The CLAUDE.md defines what the wiki
should contain; the contract makes a subset of those rules
machine-checkable. The validator reads the contract at runtime and
checks every wiki file against it — no hardcoded entity types or
field names in the validation code.

## Format

The contract is a YAML file with two top-level keys.

### `entity_types`

A map from type name to its validation rules. Each entity type can
declare:

- **`required_fields`** (list of strings, mandatory): frontmatter fields
  that must be present and non-empty on every entity of this type. The
  fields `id` and `type` should always be included.

- **`status_values`** (list of strings, optional): if the entity type has
  a `status` field, the allowed values. Omit if there's no status field.

- **`optional_fields`** (list of strings, optional): frontmatter fields
  that may be present. The validator won't complain if these are absent
  but will flag fields that appear in neither `required_fields` nor
  `optional_fields` as unexpected.

Example:

```yaml
entity_types:
  concept:
    required_fields: [id, type, created]
    optional_fields: [aliases, related]
  claim:
    required_fields: [id, type, created, status, confidence, provenance]
    status_values: [conjectured, supported, well-supported, contested, refuted, obsolete]
    optional_fields: [about, supported-by, refuted-by, supersedes, superseded-by, source-turns, history]
```

### `bidirectional_edges`

A list of two-element lists. Each pair declares that if entity A has
field X referencing entity B, then entity B must have field Y
referencing entity A (and vice versa).

A field name may appear in multiple pairs. For example, if Evidence
entities use `supports` to point at both Claims (which use
`supported-by`) and Hypotheses (which use `evidence-for`), declare
both pairs:

```yaml
bidirectional_edges:
  - [supports, supported-by]
  - [supports, evidence-for]
```

The validator checks both directions: if A lists B under `supports`,
B must list A under `supported-by` or `evidence-for` (whichever pair
matched the field name).

## What the validator checks

Given a contract and a set of wiki files, the per-tick validator checks:

1. Every wiki file's `type` field matches a key in `entity_types`.
2. All `required_fields` for that type are present and non-empty.
3. If `status_values` is declared, the `status` field's value is in the list.
4. If `optional_fields` is declared, any field not in `required_fields`
   or `optional_fields` is flagged as unexpected (warning, not error).
5. Every ID referenced in an edge field exists as a wiki entity.
6. Bidirectional edges are symmetric (both sides present).
7. All `id` values are unique across the wiki.
8. IDs match the `<type>-<slug>` format convention.

## What the validator does NOT check

- Prose quality, accuracy, or completeness.
- Whether evidence actually supports the claims it's linked to.
- Whether `index.md` is up to date.
- Anything semantic — that's the LLM audit's job.

## Generating the contract

When writing your CLAUDE.md, include instructions like:

```
On your first tick, before processing any conversation:
1. Read this entire file
2. Read wiki_contract_spec.md for the contract format
3. Generate wiki_contract.yaml from the entity definitions above
4. The framework validates the contract and all wiki edits against it
```

The framework copies `wiki_contract_spec.md` into the workspace at init.
Your CLAUDE.md defines the entities; this spec defines how to express
them as a machine-checkable contract.
