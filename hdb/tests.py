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
        cls.l1 = Location.objects.create(name="L1", institution=cls.inst)
        cls.l2 = Location.objects.create(name="L2", institution=cls.inst)
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
        Location.objects.create(name="L1", institution=other)
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
        l3 = Location.objects.create(name="L3", institution=self.inst)
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
