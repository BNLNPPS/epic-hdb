"""
Management command: python manage.py seed_hdb
Populates the HDB with the BEMC / BTOF sample dataset.

Kept in sync with the current hdb/models.py schema -- it only touches
fields and models that actually exist there. In particular:
  * There is no ComponentFunction model and no Component.function field,
    so components are described directly via name/description instead.
  * Django's built-in Group model has no description field, so groups
    are created by name only.
  * ComponentInstance has no qr_id field; instances are identified by
    their human-readable tag (unique per component).

Idempotent: safe to run multiple times, never duplicates or clobbers
existing records (uses get_or_create() throughout; passwords are only
set the first time an account is created). DesignTemplates are the one
exception to "built up here": they're loaded from the tracked YAML files
under data/ via hdb_client, same as running `hdb load-template` by hand
-- see the import section below.
"""
import os
import random
import sys
from datetime import datetime, time as dt_time

from django.conf import settings
from django.contrib.auth.models import User, Group
from django.core.files import File
from django.core.management.base import BaseCommand
from django.utils import timezone
from hdb.models import (
    Institution, Location, TechnicalSystem, Component, ComponentInstance,
    UserProfile, PropertyType, PropertyValue, Design, DesignElement,
    DesignTemplate, LogEntry,
)

# DesignTemplates are seeded from the tracked YAML files under data/,
# below ("DesignTemplate(s): loaded from..."), not built up here
# field-by-field -- see data/README.md and client/README.md's "Design
# Templates -- loading from YAML" / "Provenance and drift detection" for
# why: a template built directly via the ORM has no source_path/
# source_sha256 behind it, so it can never be checked for drift against
# a tracked file the way one loaded through hdb_client can. That needs
# hdb_client (client/), which isn't on sys.path by default for a
# management command the way it is for `PYTHONPATH=client python
# manage.py shell` -- add it explicitly, once, here.
_CLIENT_DIR = str(settings.BASE_DIR / "client")
if _CLIENT_DIR not in sys.path:
    sys.path.insert(0, _CLIENT_DIR)
from hdb_client import HDBClient  # noqa: E402  (must follow the sys.path insert above)


class Command(BaseCommand):
    help = "Seed the HDB with the BEMC/BTOF sample dataset"

    def handle(self, *args, **options):
        self.stdout.write("Seeding HDB …")

        # Groups
        grp = {}
        for n in ["BEMC", "BTOF", "PFRICH"]:
            g, _ = Group.objects.get_or_create(name=n)
            grp[n] = g

        # Users
        # Note: a freshly created User has password="", and Django's
        # has_usable_password() treats an empty string as "usable" (it only
        # checks for the "!" unusable-password marker) -- so it can't be used
        # here to detect "needs a password set". Use the get_or_create()
        # "created" flag instead, so the password is set once on first seed
        # and never clobbered on subsequent idempotent re-runs.
        def mkuser(username, is_staff=False, is_superuser=False, groups=(),
                   first_name="", last_name="", email=""):
            u, created = User.objects.get_or_create(
                username=username,
                defaults={"is_staff": is_staff, "is_superuser": is_superuser},
            )
            if created:
                u.set_password(username)
            # first/last name and email are safe to keep in sync on every
            # run (unlike the password, they're not secret and there's no
            # harm in re-applying the seed's intended values).
            u.first_name = first_name
            u.last_name  = last_name
            u.email      = email
            u.save()
            if groups:
                u.groups.set([grp[g] for g in groups])
            return u

        mkuser("admin", is_staff=True, is_superuser=True)
        maxim = mkuser("maxim", is_staff=True, is_superuser=True,
                        first_name="Maxim", last_name="Potekhin", email="potekhin@bnl.gov")
        ottjenni = mkuser("ottjenni", groups=["BTOF"],
                           first_name="Jennifer", last_name="Ott",
                           email="ottjenni@hawaii.edu")
        gnigmat = mkuser("gnigmat", groups=["BTOF"],
                          first_name="Grigory", last_name="Nigmatkulov",
                          email="gnigmat@uic.edu")
        crafts  = mkuser("crafts",  groups=["BEMC"],
                          first_name="Casey", last_name="Crafts",
                          email="crafts@cua.edu")
        ullrich = mkuser("ullrich", groups=["BEMC", "BTOF"],
                          first_name="Thomas", last_name="Ullrich",
                          email="thomas.ullrich@bnl.gov")
        # PFRICH: pfRICH (proximity-focusing RICH) detector developers,
        # both BNL -- confirmed via public ePIC/pfRICH meeting agendas and
        # talks on indico.bnl.gov (pfRICH kick-off meeting, general
        # meetings, CDR, and DAC status updates).
        bpage = mkuser("bpage", groups=["PFRICH"],
                        first_name="Brian", last_name="Page",
                        email="bpage@bnl.gov")
        ayk = mkuser("ayk", groups=["PFRICH"],
                      first_name="Alexander", last_name="Kiselev",
                      email="ayk@bnl.gov")

        # Technical systems, each owned by a responsible group
        ts = {}
        for n, group_name in [
            ("BEMC-CRYSTAL",  "BEMC"),
            ("BEMC-PM",       "BEMC"),
            ("BTOF-Sensor",   "BTOF"),
            ("BTOF-Readout",  "BTOF"),
            ("PFRICH-HRPPD",  "PFRICH"),
        ]:
            o, _ = TechnicalSystem.objects.get_or_create(
                name=n, defaults={"group": grp[group_name]}
            )
            ts[n] = o

        # Institutions + locations
        cua, _ = Institution.objects.get_or_create(
            name="Catholic University of America",
            defaults={"abbreviation": "CUA", "country": "USA", "city": "Washington, DC",
                      "url": "https://www.cua.edu"},
        )
        uic, _ = Institution.objects.get_or_create(
            name="University of Illinois Chicago",
            defaults={"abbreviation": "UIC", "country": "USA", "city": "Chicago",
                      "url": "https://www.uic.edu"},
        )
        bnl, _ = Institution.objects.get_or_create(
            name="Brookhaven National Laboratory",
            defaults={"abbreviation": "BNL", "country": "USA", "city": "Upton, NY",
                      "url": "https://www.bnl.gov"},
        )
        hawaii, _ = Institution.objects.get_or_create(
            name="University of Hawaii",
            defaults={"abbreviation": "UH", "country": "USA", "city": "Honolulu, HI",
                      "url": "https://www.hawaii.edu"},
        )

        storage_room, _ = Location.objects.get_or_create(
            name="Storage Room", location_type="room", institution=cua,
        )
        test_lab, _ = Location.objects.get_or_create(
            name="Test Lab", location_type="room", institution=uic,
        )
        bldg_510a, _ = Location.objects.get_or_create(
            name="Bldg,510A", location_type="building", institution=bnl,
        )

        # Link each user to their home institution.
        for user, inst in [(crafts, cua), (gnigmat, uic), (ullrich, bnl), (maxim, bnl), (ottjenni, hawaii),
                           (bpage, bnl), (ayk, bnl)]:
            UserProfile.objects.get_or_create(user=user, defaults={"institution": inst})

        # Components  (Component has no "function" field -- the functional
        # role is folded into the description instead)
        def mkcomp(name, model, desc, sys_name, group_name, owner):
            c, _ = Component.objects.get_or_create(name=name, project="ePIC", defaults=dict(
                model_number=model, description=desc,
                technical_system=ts.get(sys_name),
                owner_group=grp[group_name], owner_user=owner, created_by=owner))
            return c

        crystal = mkcomp(
            "PbWO4 Crystal", "PWO-BEMC-01",
            "Lead tungstate scintillating crystal for the Backward EMCal.",
            "BEMC-CRYSTAL", "BEMC", crafts,
        )
        pm = mkcomp(
            "Hamamatsu S14160-3010PS", "S14160-3010PS",
            "3x3mm 10um-pitch SiPM photosensor for BEMC readout.",
            "BEMC-PM", "BEMC", crafts,
        )
        sensor = mkcomp(
            "AC-LGAD Sensor", "AC-LGAD-v1",
            "AC-coupled Low Gain Avalanche Diode sensor for BTOF.",
            "BTOF-Sensor", "BTOF", gnigmat,
        )
        readout = mkcomp(
            "FCFDv2 Readout", "FCFDv2",
            "Fast Constant-Fraction Discriminator readout ASIC for BTOF.",
            "BTOF-Readout", "BTOF", gnigmat,
        )

        # Component instances -- 2 to 5 per component, spread across the
        # two locations. Identified by tag (unique per component), not by
        # any qr_id field.
        def mkinst(tag, comp, loc, owner, group_name, serial=""):
            i, _ = ComponentInstance.objects.get_or_create(tag=tag, component=comp, defaults=dict(
                location=loc, serial_number=serial,
                owner_group=grp[group_name], owner_user=owner, created_by=owner))
            return i

        crystal_instances = []
        for i in range(1, 4):  # 3 instances
            loc = storage_room if i % 2 else test_lab
            crystal_instances.append(
                mkinst(f"BEMC-CRYSTAL-{i:03d}", crystal, loc, crafts, "BEMC", f"PWO-{i:04d}")
            )

        for i in range(1, 5):  # 4 instances
            loc = storage_room if i % 2 else test_lab
            mkinst(f"BEMC-PM-{i:03d}", pm, loc, crafts, "BEMC", f"HAM-{i:04d}")

        # 12 additional Hamamatsu units, all delivered to CUA / Storage Room --
        # same tag/serial pattern as the first four (BEMC-PM-NNN / HAM-NNNN),
        # just continuing the numbering upward.
        for i in range(5, 17):  # 12 more instances
            mkinst(f"BEMC-PM-{i:03d}", pm, storage_room, crafts, "BEMC", f"HAM-{i:04d}")

        for i in range(1, 3):  # 2 instances
            loc = test_lab if i % 2 else storage_room
            mkinst(f"BTOF-SENSOR-{i:03d}", sensor, loc, gnigmat, "BTOF", f"LGAD-{i:04d}")

        for i in range(1, 6):  # 5 instances
            loc = test_lab if i % 2 else storage_room
            mkinst(f"BTOF-READOUT-{i:03d}", readout, loc, gnigmat, "BTOF", f"FCFD-{i:04d}")

        # Property types + a couple of example values, to demonstrate
        # component -> instance property inheritance (and overriding).
        weight_pt, _ = PropertyType.objects.get_or_create(
            name="Weight", defaults={"category": "physical", "default_units": "kg"},
        )
        timing_constant_pt, _ = PropertyType.objects.get_or_create(
            name="Timing Constant", defaults={"category": "physical", "default_units": "ps"},
        )
        length_pt, _ = PropertyType.objects.get_or_create(
            name="Length", defaults={"category": "physical", "default_units": "cm"},
        )
        width_pt, _ = PropertyType.objects.get_or_create(
            name="Width", defaults={"category": "physical", "default_units": "cm"},
        )
        height_pt, _ = PropertyType.objects.get_or_create(
            name="Height", defaults={"category": "physical", "default_units": "cm"},
        )
        image_pt, _ = PropertyType.objects.get_or_create(
            name="Image", defaults={"category": "documentation", "handler": "image"},
        )

        # Component-level defaults: every instance of the crystal inherits
        # these unless it overrides the same (property_type, tag) pair.
        PropertyValue.objects.get_or_create(
            component=crystal, property_type=weight_pt, tag="",
            defaults={"value": "0.45", "units": "kg"},
        )
        PropertyValue.objects.get_or_create(
            component=crystal, property_type=length_pt, tag="",
            defaults={"value": "20", "units": "cm"},
        )
        PropertyValue.objects.get_or_create(
            component=crystal, property_type=width_pt, tag="",
            defaults={"value": "2", "units": "cm"},
        )

        # Component-level default: Hamamatsu SiPM weight. Uses the
        # get_or_create + conditional-update pattern (not plain
        # get_or_create with defaults=) so that if a Weight row already
        # exists for this component -- e.g. entered manually through the
        # web UI before this seed code existed -- re-running the seed
        # still forces it to the correct 2.1 g rather than leaving a
        # stale value in place.
        pm_weight_pv, pm_weight_created = PropertyValue.objects.get_or_create(
            component=pm, property_type=weight_pt, tag="",
            defaults={"value": "2.1", "units": "g"},
        )
        if not pm_weight_created and (pm_weight_pv.value != "2.1" or pm_weight_pv.units != "g"):
            pm_weight_pv.value = "2.1"
            pm_weight_pv.units = "g"
            pm_weight_pv.save()

        # Component-level defaults: Hamamatsu SiPM length and width.
        # Same conditional-update pattern as the weight above, so a
        # pre-existing manually-entered value gets corrected on re-seed
        # instead of silently surviving.
        pm_length_pv, pm_length_created = PropertyValue.objects.get_or_create(
            component=pm, property_type=length_pt, tag="",
            defaults={"value": "3", "units": "mm"},
        )
        if not pm_length_created and (pm_length_pv.value != "3" or pm_length_pv.units != "mm"):
            pm_length_pv.value = "3"
            pm_length_pv.units = "mm"
            pm_length_pv.save()

        pm_width_pv, pm_width_created = PropertyValue.objects.get_or_create(
            component=pm, property_type=width_pt, tag="",
            defaults={"value": "3", "units": "mm"},
        )
        if not pm_width_created and (pm_width_pv.value != "3" or pm_width_pv.units != "mm"):
            pm_width_pv.value = "3"
            pm_width_pv.units = "mm"
            pm_width_pv.save()

        PropertyValue.objects.get_or_create(
            component=crystal, property_type=height_pt, tag="",
            defaults={"value": "2", "units": "cm"},
        )

        # Attach the crystal's reference photo as an Image-type property.
        # Only assign the file the first time (or if it's missing) --
        # FileField.save() renames on every call to avoid clobbering an
        # existing file, so calling it unconditionally on every idempotent
        # re-seed would pile up "PbWO4_crystal_<hash>.jpg" duplicates.
        image_pv, _ = PropertyValue.objects.get_or_create(
            component=crystal, property_type=image_pt, tag="",
        )
        if not image_pv.file:
            photo_path = os.path.join(settings.BASE_DIR, "assets", "images", "PbWO4_crystal.jpg")
            if os.path.exists(photo_path):
                with open(photo_path, "rb") as f:
                    image_pv.file.save("PbWO4_crystal.jpg", File(f), save=True)

        # Attach the Hamamatsu SiPM's reference photo as an Image-type
        # property, same idempotent-attach pattern as the crystal's photo
        # above.
        pm_image_pv, _ = PropertyValue.objects.get_or_create(
            component=pm, property_type=image_pt, tag="",
        )
        if not pm_image_pv.file:
            pm_photo_path = os.path.join(settings.BASE_DIR, "assets", "images", "HamamatsuS14160-3010PS.jpg")
            if os.path.exists(pm_photo_path):
                with open(pm_photo_path, "rb") as f:
                    pm_image_pv.file.save("HamamatsuS14160-3010PS.jpg", File(f), save=True)

        # One instance overrides the inherited Weight -- this particular
        # crystal was measured slightly lighter than the catalog default.
        if crystal_instances:
            PropertyValue.objects.get_or_create(
                component_instance=crystal_instances[0], property_type=weight_pt, tag="",
                defaults={"value": "0.44", "units": "kg"},
            )

        # DesignTemplate(s): loaded from the tracked YAML files under data/,
        # not built up here field-by-field -- see the sys.path/import note
        # above. Every template this creates or touches gets real
        # source_path/source_sha256/source_git_commit provenance (see
        # DesignTemplate in hdb/models.py), the same as running
        # `hdb load-template` by hand would -- this is that same code
        # path, just invoked from the seed script instead of the CLI.
        # `crafts` as the acting user matches client/README.md's own
        # documented example (`hdb --user crafts load-template ...`).
        #
        # Idempotent the same way the rest of this command is: re-running
        # `seed_hdb` re-loads each file, which never duplicates or
        # clobbers an existing template's structure (get_or_create
        # throughout the loader too) -- it just re-stamps provenance to
        # reflect this load, same as any other `load-template` run.
        design_client = HDBClient(user=crafts).designs
        for yaml_name in [
            "bemc_tower_template.yaml",
            # BTOF hierarchy -- split one-template-per-file, see
            # data/btof_split/README.md; order doesn't matter (uploads
            # are asynchronous/order-independent), listed leaf-first here
            # only because that's the traditional reading order.
            os.path.join("btof_split", "btof_stavelet.yaml"),
            os.path.join("btof_split", "btof_half_stave.yaml"),
            os.path.join("btof_split", "btof_stave.yaml"),
        ]:
            design_client.load_templates_from_yaml(
                str(settings.BASE_DIR / "data" / yaml_name)
            )

        tower_tpl = DesignTemplate.objects.get(name="BEMC tower")

        # Design: "BEMC tower" -- a placeholder assembly referencing catalog
        # items (not specific inventory instances) for one crystal and its
        # four readout photosensors.
        bemc_tower, bemc_tower_created = Design.objects.get_or_create(
            name="BEMC tower",
            defaults={"project": "ePIC", "owner_group": grp["BEMC"], "owner_user": crafts,
                      "description": "One BEMC tower: a PbWO4 crystal read out by four SiPMs."},
        )
        if not bemc_tower_created and bemc_tower.owner_group_id != grp["BEMC"].id:
            bemc_tower.owner_group = grp["BEMC"]
            bemc_tower.save()
        if not bemc_tower_created and bemc_tower.owner_user_id != crafts.id:
            bemc_tower.owner_user = crafts
            bemc_tower.save()
        if bemc_tower.template_id != tower_tpl.id:
            bemc_tower.template = tower_tpl
            bemc_tower.save()
        DesignElement.objects.get_or_create(
            design=bemc_tower, element_name="Crystal",
            defaults={"component": crystal, "quantity": 1},
        )
        DesignElement.objects.get_or_create(
            design=bemc_tower, element_name="SiPM",
            defaults={"component": pm, "quantity": 4},
        )
        log_entry, log_created = LogEntry.objects.get_or_create(
            design=bemc_tower, entry="BEMC Tower Definition added",
            defaults={"logged_by": crafts, "topic": "design"},
        )
        if not log_created and log_entry.logged_by_id != crafts.id:
            log_entry.logged_by = crafts
            log_entry.save()
        if not log_created and log_entry.topic != "design":
            log_entry.topic = "design"
            log_entry.save()

        # Backdated record: BTOF-SENSOR-001 was actually added to inventory
        # on 2026-07-09, at some point during the day -- the exact time
        # wasn't tracked at the time, so a plausible one is picked once and
        # then left alone on every subsequent (idempotent) re-seed.
        sensor1 = ComponentInstance.objects.filter(tag="BTOF-SENSOR-001").first()
        if sensor1:
            backdated_time = timezone.make_aware(datetime.combine(
                datetime(2026, 7, 9).date(),
                dt_time(random.randint(0, 23), random.randint(0, 59), random.randint(0, 59)),
            ))
            sensor_log, sensor_log_created = LogEntry.objects.get_or_create(
                component_instance=sensor1, entry="Inventory item created",
                defaults={"topic": "inventory", "logged_by": gnigmat, "timestamp": backdated_time},
            )
            if not sensor_log_created and sensor_log.topic != "inventory":
                sensor_log.topic = "inventory"
                sensor_log.save()

        self.stdout.write(self.style.SUCCESS(
            f"\nDone.  admin/admin, maxim/maxim | "
            f"Groups:{Group.objects.filter(name__in=['BEMC','BTOF','PFRICH']).count()}  "
            f"Systems:{TechnicalSystem.objects.count()}  "
            f"Components:{Component.objects.count()}  "
            f"Instances:{ComponentInstance.objects.count()}  "
            f"Institutions:{Institution.objects.count()}"
        ))
    