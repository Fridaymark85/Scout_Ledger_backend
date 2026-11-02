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
    """Read: any authenticated user. Create: REQUESTOR only. Edit/submit: requestor (owner). Delete: owner, admin, developer."""

    def has_permission(self, request: Request, view) -> bool:
        role = _role(request)
        if role is None:
            return False
        if view.action == "create":
            return role == User.Role.REQUESTOR
        if view.action == "export":
            return role in User.ADMIN_ROLES
        if view.action == "destroy":
            return role == User.Role.REQUESTOR or role in User.ADMIN_ROLES  # owner (via queryset), admin, developer
        if view.action in ("update", "partial_update", "submit"):
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
            return role in User.ADMIN_ROLES
        if request.method in SAFE_METHODS:
            return True
        return role in (User.Role.SENIOR_APPROVER, *User.ADMIN_ROLES)

    def has_object_permission(self, request: Request, view, obj) -> bool:
        if request.method in SAFE_METHODS or _role(request) in User.ADMIN_ROLES:
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
        if view.action == "destroy":
            return role == User.Role.DEVELOPER  # deleting accounts is developer-only
        return role in User.ADMIN_ROLES
