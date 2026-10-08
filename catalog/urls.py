"""Master data routes, mounted at /api/catalog/ by config/urls.py."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "catalog"

router = DefaultRouter()
# Sites
router.register("tailoring-centers", views.TailoringCenterViewSet, "tailoring-center")
router.register("warehouses", views.WarehouseViewSet, "warehouse")
router.register("schools", views.SchoolViewSet, "school")
# Products
router.register("garments", views.GarmentViewSet, "garment")
router.register("sizes", views.SizeViewSet, "size")
router.register("skus", views.SkuViewSet, "sku")
router.register("minimum-stock-levels", views.MinimumStockLevelViewSet, "minimum-stock-level")
# Pricing
# Basename "garment-price", not "price": a router basename of "price" would
# generate a route named "price-list" for the *table*, colliding with the
# PS/HS price-list endpoint below, which is a different thing entirely.
router.register("prices", views.GarmentPriceViewSet, "garment-price")
# Kits
router.register("kits", views.KitViewSet, "kit")
router.register("kit-items", views.KitItemViewSet, "kit-item")

urlpatterns = [
    # Not an action on the garment viewset: an import creates sizes, SKUs
    # and prices as well as garments, so hanging it off one resource would
    # understate what it does.
    path("import/", views.CatalogueImportView.as_view(), name="catalogue-import"),
    # Before `include(router.urls)`, so "import" is not read as a kit's id.
    # Its own path rather than an action on the viewset, same reasoning as
    # the catalogue import above: it writes a kit's components too, and can
    # remove them.
    path("kits/import/", views.KitImportView.as_view(), name="kit-import"),
    path("price-lists/", views.PriceListView.as_view(), name="price-list"),
    path("price-lists/gaps/", views.PriceGapView.as_view(), name="price-gaps"),
    # The kit half of F15/F51. Mounted under price-lists/ rather than kits/
    # because it is a pricing document, not a catalogue read — the audience
    # and the permissions follow the price list, not the kit.
    path("price-lists/kits/", views.KitPriceListView.as_view(), name="kit-price-list"),
    path(
        "price-lists/kits/gaps/",
        views.KitPriceGapView.as_view(),
        name="kit-price-gaps",
    ),
    path("", include(router.urls)),
]
