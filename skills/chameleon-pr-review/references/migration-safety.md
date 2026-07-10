# pr-review reference — Step 2.7: Migration-safety pass

### Step 2.7: Migration-safety pass (Rails `db/migrate/*.rb` only)

Run this on every changed file whose path is under `db/migrate/` and which is a Ruby file (`.rb`). Skip every other file. This pass is a pure parse of the migration's text and the diff: it makes NO network calls, runs nothing, and reads no profile data. The convention review (Step 2) cannot help here because a dangerous migration matches its safe siblings on top-level shape; this pass reads the migration DSL inside the change directly.

The DSL calls live inside a `change`, `up`, or `down` method at deeper indentation than the top-level archetype shape the profile matches on. Read the call name and its keyword arguments (`null:`, `default:`, `algorithm:`) across the whole call, including a call that wraps onto a second line. The three checks below are independent; run every one that applies.

This pass has exactly one BLOCK-eligible check and two advisory reminders. Keep the tiers separate. The reminders are NOT findings about this migration being wrong: the dangerous condition (a populated or large table) is a runtime fact this static read cannot see, and the repo's own clean migrations share the same shapes. They are "go verify the table size" prompts for the author, capped at FIX, never BLOCK.

#### 2.7a. Irreversible `change` block (BLOCK)

A `def change` method lets Rails auto-generate the rollback. That only works when every operation in the block is reversible. An irreversible operation inside `change` with no `up`/`down` pair gives a migration that cannot be rolled back: `rails db:rollback` raises `ActiveRecord::IrreversibleMigration` at the worst possible time.

Raise a **BLOCK** when a `change` method contains an operation Rails cannot auto-reverse and the migration does NOT instead define a `def up` / `def down` pair (which makes the rollback explicit and is the correct fix). The irreversible operations are: a bare `remove_column` without the column type and options Rails needs to recreate it, `change_column` (a column TYPE change — always irreversible, Rails cannot know the prior type), `execute` with raw SQL, `remove_index` without the full index definition, `drop_table` without a block describing the table, and `change_column_default` given only the new value with no `from:`/`to:` pair. Note `change_column_null` is NOT in this list: Rails inverts it (it flips the null flag back), so it is auto-reversible and belongs only to the 2.7b table-size check, not here — do not BLOCK on it. A `change` that calls only auto-reversible operations (`create_table`, `add_column`, `add_index`, `add_reference`, `change_column_null`) is correct; do not flag it. A `reversible do |dir| ... end` block clears the BLOCK ONLY when it defines BOTH directions for the irreversible op (`dir.up` AND `dir.down`); a one-directional `reversible` block (`dir.up { execute … }` with no matching `dir.down`) still cannot roll back and does NOT clear the BLOCK — treat the wrapped irreversible op as unhandled.

This is the one clean static win in this pass: an irreversible op inside `change` is a witnessed structural fact in the diff, not a guess about table size, so it earns a BLOCK. Cite the file, the line of the irreversible call, and name the operation. The fix to state: move the body into `def up` / `def down`, or wrap the irreversible part in `reversible do |dir|`.

#### 2.7b. `null:false` added without a default (advisory FIX — verify table size)

Flag an `add_column` (or `add_reference`) carrying `null: false` with no `default:` keyword, and a `change_column_null ..., false` with no backfill in the same migration. On a populated table this fails: existing rows have NULL in the new column and the NOT NULL constraint rejects them mid-migration.

Raise a **FIX**, never BLOCK, and label it exactly: "advisory, verify table size: `null: false` with no `default:` fails on a populated table because existing rows violate the constraint. Safe on an empty table; this static read cannot see the row count." The fix to suggest: add a `default:`, or backfill the column in a prior step before adding the constraint. Do not present this as a confirmed defect; the repo's safe migrations use this same shape on tables that happen to be empty.

#### 2.7c. `add_index` without `algorithm: :concurrently` (advisory FIX — verify table size)

Flag an `add_index` (or `add_reference ..., index: true`) call that does NOT pass `algorithm: :concurrently`. A plain `add_index` takes a lock that blocks writes for the duration of the build; on a large table in production that is a write outage.

Raise a **FIX**, never BLOCK, and label it exactly: "advisory, verify table size: `add_index` without `algorithm: :concurrently` locks the table against writes while the index builds. Fine on a small table; this static read cannot see the row count." The fix to suggest: add `algorithm: :concurrently` (and `disable_ddl_transaction!` at the top of the migration, which the concurrent build requires). Do not present this as a confirmed defect; most index migrations in a typical repo omit `concurrently` and are fine because the table was small.

Never let 2.7b or 2.7c reach BLOCK. They are table-size reminders the author resolves by checking the row count, not findings backed by anything this pass can see. Only the irreversible-`change` check (2.7a) is a witnessed structural fact and the only BLOCK this pass can raise.
