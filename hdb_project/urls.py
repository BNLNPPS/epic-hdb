from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("admin/", admin.site.urls),
    path("",       include("hdb.urls_web")),   # web UI (server-rendered)
]

# REST API routes — only registered when djangorestframework is installed.
try:
    import rest_framework  # noqa
    from hdb import views
    urlpatterns += [
        path("api/",                                     views.api_root,                        name="api-root"),
        path("api/groups/",                              views.GroupListView.as_view(),          name="group-list"),
        path("api/groups/<int:pk>/",                     views.GroupDetailView.as_view(),        name="group-detail"),
        path("api/institutions/",                        views.InstitutionListView.as_view(),    name="institution-list-api"),
        path("api/locations/",                           views.LocationListView.as_view(),       name="location-list"),
        path("api/locations/<str:pk>/",                  views.LocationDetailView.as_view(),     name="location-detail"),
        path("api/locations/<str:pk>/children/",         views.LocationChildrenView.as_view(),   name="location-children"),
        path("api/locations/<str:pk>/instances/",        views.LocationInstancesView.as_view(),  name="location-instances"),
        path("api/components/",                          views.ComponentListView.as_view(),      name="component-list-api"),
        path("api/components/<str:pk>/",                 views.ComponentDetailView.as_view(),    name="component-detail-api"),
        path("api/components/<str:pk>/instances/",       views.ComponentInstancesView.as_view(), name="component-instances"),
        path("api/components/<str:pk>/designs/",         views.ComponentDesignsView.as_view(),   name="component-designs"),
        path("api/inventory/",                           views.ComponentInstanceListView.as_view(),   name="instance-list"),
        path("api/inventory/<str:pk>/",                  views.ComponentInstanceDetailView.as_view(), name="instance-detail"),
        path("api/designs/",                             views.DesignListView.as_view(),   name="design-list-api"),
        path("api/designs/<str:pk>/",                    views.DesignDetailView.as_view(), name="design-detail-api"),
        path("api/designs/<str:pk>/bom/",                views.DesignBOMView.as_view(),    name="design-bom"),
        path("api/property-types/",                      views.PropertyTypeListView.as_view(), name="propertytype-list"),
        path("api/logs/",                                views.LogListView.as_view(),          name="log-list-api"),
    ]
except ImportError:
    pass

# Serve user-uploaded files (log attachments, property documents/images) in
# development. In production this should be handled by the web server / a
# proper storage backend instead.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
