"""Role-based DRF permission classes."""
from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request

from .models import User


def _role(request: Request) -> str | None:
    """Role of the authenticated user, or None for anonymous."""
    user = request.user
    return getattr(user, "role", None) if user and user.is_authenticated else None


class RequisitionPermission(BasePermission):
    """Read: any authenticated user. Create: REQUESTOR only. Edit/delete/submit: requestor (owner via queryset)."""

    def has_permission(self, request: Request, view) -> bool:
        role = _role(request)
        if role is None:
            return False
        if view.action == "create":
            return role == User.Role.REQUESTOR
        if view.action == "export":
            return role in (User.Role.PURCHASING_AGENT, User.Role.ADMIN)
        if view.action in ("update", "partial_update", "destroy", "submit"):
            return role == User.Role.REQUESTOR
        return request.method in SAFE_METHODS


class ApprovalStepPermission(BasePermission):
    """Read: any authenticated user. Decide: the assigned SENIOR_APPROVER, or ADMIN (override).
    Reassign: ADMIN only."""

    def has_permission(self, request: Request, view) -> bool:
        role = _role(request)
        if role is None:
            return False
        if view.action == "reassign":
            return role == User.Role.ADMIN
        if request.method in SAFE_METHODS:
            return True
        return role in (User.Role.SENIOR_APPROVER, User.Role.ADMIN)

    def has_object_permission(self, request: Request, view, obj) -> bool:
        if request.method in SAFE_METHODS or _role(request) == User.Role.ADMIN:
            return True
        # Approvers may only act on their own step.
        return obj.approver_id == request.user.id


class UserAdminPermission(BasePermission):
    """User management is ADMIN only; the approver picker list is open to any signed-in user."""

    def has_permission(self, request: Request, view) -> bool:
        role = _role(request)
        if role is None:
            return False
        if view.action == "approvers":
            return True
        return role == User.Role.ADMIN
