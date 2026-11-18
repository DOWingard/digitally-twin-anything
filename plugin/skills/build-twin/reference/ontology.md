# Ontology

The ontology is one LinkML file in the workspace that names the kinds of objects the twin knows (classes), their properties (attributes), and how objects are linked. `ontology.path` names it, relative to the workspace. `dta ontology check` reads it and reports every broken rule at once.

## Design it from the questions

1. List every noun the contract's questions, controls and objective use: those are the candidate classes ("bed", "greenhouse", "room", "compressor").
2. Keep a class only if a source holds rows about it. A noun no source describes cannot be filled.
3. For every value a question asks about, add a property to the class it describes, with its unit.
4. For every "belongs to", "is in", "is fed by" relation between two classes, add a reference property on the class that has one of the other (a bed is in one house: `Bed.house`, range `House`).
5. A relation that carries values of its own, or where one object relates to many others of the same kind, is a link class (below).
6. The twin's acceptance needs at least one reference or link between mapped classes, and some object whose neighbours reach two steps out (a bed, its house, the house's other beds). A twin of unrelated lists fails the check `readiness.R3`.

## File layout

Copy this layout; change the names, the descriptions and the classes.

```yaml
id: https://example.org/dta/<id>
name: <id>
description: <one sentence: what the twin's objects are>
prefixes:
  linkml: https://w3id.org/linkml/
  <prefix>: https://example.org/dta/<id>/
default_prefix: <prefix>
default_range: string
imports:
  - linkml:types

classes:
  House:
    description: One greenhouse.
    attributes:
      uid:
        identifier: true
        required: true
      name:
        required: true
        description: Name the growers use for the house.
  Bed:
    description: A growing bed inside a greenhouse.
    attributes:
      uid:
        identifier: true
        required: true
      house:
        range: House
        required: true
        description: Greenhouse the bed is in.
      moisture:
        range: float
        minimum_value: 0
        maximum_value: 100
        description: Volumetric soil moisture.
        unit:
          ucum_code: "%"
        annotations:
          dta_role: state
```

`<prefix>` is a short lowercase word, such as `gh`.

## Rules the check enforces

| Rule | What it requires |
|---|---|
| Imports | `imports` lists only `linkml:types`. |
| Class names | CamelCase: `^[A-Z][A-Za-z0-9]*$`. A class name may not start with `Twin` (reserved). |
| Property names | snake_case: `^[a-z][a-z0-9_]*$`. |
| Identifier | Every object class has `uid` with `identifier: true` and `required: true`, a single string. Never map `uid` yourself: the twin makes it from the mapping's identity columns. |
| Ranges | `string` (the default), `integer`, `float`, `double`, `decimal`, `boolean`, `datetime`, `date`, `uri`, an enum of the file, or another class (a reference). |
| References | A reference property holds one object; `multivalued: true` on a reference is refused. Model "one to many" as a reference on the "many" side, or as a link class. |
| Bounds | `minimum_value` and `maximum_value` only on numeric properties, minimum not above maximum. Rows outside the bounds are refused when loaded. |
| Units | `unit: {ucum_code: <code>}` with a UCUM code (`m`, `Cel`, `kW`, `%`, `L/min`). Give a unit to every numeric property that has one. |
| Roles | `annotations: {dta_role: static}` (the default: a fixed attribute), `state` (changes with time) or `derived` (computed by the twin). |
| Long text | `annotations: {dta_long: true}` only on string properties that hold long text. |
| Not supported | `equals_string`, `equals_number`, `equals_expression` and similar slot constraints are refused, not ignored. |

## Roles and time

- A property that a source reports with a time (a reading, a status that changes) has `dta_role: state`, and its mapping names a `valid_time` column. The check `ontology.role` warns when a state property is mapped without a time, or a static one with a time.
- A property set once (a name, a size, a model number) keeps the default role, and its mapping has no `valid_time`, even when the source keeps an `updated_at` for it. Its value is in force from the first version of the loaded history; a later change is written at the version of the sync that finds it.
- An append-only record of something that happened (a delivery, an alarm, a meter reading the source later corrects) is a class with `annotations: {dta_event: true}`; its properties may be mapped with a `valid_time` whatever their role.

## Link classes

A link between two objects that carries values of its own is a class with the annotations `link_source` and `link_target`, each naming a different required reference property of the class:

```yaml
  Supplies:
    description: A pipe that carries chilled brine from a compressor to a room.
    annotations:
      link_source: compressor
      link_target: room
    attributes:
      uid:
        identifier: true
        required: true
      compressor:
        range: Compressor
        required: true
      room:
        range: Room
        required: true
      length:
        range: float
        unit:
          ucum_code: m
```

A link class is mapped like any class: its identity columns, and a column for each of the two references.

A link class may hold further reference properties besides the two it links (a trip links two stations and also names its bike); they are ordinary references. A link that records something that happened (a trip, a delivery) may also be an event class: give it `dta_event: true` beside `link_source` and `link_target`.

## Enums

A property with a fixed set of values uses an enum declared in the same file:

```yaml
enums:
  BedStatus:
    permissible_values:
      planted: {}
      fallow: {}
```

and `range: BedStatus` on the property. An enum needs at least one value.

## Checks on rows

A class may carry checks that every row must pass, as annotations named `dta_check_<name>` holding a condition over the class's own property names, with numbers, single-quoted texts, comparisons, arithmetic, `AND`, `OR`, `NOT`, `IS`, `NULL` and `IN`:

```yaml
  Room:
    annotations:
      dta_check_setpoint_range: setpoint IS NULL OR setpoint <= 4
```

Function calls, comments and any other keyword are refused.

## Abstract classes

`abstract: true` with `is_a: <Parent>` on its subclasses lets a reference point at any of several classes. Map rows to the concrete classes only: mapping to an abstract class is refused (`ontology.abstract_mapping`). Use this only when the user's system really has such a family; a flat set of classes is easier to map and to explain.

## What the user agrees to

The user agrees the types: the classes, which are link classes, and which classes are abstract. That is what `ontology.type_signature` holds. Adding or renaming a property does not change it; adding, removing or renaming a class does, and then the user agrees again ([stage 4](stage-4-design.md), step 2).
