from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView
from inventory.views import (
    LocationViewSet,
    ItemViewSet,
    CategoryViewSet,
    InspectionCertificateViewSet,
    StockEntryViewSet,
    ItemInstanceViewSet,
    LocationInventoryViewSet,
    UserProfileViewSet,
    UserActivityViewSet,
    CustomTokenObtainPairView
)

router = DefaultRouter()
router.register(r'locations', LocationViewSet, basename='location')
router.register(r'items', ItemViewSet, basename='item')
router.register(r'categories', CategoryViewSet, basename='category')
router.register(r'inspection-certificates', InspectionCertificateViewSet, basename='inspection-certificate')
router.register(r'stock-entries', StockEntryViewSet, basename='stock-entry')
router.register(r'item-instances', ItemInstanceViewSet, basename='item-instance')
router.register(r'location-inventory', LocationInventoryViewSet, basename='location-inventory')
router.register(r'users', UserProfileViewSet, basename='user-profile')
router.register(r'activities', UserActivityViewSet, basename='user-activity')

urlpatterns = [
    path('auth/login/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('', include(router.urls)),
]