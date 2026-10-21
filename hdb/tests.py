"""Regression tests for the bug-review fixes and the feature work built on them."""
import io
from unittest import mock
from contextlib import redirect_stdout

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse

from .models import (
    DesignTemplate, DesignTemplateElement,
    Component, ComponentInstance, Design, DesignElement, DesignElementInstance,
    Institution, Location, PropertyType, PropertyValue, TechnicalSystem,
)


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.group = Group.objects.create(name="G")
        cls.user = User.objects.create_user("u", password="x")
        cls.user.groups.add(cls.group)
        cls.inst = Institution.objects.create(name="BNL")
        cls.bldg = Location.objects.create(name="B", location_type="building", institution=cls.inst)
        cls.l1 = Location.objects.create(name="L1", institution=cls.inst, parent=cls.bldg)
        cls.l2 = Location.objects.create(name="L2", institution=cls.inst, parent=cls.bldg)
        cls.ts1 = TechnicalSystem.objects.create(name="TS1")
        cls.ts2 = TechnicalSystem.objects.create(name="TS2")
        cls.comp = Component.objects.create(name="C", technical_system=cls.ts1,
                                            owner_group=cls.group)

    def setUp(self):
        self.client.force_login(self.user)

    def instance(self, tag="t", location=None):
        return ComponentInstance.objects.create(
            component=self.comp, tag=tag, location=location or self.l1,
            owner_group=self.group, owner_user=self.user)

    def design(self, name="D", location=None):
        return Design.objects.create(name=name, location=location or self.l1,
                                     owner_group=self.group, owner_user=self.user)

    def install(self, design, instance, element_name="e"):
        el = DesignElement.objects.create(design=design, element_name=element_name,
                                          component=self.comp)
        DesignElementInstance.objects.create(element=el, instance=instance)
        return el


class LocationInvariant(Base):
    def test_single_move_of_installed_instance_refused(self):
        i = self.instance()
        self.install(self.design(), i)
        r = self.client.post(reverse("inventory-update-location", args=[i.pk]),
                             {"location": self.l2.pk}, follow=True)
        i.refresh_from_db()
        self.assertEqual(i.location_id, self.l1.pk)
        self.assertContains(r, "installed in a design")

    def test_single_move_of_free_instance_works(self):
        i = self.instance()
        self.client.post(reverse("inventory-update-location", args=[i.pk]),
                         {"location": self.l2.pk})
        i.refresh_from_db()
        self.assertEqual(i.location_id, self.l2.pk)

    def test_batch_skips_installed_and_bumps_modified_on(self):
        free, installed = self.instance("a"), self.instance("b")
        self.install(self.design(), installed)
        before = free.modified_on
        r = self.client.post(reverse("inventory-batch-location"), {
            "instances": [free.pk, installed.pk], "target_location": self.l2.pk})
        free.refresh_from_db(); installed.refresh_from_db()
        self.assertEqual(free.location_id, self.l2.pk)
        self.assertEqual(installed.location_id, self.l1.pk)
        self.assertGreater(free.modified_on, before)
        self.assertIn("batch_updated=1", r["Location"])
        self.assertIn("batch_installed_skipped=1", r["Location"])

    def test_design_move_carries_installed_items_and_subdesigns(self):
        parent, child = self.design("P"), self.design("Ch")
        DesignElement.objects.create(design=parent, element_name="sub", child_design=child)
        i1, i2 = self.instance("a"), self.instance("b")
        self.install(parent, i1, "x")
        self.install(child, i2, "y")
        r = self.client.post(reverse("design-update-location", args=[parent.pk]),
                             {"location": self.l2.pk}, follow=True)
        for obj in (parent, child, i1, i2):
            obj.refresh_from_db()
            self.assertEqual(obj.location_id, self.l2.pk)
        self.assertContains(r, "2 installed item(s)")
        self.assertTrue(i1.log_entries.filter(entry__contains="moved with design").exists())

    def test_design_move_all_or_nothing_on_permission(self):
        d = self.design()
        foreign = ComponentInstance.objects.create(
            component=self.comp, tag="f", location=self.l1,
            owner_group=Group.objects.create(name="Other"))
        mine = self.instance("m")
        self.install(d, mine, "x"); self.install(d, foreign, "y")
        self.client.post(reverse("design-update-location", args=[d.pk]),
                         {"location": self.l2.pk})
        for obj in (d, mine, foreign):
            obj.refresh_from_db()
            self.assertEqual(obj.location_id, self.l1.pk)

    def test_design_move_refused_for_shared_subdesign_and_blank_target(self):
        parent, child, other = self.design("P"), self.design("Ch"), self.design("O")
        DesignElement.objects.create(design=parent, element_name="s", child_design=child)
        DesignElement.objects.create(design=other, element_name="s", child_design=child)
        self.client.post(reverse("design-update-location", args=[parent.pk]),
                         {"location": self.l2.pk})
        parent.refresh_from_db(); child.refresh_from_db()
        self.assertEqual((parent.location_id, child.location_id), (self.l1.pk, self.l1.pk))
        d = self.design("Q"); self.install(d, self.instance("z"))
        self.client.post(reverse("design-update-location", args=[d.pk]), {"location": ""})
        d.refresh_from_db()
        self.assertEqual(d.location_id, self.l1.pk)

    def test_design_location_changes_when_nothing_installed(self):
        d = self.design()
        self.client.post(reverse("design-update-location", args=[d.pk]),
                         {"location": self.l2.pk})
        d.refresh_from_db()
        self.assertEqual(d.location_id, self.l2.pk)


class DesignDeletion(Base):
    def test_component_in_design_is_protected(self):
        self.install(self.design(), self.instance())
        with self.assertRaises(ProtectedError):
            self.comp.delete()

    def test_cannot_delete_subdesign_used_by_parent(self):
        parent, child = self.design("P"), self.design("Ch")
        DesignElement.objects.create(design=parent, element_name="sub", child_design=child)
        self.client.post(reverse("design-delete", args=[child.pk]))
        self.assertTrue(Design.objects.filter(pk=child.pk).exists())
        self.assertTrue(parent.elements.filter(child_design=child).exists())

    def test_deleting_parent_cascades_exclusive_children_only(self):
        parent, child, grand = self.design("P"), self.design("C1"), self.design("C2")
        shared, other = self.design("Shared"), self.design("Other")
        DesignElement.objects.create(design=parent, element_name="a", child_design=child)
        DesignElement.objects.create(design=child, element_name="b", child_design=grand)
        DesignElement.objects.create(design=parent, element_name="s", child_design=shared)
        DesignElement.objects.create(design=other, element_name="s", child_design=shared)
        self.client.post(reverse("design-delete", args=[parent.pk]))
        names = set(Design.objects.values_list("name", flat=True))
        self.assertEqual(names, {"Shared", "Other"})


class PropertyDuplicates(Base):
    def test_override_survives_duplicate_rows(self):
        pt = PropertyType.objects.create(name="Weight")
        base = PropertyValue.objects.create(component=self.comp, property_type=pt,
                                            tag="w", value="1")
        i = self.instance()
        PropertyValue.objects.create(component_instance=i, property_type=pt, tag="w", value="2")
        PropertyValue.objects.create(component_instance=i, property_type=pt, tag="w", value="3")
        r = self.client.post(
            reverse("inventory-property-update", args=[i.pk, base.pk]), {"value": "9"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(
            PropertyValue.objects.filter(component_instance=i, tag="w", value="9").count(), 1)


class TechnicalSystemSync(Base):
    def test_component_change_propagates_to_inheriting_instances(self):
        i = self.instance()
        self.assertEqual(i.technical_system_id, self.ts1.pk)
        self.comp.technical_system = self.ts2
        self.comp.save()
        i.refresh_from_db()
        self.assertEqual(i.technical_system_id, self.ts2.pk)

    def test_system_filter_follows_component(self):
        self.instance()
        Component.objects.filter(pk=self.comp.pk).update(technical_system=self.ts2)  # bypass sync
        r = self.client.get(reverse("location-inventory", args=[self.l1.pk]), {"system": "TS2"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "TS2")
        r = self.client.get(reverse("location-inventory", args=[self.l1.pk]), {"system": "TS1"})
        self.assertNotContains(r, "<td>t</td>")


class ApiRoot(Base):
    def test_links_point_at_api_not_web_pages(self):
        data = self.client.get(reverse("api-root")).json()
        for key in ("components", "designs", "logs"):
            self.assertIn("/api/", data[key], key)


class SeedGuard(TestCase):
    def test_refuses_non_sqlite(self):
        from unittest import mock
        with mock.patch("django.db.connection.vendor", "postgresql"):
            with self.assertRaises(CommandError):
                call_command("seed_hdb")


class ClientCreateDisambiguation(Base):
    def test_duplicate_location_name_needs_institution(self):
        import sys
        from django.conf import settings
        sys.path.insert(0, str(settings.BASE_DIR / "client"))
        from hdb_client import HDBClient
        other = Institution.objects.create(name="CERN")
        ob = Location.objects.create(name="OB", location_type="building", institution=other)
        Location.objects.create(name="L1", institution=other, parent=ob)
        c = HDBClient(user=self.user)
        with self.assertRaises(ValueError):
            c.inventory.create(component_name="C", location_name="L1")
        res = c.inventory.create(component_name="C", location_name="L1",
                                 institution_name="BNL")
        self.assertEqual(ComponentInstance.objects.get(pk=res["id"]).location_id, self.l1.pk)


class YamlProvenance(Base):
    def _client(self):
        import sys
        from django.conf import settings
        sys.path.insert(0, str(settings.BASE_DIR / "client"))
        from hdb_client import HDBClient
        return HDBClient(user=self.user)

    def _write(self, qty):
        import tempfile, os
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as fh:
            fh.write(f"template:\n  name: T\nelements:\n  - element_name: e\n"
                     f"    quantity: {qty}\n    component: C\n")
        self.addCleanup(os.unlink, path)
        return path

    def test_drifted_template_not_restamped(self):
        from .models import DesignTemplate
        c = self._client()
        first = c.designs.load_templates_from_yaml(self._write(4))
        self.assertTrue(first["templates"][0]["provenance_stamped"])
        sha_before = DesignTemplate.objects.get(name="T").source_sha256
        second = c.designs.load_templates_from_yaml(self._write(9))
        res = second["templates"][0]
        self.assertFalse(res["provenance_stamped"])
        self.assertIn("elements", res["drift"])
        self.assertEqual(DesignTemplate.objects.get(name="T").source_sha256, sha_before)
        # a clean reload of the original content still restamps
        third = c.designs.load_templates_from_yaml(self._write(4))
        self.assertTrue(third["templates"][0]["provenance_stamped"])


class GroupPages(Base):
    def test_nav_has_separate_users_and_groups_links(self):
        r = self.client.get(reverse("user-list"))
        self.assertContains(r, 'href="%s"' % reverse("user-list"))
        self.assertContains(r, 'href="%s"' % reverse("group-page-list"))
        self.assertNotContains(r, "Users &amp; Groups")

    def test_group_list_shows_tiles_linking_to_detail(self):
        r = self.client.get(reverse("group-page-list"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, reverse("group-page-detail", args=[self.group.name]))
        self.assertContains(r, ">G<")
        self.assertContains(r, "1 member<")

    def test_group_detail_shows_title(self):
        r = self.client.get(reverse("group-page-detail", args=[self.group.name]))
        self.assertContains(r, "<h1>G</h1>", html=True)
        self.assertEqual(self.client.get(reverse("group-page-detail", args=["nope"])).status_code, 404)

    def test_group_pages_require_login(self):
        self.client.logout()
        for url in (reverse("group-page-list"), reverse("group-page-detail", args=[self.group.name])):
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_group_tile_tallies(self):
        l3 = Location.objects.create(name="L3", institution=self.inst, parent=self.bldg)
        self.instance("a"); self.instance("b", location=self.l2)
        self.design("D1", location=self.l2); self.design("D2", location=l3)
        ComponentInstance.objects.create(component=self.comp, tag="x", location=self.l1)  # no group
        r = self.client.get(reverse("group-page-list"))
        tile = [g for g in r.context["groups"] if g.pk == self.group.pk][0]
        self.assertEqual((tile.item_count, tile.design_count, tile.location_count), (2, 2, 3))
        self.assertContains(r, "2 inventory items<")
        self.assertContains(r, "2 designs<")
        self.assertContains(r, "3 locations<")

    def test_group_detail_url_uses_name_and_lists_members(self):
        self.assertEqual(reverse("group-page-detail", args=["G"]), "/groups/G/")
        inst = Institution.objects.create(name="Argonne", abbreviation="ANL")
        from .models import UserProfile
        self.user.first_name, self.user.last_name = "Ann", "Lee"
        self.user.save()
        UserProfile.objects.create(user=self.user, institution=inst)
        outsider = User.objects.create_user("outsider")
        r = self.client.get(reverse("group-page-detail", args=["G"]))
        self.assertContains(r, "Ann Lee")
        self.assertContains(r, "ANL")
        self.assertContains(r, reverse("user-inventory", args=["u"]))
        self.assertNotContains(r, outsider.username)

    def test_group_name_with_space_and_slash_resolves(self):
        g = Group.objects.create(name="Sim / Reco team")
        url = reverse("group-page-detail", args=[g.name])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertContains(self.client.get(reverse("group-page-list")), url)


class InstitutionGroupsSection(Base):
    def test_groups_section_lists_groups_of_the_institutions_users(self):
        from .models import UserProfile
        other_inst = Institution.objects.create(name="CERN")
        g2 = Group.objects.create(name="Other / Team")
        lone = Group.objects.create(name="Elsewhere")
        u2 = User.objects.create_user("u2"); u2.groups.add(self.group, g2)
        u3 = User.objects.create_user("u3"); u3.groups.add(lone)
        admin = User.objects.create_user("admin"); admin.groups.add(Group.objects.create(name="AdminOnly"))
        UserProfile.objects.create(user=self.user, institution=self.inst)
        UserProfile.objects.create(user=u2, institution=self.inst)
        UserProfile.objects.create(user=u3, institution=other_inst)
        UserProfile.objects.create(user=admin, institution=self.inst)
        r = self.client.get(reverse("institution-list"))
        by_name = {i.name: i for i in r.context["institutions"]}
        self.assertEqual(
            [(g["name"], g["user_count"]) for g in by_name["BNL"].group_summaries],
            [("G", 2), ("Other / Team", 1)])
        self.assertEqual([g["name"] for g in by_name["CERN"].group_summaries], ["Elsewhere"])
        self.assertContains(r, reverse("group-page-detail", args=["Other / Team"]))
        self.assertNotIn("AdminOnly", [g["name"] for i in r.context["institutions"] for g in i.group_summaries])
        self.assertContains(r, "2 users here")

    def test_user_rows_are_table_columns_name_groups_owned_items(self):
        from .models import UserProfile
        g2 = Group.objects.create(name="Second")
        self.user.groups.add(g2)
        UserProfile.objects.create(user=self.user, institution=self.inst)
        loner = User.objects.create_user("loner")
        UserProfile.objects.create(user=loner, institution=self.inst)
        html = self.client.get(reverse("institution-list")).content.decode()
        self.assertIn('<table class="inst-users">', html)
        rows = [r for r in html.split("<tr>")[1:] if "iu-name" in r]
        by_user = {("u" if "u: view owned items" in r else "loner"): r for r in rows}
        r = by_user["u"]
        self.assertLess(r.index("iu-name"), r.index("iu-groups"))
        self.assertLess(r.index("iu-groups"), r.index("iu-items"))
        self.assertIn(reverse("group-page-detail", args=["G"]), r)
        self.assertIn(reverse("group-page-detail", args=["Second"]), r)
        self.assertIn("&mdash;", by_user["loner"].split("iu-groups")[1].split("iu-items")[0])
        self.assertIn("loner: view owned items", by_user["loner"])

    def test_groups_section_is_one_comma_separated_line(self):
        from .models import UserProfile
        g2 = Group.objects.create(name="Second")
        self.user.groups.add(g2)
        UserProfile.objects.create(user=self.user, institution=self.inst)
        html = self.client.get(reverse("institution-list")).content.decode()
        sect = html[html.index(">Groups</div>"):html.index('class="inst-users"')]
        self.assertEqual(sect.count('class="loc-row"'), 1)
        self.assertIn("(1 user here)</span>,</span>", sect)


class InstitutionLeafLocationCount(Base):
    def test_count_includes_only_leaf_locations(self):
        bldg = Location.objects.create(name="B1", location_type="building", institution=self.inst)
        Location.objects.create(name="R1", parent=bldg, institution=self.inst)
        Location.objects.create(name="R2", parent=bldg, institution=self.inst)
        r = self.client.get(reverse("institution-list"))
        inst = [i for i in r.context["institutions"] if i.pk == self.inst.pk][0]
        # L1, L2, R1, R2 are leaves -- buildings B and B1 have children, so are not counted
        self.assertEqual(inst.leaf_location_count, 4)
        self.assertEqual(inst.locations.count(), 6)
        self.assertContains(r, "4 locations, 0 items, 0 designs<")

    def test_count_singular_and_building_without_rooms_counts(self):
        inst2 = Institution.objects.create(name="Solo")
        Location.objects.create(name="Hall", location_type="building", institution=inst2)
        r = self.client.get(reverse("institution-list"))
        solo = [i for i in r.context["institutions"] if i.pk == inst2.pk][0]
        self.assertEqual(solo.leaf_location_count, 1)
        self.assertContains(r, "1 location, 0 items, 0 designs<")


class LocationHierarchy(Base):
    def make(self, **kw):
        kw.setdefault("institution", self.inst)
        return Location.objects.create(**kw)

    def test_room_needs_a_building_parent(self):
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.make(name="Orphan room", location_type="room")
        with self.assertRaises(ValidationError):  # room under a room
            self.make(name="R", location_type="room", parent=self.l1)
        self.make(name="Ok room", location_type="room", parent=self.bldg)

    def test_cabinet_needs_room_and_shelf_needs_cabinet(self):
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.make(name="C", location_type="cabinet")
        with self.assertRaises(ValidationError):  # cabinet straight in a building
            self.make(name="C", location_type="cabinet", parent=self.bldg)
        cab = self.make(name="C", location_type="cabinet", parent=self.l1)
        with self.assertRaises(ValidationError):
            self.make(name="S", location_type="shelf")
        with self.assertRaises(ValidationError):  # shelf in a room
            self.make(name="S", location_type="shelf", parent=self.l1)
        self.make(name="S", location_type="shelf", parent=cab)

    def test_building_is_top_level_and_other_is_free(self):
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.make(name="B2", location_type="building", parent=self.bldg)
        self.make(name="Truck", location_type="other")
        self.make(name="Box", location_type="other", parent=self.l1)

    def test_parent_must_be_in_same_institution(self):
        from django.core.exceptions import ValidationError
        other = Institution.objects.create(name="CERN")
        with self.assertRaises(ValidationError):
            self.make(name="R", location_type="room", parent=self.bldg, institution=other)

    def test_cannot_retype_or_reparent_so_children_become_invalid(self):
        from django.core.exceptions import ValidationError
        self.bldg.location_type = "other"
        with self.assertRaises(ValidationError):
            self.bldg.save()           # rooms L1/L2 still need a building
        self.l1.parent = None
        with self.assertRaises(ValidationError):
            self.l1.save()

    def test_create_view_rejects_violations_and_reports_reason(self):
        su = User.objects.create_superuser("root", password="x")
        self.client.force_login(su)
        url = reverse("location-create", args=[self.inst.pk])
        r = self.client.post(url, {"name": "Lonely", "location_type": "room"}, follow=True)
        self.assertFalse(Location.objects.filter(name="Lonely").exists())
        self.assertContains(r, "must belong to a building")
        self.client.post(url, {"name": "Good", "location_type": "room", "parent": self.bldg.pk})
        self.assertTrue(Location.objects.filter(name="Good", parent=self.bldg).exists())
        self.client.post(url, {"name": "Tower", "location_type": "building"})
        self.assertTrue(Location.objects.filter(name="Tower", parent=None).exists())

    def test_update_parent_view_repairs_legacy_orphan_and_never_clears(self):
        su = User.objects.create_superuser("root", password="x")
        self.client.force_login(su)
        orphan = self.make(name="Legacy", location_type="other")
        Location.objects.filter(pk=orphan.pk).update(location_type="room")   # simulate legacy row
        url = reverse("location-update-parent", args=[orphan.pk])
        self.client.post(url, {"parent": self.l1.pk})        # wrong type -> no-op
        orphan.refresh_from_db(); self.assertIsNone(orphan.parent_id)
        self.client.post(url, {"parent": self.bldg.pk})
        orphan.refresh_from_db(); self.assertEqual(orphan.parent_id, self.bldg.pk)
        self.client.post(url, {"parent": ""})                # blank must not clear
        orphan.refresh_from_db(); self.assertEqual(orphan.parent_id, self.bldg.pk)

    def test_check_locations_command_flags_legacy_rows(self):
        from django.core.management.base import CommandError
        out = io.StringIO()
        call_command("check_locations", stdout=out)
        self.assertIn("satisfy", out.getvalue())
        bad = self.make(name="Legacy", location_type="other")
        Location.objects.filter(pk=bad.pk).update(location_type="shelf")
        with self.assertRaises(CommandError):
            call_command("check_locations", stdout=io.StringIO())

    def test_header_shows_items_and_designs_at_the_institution(self):
        other = Institution.objects.create(name="CERN")
        ob = Location.objects.create(name="OB", location_type="building", institution=other)
        orm = Location.objects.create(name="OR", parent=ob, institution=other)
        self.instance("a"); self.instance("b", location=self.l2)
        ComponentInstance.objects.create(component=self.comp, tag="far", location=orm)
        self.design("D1"); self.design("D2", location=self.l2); self.design("D3", location=orm)
        r = self.client.get(reverse("institution-list"))
        by = {i.name: i for i in r.context["institutions"]}
        self.assertEqual((by["BNL"].item_count, by["BNL"].design_count), (2, 2))
        self.assertEqual((by["CERN"].item_count, by["CERN"].design_count), (1, 1))
        self.assertContains(r, "2 locations, 2 items, 2 designs<")
        self.assertContains(r, "1 location, 1 item, 1 design<")


class DesignListLocationColumn(Base):
    def test_location_column_shown_and_sortable(self):
        inst = Institution.objects.create(name="Argonne", abbreviation="ANL")
        b = Location.objects.create(name="AB", location_type="building", institution=inst)
        far = Location.objects.create(name="Annex", parent=b, institution=inst)
        self.design("Alpha", location=self.l1)
        self.design("Beta", location=far)
        Design.objects.create(name="Gamma", owner_group=self.group)  # no location
        r = self.client.get(reverse("design-list"))
        self.assertContains(r, "<th class=\"th-sort\">")
        self.assertContains(r, "Location")
        self.assertContains(r, "ANL / Annex")
        self.assertContains(r, "BNL / L1")
        r = self.client.get(reverse("design-list"), {"sort": "location", "dir": "asc"})
        names = [d.name for d in r.context["page_obj"]]
        self.assertEqual(names.index("Beta") < names.index("Alpha"), True)  # Annex < L1

    def test_location_rows_show_slash_path_from_building(self):
        room = self.l1
        cab = Location.objects.create(name="Cab", location_type="cabinet", parent=room, institution=self.inst)
        Location.objects.create(name="Shelf3", location_type="shelf", parent=cab, institution=self.inst)
        r = self.client.get(reverse("institution-list"))
        for path in ("B / L1 / Cab / Shelf3", "B / L1 / Cab", "B / L1", "B / L2"):
            self.assertContains(r, ">%s</a>" % path, msg_prefix=path)
        html = r.content.decode()
        self.assertLess(html.index(">B / L1</a>"), html.index(">B / L1 / Cab</a>"))
        self.assertLess(html.index(">B / L1 / Cab / Shelf3</a>"), html.index(">B / L2</a>"))
        self.assertNotContains(r, "&rarr; B<")


class UserPage(Base):
    def test_users_list_note(self):
        r = self.client.get(reverse("user-list"))
        self.assertContains(r, "Click on a table row to see information about the user.")
        self.assertNotContains(r, "inventory owned by the user")

    def test_user_page_layout(self):
        from .models import UserProfile
        inst = Institution.objects.create(name="Argonne National Lab", abbreviation="ANL")
        self.user.first_name, self.user.last_name = "Ann", "Lee"; self.user.save()
        self.user.groups.add(Group.objects.create(name="Second"))
        UserProfile.objects.create(user=self.user, institution=inst)
        self.instance("mine")
        html = self.client.get(reverse("user-inventory", args=["u"])).content.decode()
        self.assertIn("<h1>Ann Lee</h1>", html)
        self.assertNotIn("Inventory: Ann", html)
        i_inst, i_groups = html.index("Argonne National Lab"), html.index("Groups:")
        i_title, i_table = html.index(">Inventory owned</h2>"), html.index("<table")
        self.assertTrue(html.index("<h1>") < i_inst < i_groups < i_title < i_table)
        self.assertIn(reverse("group-page-detail", args=["G"]), html)
        self.assertIn(reverse("group-page-detail", args=["Second"]), html)
        self.assertIn("mine", html)

    def test_user_without_profile_or_groups(self):
        bare = User.objects.create_user("bare")
        html = self.client.get(reverse("user-inventory", args=["bare"])).content.decode()
        self.assertIn("<h1>bare</h1>", html)
        self.assertIn("No institution", html)

    def test_users_table_starts_with_username_column(self):
        html = self.client.get(reverse("user-list")).content.decode()
        head = html[html.index("<thead>"):html.index("</thead>")]
        self.assertLess(head.index("Username"), head.index("First Name"))
        self.assertIn('<td><code class="mono-code">u</code></td>', html)

    def test_user_page_shows_email_in_parentheses_after_name_when_present(self):
        self.user.first_name, self.user.last_name, self.user.email = "Ann", "Lee", "ann@example.org"
        self.user.save()
        html = self.client.get(reverse("user-inventory", args=["u"])).content.decode()
        h1 = html[html.index("<h1>"):html.index("</h1>")]
        self.assertTrue(h1.startswith("<h1>Ann Lee"))
        self.assertIn("(ann@example.org)", h1)
        self.user.email = ""; self.user.save()
        html = self.client.get(reverse("user-inventory", args=["u"])).content.decode()
        self.assertIn("<h1>Ann Lee</h1>", html)
        self.assertNotIn("()", html)


class BugReviewFixes(Base):
    def test_batch_banner_blames_installation_not_owner(self):
        free, installed = self.instance("a"), self.instance("b")
        self.install(self.design(), installed)
        r = self.client.post(reverse("inventory-batch-location"), {
            "instances": [free.pk, installed.pk], "target_location": self.l2.pk}, follow=True)
        self.assertContains(r, "installed in a design")
        self.assertNotContains(r, "chosen owner isn")

    def test_location_create_rejects_unknown_type(self):
        su = User.objects.create_superuser("root", password="x")
        self.client.force_login(su)
        self.client.post(reverse("location-create", args=[self.inst.pk]),
                         {"name": "Weird", "location_type": "foo"})
        self.assertFalse(Location.objects.filter(name="Weird").exists())

    def test_client_installed_in_design(self):
        import sys
        from django.conf import settings
        sys.path.insert(0, str(settings.BASE_DIR / "client"))
        from hdb_client import HDBClient
        i, j = self.instance("in"), self.instance("out")
        self.install(self.design("Rack"), i)
        found = HDBClient(user=self.user).inventory.installed_in_design("Rack")
        self.assertEqual([x.pk for x in found], [i.pk])

    def test_design_instantiation_is_all_or_nothing(self):
        tpl = DesignTemplate.objects.create(name="T", owner_group=self.group)
        DesignTemplateElement.objects.create(template=tpl, element_name="e", component=self.comp)
        fake = mock.MagicMock()
        fake.objects.create.side_effect = IntegrityError("boom")
        with mock.patch("hdb.views_web.DesignElement", fake):
            r = self.client.post(reverse("design-list"), {"template": tpl.pk, "name": "NewD"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Design.objects.filter(name="NewD").exists())
        self.assertContains(r, "already exists")

    def test_client_system_views_follow_the_component(self):
        import sys
        from django.conf import settings
        sys.path.insert(0, str(settings.BASE_DIR / "client"))
        from hdb_client import HDBClient
        i = self.instance("x")                      # inherits TS1
        Component.objects.filter(pk=self.comp.pk).update(technical_system=self.ts2)  # drift
        c = HDBClient(user=self.user)
        self.assertEqual([x.pk for x in c.systems.instances("TS2")], [i.pk])
        self.assertEqual(list(c.systems.instances("TS1")), [])
        counts = {r["name"]: r["instances"] for r in c.systems.instance_counts()}
        self.assertEqual((counts["TS1"], counts["TS2"]), (0, 1))
        self.assertEqual(c.systems.summary("TS2")["instance_count"], 1)
        self.assertEqual(c.inventory.get(i.pk) and c.where_is(i.pk)["technical_system"], "TS2")


class ItemLocationRule(Base):
    """Inventory items live in rooms (or cabinets/shelves in them), never in a building."""

    def test_model_rejects_building_and_orphan_other(self):
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.instance("x", location=self.bldg)
        truck = Location.objects.create(name="Truck", location_type="other", institution=self.inst)
        with self.assertRaises(ValidationError):
            self.instance("y", location=truck)

    def test_room_cabinet_shelf_and_other_under_room_are_allowed(self):
        cab = Location.objects.create(name="Cab", location_type="cabinet", parent=self.l1, institution=self.inst)
        shelf = Location.objects.create(name="Sh", location_type="shelf", parent=cab, institution=self.inst)
        box = Location.objects.create(name="Box", location_type="other", parent=self.l1, institution=self.inst)
        for loc in (self.l1, cab, shelf, box):
            self.instance("t-" + loc.name, location=loc)
        pks = {l.pk for l in Location.item_locations()}
        self.assertTrue({self.l1.pk, cab.pk, shelf.pk, box.pk} <= pks)
        self.assertNotIn(self.bldg.pk, pks)

    def test_legacy_item_in_building_can_still_be_edited_but_not_moved_there(self):
        i = self.instance("legacy")
        ComponentInstance.objects.filter(pk=i.pk).update(location=self.bldg)   # legacy row
        i.refresh_from_db()
        i.description = "edited"; i.save()                                      # unrelated edit is fine
        i.location = self.bldg
        i.description = "again"; i.save()                                       # unchanged location: fine
        i.location = self.l2; i.save()                                          # moving into a room: fine

    def test_single_move_to_building_refused(self):
        i = self.instance()
        r = self.client.post(reverse("inventory-update-location", args=[i.pk]),
                             {"location": self.bldg.pk}, follow=True)
        i.refresh_from_db()
        self.assertEqual(i.location_id, self.l1.pk)
        self.assertContains(r, "not directly in a building")

    def test_batch_move_to_building_refused(self):
        i = self.instance()
        r = self.client.post(reverse("inventory-batch-location"),
                             {"instances": [i.pk], "target_location": self.bldg.pk}, follow=True)
        i.refresh_from_db()
        self.assertEqual(i.location_id, self.l1.pk)
        self.assertContains(r, "not directly in a building")

    def test_create_forms_refuse_building_and_pickers_only_offer_valid_locations(self):
        r = self.client.post(reverse("inventory-list"), {
            "component": self.comp.pk, "location": self.bldg.pk, "tag": "n"})
        self.assertEqual(ComponentInstance.objects.filter(tag="n").count(), 0)
        self.assertContains(r, "not directly in a building")
        r = self.client.post(reverse("component-instance-create", args=[self.comp.pk]),
                             {"location": self.bldg.pk, "tag": "n2"}, follow=True)
        self.assertEqual(ComponentInstance.objects.filter(tag="n2").count(), 0)
        self.assertContains(r, "not directly in a building")
        page = self.client.get(reverse("inventory-list")).context
        self.assertNotIn(self.bldg.pk, [l.pk for l in page["item_locations"]])
        self.assertIn(self.bldg.pk, [l.pk for l in page["locations"]])   # filter still lists everything

    def test_design_move_with_items_to_building_refused_but_empty_design_may_go(self):
        d = self.design("D"); self.install(d, self.instance())
        self.client.post(reverse("design-update-location", args=[d.pk]), {"location": self.bldg.pk})
        d.refresh_from_db(); self.assertEqual(d.location_id, self.l1.pk)
        empty = self.design("E")
        self.client.post(reverse("design-update-location", args=[empty.pk]), {"location": self.bldg.pk})
        empty.refresh_from_db(); self.assertEqual(empty.location_id, self.bldg.pk)

    def test_client_create_reports_building_error(self):
        import sys
        from django.conf import settings
        sys.path.insert(0, str(settings.BASE_DIR / "client"))
        from hdb_client import HDBClient
        with self.assertRaisesRegex(ValueError, "room"):
            HDBClient(user=self.user).inventory.create(component_name="C", location_name="B")

    def test_check_locations_reports_stranded_items(self):
        from django.core.management.base import CommandError
        i = self.instance("legacy")
        ComponentInstance.objects.filter(pk=i.pk).update(location=self.bldg)
        out = io.StringIO()
        with self.assertRaises(CommandError):
            call_command("check_locations", stdout=out)
        self.assertIn("not in a room", out.getvalue())


class ComponentBatchOwner(Base):
    """Components page: row selection + bulk owner change."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other_group = Group.objects.create(name="H")
        cls.u2 = User.objects.create_user("u2", password="x")
        cls.u2.groups.add(cls.group)
        cls.outsider = User.objects.create_user("out", password="x")
        cls.outsider.groups.add(cls.other_group)
        cls.c2 = Component.objects.create(name="C2", owner_group=cls.group, owner_user=cls.user)
        cls.c3 = Component.objects.create(name="C3", owner_group=cls.other_group)

    def post(self, **data):
        return self.client.post(reverse("component-batch-owner"), data)

    def test_page_renders_checkboxes_only_for_editable_rows(self):
        r = self.client.get(reverse("component-list"))
        self.assertContains(r, 'name="components" value="%s"' % self.c2.pk)
        self.assertNotContains(r, 'name="components" value="%s"' % self.c3.pk)
        self.assertContains(r, reverse("component-batch-owner"))

    def test_reassign_selected(self):
        r = self.post(components=[self.comp.pk, self.c2.pk], target_owner=self.u2.pk)
        self.assertEqual(r.status_code, 302)
        self.assertIn("batch_updated=2", r["Location"])
        self.comp.refresh_from_db(); self.c2.refresh_from_db()
        self.assertEqual(self.comp.owner_user, self.u2)
        self.assertEqual(self.c2.owner_user, self.u2)
        self.assertEqual(self.c2.log_entries.count(), 1)

    def test_invalid_owner_skipped_and_counted(self):
        r = self.post(components=[self.c2.pk], target_owner=self.outsider.pk)
        self.assertIn("batch_skipped=1", r["Location"])
        self.c2.refresh_from_db()
        self.assertEqual(self.c2.owner_user, self.user)

    def test_unauthorized_pk_ignored(self):
        self.post(components=[self.c3.pk], target_owner=self.outsider.pk)
        self.c3.refresh_from_db()
        self.assertIsNone(self.c3.owner_user)

    def test_all_filtered_mode_respects_filters(self):
        r = self.post(all_filtered="1", q="C2", target_owner=self.u2.pk)
        self.assertIn("batch_updated=1", r["Location"])
        self.assertIn("q=C2", r["Location"])
        self.comp.refresh_from_db()
        self.assertNotEqual(self.comp.owner_user, self.u2)

    def test_superuser_can_target_any_group_and_get_is_noop(self):
        admin = User.objects.create_superuser("adm", password="x")
        self.client.force_login(admin)
        r = self.post(components=[self.c3.pk], target_owner=admin.pk)
        self.assertIn("batch_updated=1", r["Location"])
        self.assertEqual(self.client.get(reverse("component-batch-owner")).status_code, 302)

    def test_banner(self):
        r = self.client.get(reverse("component-list") + "?batch_updated=2&batch_skipped=1")
        self.assertContains(r, "Updated 2 components.")
        self.assertContains(r, "Skipped 1")


class ComponentCreateGroupSystem(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = User.objects.create_superuser("adm", password="x")
        cls.h = Group.objects.create(name="H")
        cls.ts1.group = cls.group; cls.ts1.save()
        cls.ts2.group = cls.h; cls.ts2.save()

    def setUp(self):
        self.client.force_login(self.admin)

    def create(self, **kw):
        return self.client.post(reverse("component-list"), kw)

    def test_group_and_matching_system_saved(self):
        r = self.create(name="N1", owner_group=self.group.pk, technical_system=self.ts1.pk)
        self.assertEqual(r.status_code, 302)
        c = Component.objects.get(name="N1")
        self.assertEqual(c.owner_group, self.group)
        self.assertEqual(c.technical_system, self.ts1)

    def test_system_of_other_group_rejected(self):
        r = self.create(name="N2", owner_group=self.group.pk, technical_system=self.ts2.pk)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Component.objects.filter(name="N2").exists())
        self.assertContains(r, "does not belong to the selected group")

    def test_system_without_group_rejected_but_group_alone_ok(self):
        self.assertEqual(self.create(name="N3", technical_system=self.ts1.pk).status_code, 200)
        self.assertEqual(self.create(name="N4", owner_group=self.h.pk).status_code, 302)

    def test_dialog_has_group_selector_and_system_data(self):
        r = self.client.get(reverse("component-list"))
        self.assertContains(r, 'name="owner_group"')
        self.assertContains(r, 'data-group="%s"' % self.group.pk)


class ComponentBatchOwnerChoices(Base):
    def test_page_exposes_user_groups_and_row_groups(self):
        u2 = User.objects.create_user("zz", password="x")
        r = self.client.get(reverse("component-list"))
        self.assertContains(r, 'data-group="%s"' % self.group.pk)
        self.assertContains(r, 'class="owner-data"')
        self.assertContains(r, 'data-groups="%s"' % self.group.pk)
        self.assertEqual(list(r.context["filtered_group_ids"]), [self.group.pk])


class ComponentAlternateName(Base):
    def url(self):
        return reverse("component-details-update", args=[self.comp.pk])

    def test_member_can_set_and_clear(self):
        self.client.post(self.url(), {"alternate_name": "  Alt  "})
        self.comp.refresh_from_db()
        self.assertEqual(self.comp.alternate_name, "Alt")
        self.assertEqual(self.comp.log_entries.count(), 1)
        self.client.post(self.url(), {"alternate_name": ""})
        self.comp.refresh_from_db()
        self.assertEqual(self.comp.alternate_name, "")

    def test_non_member_forbidden(self):
        other = User.objects.create_user("o", password="x")
        self.client.force_login(other)
        self.assertEqual(self.client.post(self.url(), {"alternate_name": "x"}).status_code, 403)
        self.comp.refresh_from_db()
        self.assertEqual(self.comp.alternate_name, "")

    def test_detail_shows_form_for_member(self):
        r = self.client.get(reverse("component-detail", args=[self.comp.pk]))
        self.assertContains(r, 'name="alternate_name"')
        self.assertEqual(r.content.decode().count("Save changes"), 1)


class ComponentDescription(Base):
    def url(self):
        return reverse("component-details-update", args=[self.comp.pk])

    def test_member_can_set_and_clear(self):
        self.client.post(self.url(), {"description": " Line1\nLine2 "})
        self.comp.refresh_from_db()
        self.assertEqual(self.comp.description, "Line1\nLine2")
        self.assertEqual(self.comp.log_entries.count(), 1)
        self.client.post(self.url(), {"description": ""})
        self.comp.refresh_from_db()
        self.assertEqual(self.comp.description, "")

    def test_non_member_forbidden_and_form_shown_to_member(self):
        r = self.client.get(reverse("component-detail", args=[self.comp.pk]))
        self.assertContains(r, '<textarea name="description"')
        self.client.force_login(User.objects.create_user("o", password="x"))
        self.assertEqual(self.client.post(self.url(), {"description": "x"}).status_code, 403)


class ComponentDetailsCombined(Base):
    def test_all_three_fields_in_one_post(self):
        u2 = User.objects.create_user("u2", password="x"); u2.groups.add(self.group)
        self.client.post(reverse("component-details-update", args=[self.comp.pk]),
                         {"alternate_name": "A", "description": "D", "owner_user": u2.pk})
        self.comp.refresh_from_db()
        self.assertEqual((self.comp.alternate_name, self.comp.description, self.comp.owner_user), ("A", "D", u2))
        self.assertEqual(self.comp.log_entries.count(), 3)

    def test_owner_outside_group_ignored(self):
        out = User.objects.create_user("out", password="x")
        self.client.post(reverse("component-details-update", args=[self.comp.pk]),
                         {"alternate_name": "A", "owner_user": out.pk})
        self.comp.refresh_from_db()
        self.assertEqual(self.comp.alternate_name, "A")
        self.assertIsNone(self.comp.owner_user)


class ComponentCreateByUser(Base):
    """Regular users may create components, but only for their own groups."""

    def setUp(self):
        super().setUp()
        self.other = Group.objects.create(name="H")
        self.ts1.group = self.group; self.ts1.save()

    def create(self, **kw):
        return self.client.post(reverse("component-list"), kw)

    def test_button_and_group_choices_for_member(self):
        r = self.client.get(reverse("component-list"))
        self.assertContains(r, "+ New Component")
        self.assertEqual(list(r.context["creatable_groups"]), [self.group])

    def test_member_creates_in_own_group(self):
        r = self.create(name="M1", owner_group=self.group.pk, technical_system=self.ts1.pk)
        self.assertEqual(r.status_code, 302)
        c = Component.objects.get(name="M1")
        self.assertEqual(c.owner_group, self.group)
        self.assertEqual(c.owner_user, self.user)

    def test_other_group_forbidden_and_group_required(self):
        self.assertEqual(self.create(name="M2", owner_group=self.other.pk).status_code, 403)
        self.assertEqual(self.create(name="M3").status_code, 200)
        self.assertFalse(Component.objects.filter(name__in=["M2", "M3"]).exists())

    def test_user_without_groups_has_no_button_and_cannot_post(self):
        loner = User.objects.create_user("loner", password="x")
        self.client.force_login(loner)
        self.assertNotContains(self.client.get(reverse("component-list")), "+ New Component")
        self.assertEqual(self.create(name="M4").status_code, 403)


class ComponentFilterOrder(Base):
    def test_group_selector_precedes_system_and_systems_carry_group(self):
        self.ts1.group = self.group; self.ts1.save()
        h = self.client.get(reverse("component-list")).content.decode()
        self.assertLess(h.index('id="f-group"'), h.index('id="f-system"'))
        self.assertIn('value="TS1" data-group="G"', h)


class CreationLogging(Base):
    def test_component_creation_logged(self):
        admin = User.objects.create_superuser("adm2", password="x")
        self.client.force_login(admin)
        self.client.post(reverse("component-list"), {"name": "Newc", "owner_group": self.group.pk})
        c = Component.objects.get(name="Newc")
        e = c.log_entries.get()
        self.assertIn("created by", e.entry)
        self.assertEqual(e.topic, "components")
        self.assertEqual(e.logged_by, admin)

    def test_inventory_creation_logged_both_paths(self):
        self.client.post(reverse("component-instance-create", args=[self.comp.pk]),
                         {"tag": "T1", "location": self.l1.pk})
        self.client.post(reverse("inventory-list"),
                         {"component": self.comp.pk, "tag": "T2", "location": self.l1.pk})
        for tag in ("T1", "T2"):
            i = ComponentInstance.objects.get(tag=tag)
            self.assertEqual(i.log_entries.count(), 1, tag)


class InventoryFilterOrder(Base):
    def test_group_before_system_with_group_data(self):
        self.ts1.group = self.group; self.ts1.save()
        h = self.client.get(reverse("inventory-list")).content.decode()
        self.assertLess(h.index('id="f-group"'), h.index('id="f-system"'))
        self.assertIn('value="TS1" data-group="G"', h)


class ComponentOwnerFilter(Base):
    def test_filter_order_and_result_and_mirror(self):
        Component.objects.create(name="Mine", owner_group=self.group, owner_user=self.user)
        r = self.client.get(reverse("component-list") + "?owner=u")
        h = r.content.decode()
        self.assertLess(h.index('id="f-group"'), h.index('id="f-system"'))
        self.assertLess(h.index('id="f-system"'), h.index('name="owner" class="filter-select"'))
        self.assertEqual([c.name for c in r.context["page_obj"]], ["Mine"])
        self.assertIn('<input type="hidden" name="owner" value="u">', h)

    def test_batch_all_filtered_respects_owner(self):
        u2 = User.objects.create_user("u2", password="x"); u2.groups.add(self.group)
        Component.objects.create(name="Mine", owner_group=self.group, owner_user=self.user)
        self.client.post(reverse("component-batch-owner"),
                         {"all_filtered": "1", "owner": "u", "target_owner": u2.pk})
        self.assertEqual(Component.objects.get(name="Mine").owner_user, u2)
        self.comp.refresh_from_db()
        self.assertIsNone(self.comp.owner_user)


class ComponentLogTopic(Base):
    def test_edits_and_transfers_use_components_topic(self):
        u2 = User.objects.create_user("u2", password="x"); u2.groups.add(self.group)
        self.client.post(reverse("component-details-update", args=[self.comp.pk]),
                         {"alternate_name": "A", "owner_user": u2.pk})
        self.client.post(reverse("component-batch-owner"),
                         {"components": [self.comp.pk], "target_owner": self.user.pk})
        topics = set(self.comp.log_entries.values_list("topic", flat=True))
        self.assertEqual(topics, {"components"})
        self.assertEqual(self.comp.log_entries.count(), 3)


class AdminGroupEditing(Base):
    def setUp(self):
        self.admin = User.objects.create_superuser("adm3", password="x")
        self.client.force_login(self.admin)
        self.h = Group.objects.create(name="H")

    def test_group_editable_on_change_forms(self):
        i = self.instance()
        for url in (reverse("admin:hdb_component_change", args=[self.comp.pk]),
                    reverse("admin:hdb_componentinstance_change", args=[i.pk])):
            r = self.client.get(url)
            self.assertContains(r, 'name="owner_group"')
            # in the main (non-collapsed) part of the form
            html = r.content.decode()
            self.assertLess(html.index('name="owner_group"'), html.index("Ownership"))

    def test_group_editable_in_changelists(self):
        i = self.instance()
        for name in ("component", "componentinstance"):
            r = self.client.get(reverse(f"admin:hdb_{name}_changelist"))
            self.assertContains(r, 'name="form-0-owner_group"')


class AdminGroupChangeLogged(Base):
    def setUp(self):
        self.admin = User.objects.create_superuser("adm4", password="x")
        self.client.force_login(self.admin)
        self.h = Group.objects.create(name="H")

    def _form(self, url, obj_fields):
        return self.client.post(url, obj_fields)

    def test_changelist_edit_logged_for_component_and_instance(self):
        i = self.instance()
        r = self.client.post(reverse("admin:hdb_component_changelist"), {
            "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "1", "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000", "form-0-id": self.comp.pk,
            "form-0-owner_group": self.h.pk, "_save": "Save"})
        self.comp.refresh_from_db()
        self.assertEqual(self.comp.owner_group, self.h)
        e = self.comp.log_entries.get()
        self.assertEqual(e.topic, "components")
        self.assertIn("from G to H", e.entry)
        self.client.post(reverse("admin:hdb_componentinstance_changelist"), {
            "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "1", "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000", "form-0-id": i.pk,
            "form-0-owner_group": self.h.pk, "_save": "Save"})
        i.refresh_from_db()
        self.assertEqual(i.owner_group, self.h)
        e = i.log_entries.get()
        self.assertEqual(e.topic, "inventory")
        self.assertIn("(admin)", e.entry)

    def test_unchanged_group_not_logged(self):
        self.client.post(reverse("admin:hdb_component_changelist"), {
            "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "1", "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000", "form-0-id": self.comp.pk,
            "form-0-owner_group": self.group.pk, "_save": "Save"})
        self.assertEqual(self.comp.log_entries.count(), 0)


class UserCreationLogged(Base):
    def test_admin_user_creation_is_logged(self):
        admin = User.objects.create_superuser("adm5", password="x")
        self.client.force_login(admin)
        self.client.post(reverse("admin:auth_user_add"), {
            "username": "newbie", "usable_password": "true", "profile-TOTAL_FORMS": "0", "profile-INITIAL_FORMS": "0", "profile-MIN_NUM_FORMS": "0", "profile-MAX_NUM_FORMS": "1", "password1": "S3cure-pass-xyz", "password2": "S3cure-pass-xyz"})
        self.assertTrue(User.objects.filter(username="newbie").exists())
        from .models import LogEntry
        e = LogEntry.objects.get(entry__startswith="User newbie created")
        self.assertEqual(e.logged_by, admin)
        self.assertEqual(e.topic, "user")
        # editing the user afterwards doesn't log another creation
        u = User.objects.get(username="newbie")
        self.assertEqual(LogEntry.objects.filter(entry__startswith="User newbie created").count(), 1)


class UserDeletionLogged(Base):
    def test_single_and_bulk_deletion_logged(self):
        from .models import LogEntry
        admin = User.objects.create_superuser("adm6", password="x")
        self.client.force_login(admin)
        a = User.objects.create_user("gone1", password="x")
        b = User.objects.create_user("gone2", password="x")
        c = User.objects.create_user("gone3", password="x")
        self.client.post(reverse("admin:auth_user_delete", args=[a.pk]), {"post": "yes"})
        self.client.post(reverse("admin:auth_user_changelist"), {
            "action": "delete_selected", "_selected_action": [b.pk, c.pk], "post": "yes"})
        self.assertFalse(User.objects.filter(username__startswith="gone").exists())
        names = sorted(e.entry.split()[1] for e in LogEntry.objects.filter(topic="user", entry__contains="deleted by"))
        self.assertEqual(names, ["gone1", "gone2", "gone3"])
        self.assertEqual(LogEntry.objects.filter(topic="user").first().logged_by, admin)


class UserDeletionProtected(Base):
    def setUp(self):
        self.admin = User.objects.create_superuser("adm7", password="x")
        self.client.force_login(self.admin)

    def test_owner_cannot_be_deleted_in_admin(self):
        victim = User.objects.create_user("owner1", password="x")
        Component.objects.create(name="OwnedC", owner_group=self.group, owner_user=victim)
        r = self.client.get(reverse("admin:auth_user_delete", args=[victim.pk]))
        self.assertContains(r, "owns 1 components: OwnedC")
        self.assertNotContains(r, 'name="post"')
        self.client.post(reverse("admin:auth_user_delete", args=[victim.pk]), {"post": "yes"})
        self.assertTrue(User.objects.filter(pk=victim.pk).exists())
        from .models import LogEntry
        self.assertFalse(LogEntry.objects.filter(entry__contains="User owner1 deleted").exists())

    def test_bulk_action_blocked_too(self):
        victim = User.objects.create_user("owner2", password="x")
        self.design("OwnedD").__class__.objects.filter(name="OwnedD").update(owner_user=victim)
        self.client.post(reverse("admin:auth_user_changelist"), {
            "action": "delete_selected", "_selected_action": [victim.pk], "post": "yes"})
        self.assertTrue(User.objects.filter(pk=victim.pk).exists())

    def test_model_level_guard_and_free_user_deletable(self):
        from django.db.models import ProtectedError
        victim = User.objects.create_user("owner3", password="x")
        i = self.instance("IT")
        ComponentInstance.objects.filter(pk=i.pk).update(owner_user=victim)
        from django.db import transaction
        with self.assertRaises(ProtectedError), transaction.atomic():
            victim.delete()
        ComponentInstance.objects.filter(pk=i.pk).update(owner_user=self.user)
        victim.delete()
        self.assertFalse(User.objects.filter(pk=victim.pk).exists())
