# BTOF template hierarchy, split for async-upload testing

`btof_stavelet.yaml`, `btof_half_stave.yaml`, `btof_stave.yaml` are the same
three templates as `../btof_stave_templates.yaml` (same names, same
elements, same sourcing/citations -- see that file's header), just split
one-per-file instead of one monolithic `templates:` list. Nothing else
changed.

Each references the level below it by name (`child_template: "BTOF Half-
Stave"`, `child_template: "BTOF Stavelet"`) rather than by load order, so
any of the three can be uploaded in any sequence -- that's exactly the
async/order-independent loading this split exists to exercise. Suggested
sequences to try, each with `hdb load-template`:

- **Leaf-first** (`btof_stavelet.yaml`, then `btof_half_stave.yaml`, then
  `btof_stave.yaml`) -- the traditional order; every reference resolves
  immediately, nothing is ever pending.
- **Parent-first** (`btof_stave.yaml`, then `btof_half_stave.yaml`, then
  `btof_stavelet.yaml`) -- the deepest case: `BTOF Stave` loads with a
  pending placeholder; after the second file it's still incomplete (the
  gap has just moved one level down, into `BTOF Half-Stave`'s own pending
  placeholder); only after the third file does everything resolve and
  `BTOF Stave` become complete and instantiable.
- **Middle-first, or any other order** -- should behave the same as the
  above two: complete and identical once all three are in, incomplete
  with a specific named gap at every point before that.

What to check at each step: the template detail page's "Incomplete"
banner and its "Waiting on: ..." name, the "Pending" tag in the
placeholder table, the Design Templates list's "Incomplete" badge/filter,
that "New from Template" refuses an incomplete `BTOF Stave` (both the
annotated dropdown option and a direct POST), and that `hdb bom-template
"BTOF Stave"` matches the original monolithic file's output once all
three are loaded (verified programmatically before delivery -- all three
orders above reproduce an identical, complete BOM).

**Your real database already has templates named "BTOF Stave", "BTOF
Half-Stave", and "BTOF Stavelet"** -- the pre-existing ones loaded before
the `child_template` rewrite, whose placeholders still point at Components
rather than nested templates (the root cause diagnosed earlier, fix still
on hold). Since template names are globally unique and every element name
in these split files matches an element name already present on those
real rows, uploading these files against your live database as-is will
find each row already there and leave it untouched (`get_or_create` never
overwrites) -- so none of the pending/resolution behavior above would
actually be exercised, silently. To test for real, either point `--root`
at a scratch database (fresh `db.sqlite3`, migrated but not seeded with
these three templates), or delete the three existing stale templates
first so these files have a clean name to create against -- don't do the
latter against the real database without saying so first, since that's
the real-data fix that's still explicitly on hold.
