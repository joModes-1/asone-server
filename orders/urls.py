"""School order routes, mounted at /api/orders/ by config/urls.py."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "orders"

router = DefaultRouter()
router.register("school-orders", views.SchoolOrderViewSet, "school-order")
router.register("backorders", views.BackorderViewSet, "backorder")
# Read only: a shipment is created by despatching an order, never by
# POSTing a row — see ShipmentViewSet.
router.register("shipments", views.ShipmentViewSet, "shipment")

urlpatterns = [
    # F38 — the picking backlog, the shipping screen's landing view.
    path("picking/queue/", views.PickingQueueView.as_view(), name="picking-queue"),
    # F43, F44 — orders held because their warehouse is short.
    path(
        # Not "school-orders/awaiting-stock/": the router's detail route
        # is school-orders/<pk>/ and would swallow it.
        "awaiting-stock/",
        views.OrdersAwaitingStockView.as_view(),
        name="orders-awaiting-stock",
    ),
    # F42 — the consolidated weekly despatch.
    path("despatch/queue/", views.DespatchQueueView.as_view(), name="despatch-queue"),
    path("despatch/", views.DespatchView.as_view(), name="despatch"),
    path("reports/on-hold/", views.OrdersOnHoldView.as_view(), name="orders-on-hold"),
    path(
        "reports/backorders/",
        views.OutstandingBackordersView.as_view(),
        name="backorders-outstanding",
    ),
    path(
        "reports/part-processed/",
        views.PartProcessedOrdersView.as_view(),
        name="orders-part-processed",
    ),
    path(
        "reports/shipments-costed/",
        views.CostedShipmentsView.as_view(),
        name="shipments-costed",
    ),
    path("", include(router.urls)),
]
