# Persistence and Storage

## Choosing Persistence and Storage

When the user asks for storage, persistence, saved state, accounts, records, history, progress, or data that should survive across sessions, default to platform-backed persistence rather than browser-only storage.

Use D1 for persistent structured state that needs to survive page reloads or sessions, especially when it represents product data rather than transient UI state.

Typical D1 fits include:

- Users, profiles, settings, tasks, notes, posts, comments, scores, progress, leaderboards, and workflow state.
- Relational data that needs filtering, sorting, joins, indexing, ownership checks, or durable ids.
- Metadata for uploaded or generated files.

Use R2 for uploads, documents, images, videos, audio, exports, generated assets, and other blobs.

Use D1 and R2 together when D1 stores metadata and R2 stores bytes, such as file ownership, filenames, content type, processing status, or searchable fields.

Use browser storage only for device-local, non-authoritative UI preferences such as dismissed banners, theme choice, or temporary draft state. Do not use `localStorage`, `sessionStorage`, or in-memory state as the source of truth for user data that the product is expected to remember.

Leave unused bindings `null`. Do not add persistence or object storage speculatively, but when the product requires durable state, prefer platform storage over browser-only storage.

## Adding Persistence and Storage

Use this flow after choosing the storage shape the product needs.

1. Set the needed logical bindings in `.openai/hosting.json`:
   - use `d1`, usually `DB`, when D1 is required.
   - use `r2` when R2 is required.
   - leave unused bindings `null`.
2. For D1-backed state:
   - put schema definitions in `db/schema.ts`.
   - keep D1 access behind a small helper instead of reading the runtime binding throughout route handlers.
   - use prepared statements on the raw D1 binding for application queries. Generated Drizzle migrations own production schema changes; do not create or alter the same tables or columns during runtime initialization.
   - pass exactly one SQL statement to each `prepare()` call. A single statement may span multiple lines; do not combine multiple semicolon-delimited statements in one prepared SQL string.
   - when one application operation needs multiple statements, prepare them separately and execute them with `batch([...])`.
   - generate and inspect Drizzle SQL after schema changes.
   - save generated migration files with the site source.
   - migrations are applied and recorded individually before Worker upload, so a failed publish may still leave some or all migrations applied. Treat each applied `drizzle/*.sql` file and its matching `drizzle/meta/**` snapshot and journal entry as immutable; append only new migrations and metadata for subsequent schema changes.
   - keep migrations schema-only and bounded. Do not put seed or backfill datasets, huge `VALUES` lists, or long `UNION ALL` chains in migration files.
   - before saving a version, inspect each new SQL file for complete statements and D1-safe deltas. For an existing table, any default on an ordinary added column must be constant; `NOT NULL` additionally needs a non-`NULL` constant default. An added `REFERENCES` column must be nullable with an implicit or explicit `NULL` default. Backfill separately.
   - if publish returns a deterministic `SQLITE_*` migration error, correct only the specifically identified failed, unapplied migration and its matching unapplied metadata or schema, then save and publish a new version; appending a correction does not bypass an earlier failed migration. If the applied/unapplied boundary is uncertain, stop instead of rewriting history or retrying the same archive.
3. For R2-backed files:
   - keep large file payloads in R2 rather than D1.
   - store searchable, relational, or ownership metadata in D1 when the product needs it.
4. Keep the implementation tied to the requested product workflow rather than adding generic storage abstractions the site does not yet use.

Do not satisfy a durable-state request with `localStorage`, `sessionStorage`, or in-memory state unless the user explicitly wants device-local behavior.
