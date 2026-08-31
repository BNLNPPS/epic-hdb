# The data folder

YAML input files for the `hdb_client` CLI (`client/hdb.py`). Two kinds:
`new_crystal.yaml` is a standalone sample matching an entity `seed_hdb`
already creates directly. The DesignTemplate files
(`bemc_tower_template.yaml`, `btof_split/*.yaml`) are the other way
around -- they're the actual source `python manage.py seed_hdb` loads
*from* (via `hdb_client`, same as running `hdb load-template` by hand)
to create the "BEMC tower" / BTOF templates in the first place, not just
samples that happen to match what seeding already did. See
`hdb/management/commands/seed_hdb.py`.

## `new_crystal.yaml` — create a new inventory instance

Creates one new `ComponentInstance` of the seeded "PbWO4 Crystal" component,
stored at the seeded "Storage Room" location (CUA), owned by the BEMC group.
`create-instance` requires `--user`, and that user must belong to the
group given in the file (or be staff/superuser) — see
[`client/README.md`](../client/README.md#create-instance--write-a-new-inventory-item).

```bash
python client/hdb.py --user crafts create-instance --from-yaml data/new_crystal.yaml
```

`crafts` is a member of BEMC in the seed data, so this succeeds. Real
output:

```
Created: be0eacde-d2a0-490d-a379-5bd1fcd9a6ba
id: be0eacde-d2a0-490d-a379-5bd1fcd9a6ba
tag: BEMC-CRYSTAL-099
serial_number: PWO-0099
description: New delivery, batch 12, visual inspection passed
component: PbWO4 Crystal
technical_system: BEMC-CRYSTAL
location: CUA / Storage Room
owner_user: crafts
owner_group: BEMC
...
```

### Avoiding duplicate-looking tags on re-runs

Re-running this command creates a brand-new `ComponentInstance` every time —
`tag` and `serial_number` are plain text fields with **no uniqueness
enforced at the database level** (no `unique` or `unique_together`
constraint on either one). Running the example above twice unmodified
doesn't error or overwrite anything; it silently creates a second instance
that displays the same `tag: BEMC-CRYSTAL-099`, distinguishable from the
first only by its internal UUID `id`. There's no built-in feature that
prevents or auto-resolves this — avoiding it is entirely up to you, and
comes down to picking a fresh `tag`/`serial` value yourself before each
create, by one of two routes:

1. **Edit the YAML file** — open `data/new_crystal.yaml` and change `tag`
   and `serial` to new values (e.g. `BEMC-CRYSTAL-100` / `PWO-0100`) before
   running `create-instance --from-yaml` again.
2. **Skip the file and pass flags directly** — every field the YAML sets
   has an equivalent CLI flag, so you can create another instance without
   touching the file at all:
   ```bash
   python client/hdb.py --user crafts create-instance \
       --by-name "PbWO4 Crystal" --tag BEMC-CRYSTAL-100 --serial PWO-0100 \
       --group BEMC --description "New delivery, batch 13"
   ```

To check which tags already exist before picking a new one:
```bash
python client/hdb.py find instance "BEMC-CRYSTAL*"
```

(This is the same pattern `seed_hdb.py` uses internally for its own
sequentially-numbered tags — see `client/README.md`'s
[`create-instance`](../client/README.md#create-instance--write-a-new-inventory-item)
section for the full flag reference.)

## `btof_split/` — load Design Templates (current)

A realistic, multi-level `DesignTemplate` hierarchy for the ePIC BTOF
detector (Stave → Half-Stave → Stavelet), split one template per file so
each level can be uploaded independently — see
[`btof_split/README.md`](btof_split/README.md) for why and suggested
upload orders. Used with `load-template` instead of `create-instance`:

```bash
python client/hdb.py --user crafts load-template btof_split/btof_stavelet.yaml
python client/hdb.py --user crafts load-template btof_split/btof_half_stave.yaml
python client/hdb.py --user crafts load-template btof_split/btof_stave.yaml
```

This is a curator/administrative operation, not a per-request scoped
write, and it's idempotent — safe to re-run. `python manage.py seed_hdb`
loads these same three files itself (in this same leaf-first order) as
part of seeding, so a fresh database gets working BTOF templates with
real provenance without a separate manual `load-template` step; running
`load-template` on them by hand afterwards is still fine too, and does
nothing new (get_or_create, same as always). See
[`client/README.md`](../client/README.md#design-templates--loading-from-yaml)
for the full YAML format and
[the top-level `README.md`](../README.md#domain-3--designs) for why
multi-level template hierarchies are modeled this way.

`btof_stave_templates.yaml` (no longer split) was the original source for
these same three templates. It's now **deprecated** — see the header
comment in that file — kept only for its sourcing citations, not as
something to load or re-load. `btof_split/` is canonical.

## `bemc_tower_template.yaml` — load the "BEMC tower" Design Template

A flat, single-level `DesignTemplate`: one PbWO4 crystal, read out by
four Hamamatsu SiPMs -- no nested sub-templates (see `btof_split/` above
for what a multi-level one looks like). Its two `component:` entries are
plain strings, not dicts, meaning both ("PbWO4 Crystal", "Hamamatsu
S14160-3010PS") are expected to already exist in the catalog rather than
being auto-created -- see the file's own header comment.

```bash
python client/hdb.py --user crafts load-template bemc_tower_template.yaml
```

Like `btof_split/`, `python manage.py seed_hdb` loads this file itself as
part of seeding (after creating the two components above it depends on)
-- this is what actually creates the "BEMC tower" `DesignTemplate` now,
replacing what used to be a direct, in-script `DesignTemplate.objects
.get_or_create(...)` call with no file behind it at all. Loading it by
hand afterwards is safe and does nothing new.
