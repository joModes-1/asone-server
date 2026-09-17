"""The Settings screen — GET for anyone signed in, PATCH for leads only.

One object, always ``Settings.load()`` — the same "no lookup, no queryset"
shape `accounts.views.MeView` uses for "the signed-in user", because there is
exactly one row to ever address here.
"""

from drf_spectacular.utils import extend_schema
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import IsAuthenticated

from accounts.permissions import AUTHENTICATED, CanUpdateTables

from .models import Settings
from .serializers import SettingsSerializer


@extend_schema(tags=["Organization"])
class SettingsView(RetrieveUpdateAPIView):
    """The Settings screen.

    Read by anyone signed in — timezone and currency are needed to render
    the app consistently regardless of role. Written by Program Lead and
    Operations Manager only, the same "Table Updates" column that gates
    every other piece of master data.
    """

    serializer_class = SettingsSerializer
    http_method_names = ["get", "patch", "head", "options"]

    def get_permissions(self):
        if self.request.method in ("PATCH", "PUT"):
            return [permission() for permission in [*AUTHENTICATED, CanUpdateTables]]
        return [IsAuthenticated()]

    def get_object(self):
        return Settings.load()

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)
