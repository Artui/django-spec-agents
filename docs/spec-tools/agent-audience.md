# Hide plumbing from the model

An output declared for your API is handed to the model verbatim when the
same spec becomes a tool. Everything in it is equally visible, so records get
referred to by primary key, a status reads as `IN_STOCK` rather than "In stock",
and internal fields get narrated as if they were content.

The marking travels to **both** halves of what the model sees. The payload is
projected as it is rendered, and the tool's `return_schema` is generated from
the same declaration — so a hidden field is absent from the schema as well as
from the result, and a handle carries its description in both. A schema still
advertising a field the render drops would be worse than no schema at all: the
model asks for it, gets nothing back, and is told nothing about why.

## Mark the fields

The marking is `FieldMarking`, from
[django-service-specs](https://artui.github.io/django-service-specs/), declared
on the output field it marks. On the kernel's `DataclassPresenter` it goes in
the field's annotation, so the dataclass that declares the output declares its
audience too:

```python
from dataclasses import dataclass
from typing import Annotated

from django.db import models
from django_service_specs import DataclassPresenter, FieldMarking


class WidgetStatus(models.TextChoices):
    IN_STOCK = "IN_STOCK", "In stock"
    SOLD_OUT = "SOLD_OUT", "Sold out"


@dataclass
class WidgetOut:
    id: Annotated[int, FieldMarking.handle("Widget handle.")]
    name: Annotated[str, FieldMarking.label()]
    price: Annotated[str, FieldMarking.hidden()]
    status: WidgetStatus


presenter = DataclassPresenter(WidgetOut)
```

A presenter of your own carries the same marking on each `OutputField` its
`output()` declares, as `marking=`.

Nothing changes at the toolset. A tool rendering through that presenter returns:

```python
{"items": [{"id": 1, "name": "Sprocket", "status": "In stock"}], "page": 1, ...}
```

(the [pagination envelope](quickstart.md#every-list-result-is-a-page) is the
list-tool wrapper; the projection lands on the rows inside it, never on the
envelope's own keys, which belong to no presenter).

`price` is gone. `status` reads as a person would say it: a field with choices
is rendered for the agent as its display, which the kernel takes from a Django
`Choices` enum's label. `id` is untouched — a
handle is another tool's input, so its value is never re-spelled.

The derived instructions gain one line, and only when some tool in the toolset
actually returns a handle:

> Some tools return opaque identifier fields, described as such in the tool's
> output. Pass them to other tools that ask for one; refer to records by their
> name in anything you say, never by the identifier.

A handle that declares no description of its own is described in the output
schema with the field-level half of the same advice. Both sentences are yours to
reword, or drop, through
[`conventions=`](quickstart.md#changing-what-the-model-is-told): `handles` and
`handle_field_description`.

## One tool that needs what its siblings hide

The presenter is the declaration and stays authoritative. The exception is a
tool whose whole job is handing back something the others drop — a lookup
returning the price a list view hides. That tool gets a presenter of its own,
declaring the field without the marking, on its own spec:

```python
from dataclasses import dataclass, replace


@dataclass
class WidgetPriceOut:
    id: Annotated[int, FieldMarking.handle("Widget handle.")]
    name: Annotated[str, FieldMarking.label()]
    price: str


registry.register(
    "lookup_widget",
    replace(specs.lookup_widget, presenter=DataclassPresenter(WidgetPriceOut)),
)
```

The marking is part of the spec's output declaration, so `SpecToolset`
projects it whether it is handed the registry or `registry.specs()`, and so does
an MCP server registering the same spec — which is the reason it lives there
rather than in either constructor.
`FieldMarking`'s axis is *audience*, not transport: an in-process toolset and an
MCP server want the same thing as each other and something different from a
browser, so a field hidden from one agent caller and visible to another is a bug
you find in a transcript rather than in a test.

Two fields left claiming `FieldMarking.label()` raises `ImproperlyConfigured`
when the toolset is built (`SpecToolset tool 'list_widgets': Output: both 'id'
and 'name' are marked FieldMarking.label()...`): a record has one name. The refusal is
the kernel's, from the spec's output declaration; the toolset prefixes it with
the tool, since the kernel names only the fields.

## What it costs

Nothing per call. The markings are pure in the presenter's output declaration,
like the tool schemas, so `SpecToolset` resolves them once at construction and
reuses them.

## Other callers are unaffected

A marking applies only for a caller that names an agent audience, as the
kernel's `FieldMarking` documents. Any other
caller of the same spec, a browser among them, is rendered the full output,
which is the point: one declaration, whichever consumer is reading.
