"""Regression tests for the "Fix first" bug-review items."""
import io
from contextlib import redirect_stdout

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse

from .models import (
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
        self.assertIn("batch_skipped=1", r["Location"])

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
