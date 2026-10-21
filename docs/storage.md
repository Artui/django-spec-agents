# Storage

Storage here is **contracts plus reference implementations**. The protocols are
core because every transport needs the same persistence semantics; the HTTP
*views* over them are not — a browser thread drawer and an agent-to-agent peer
model the same history completely differently.

## The contracts

Two `Protocol`s, both `runtime_checkable`, both fully async so an implementation
can use the async ORM or a network backend:

- **`ConversationStore`** — `load` / `save` / `delete` a conversation, plus
  listing metadata for a history drawer.
- **`AttachmentStore`** — `save` / `open` / `delete` files a user attached.

You pass an instance to your transport; nothing is resolved from a dotted path.

## Owner scoping is the security boundary

Both contracts are **owner-scoped**, and this is the part to get right in a
custom implementation: a store filters by the acting user so one user can never
read or delete another's data.

`AttachmentStore.open` returns `None` for a missing **or cross-owner** id rather
than raising, so a caller maps both to a 404 — a cross-owner id must not be
distinguishable from a missing one.

`AttachmentStore.save` deliberately validates nothing about size or type. That
is the calling view's job, from its own config; the store just persists bytes
and returns a durable `AttachmentRef`.

## The `read_attachment` tool

The bytes never travel on the wire. A transport that has an attachment store
composes `build_attachment_toolset(store, request)` into the agent, and the
model reaches a file server-side, by id, only when it asks — through the same
owner-scoped `open` a download view would use.

Reading one has three outcomes:

| The attachment | What the model gets |
| --- | --- |
| Textual (`text/*`, JSON, XML, JavaScript, YAML, SVG) and valid UTF-8 | Its text, inline. |
| A type providers can read — PDF, PNG, JPEG, GIF, WebP — within the size cap | The bytes, as attached file content it can look at directly. |
| Anything else: an unreadable type, an oversized file, undecodable "text" | A one-line note with the name, type and size. |

Tune the middle row with `AttachmentInlineConfig`:

```python
from django_spec_agents import AttachmentInlineConfig
from django_spec_agents.agent.attachment_toolset import build_attachment_toolset

toolset = build_attachment_toolset(
    store,
    request,
    inline=AttachmentInlineConfig(max_bytes=1024 * 1024),
)
```

**The type list is an allowlist rather than "everything that is not text", and
that is on purpose.** Bytes a provider cannot interpret are worse than useless:
a `.zip` or a `.exe` handed over as file content makes the provider reject the
whole request, so a broad rule would trade a model that cannot read your PDF
for a run that does not start.

**The size cap is much smaller than any upload cap you would set**, and for a
different reason: it bounds what one *request* carries. An inlined file rides in
a synthetic `user` message that the tool return serialises into, and that
message stays in the run's history, so every further model request in the same
run ships the file again. Base64 adds about a third — a 4 MiB PDF is roughly
5.5 MiB of provider payload each time, in tokens, in latency, and in the memory
the run holds it in.

It does not cost the conversation. The bytes never travel on the event stream,
so the client never receives them and the history it posts on the next turn
carries none; whether they outlive the run is the transport's decision, and the
AG-UI transport strips them on the way to storage. A *follow-up* question about
the same file is answered by reading the attachment again, server-side. That is
why the default sits at 4 MiB rather than at whatever your upload endpoint
accepts.

To never attach bytes:

```python
toolset = build_attachment_toolset(
    store,
    request,
    inline=AttachmentInlineConfig(media_types=frozenset()),
)
```

`AttachmentRef.mime` is client-declared, so the decision is made on a hint. The
failure mode of a mislabelled file is a provider rejecting the request, not a
disclosure — the store is owner-scoped either way.

## Threads key by `(owner_id, thread_id)`

Which has a consequence worth knowing before you mount two endpoints: **two
transports sharing one store share one user's thread list.** A conversation
started at `/internal/agent` shows up in `/public/agent`'s history and can be
resumed there — under the *public* agent's model, tools and guard policy.

`ScopedConversationStore` partitions them without a migration:

```python
from django_spec_agents import ScopedConversationStore

internal = ScopedConversationStore(store, scope="internal")
public = ScopedConversationStore(store, scope="public")
```

**`:` is reserved in a scope name and one containing it is refused.** The
partition *is* the key prefix `scope:`, so a scope name that extends another —
`admin` and `admin:readonly` — makes the shorter scope's prefix match the
longer one's threads: it lists them, and load, rename and delete resolve there
too, silently and in both directions. Names that merely share a prefix
(`admin` / `administrators`) are fine; the separator ends the scope.

## What ships

| Implementation | Use it when |
| --- | --- |
| `NullConversationStore` | The default. `load` returns `None`, `save`/`delete` no-op — the server stays stateless and the conversation lives in the client's posted history. A transport treats this as "persistence off". |
| `NullAttachmentStore` | The default. Uploads off. |
| `DjangoSessionConversationStore` | Per-user persistence with **no migration** — conversations live in the Django session. Good for a demo or a small deployment. |
| `ModelConversationStore` / `ModelAttachmentStore` | Abstract model-backed bases to subclass against your own models. |
| `ScopedConversationStore` | Wraps any of the above to partition by scope. |

## The reference models

The base package ships **no model of its own**, so projects that don't opt in
get no migration. To use the ready-made ones:

```python
INSTALLED_APPS = [
    ...,
    "django_spec_agents.contrib.store",
]
```

Then `migrate`, and pass the matching store to your transport
(`conversation_store=` / `attachment_store=` / `step_store=`). The app provides
`DefaultConversationStore`, `DefaultAttachmentStore`, `DefaultStepStore` and
`DefaultMemoryStore`, and each is imported **from its own module**:

```python
from django_spec_agents.contrib.store.default_attachment_store import (
    DefaultAttachmentStore,
)
from django_spec_agents.contrib.store.default_conversation_store import (
    DefaultConversationStore,
)
from django_spec_agents.contrib.store.default_memory_store import (
    DefaultMemoryStore,
)
from django_spec_agents.contrib.store.default_step_store import DefaultStepStore
```

The shorter `from django_spec_agents.contrib.store import DefaultConversationStore`
is the natural guess and it does not exist. It cannot: this is a Django *app*, so
`INSTALLED_APPS` imports the package itself, and a re-export there would import
models before the app registry is ready — `AppRegistryNotReady` at startup, for
every project, whether or not it uses the stores. The leaf path is the price of the
package being importable at all.

### The app label, and upgrading from django-pydantic-agent

The app's label is `django_spec_agents_store`, so its nine tables are
`django_spec_agents_store_<model>` and its content types, and the permissions
hanging off them, sit under that label.

The same app shipped in django-pydantic-agent as `django_pydantic_agent_store`.
A database that installed it upgrades with the `migrate` a deploy runs anyway:
swap the `INSTALLED_APPS` entry,

```python
INSTALLED_APPS = [
    ...,
    "django_spec_agents.contrib.store",  # was "django_pydantic_agent.contrib.store"
]
```

and run `migrate`. The app's one migration sees the old tables and renames them in
place: the tables, their constraints and named indexes, and the content types, so
permissions, group grants and admin log entries come along by key. The old app's
rows in `django_migrations` are deleted, and nothing is copied, so the rows are
the ones you had.

- **The old install has to be at its last migration**, `0005`, which shipped in
  django-pydantic-agent 0.20. Short of it, `migrate` stops with
  `OldInstallBehindError` and renames nothing: run `migrate` under
  django-pydantic-agent first.
- **Rolling back is one command before the old pin goes back**:
  `migrate django_spec_agents_store zero` renames everything back and records the
  old app's migrations again.
- **Some objects Django named itself keep the old names on PostgreSQL**: the index
  on an attachment's file, the check on its size, the link table's foreign keys and
  each table's primary key and sequence. When a later migration drops or replaces
  one of them, Django asks the database for the column's constraints rather than
  computing the name, so the old names show only to a person reading the schema.
  The exception is the `_like` index PostgreSQL keeps beside the file column,
  which Django drops by a name it computes, so the upgrade renames it too. On
  SQLite a constraint changes only by remaking its table, so there every name
  comes out new.

**Unapplying does the same on a database that never ran django-pydantic-agent.**
The migration cannot tell how it was applied, so on a fresh install
`migrate django_spec_agents_store zero` does not drop the store's tables: it leaves
them under django-pydantic-agent's names, records that package's five migrations
in `django_migrations` and moves the content types to its label. To remove the
store from such a database, drop those tables and delete those rows yourself.

What the upgrade leaves alone is everything stored or configured outside the
schema: the [attachment storage alias](#putting-attachments-somewhere-other-than-the-default)
and the stored files' `django_pydantic_agent/attachments/` prefix, the session key
`DjangoSessionConversationStore` keeps conversations under, and the
`django_pydantic_agent.audit` and `django_pydantic_agent.failure` loggers. Each
keeps django-pydantic-agent's spelling, so a project's `STORAGES`, `LOGGING`, live
sessions and stored files need nothing.

`DefaultAttachmentStore` keeps bytes in Django `Storage` and metadata in a row,
so S3 and friends come free through `STORAGES` — you don't configure anything
here beyond your normal Django storage backend.

### Putting attachments somewhere other than the default

Agent uploads are user-supplied files, and a project often wants them in a
private bucket rather than wherever its other media goes. Name a backend under
the `django_pydantic_agent_attachments` alias and attachments use it; leave the
alias out and they use `default`:

```python
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    "django_pydantic_agent_attachments": {
        "BACKEND": "myproject.storages.PrivateMediaStorage",
    },
}
```

The alias is resolved once, when the model's field is constructed, so it has to
be in `STORAGES` before the app's models are imported — settings, in other
words, which is where it already is.

**Point it somewhere new and the old attachments do not come with it.** Rows keep
the path they were written with, and that path is asked of the *new* backend — so
every attachment written before the switch reads as unavailable until you move
the blobs across yourself. Re-uploading is not a shortcut: it writes fresh copies
rather than finding the originals. Move the files, or accept the loss knowingly.

**The bucket policy needs `s3:ListBucket`** if you point the alias at S3. Saving
an attachment asks the backend whether a matching blob is already there, and with
that permission S3 answers 404 for a key that is absent. Without it S3 answers
403 instead, which django-storages re-raises — turning an upload that would have
been deduplicated into a 500.

A misspelled `BACKEND` raises rather than falling back to `default`. Silently
writing user-supplied files to the public default over a one-character typo is
the outcome this alias exists to prevent.

Everything from here down is the **reference implementation's** behaviour, not
the contract. `ConversationStore` and `AttachmentStore` stay id-based, the wire
is unchanged, and a session-backed or S3-only store you plug in yourself owes
none of this.

## The same file, uploaded twice

`StoredAttachment` carries a `sha256` of the file's bytes, hashed in chunks at
upload so a large file is never held in memory whole just to fingerprint it.
When an upload matches one the same owner already has, the new row points at the
blob already in storage instead of writing a second copy.

**A row per upload, even on a hit.** The row holds the `name` the composer shows
on the chip, so the same bytes sent again as `contract-final.pdf` keep that name
rather than inheriting the first upload's. What is shared is the file
underneath; the bytes are removed only when the last row pointing at them goes.

**Deduplication never crosses owners, and that is not an oversight.** Sharing one
copy of a document held by a hundred tenants would be the better trade on
storage alone. It is not available: a cross-owner hit tells you another tenant
holds a file whose bytes you already have, which is how you confirm that a named
party is a customer, or where a leaked document came from. The lookup is scoped
to one owner, and the index on the column leads with `owner_id` so the
cross-tenant question is not even the cheap one to ask.

Rows written before the column existed have no hash, so they cannot be matched
until `agent_store_backfill_hashes` fills them in.

## Attachment lifecycle

An attachment is reachable as long as a conversation refers to it, and the
reference is derived, not declared. Saving a conversation reconciles the
`StoredConversation.attachments` relation from the attachment ids its own
messages carry — the web component already puts them there, on the user message,
as an `attachments` array of the refs it uploaded. No wire change and no client
change: the ids are in the payload either way.

The parse is total. A malformed entry degrades to "no reference" rather than
raising inside a save, and an id is resolved only against its own owner's
attachments, so a guessed or copied id links nothing.

Two consequences:

- **Deleting a conversation deletes the attachments nothing else refers to**, and
  their bytes. One quoted by a second thread survives untouched.
- **An upload that was never sent belongs to no conversation**, so no cascade
  will ever reach it. `agent_store_prune_attachments` is what collects those.

`StoredAttachment.thread_id` is untouched by all of this and stays. It is a loose
label a project may set; the relation, not the column, is what lifecycle runs on.

**Nothing is ever deleted on a timer the library set up.** Deletion happens on a
cascade you can point at, or on a command you schedule. There is no TTL sweep.

Deleting `StoredConversation` rows by another route — a queryset `delete()`, the
admin — skips the cascade and leaves the attachments unreferenced. They are not
stranded permanently; the prune command is exactly what collects them.

## Maintenance commands

The `contrib.store` app ships four, all of them opt-in and none of them
scheduled for you:

| Command | What it does |
| --- | --- |
| `agent_store_backfill_hashes` | Hash attachments stored before the `sha256` column existed, so they can take part in deduplication. A missing blob is a reported skip, not a failure. |
| `agent_store_prune_attachments` | Delete unreferenced attachments older than `--older-than` (default `24h`). |
| `agent_store_strip_inline_bytes` | Rewrite stored conversations to drop inlined base64 file content. |
| `agent_store_purge_memory` | Erase every stored memory file for one owner. |

All four take `--dry-run`.

**`--older-than` is why the prune command is safe to run.** References alone
cannot tell an upload abandoned last month from one sitting in a composer right
now — both have zero. The threshold is the floor on how long an upload must
survive before it counts as abandoned, so set it to comfortably more than the
longest a message may sit unsent in your product:

```bash
python manage.py agent_store_prune_attachments --older-than 7d --dry-run
```

### Reclaiming space from threads that inlined files

A transport that hands the model an attachment inline serialises the file into
the message list as base64, and the message list is persisted: a 2.6 MB PDF costs
roughly 3.5 MB in one row, loaded whole every time the thread is opened.
Transports have stopped writing them and strip them on the way in, but a row
already written keeps its payload until something rewrites it:

```bash
python manage.py agent_store_strip_inline_bytes --dry-run
python manage.py agent_store_strip_inline_bytes
```

It edits the JSON structurally rather than round-tripping it through a message
type, because that round trip is exactly what would drop each message's `id` and
the non-standard `attachments` array — the array that renders the chips and that
the attachment relation is reconciled from. The bytes themselves are not lost:
the attachment is untouched in the attachment store, and the model reaches it the
way it always does, by id.

## Step persistence

`DefaultStepStore` is the durable, owner-scoped equivalent of
`pydantic-ai-harness`'s own `SqliteStepStore` / `FileStepStore`. Note the
direction of the contract: it **structurally satisfies harness's `StepStore`
protocol** — that protocol is upstream's, not this package's, and this package
does not redeclare it. Owner scoping is entirely ours; no harness record carries
one, which is what makes the store multi-tenant rather than a single-user
ledger.

It needs the `[harness]` extra, since the protocol it satisfies lives there.

### `list_runs` answers oldest first

Ascending `started_at` is upstream's documented protocol order — `StepStore`
invites callers to take the most recent run with `[-1]` — so the store keeps it
rather than serving the order a list happens to want to display. A UI showing
recent runs first sorts where it renders; a store that reversed the contract
would hand that idiom the oldest run instead, silently, and only where more than
one run exists.

Full signatures in the [persistence reference](reference/persistence.md).

## Per-user memory

`pydantic-ai-harness` ships a complete memory capability — four tools
(`write_memory`, `read_memory`, `delete_memory`, `search_memory`), a
`MemoryStore` protocol, bounded injection, compare-and-set versioning and
idempotency receipts. **This package does not reimplement any of it.** It ships
one thing the harness cannot: a Django-backed store, and the scoping that makes
it multi-tenant.

`DefaultMemoryStore` is that store, and the direction of the contract is the same
as `DefaultStepStore`'s: it **structurally satisfies harness's `MemoryStore`
protocol**, which is upstream's and is not redeclared here. It needs the
`[harness]` extra.

```python
from pydantic_ai_harness.memory import Memory

from django_spec_agents import memory_namespace_for_user
from django_spec_agents.contrib.store.default_memory_store import (
    DefaultMemoryStore,
)

capability = Memory(
    DefaultMemoryStore(),
    namespace=lambda ctx: memory_namespace_for_user(ctx.deps.user),
)
```

Hand it to your transport the way you would any other capability —
`AGUIServer(capabilities=[capability])` for django-ag-ui. There is no settings
key and no new configuration surface: `AgentConfig.capabilities` already reaches
`Agent(capabilities=...)`.

**The resolver reads `ctx.deps`, not a request, and that is not incidental.** A
capability list is resolved **once**, at mount time, where no request exists — so
nothing request-shaped can be closed over in it. Per-user scoping rides the *run*
instead: pydantic-ai clones every capability per run, and `Memory` re-resolves
its namespace in the clone, so one agent built once serves every caller. The
transport sets `AgentDeps.user` from the authenticated request, so it is
server-resolved and not something a client can choose.

### Two scoping modes, and which one you get

| Constructed as | Owner is | Use when |
| --- | --- | --- |
| `DefaultMemoryStore()` | the leading path segment — the namespace the capability resolved | the usual case: a mount-time `capabilities=` list |
| `DefaultMemoryStore(request)` | resolved server-side from the request; the path's namespace is not consulted at all | you build the store per request — a custom view |

The request-bound mode is strictly stronger: it partitions rows independently of
the path, so a resolver reading anything user-controlled still cannot cross
scopes (a `/` inside a resolved namespace is *accepted* by the harness and simply
opens further segments). Reach for it if you have a request to hand. Otherwise
pair the namespace-scoped mode with `memory_namespace_for_user`, which derives
the segment from a user the server resolved, and the namespace is a boundary the
client cannot choose either.

`allow_anonymous` applies only to the request-bound mode. Without a request there
is no anonymity to detect, and the resolver decides what an anonymous caller is
called — `memory_namespace_for_user` returns one shared `anon` namespace, since
with no request there is no session to key a per-visitor bucket on. Transports
that refuse anonymous callers by default (django-ag-ui does) never reach it.

### Turn the tool guard on first

Memory may be allowed to steer what the model **says**. It must not be allowed to
decide what the model **does** — and memory is *durable*, model-written, and
replayed into every later run, so a note that reads as an instruction keeps
working long after whoever planted it has gone.

`ToolGuard` is what holds that line: it reads the `x-destructive` stamp
server-side, so a memory-planted "call `refund_order` first" still hits the
approval interrupt. With no `tool_guard`, `build_agent`'s own docstring is blunt
that "every server-side tool here runs the moment the model calls it".

**Enable `TOOL_GUARD` before you enable memory.** A mount with memory on and the
guard off is a durable, user-writable path to unattended destructive calls. Each
setting is defensible alone; they are only wrong together.

### `namespace` is the user, `agent_name` is the mount

The harness composes the scope key as `f"{namespace}/{agent_name}"`, so two
mounts sharing one store are partitioned by construction — `Memory(store,
agent_name="internal")` and `Memory(store, agent_name="public")` — and no
`ScopedMemoryStore` wrapper is needed the way it is for conversations and steps.

Use one of the two shipped resolvers for the user half — `memory_namespace_for_user(ctx.deps.user)`
at mount time, or `memory_namespace(request)` when you hold a request — rather
than reaching for the owner id the other stores partition on. That id is
`anon:<session_key>` for an anonymous request, and a colon is outside the
alphabet the harness accepts for a path segment, so `Memory` raises
`invalid memory path` — from namespace resolution, which sits *outside* the store
read that `injection_errors` guards, so upstream's `"ignore"` default does not
catch it and the whole run 500s.

Both resolvers always return a valid segment: a segment-safe primary key is
carried through readably behind a `u-` prefix, and anything else — an email
primary key, a natural key with a slash — is replaced by a digest of it rather
than sanitised, because sanitising maps `tenant/42` and `tenant-42` onto one
namespace.

### What the store adds over the reference implementations

| | Why |
| --- | --- |
| **Owner-partitioned rows** | The harness's stores are single-tenant; the scope lives only in the path, and the path comes from a resolver the host wrote. |
| **Fence-tag neutralisation on write** | Injected memory is wrapped in `<memory>` markers, and upstream says plainly this "is not a hard prompt-injection boundary". A stored note containing the closing tag ends the block early, and everything after it reads as the user's own turn — durably, on every later run. Every write here escapes the angle brackets of both tags. |
| **Per-owner ceilings** | `max_memory_size` bounds one file and the injection budget bounds what is *read*; nothing upstream caps how many files a namespace accumulates or how large they grow in total. `max_files` and `max_total_chars` do. |
| **Anonymous degradation** | The capability's hooks fire mid-run, so refusing by raising would abort it. Writes no-op and reads return empty unless the store is built with `allow_anonymous=True`. |
| **`purge(owner_id)`** | Erasure, which the protocol cannot express. |

Neutralisation happens on `write` rather than `read` on purpose: it makes the
stored bytes safe for *every* consumer — the injection, `read_memory`,
`search_memory`, an app-side read — and it keeps `write_memory`'s `old_text`
replacement working, because the model edits against the same escaped text it was
shown. It rewrites only the literal tag forms, never the word `memory`, which
belongs in ordinary notes.

### App-authored facts do not belong in the store

The store has no provenance column, so a trusted fact written there becomes
indistinguishable from a model-written one at read time — and it is quoted back
inside the same `<memory>` framing that tells the model this is unverified
background. A fact the operator vouches for belongs in the operator channel:
your transport's instructions hook, or `AgentDeps` for something a tool should
read. The missing provenance column is not a gap to fill; it is a boundary
telling you which facts belong in it.

### Erasure

Memory is durable personal data written *about* a user, so an erasure request has
to be able to reach it. The `MemoryStore` protocol has no bulk or prefix delete —
composing `list_paths` with `delete` needs each path's current version, giving an
unbounded read-then-delete loop a concurrent `write_memory` can lose to — so the
store carries a non-protocol `purge(owner_id)` that is one statement, plus a
command:

```bash
python manage.py agent_store_purge_memory 42 --dry-run
python manage.py agent_store_purge_memory 42
```

It is deliberately **not** wired to a `post_delete` signal on your user model.
Whether deleting an account erases its memory is a product policy; a library's
job here is to make the operation possible.

### Memory must be inspectable before it is switched on

A model quietly accumulating durable notes about someone, replayed into every
future session with no surface listing them, is what makes this feature feel
spooky rather than tailored. The minimum honest surface is **list, read and
delete**, and the protocol provides all three (`list_paths`, `read`, `delete`),
so a host can build one. Do that before turning memory on for real users.
