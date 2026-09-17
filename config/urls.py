"""Root URL configuration.

Routing only. Nothing here knows any business rules — each app owns its own
urls.py and this file just mounts it.

    /             redirects to the documentation
    /admin/       Django admin, used by Central Office for master data
    /api/         a directory of what the API offers
    /api/auth/    authentication (accounts app)
    /api/catalog/ master data (catalog app)
    /api/procurement/ group and production orders, receipts
    /api/inventory/   the stock ledger and derived stock levels
    /api/orders/      school orders — the point of sale
    /api/organization/ the Settings screen — one row, org-wide
    /api/schema/  the OpenAPI document
    /api/docs/    interactive API documentation
"""

from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from .health import HealthView
from .views import ApiRootView

# AsOne asked that the system carry their branding. This covers the admin,
# which is the one screen not built in React.
# OPEN QUESTION Q8 — whether an otherwise-standard Django admin is acceptable
# for Central Office, or whether master data needs bespoke branded screens.
# The answer moves the timeline, so it is worth settling early.
admin.site.site_header = "AsOne Logistics"
admin.site.site_title = "AsOne Logistics"
admin.site.index_title = "System administration"

urlpatterns = [
    # Opening the bare server in a browser should land somewhere useful
    # rather than on a 404.
    path("", RedirectView.as_view(pattern_name="swagger-ui", permanent=False)),
    path("admin/", admin.site.urls),
    path("api/", ApiRootView.as_view(), name="api-root"),
    path("api/auth/", include("accounts.urls")),
    path("api/catalog/", include("catalog.urls")),
    path("api/procurement/", include("procurement.urls")),
    path("api/inventory/", include("inventory.urls")),
    path("api/orders/", include("orders.urls")),
    path("api/dashboard/", include("dashboard.urls")),
    path("api/organization/", include("organization.urls")),
    path("api/health/", HealthView.as_view(), name="health"),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
]
