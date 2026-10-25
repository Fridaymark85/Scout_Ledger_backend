"""API routes for the procurement app."""
from rest_framework.routers import DefaultRouter

from .views import (
    ApprovalStepViewSet, CostCenterViewSet, PlantViewSet, RequisitionViewSet, UserViewSet,
)

router = DefaultRouter()
router.register("plants", PlantViewSet, basename="plant")
router.register("cost-centers", CostCenterViewSet, basename="costcenter")
router.register("requisitions", RequisitionViewSet, basename="requisition")
router.register("approval-steps", ApprovalStepViewSet, basename="approvalstep")
router.register("users", UserViewSet, basename="user")

urlpatterns = router.urls
