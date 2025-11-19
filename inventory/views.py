# views.py - ENHANCED VERSION
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.views import TokenObtainPairView
from django.utils import timezone
from django.db.models import Q, Count, Sum, F
from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404
from django.db import transaction
from inventory.models import *
from inventory.serializers import *
from inventory.permissions import *
from user_management.models import UserProfile, UserActivity, UserRole

from django.http import HttpResponse
import os
from django.conf import settings

# Import your generator
from .generator import InspectionCertificateGenerator

# ==================== CUSTOM JWT VIEW ====================
class CustomTokenObtainPairView(TokenObtainPairView):
    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        
        if response.status_code == 200:
            try:
                user = User.objects.get(username=request.data['username'])
                
                # Ensure profile exists
                profile, created = UserProfile.objects.get_or_create(
                    user=user,
                    defaults={
                        'role': UserRole.SYSTEM_ADMIN if user.is_superuser else UserRole.STOCK_INCHARGE,
                        'is_active': user.is_active
                    }
                )
                
                # Update profile for superusers
                if user.is_superuser and profile.role != UserRole.SYSTEM_ADMIN:
                    profile.role = UserRole.SYSTEM_ADMIN
                    profile.save()
                
                # Get permissions summary
                permissions = profile.get_permissions_summary()
                
                # Get pending tasks count
                pending_tasks = self._get_pending_tasks_count(user, profile)
                
                response.data['user'] = {
                    'id': user.id,
                    'username': user.username,
                    'email': user.email,
                    'full_name': user.get_full_name() or user.username,
                    'role': profile.role,
                    'role_display': profile.get_role_display(),
                    'assigned_locations': list(profile.assigned_locations.values_list('id', flat=True)),
                    'accessible_stores': LocationMinimalSerializer(profile.get_accessible_stores(), many=True).data,
                    'accessible_standalone': LocationMinimalSerializer(profile.get_standalone_locations(), many=True).data,
                    'permissions': permissions,
                    'pending_tasks': pending_tasks,
                }
                
                # Log activity
                UserActivity.objects.create(
                    user=user,
                    action='LOGIN',
                    model='User',
                    object_id=user.id,
                    ip_address=self.get_client_ip(request)
                )
                
            except User.DoesNotExist:
                return Response(
                    {'error': 'Invalid credentials'}, 
                    status=status.HTTP_401_UNAUTHORIZED
                )
            except Exception as e:
                print(f"Login error: {e}")
        
        return response
    
    def _get_pending_tasks_count(self, user, profile):
        """Get count of pending tasks for the user"""
        pending = {
            'total': 0,
            'acknowledgments': 0,
            'inspections': 0,
        }
        
        if profile.role == UserRole.STOCK_INCHARGE:
            # Pending acknowledgments for stock entries
            accessible_stores = profile.get_accessible_stores()
            pending['acknowledgments'] = StockEntry.objects.filter(
                status='PENDING_ACK',
                to_location__in=accessible_stores
            ).count()
            
            # Pending inspection stock details
            department_ids = set()
            for store in accessible_stores:
                if store.is_main_store and store.parent_location:
                    parent_standalone = store.get_parent_standalone()
                    if parent_standalone:
                        department_ids.add(parent_standalone.id)
            
            pending['inspections'] = InspectionCertificate.objects.filter(
                department_id__in=department_ids,
                stage='STOCK_DETAILS'
            ).count()
            
        elif profile.role == UserRole.LOCATION_HEAD:
            # Pending inspection initiation
            accessible_locations = profile.get_accessible_locations()
            pending['inspections'] = InspectionCertificate.objects.filter(
                department__in=accessible_locations,
                stage='INITIATED'
            ).count()
            
        elif profile.role == UserRole.AUDITOR:
            # Pending audit reviews
            pending['inspections'] = InspectionCertificate.objects.filter(
                stage='AUDIT_REVIEW'
            ).count()
        
        pending['total'] = pending['acknowledgments'] + pending['inspections']
        return pending
    
    def get_client_ip(self, request):
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip

# ==================== USER MANAGEMENT VIEWSET ====================
class UserProfileViewSet(viewsets.ModelViewSet):
    queryset = UserProfile.objects.all()
    serializer_class = UserProfileSerializer
    permission_classes = [IsAuthenticated, CanManageUsers]
    
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if not hasattr(user, 'profile'):
            return queryset.none()
        
        profile = user.profile
        
        if profile.role == UserRole.SYSTEM_ADMIN:
            return queryset
        elif profile.role == UserRole.LOCATION_HEAD:
            return queryset.filter(Q(created_by=user) | Q(user=user))
        
        return queryset.filter(user=user)
    
    @action(detail=True, methods=['post'])
    def reset_password(self, request, pk=None):
        """Reset user password"""
        profile = self.get_object()
        new_password = request.data.get('password')
        
        if not new_password:
            return Response({'error': 'Password is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        if request.user.profile.role == UserRole.LOCATION_HEAD:
            if profile.created_by != request.user:
                raise PermissionDenied("You can only reset passwords for users you created")
        
        profile.user.set_password(new_password)
        profile.user.save()
        
        UserActivity.objects.create(
            user=request.user,
            action='RESET_PASSWORD',
            model='User',
            object_id=profile.user.id,
            details={'target_user': profile.user.username}
        )
        
        return Response({'message': 'Password reset successfully'})
    
    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        """Activate/deactivate user"""
        profile = self.get_object()
        
        if request.user.profile.role == UserRole.LOCATION_HEAD:
            if profile.created_by != request.user:
                raise PermissionDenied("You can only modify users you created")
        
        profile.is_active = not profile.is_active
        profile.user.is_active = profile.is_active
        profile.save()
        profile.user.save()
        
        UserActivity.objects.create(
            user=request.user,
            action='TOGGLE_USER_ACTIVE',
            model='UserProfile',
            object_id=profile.id,
            details={'username': profile.user.username, 'is_active': profile.is_active}
        )
        
        return Response({
            'message': f'User {"activated" if profile.is_active else "deactivated"}',
            'is_active': profile.is_active
        })
    
    @action(detail=False, methods=['get'])
    def my_profile(self, request):
        """Get current user's profile"""
        if not hasattr(request.user, 'profile'):
            return Response({'error': 'Profile not found'}, status=status.HTTP_404_NOT_FOUND)
        
        serializer = self.get_serializer(request.user.profile)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def my_permissions(self, request):
        """Get current user's permissions with enhanced standalone awareness"""
        if not hasattr(request.user, 'profile'):
            return Response({'error': 'Profile not found'}, status=status.HTTP_404_NOT_FOUND)
        
        profile = request.user.profile
        permissions = profile.get_permissions_summary()
        
        return Response(permissions)
    
    @action(detail=False, methods=['get'])
    def my_pending_tasks(self, request):
        """Get pending tasks for current user"""
        if not hasattr(request.user, 'profile'):
            return Response({'error': 'Profile not found'}, status=status.HTTP_404_NOT_FOUND)
        
        profile = request.user.profile
        pending_tasks = self._get_user_pending_tasks(request.user, profile)
        
        return Response(pending_tasks)
    
    def _get_user_pending_tasks(self, user, profile):
        """Get detailed pending tasks for user"""
        tasks = {
            'acknowledgments': [],
            'inspections': [],
            'counts': {
                'total': 0,
                'acknowledgments': 0,
                'inspections': 0,
            }
        }
        
        if profile.role == UserRole.STOCK_INCHARGE:
            accessible_stores = profile.get_accessible_stores()
            
            # Pending acknowledgments
            pending_ack = StockEntry.objects.filter(
                status='PENDING_ACK',
                to_location__in=accessible_stores
            ).select_related('item', 'from_location', 'to_location')
            
            tasks['acknowledgments'] = StockEntrySerializer(pending_ack, many=True).data
            tasks['counts']['acknowledgments'] = pending_ack.count()
            
            # Pending inspection stock details
            department_ids = set()
            for store in accessible_stores:
                if store.is_main_store and store.parent_location:
                    parent_standalone = store.get_parent_standalone()
                    if parent_standalone:
                        department_ids.add(parent_standalone.id)
            
            pending_inspections = InspectionCertificate.objects.filter(
                department_id__in=department_ids,
                stage='STOCK_DETAILS'
            )
            tasks['inspections'] = InspectionCertificateSerializer(pending_inspections, many=True).data
            tasks['counts']['inspections'] = pending_inspections.count()
            
        elif profile.role == UserRole.LOCATION_HEAD:
            accessible_locations = profile.get_accessible_locations()
            pending_inspections = InspectionCertificate.objects.filter(
                department__in=accessible_locations,
                stage='INITIATED'
            )
            tasks['inspections'] = InspectionCertificateSerializer(pending_inspections, many=True).data
            tasks['counts']['inspections'] = pending_inspections.count()
            
        elif profile.role == UserRole.AUDITOR:
            pending_inspections = InspectionCertificate.objects.filter(
                stage='AUDIT_REVIEW'
            )
            tasks['inspections'] = InspectionCertificateSerializer(pending_inspections, many=True).data
            tasks['counts']['inspections'] = pending_inspections.count()
        
        tasks['counts']['total'] = tasks['counts']['acknowledgments'] + tasks['counts']['inspections']
        return tasks
    
    @action(detail=False, methods=['get'])
    def my_item_default_locations(self, request):
        """Get standalone locations that can be used as item default locations"""
        if not hasattr(request.user, 'profile'):
            return Response({'error': 'Profile not found'}, status=status.HTTP_404_NOT_FOUND)
        
        profile = request.user.profile
        default_locations = profile.get_item_default_locations()
        
        return Response({
            'standalone_locations': LocationMinimalSerializer(default_locations, many=True).data,
            'message': 'Items must belong to standalone locations'
        })

# ==================== LOCATION VIEWSET ====================
class LocationViewSet(viewsets.ModelViewSet):
    queryset = Location.objects.all()
    serializer_class = LocationSerializer
    permission_classes = [IsAuthenticated, HasLocationAccess]
    
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if not hasattr(user, 'profile'):
            return queryset.none()
        
        profile = user.profile
        
        # SYSTEM_ADMIN and AUDITOR can see all locations
        if profile.role in [UserRole.SYSTEM_ADMIN, UserRole.AUDITOR]:
            pass
        else:
            # Filter to accessible locations
            accessible_locations = profile.get_accessible_locations()
            queryset = queryset.filter(id__in=accessible_locations.values_list('id', flat=True))
        
        # Apply filters
        location_type = self.request.query_params.get('type')
        is_store = self.request.query_params.get('is_store')
        is_standalone = self.request.query_params.get('is_standalone')
        is_main_store = self.request.query_params.get('is_main_store')
        can_have_sub_locations = self.request.query_params.get('can_have_sub_locations')
        parent_id = self.request.query_params.get('parent')
        search = self.request.query_params.get('search')
        
        if location_type:
            queryset = queryset.filter(location_type=location_type)
        if is_store is not None:
            queryset = queryset.filter(is_store=is_store.lower() == 'true')
        if is_standalone is not None:
            queryset = queryset.filter(is_standalone=is_standalone.lower() == 'true')
        if is_main_store is not None:
            queryset = queryset.filter(is_main_store=is_main_store.lower() == 'true')
        if can_have_sub_locations is not None:
            if can_have_sub_locations.lower() == 'true':
                queryset = queryset.filter(is_standalone=True)
        if parent_id:
            queryset = queryset.filter(parent_location_id=parent_id)
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) |
                Q(code__icontains=search) |
                Q(address__icontains=search)
            )
        
        return queryset.select_related('parent_location').prefetch_related('child_locations')
    
    def perform_create(self, serializer):
        """Create location with permission checks and auto-create main store for standalone"""
        if not hasattr(self.request.user, 'profile'):
            raise PermissionDenied("User profile required")
        
        profile = self.request.user.profile
        parent_location = serializer.validated_data.get('parent_location')
        
        # Check if user can create location
        if not profile.can_create_location(parent_location):
            raise PermissionDenied("You don't have permission to create locations under this parent")
        
        # Create the location
        location = serializer.save(created_by=self.request.user)
        
        # Log activity
        UserActivity.objects.create(
            user=self.request.user,
            action='CREATE_LOCATION',
            model='Location',
            object_id=location.id,
            details={
                'location_name': location.name,
                'location_code': location.code,
                'location_type': location.location_type,
                'is_standalone': location.is_standalone,
                'parent': parent_location.name if parent_location else 'Root',
                'is_store': location.is_store,
                'auto_created_main_store': location.auto_created_store.name if location.auto_created_store else None
            }
        )
    
    @action(detail=True, methods=['get'])
    def stores(self, request, pk=None):
        """Get all stores under this location"""
        location = self.get_object()
        stores = location.get_all_stores()
        serializer = LocationMinimalSerializer(stores, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def children(self, request, pk=None):
        """Get immediate children of this location"""
        location = self.get_object()
        children = location.get_immediate_children()
        serializer = LocationSerializer(children, many=True, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def standalone_children(self, request, pk=None):
        """Get only standalone children (departments, buildings, etc.)"""
        location = self.get_object()
        children = location.get_standalone_children()
        serializer = LocationMinimalSerializer(children, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def standalone_locations(self, request):
        """Get all standalone locations (can have sub-locations)"""
        standalone = self.get_queryset().filter(
            is_standalone=True,
            is_active=True
        ).order_by('name')
        
        serializer = LocationMinimalSerializer(standalone, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def root_location(self, request):
        """Get the root location (Main University)"""
        root = Location.objects.filter(parent_location__isnull=True).first()
        if root:
            serializer = LocationSerializer(root, context={'request': request})
            return Response(serializer.data)
        return Response({'error': 'Root location not found'}, status=status.HTTP_404_NOT_FOUND)
    
    @action(detail=False, methods=['get'])
    def store_locations(self, request):
        """Get all store locations accessible to user"""
        user = request.user
        
        if not hasattr(user, 'profile'):
            return Response({'error': 'Profile not found'}, status=status.HTTP_404_NOT_FOUND)
        
        profile = user.profile
        stores = profile.get_accessible_stores()
        
        serializer = LocationMinimalSerializer(stores, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def main_stores(self, request):
        """Get all main stores accessible to user"""
        user = request.user
        
        if not hasattr(user, 'profile'):
            return Response({'error': 'Profile not found'}, status=status.HTTP_404_NOT_FOUND)
        
        profile = user.profile
        
        if profile.role == UserRole.SYSTEM_ADMIN:
            main_stores = Location.objects.filter(is_main_store=True, is_active=True)
        else:
            accessible_stores = profile.get_accessible_stores()
            main_stores = accessible_stores.filter(is_main_store=True)
        
        serializer = LocationMinimalSerializer(main_stores, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def user_accessible_locations(self, request):
        """Get all locations accessible to the current user"""
        if not hasattr(request.user, 'profile'):
            return Response({'error': 'Profile not found'}, status=404)
        
        profile = request.user.profile
        accessible_locations = profile.get_accessible_locations()
        
        serializer = LocationMinimalSerializer(accessible_locations, many=True)
        return Response({
            'user_role': profile.role,
            'responsible_location': LocationMinimalSerializer(
                profile.get_responsible_location()
            ).data if profile.get_responsible_location() else None,
            'locations': serializer.data,
            'count': accessible_locations.count()
        })
    
    @action(detail=False, methods=['get'])
    def user_accessible_stores(self, request):
        """Get all stores accessible to the current user"""
        if not hasattr(request.user, 'profile'):
            return Response({'error': 'Profile not found'}, status=404)
        
        profile = request.user.profile
        accessible_stores = profile.get_accessible_stores()
        
        serializer = LocationMinimalSerializer(accessible_stores, many=True)
        return Response({
            'user_role': profile.role,
            'stores': serializer.data,
            'count': accessible_stores.count(),
            'is_main_store_incharge': profile.is_main_store_incharge(),
            'can_issue_upward': profile.can_issue_to_parent_standalone()
        })
    
    @action(detail=False, methods=['get'])
    def creation_options(self, request):
        """Get options for creating new locations"""
        if not hasattr(request.user, 'profile'):
            return Response({'error': 'Profile not found'}, status=404)
        
        profile = request.user.profile
        
        # Get possible parent locations
        accessible_locations = profile.get_accessible_locations()
        possible_parents = accessible_locations.filter(is_standalone=True)
        
        # If user can create root locations (only SYSTEM_ADMIN)
        can_create_root = profile.role == UserRole.SYSTEM_ADMIN
        root_allowed_types = []
        if can_create_root:
            root_allowed_types = [LocationType.DEPARTMENT, LocationType.BUILDING, 
                                LocationType.JUNKYARD, LocationType.OTHER]
        
        return Response({
            'can_create_root': can_create_root,
            'root_allowed_types': root_allowed_types,
            'possible_parents': LocationMinimalSerializer(possible_parents, many=True).data,
            'user_role': profile.role,
            'responsible_location': LocationMinimalSerializer(
                profile.get_responsible_location()
            ).data if profile.get_responsible_location() else None
        })
    
    @action(detail=False, methods=['get'])
    def assignment_options(self, request):
        """Get locations that can be assigned to users based on role"""
        if not hasattr(request.user, 'profile'):
            return Response({'error': 'Profile not found'}, status=404)
        
        profile = request.user.profile
        role = request.query_params.get('role')
        
        if not role:
            return Response({'error': 'Role parameter required'}, status=400)
        
        if role == UserRole.LOCATION_HEAD:
            # For Location Head assignment - standalone locations only
            locations = profile.get_standalone_locations()
        elif role == UserRole.STOCK_INCHARGE:
            # For Stock Incharge assignment - stores only
            locations = profile.get_accessible_stores()
        else:
            locations = Location.objects.none()
        
        serializer = LocationMinimalSerializer(locations, many=True)
        return Response({
            'role': role,
            'locations': serializer.data,
            'count': locations.count()
        })
    
    @action(detail=True, methods=['get'])
    def issuance_targets(self, request, pk=None):
        """Get locations this store can issue to based on complex hierarchy rules"""
        location = self.get_object()
        
        if not location.is_store:
            return Response({
                'error': 'Only stores can have issuance targets'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Get parent standalone
        parent_standalone = location.get_parent_standalone()
        if not parent_standalone:
            return Response({
                'internal_targets': [],
                'standalone_targets': [],
                'can_issue_upward': False
            })
        
        internal_targets = []  # Non-standalone locations within hierarchy
        standalone_targets = []  # Standalone locations for upward transfer
        
        if location.is_main_store:
            # Main store can issue to:
            # 1. Non-standalone locations within parent standalone
            descendants = parent_standalone.get_descendants(include_self=False)
            internal_targets = descendants.filter(
                is_standalone=False,
                is_store=False,
                is_active=True
            )
            
            # 2. Standalone locations within parent (for transfer to their main store)
            standalone_targets = descendants.filter(
                is_standalone=True,
                is_active=True
            )
            
            # 3. Can issue upward to parent of parent standalone
            can_issue_upward = parent_standalone.parent_location is not None
            upward_target = None
            if can_issue_upward and parent_standalone.parent_location:
                upward_standalone = parent_standalone.parent_location.get_parent_standalone()
                if upward_standalone:
                    upward_target = upward_standalone
        else:
            # Non-main store can only issue within same standalone location
            descendants = parent_standalone.get_descendants(include_self=False)
            internal_targets = descendants.filter(
                is_standalone=False,
                is_store=False,
                is_active=True
            )
            can_issue_upward = False
            upward_target = None
        
        response_data = {
            'location': LocationMinimalSerializer(location).data,
            'parent_standalone': LocationMinimalSerializer(parent_standalone).data,
            'internal_targets': LocationMinimalSerializer(internal_targets, many=True).data,
            'standalone_targets': LocationMinimalSerializer(standalone_targets, many=True).data,
            'can_issue_upward': can_issue_upward,
            'upward_target': LocationMinimalSerializer(upward_target).data if upward_target else None,
            'is_main_store': location.is_main_store
        }
        
        return Response(response_data)

# ==================== CATEGORY VIEWSET ====================
class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    permission_classes = [IsAuthenticated, IsSystemAdminOrReadOnly]

# ==================== ITEM VIEWSET ====================
class ItemViewSet(viewsets.ModelViewSet):
    queryset = Item.objects.all()
    serializer_class = ItemSerializer
    permission_classes = [IsAuthenticated, CanManageItems]
    
    def get_queryset(self):
        queryset = super().get_queryset()
        category = self.request.query_params.get('category')
        search = self.request.query_params.get('search')
        default_location = self.request.query_params.get('default_location')
        
        if category:
            queryset = queryset.filter(category_id=category)
        if default_location:
            queryset = queryset.filter(default_location_id=default_location)
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) |
                Q(code__icontains=search)
            )
        return queryset
    
    def perform_create(self, serializer):
        """Track who created the item"""
        if not hasattr(self.request.user, 'profile'):
            raise PermissionDenied("User profile required")
        
        serializer.save(created_by=self.request.user)
        
        UserActivity.objects.create(
            user=self.request.user,
            action='CREATE_ITEM',
            model='Item',
            object_id=serializer.instance.id,
            details={
                'item_name': serializer.instance.name,
                'item_code': serializer.instance.code,
                'default_location': serializer.instance.default_location.name
            }
        )

# ==================== INSPECTION CERTIFICATE VIEWSET ====================
class InspectionCertificateViewSet(viewsets.ModelViewSet):
    queryset = InspectionCertificate.objects.all()
    serializer_class = InspectionCertificateSerializer
    permission_classes = [IsAuthenticated, InspectionCertificatePermission]
    
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if not hasattr(user, 'profile'):
            return queryset.none()
        
        profile = user.profile
        
        if profile.role in [UserRole.SYSTEM_ADMIN, UserRole.AUDITOR]:
            pass
        elif profile.role == UserRole.STOCK_INCHARGE:
            # CRITICAL FIX: Central Store Incharge vs Department Store Incharge
            if profile.is_main_store_incharge():
                # Central Store Incharge (Root location's main store)
                # Can see ALL certificates that are in CENTRAL_REGISTER or AUDIT_REVIEW stage
                # (because they need to fill central register for ALL departments)
                queryset = queryset.filter(
                    stage__in=['CENTRAL_REGISTER', 'AUDIT_REVIEW']
                )
            else:
                # Department Store Incharge
                # Can only see certificates from their own department in STOCK_DETAILS stage
                accessible_stores = profile.get_accessible_stores()
                department_ids = set()
                
                for store in accessible_stores:
                    if store.is_main_store and store.parent_location:
                        # This is a main store, get its parent standalone
                        parent_standalone = store.get_parent_standalone()
                        if parent_standalone:
                            department_ids.add(parent_standalone.id)
                
                if department_ids:
                    # Only show STOCK_DETAILS stage certificates for their department
                    queryset = queryset.filter(
                        department_id__in=department_ids,
                        stage='STOCK_DETAILS'
                    )
                else:
                    queryset = queryset.none()
        else:
            # Location Head
            accessible_locations = profile.get_accessible_locations()
            queryset = queryset.filter(department__in=accessible_locations)
        
        # Filters
        stage = self.request.query_params.get('stage')
        status_filter = self.request.query_params.get('status')
        department = self.request.query_params.get('department')
        
        if stage:
            queryset = queryset.filter(stage=stage)
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        if department:
            queryset = queryset.filter(department_id=department)
        
        return queryset
    
    def perform_create(self, serializer):
        """Create inspection certificate"""
        if not hasattr(self.request.user, 'profile'):
            raise PermissionDenied("User profile required")
        
        profile = self.request.user.profile
        
        # System admin can create for any location
        if profile.role != UserRole.SYSTEM_ADMIN:
            if not profile.can_create_inspection_certificates():
                raise PermissionDenied("Only Location Heads of standalone locations can create inspection certificates")
            
            department = serializer.validated_data.get('department')
            if department and not profile.has_location_access(department):
                raise PermissionDenied("You don't have access to this department")
        
        department = serializer.validated_data.get('department')
        main_store = department.get_main_store()
        if not main_store:
            raise PermissionDenied(f"Department {department.name} does not have a main store configured")
        
        serializer.save(
            initiated_by=self.request.user,
            initiated_at=timezone.now(),
            created_by=self.request.user
        )
    
    @action(detail=False, methods=['get'])
    def creation_options(self, request):
        """Get options for creating inspection certificates"""
        if not hasattr(request.user, 'profile'):
            return Response({'error': 'Profile not found'}, status=404)
        
        profile = request.user.profile
        
        if profile.role == UserRole.SYSTEM_ADMIN:
            # System admin can create for any standalone location
            departments = Location.objects.filter(
                is_standalone=True,
                is_active=True
            )
            can_select_department = True
        elif profile.role == UserRole.LOCATION_HEAD:
            # Location head can only create for their locations
            accessible = profile.get_accessible_locations()
            departments = accessible.filter(is_standalone=True)
            # If only one department, it should be fixed
            can_select_department = departments.count() > 1
        else:
            departments = Location.objects.none()
            can_select_department = False
        
        return Response({
            'departments': LocationMinimalSerializer(departments, many=True).data,
            'can_select_department': can_select_department,
            'user_role': profile.role
        })
    
    @action(detail=True, methods=['post'])
    @transaction.atomic
    def submit_to_stock_incharge(self, request, pk=None):
        """
        Location Head submits certificate to Stock Incharge.
        
        WORKFLOW LOGIC:
        - Root Location: INITIATED -> CENTRAL_REGISTER (skip STOCK_DETAILS)
        Goes directly to Central Store for central register
        - Non-Root Location: INITIATED -> STOCK_DETAILS
        Goes to Department Store for stock register first
        """
        inspection = self.get_object()
        
        if inspection.stage != 'INITIATED':
            return Response({
                'error': f'Certificate must be in INITIATED stage, currently in {inspection.stage}'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        main_store = inspection.get_main_store()
        if not main_store:
            return Response({
                'error': 'No main store found for this department'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Update with any submitted data
        if request.data:
            serializer = self.get_serializer(inspection, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
        
        # CRITICAL: Check if department is root location
        is_root_department = inspection.department.parent_location is None
        
        if is_root_department:
            # ROOT LOCATION FLOW (3 stages):
            # Stage 1: Location Head -> Stage 2: Central Store -> Stage 3: Auditor
            inspection.transition_stage('CENTRAL_REGISTER', request.user)
            
            return Response({
                'message': 'Certificate submitted to Central Store Incharge for central register entry',
                'new_stage': inspection.stage,
                'stage_display': inspection.get_stage_display(),
                'main_store': main_store.name,
                'is_root_flow': True,
                'workflow': 'Root Location: 3-stage workflow (Location Head -> Central Store -> Auditor)',
                'next_step': 'Central Store Incharge will fill central register details and consignee information',
                'certificate': InspectionCertificateSerializer(inspection, context={'request': request}).data
            })
        else:
            # NON-ROOT LOCATION FLOW (4 stages):
            # Stage 1: Location Head -> Stage 2: Dept Store -> Stage 3: Central Store -> Stage 4: Auditor
            inspection.transition_stage('STOCK_DETAILS', request.user)
            
            return Response({
                'message': f'Certificate submitted to Department Store Incharge of {main_store.name}',
                'new_stage': inspection.stage,
                'stage_display': inspection.get_stage_display(),
                'main_store': main_store.name,
                'is_root_flow': False,
                'workflow': 'Department Flow: 4-stage workflow (Location Head -> Dept Store -> Central Store -> Auditor)',
                'next_step': 'Department Store Incharge will fill stock register details',
                'certificate': InspectionCertificateSerializer(inspection, context={'request': request}).data
            })
    
    @action(detail=True, methods=['post'])
    @transaction.atomic
    def submit_stock_details(self, request, pk=None):
        """
        Department Store Incharge submits stock details (NON-ROOT ONLY).
        Stage 2 -> Stage 3: STOCK_DETAILS -> CENTRAL_REGISTER
        
        After this, it goes to Central Store Incharge for central register.
        """
        inspection = self.get_object()
        
        if inspection.stage != 'STOCK_DETAILS':
            return Response({
                'error': f'Certificate must be in STOCK_DETAILS stage, currently in {inspection.stage}'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Verify this is NOT root location (root skips STOCK_DETAILS)
        is_root_department = inspection.department.parent_location is None
        if is_root_department:
            return Response({
                'error': 'Root location certificates should not be in STOCK_DETAILS stage. They skip directly to CENTRAL_REGISTER.'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Update with submitted data
        if request.data:
            serializer = self.get_serializer(inspection, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
        
        items_count = inspection.inspection_items.count()
        if items_count == 0:
            return Response({
                'error': 'At least one inspection item is required.',
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Validate stock register fields are filled for at least one item
        items_with_stock_register = inspection.inspection_items.exclude(
            Q(stock_register_no__isnull=True) | Q(stock_register_no='')
        ).count()

        if items_with_stock_register == 0:
            # Try refreshing to get latest data from database
            inspection.refresh_from_db()
            items_with_stock_register = inspection.inspection_items.exclude(
                Q(stock_register_no__isnull=True) | Q(stock_register_no='')
            ).count()
            
            if items_with_stock_register == 0:
                return Response({
                    'error': 'Please fill stock register details for at least one item before submitting.',
                    'hint': 'Fill Stock Register No, Page No, and Entry Date for at least one item.'
                }, status=status.HTTP_400_BAD_REQUEST)

        # Transition to CENTRAL_REGISTER for Central Store
        inspection.transition_stage('CENTRAL_REGISTER', request.user)
        
        return Response({
            'message': 'Stock details submitted. Now forwarding to Central Store Incharge for central register entry.',
            'new_stage': inspection.stage,
            'stage_display': inspection.get_stage_display(),
            'items_count': items_count,
            'items_with_stock_register': items_with_stock_register,
            'next_step': 'Central Store Incharge will fill central register details',
            'certificate': InspectionCertificateSerializer(inspection, context={'request': request}).data
        })
    
    @action(detail=True, methods=['post'])
    @transaction.atomic
    def submit_central_register(self, request, pk=None):
        """
        Central Store Incharge submits central register details.
        Stage 3 -> Stage 4: CENTRAL_REGISTER -> AUDIT_REVIEW
        
        This happens for:
        - NON-ROOT: After department store fills stock details
        - ROOT: After location head initiates (skips STOCK_DETAILS)
        """
        inspection = self.get_object()
        
        if inspection.stage != 'CENTRAL_REGISTER':
            return Response({
                'error': f'Certificate must be in CENTRAL_REGISTER stage, currently in {inspection.stage}'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Update with submitted data
        if request.data:
            serializer = self.get_serializer(inspection, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
        
        # Validate central register fields are filled
        items_with_central_register = inspection.inspection_items.filter(
            central_register_no__isnull=False
        ).count()
        
        total_items = inspection.inspection_items.count()
        
        if items_with_central_register == 0:
            return Response({
                'error': 'Please fill central register details for at least one item before submitting.'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Check if this is root certificate
        is_root_cert = inspection.department.parent_location is None
        
        # For root certificates, also validate consignee details are filled
        if is_root_cert:
            if not inspection.consignee_name or not inspection.consignee_designation:
                return Response({
                    'error': 'Please fill consignee name and designation before submitting.'
                }, status=status.HTTP_400_BAD_REQUEST)
        
        # Transition to AUDIT_REVIEW for Auditor
        inspection.transition_stage('AUDIT_REVIEW', request.user)
        
        return Response({
            'message': 'Central register details submitted. Now forwarding to Auditor for final review.',
            'new_stage': inspection.stage,
            'stage_display': inspection.get_stage_display(),
            'items_filled': items_with_central_register,
            'total_items': total_items,
            'is_root_flow': is_root_cert,
            'next_step': 'Auditor will complete the final verification and create item instances',
            'certificate': InspectionCertificateSerializer(inspection, context={'request': request}).data
        })
    
    @action(detail=True, methods=['post'])
    @transaction.atomic
    def submit_audit_review(self, request, pk=None):
        """
        Auditor completes the certificate and creates stock entries.
        Stage 4 -> Complete: AUDIT_REVIEW -> COMPLETED
        
        This is the final stage where:
        1. Auditor fills finance/dead stock register details
        2. Certificate is marked as COMPLETED
        3. Item instances are created in the main store
        """
        inspection = self.get_object()
        
        if inspection.stage != 'AUDIT_REVIEW':
            return Response({
                'error': f'Certificate must be in AUDIT_REVIEW stage, currently in {inspection.stage}'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        main_store = inspection.get_main_store()
        if not main_store:
            return Response({
                'error': 'No main store found for this department'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Update with submitted data
        if request.data:
            serializer = self.get_serializer(inspection, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
        
        # Validate that central register is filled
        items_with_central_register = inspection.inspection_items.filter(
            central_register_no__isnull=False
        ).count()
        
        if items_with_central_register == 0:
            return Response({
                'error': 'Central Store must fill central register details before Auditor can complete the certificate.'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Check if this is root certificate
        is_root_cert = inspection.department.parent_location is None
        
        # Validate consignee details are filled
        if not inspection.consignee_name or not inspection.consignee_designation:
            return Response({
                'error': 'Consignee name and designation must be filled before completion.'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Transition to COMPLETED
        inspection.transition_stage('COMPLETED', request.user)
        
        # Create instances in main store
        created_instances_count = self._create_stock_from_inspection(inspection, request.user, main_store)
        
        return Response({
            'message': f'Audit completed successfully. Created {created_instances_count} instances in {main_store.name}.',
            'new_stage': inspection.stage,
            'stage_display': inspection.get_stage_display(),
            'instances_created': created_instances_count,
            'main_store': main_store.name,
            'is_root_flow': is_root_cert,
            'workflow_completed': True,
            'certificate': InspectionCertificateSerializer(inspection, context={'request': request}).data
        })
        
    def _is_root_department(self, department):
        """
        Check if a department is the root location (Main University).
        Root location has no parent_location.
        """
        return department.parent_location is None
    
    def _create_stock_from_inspection(self, inspection, user, main_store):
        """Create stock entries and instances in main store after audit approval"""
        total_instances = 0
        
        for insp_item in inspection.inspection_items.all():
            if insp_item.accepted_quantity <= 0:
                continue
            
            created_instances = []
            for i in range(insp_item.accepted_quantity):
                instance = ItemInstance.objects.create(
                    item=insp_item.item,
                    inspection_certificate=inspection,
                    source_location=main_store,
                    current_location=main_store,
                    current_status='IN_STORE',
                    condition='NEW',
                    purchase_date=inspection.date,
                    created_by=user
                )
                created_instances.append(instance)
            
            total_instances += len(created_instances)
            
            receipt_entry = StockEntry.objects.create(
                entry_type='RECEIPT',
                from_location=None,
                to_location=main_store,
                item=insp_item.item,
                quantity=insp_item.accepted_quantity,
                purpose=f"Receipt from Inspection {inspection.certificate_no}",
                remarks=f"Contractor: {inspection.contractor_name}",
                inspection_certificate=inspection,
                status='COMPLETED',
                created_by=user,
                acknowledged_at=timezone.now()
            )
            receipt_entry.instances.set(created_instances)
            
            inv, created = LocationInventory.objects.get_or_create(
                location=main_store,
                item=insp_item.item
            )
            inv.update_quantities()
        
        return total_instances
    
    @action(detail=True, methods=['post'])
    @transaction.atomic
    def reject(self, request, pk=None):
        """Reject inspection certificate"""
        inspection = self.get_object()
        reason = request.data.get('reason')
        
        if not reason:
            return Response({'error': 'Rejection reason required'}, status=status.HTTP_400_BAD_REQUEST)
        
        if inspection.stage in ['COMPLETED', 'REJECTED']:
            return Response({
                'error': f'Cannot reject certificate in {inspection.stage} stage'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        inspection.transition_stage('REJECTED', request.user, rejection_reason=reason)
        
        return Response({
            'message': 'Certificate rejected',
            'reason': reason,
            'certificate': InspectionCertificateSerializer(inspection, context={'request': request}).data
        })

    @action(detail=True, methods=['get'])
    def download_pdf(self, request, pk=None):
        """Download inspection certificate as PDF"""
        try:
            certificate = self.get_object()
            print(f"Starting PDF generation for certificate: {certificate.certificate_no}")
            
            # Check if certificate is completed
            if certificate.stage != 'COMPLETED':
                return Response(
                    {'error': 'Certificate must be completed to download PDF'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Prepare data for PDF generator in the exact format expected
            certificate_data = {
                'contract_no': certificate.contract_no or 'N/A',
                'date': str(certificate.date) if certificate.date else '',
                'contractor_name': certificate.contractor_name or 'N/A',
                'contractor_address': certificate.contractor_address or 'N/A',
                'indenter': certificate.indenter or 'N/A',
                'indent_no': certificate.indent_no or 'N/A',
                'consignee': certificate.consignee_name or 'N/A',
                'department': certificate.department.name if certificate.department else 'N/A',
                'date_of_delivery': str(certificate.date_of_delivery) if certificate.date_of_delivery else '',
                'delivery_status': certificate.delivery_type or 'FULL',
                'date_of_inspection': str(certificate.date_of_inspection) if certificate.date_of_inspection else '',
                'stock_register_no': [],
                'dead_stock_register_no': getattr(certificate, 'central_store_register_no', '') or '',
            }
            
            # Prepare item data - CRITICAL: Match the generator's expected format
            item_data = {
                'descriptions': [],
                'acct_unit': [],
                't_quantity': [],
                'r_quantity': [],
                'a_quantity': []
            }
            
            # Prepare rejected item data
            rejected_item_data = {
                'item_no': [],
                'reasons': []
            }
            
            # Process inspection items
            inspection_items = certificate.inspection_items.all()
            print(f"Processing {inspection_items.count()} inspection items")
            
            for index, item in enumerate(inspection_items, 1):
                # Main item data - ensure all fields have values
                description = f"{item.item.name}"
                if hasattr(item.item, 'specifications') and item.item.specifications:
                    description += f"\nSpecs: {item.item.specifications}"
                if item.remarks:
                    description += f"\nRemarks: {item.remarks}"
                    
                item_data['descriptions'].append(description)
                item_data['acct_unit'].append(item.item.acct_unit or 'N/A')
                item_data['t_quantity'].append(str(item.tendered_quantity or 0))
                item_data['r_quantity'].append(str(item.rejected_quantity or 0))
                item_data['a_quantity'].append(str(item.accepted_quantity or 0))
                
                # Stock register data
                if hasattr(item, 'stock_register_no') and item.stock_register_no:
                    certificate_data['stock_register_no'].append(item.stock_register_no)
                
                # Rejected items
                if item.rejected_quantity and item.rejected_quantity > 0:
                    rejected_item_data['item_no'].append(str(index))
                    rejection_reason = getattr(item, 'rejection_reason', 'Not meeting specifications') or "Not meeting specifications"
                    rejected_item_data['reasons'].append(rejection_reason)
            
            # Ensure all arrays have the same length
            item_count = len(item_data['descriptions'])
            for key in ['acct_unit', 't_quantity', 'r_quantity', 'a_quantity']:
                while len(item_data[key]) < item_count:
                    item_data[key].append('0' if key.endswith('quantity') else 'N/A')
            
            print(f"Data prepared - Items: {item_count}, Rejected: {len(rejected_item_data['item_no'])}")
            
            # Generate PDF
            try:
                # Import the generator
                from .generator import InspectionCertificateGenerator
                
                # Initialize generator with proper parameters
                generator = InspectionCertificateGenerator(
                    data=certificate_data,
                    item_data=item_data,
                    rejected_item_data=rejected_item_data
                )
                
                pdf_buffer = generator.get_pdf()
                
                # Verify PDF was generated
                if not pdf_buffer:
                    raise Exception("PDF buffer is None")
                    
                buffer_size = pdf_buffer.getbuffer().nbytes
                if buffer_size == 0:
                    raise Exception("Generated PDF is empty (0 bytes)")
                
                print(f"PDF generated successfully - Size: {buffer_size} bytes")
                
                # Create HTTP response
                response = HttpResponse(
                    pdf_buffer.getvalue(), 
                    content_type='application/pdf'
                )
                filename = f"Inspection-Certificate-{certificate.certificate_no}.pdf"
                response['Content-Disposition'] = f'attachment; filename="{filename}"'
                response['Content-Length'] = buffer_size
                
                # Log the download activity
                UserActivity.objects.create(
                    user=request.user,
                    action='DOWNLOAD_PDF',
                    model='InspectionCertificate',
                    object_id=certificate.id,
                    details={
                        'certificate_no': certificate.certificate_no,
                        'filename': filename,
                        'file_size': buffer_size,
                        'items_count': item_count
                    }
                )
                
                print(f"PDF download response ready for {filename}")
                return response
                
            except Exception as pdf_error:
                print(f"PDF Generation Error: {str(pdf_error)}")
                import traceback
                print(f"Traceback: {traceback.format_exc()}")
                return Response(
                    {'error': f'PDF generation failed: {str(pdf_error)}'}, 
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
        except InspectionCertificate.DoesNotExist:
            return Response(
                {'error': 'Certificate not found'}, 
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            print(f"Download PDF Error: {str(e)}")
            import traceback
            print(f"Traceback: {traceback.format_exc()}")
            return Response(
                {'error': f'Failed to download PDF: {str(e)}'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

# ==================== STOCK ENTRY VIEWSET ====================
class StockEntryViewSet(viewsets.ModelViewSet):
    queryset = StockEntry.objects.all()
    serializer_class = StockEntrySerializer
    permission_classes = [IsAuthenticated, CanManageStockEntry]
    
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if not hasattr(user, 'profile'):
            return queryset.none()
        
        profile = user.profile
        
        if profile.role not in [UserRole.SYSTEM_ADMIN, UserRole.AUDITOR]:
            user_locations = profile.get_accessible_locations()
            queryset = queryset.filter(
                Q(from_location__in=user_locations) |
                Q(to_location__in=user_locations)
            ).distinct()
        
        entry_type = self.request.query_params.get('entry_type')
        status_filter = self.request.query_params.get('status')
        pending_ack = self.request.query_params.get('pending_ack')
        
        if entry_type:
            queryset = queryset.filter(entry_type=entry_type.upper())
        if status_filter:
            queryset = queryset.filter(status=status_filter.upper())
        if pending_ack == 'true':
            user_stores = profile.get_accessible_stores()
            queryset = queryset.filter(
                status='PENDING_ACK',
                to_location__in=user_stores
            )
        
        return queryset
    
    @action(detail=False, methods=['get'])
    def create_options(self, request):
        """Get options for creating stock entries with proper target filtering"""
        if not hasattr(request.user, 'profile'):
            return Response({'error': 'Profile not found'}, status=status.HTTP_404_NOT_FOUND)
        
        profile = request.user.profile
        
        # From locations: accessible stores
        from_locations = profile.get_accessible_stores()
        
        # Build comprehensive issuance targets based on store type
        all_internal_targets = []
        all_standalone_targets = []
        can_issue_upward = False
        upward_target = None
        
        for store in from_locations:
            parent_standalone = store.get_parent_standalone()
            if not parent_standalone:
                continue
            
            if store.is_main_store:
                # Main store targets
                descendants = parent_standalone.get_descendants(include_self=False)
                
                # Internal targets: non-standalone locations (including other stores)
                internal = descendants.filter(
                    is_standalone=False,
                    is_active=True
                )
                all_internal_targets.extend(list(internal))
                
                # Standalone targets for cross-location transfer
                standalone = descendants.filter(
                    is_standalone=True,
                    is_active=True
                )
                all_standalone_targets.extend(list(standalone))
                
                # Check upward issuance
                if parent_standalone.parent_location:
                    can_issue_upward = True
                    upward_standalone = parent_standalone.parent_location.get_parent_standalone()
                    if upward_standalone:
                        upward_target = upward_standalone
            else:
                # Non-main store - internal targets within same standalone (including other stores)
                descendants = parent_standalone.get_descendants(include_self=False)
                internal = descendants.filter(
                    is_standalone=False,
                    is_active=True
                )
                all_internal_targets.extend(list(internal))
        
        # Remove duplicates
        internal_targets = list({loc.id: loc for loc in all_internal_targets}.values())
        standalone_targets = list({loc.id: loc for loc in all_standalone_targets}.values())
        
        # Get available items
        available_items = Item.objects.filter(
            instances__source_location__in=from_locations
        ).distinct()
        
        response_data = {
            'from_locations': LocationMinimalSerializer(from_locations, many=True).data,
            'internal_targets': LocationMinimalSerializer(internal_targets, many=True).data,
            'standalone_targets': LocationMinimalSerializer(standalone_targets, many=True).data,
            'available_items': ItemMinimalSerializer(available_items, many=True).data,
            'user_role': profile.role,
            'can_issue_upward': can_issue_upward,
            'upward_target': LocationMinimalSerializer(upward_target).data if upward_target else None,
            'is_main_store_incharge': profile.is_main_store_incharge()
        }
        
        return Response(response_data)
    
    @transaction.atomic
    def perform_create(self, serializer):
        """Create stock entry with automatic status updates"""
        stock_entry = serializer.save(created_by=self.request.user)
        
        if stock_entry.entry_type == 'ISSUE':
            self._process_issue_entry(stock_entry, self.request.user)
        elif stock_entry.entry_type == 'RECEIPT':
            self._process_receipt_entry(stock_entry, self.request.user)
        
        return stock_entry
    
    def _process_issue_entry(self, stock_entry, user):
        """Process issue entry with support for upward transfers"""
        instances = stock_entry.instances.all()
        
        # Determine target status based on destination
        if stock_entry.to_location.is_store:
            # Store to Store Transfer: IN_TRANSIT
            target_status = InstanceStatus.IN_TRANSIT
            stock_entry.status = 'PENDING_ACK'
            movement_type = 'UPWARD_TRANSFER' if stock_entry.is_upward_transfer else 'TRANSFER'
        else:
            # Store to Non-Store: IN_USE or TEMPORARY_ISSUED
            target_status = InstanceStatus.TEMPORARY_ISSUED if stock_entry.is_temporary else InstanceStatus.IN_USE
            stock_entry.status = 'COMPLETED'
            movement_type = 'ISSUE'
        
        stock_entry.save()
        
        for instance in instances:
            instance.change_status(
                new_status=target_status,
                user=user,
                location=stock_entry.to_location,
                notes=stock_entry.remarks or f"Issued from {stock_entry.from_location.name}"
            )
        
        self._update_inventories(stock_entry)
    
    def _process_receipt_entry(self, stock_entry, user):
        """Process receipt entry"""
        instances = stock_entry.instances.all()
        stock_entry.status = 'COMPLETED'
        stock_entry.save()
        
        for instance in instances:
            if instance.current_status == InstanceStatus.TEMPORARY_ISSUED:
                instance.actual_return_date = timezone.now().date()
            
            instance.change_status(
                new_status=InstanceStatus.IN_STORE,
                user=user,
                location=stock_entry.to_location,
                notes=stock_entry.remarks or "Returned to store"
            )
        
        self._update_inventories(stock_entry)
    
    def _update_inventories(self, stock_entry):
        """Update inventory for both locations"""
        for location in [stock_entry.from_location, stock_entry.to_location]:
            if location and location.is_store:
                inv, _ = LocationInventory.objects.get_or_create(
                    location=location,
                    item=stock_entry.item
                )
                inv.update_quantities()
    
    @action(detail=True, methods=['post'])
    @transaction.atomic
    def acknowledge_receipt(self, request, pk=None):
        """
        STEP 1: Receiver acknowledges transfer.
        
        - ACCEPTED: Create RECEIPT entry, transfer ownership, IN_STORE
        - REJECTED: Create RETURN entry, keep in IN_TRANSIT, wait for sender acknowledgment
        """
        stock_entry = self.get_object()
        
        if stock_entry.status != 'PENDING_ACK':
            return Response({
                'error': 'Entry is not pending acknowledgment'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        accepted_ids = request.data.get('accepted_instances', [])
        rejected_ids = request.data.get('rejected_instances', [])
        rejection_reason = request.data.get('rejection_reason', 'Quality issues')
        
        if not accepted_ids and not rejected_ids:
            return Response({
                'error': 'Must accept or reject at least one instance'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # ========== PROCESS ACCEPTED INSTANCES ==========
        acceptance_receipt = None
        accepted_instances_data = []
        
        if accepted_ids:
            accepted_instances = ItemInstance.objects.filter(id__in=accepted_ids)
            
            # ✅ CREATE RECEIPT ENTRY FOR ACCEPTED
            acceptance_receipt = StockEntry.objects.create(
                entry_type='RECEIPT',
                from_location=stock_entry.from_location,
                to_location=stock_entry.to_location,
                item=stock_entry.item,
                quantity=len(accepted_ids),
                purpose=f"Acceptance from transfer {stock_entry.entry_number}",
                remarks=f"✓ {len(accepted_ids)} instances ACCEPTED",
                reference_entry=stock_entry,
                status='COMPLETED',
                created_by=request.user,
                acknowledged_by=request.user,
                acknowledged_at=timezone.now()
            )
            acceptance_receipt.instances.set(accepted_instances)
            
            # Transfer ownership and mark IN_STORE
            for instance in accepted_instances:
                old_source = instance.source_location
                
                instance.source_location = stock_entry.to_location  # Transfer ownership
                instance.current_location = stock_entry.to_location
                instance.current_status = InstanceStatus.IN_STORE
                instance.status_changed_by = request.user
                instance.status_changed_at = timezone.now()
                instance.save()
                
                instance.generate_qr_code()
                instance.save()
                
                InstanceMovement.objects.create(
                    instance=instance,
                    stock_entry=acceptance_receipt,
                    from_location=stock_entry.from_location,
                    to_location=stock_entry.to_location,
                    previous_status=InstanceStatus.IN_TRANSIT,
                    new_status=InstanceStatus.IN_STORE,
                    movement_type='TRANSFER',
                    moved_by=request.user,
                    remarks=f"✓ ACCEPTED - Ownership: {old_source.name} → {stock_entry.to_location.name}",
                    acknowledged=True,
                    acknowledged_by=request.user,
                    acknowledged_at=timezone.now()
                )
                
                accepted_instances_data.append({
                    'id': instance.id,
                    'instance_code': instance.instance_code,
                    'receipt_entry': acceptance_receipt.entry_number,
                    'status': 'IN_STORE',
                    'qr_code': instance.qr_code_data
                })
            
            # Update inventory for receiver
            if stock_entry.to_location.is_store:
                inv, _ = LocationInventory.objects.get_or_create(
                    location=stock_entry.to_location,
                    item=stock_entry.item
                )
                inv.update_quantities()
        
        # ========== PROCESS REJECTED INSTANCES ==========
        # ✅ CREATE RETURN ENTRY (PENDING_ACK) - Waiting for sender acknowledgment
        return_entry = None
        rejected_instances_data = []
        
        if rejected_ids:
            rejected_instances = ItemInstance.objects.filter(id__in=rejected_ids)
            
            # ✅ CREATE RETURN ENTRY (NOT RECEIPT YET)
            return_entry = StockEntry.objects.create(
                entry_type='RETURN',  # Type: RETURN
                from_location=stock_entry.to_location,      # From receiver (Physics Lab)
                to_location=stock_entry.from_location,      # To sender (CS Main Store)
                item=stock_entry.item,
                quantity=len(rejected_ids),
                purpose=f"Return rejected items from transfer {stock_entry.entry_number}",
                remarks=f"✗ {len(rejected_ids)} instances REJECTED. Reason: {rejection_reason}",
                reference_entry=stock_entry,
                status='PENDING_ACK',  # ⚠️ WAITING for sender to acknowledge
                requires_acknowledgment=True,
                created_by=request.user
            )
            return_entry.instances.set(rejected_instances)
            
            # Keep instances IN_TRANSIT (going back to sender)
            for instance in rejected_instances:
                instance.current_location = stock_entry.from_location  # Route back to sender
                instance.current_status = InstanceStatus.IN_TRANSIT   # ⚠️ Still IN_TRANSIT
                instance.previous_status = InstanceStatus.IN_TRANSIT
                instance.status_changed_by = request.user
                instance.status_changed_at = timezone.now()
                instance.condition_notes = f"REJECTED by {stock_entry.to_location.name}: {rejection_reason}"
                # source_location UNCHANGED (still owned by sender)
                instance.save()
                
                instance.generate_qr_code()
                instance.save()
                
                InstanceMovement.objects.create(
                    instance=instance,
                    stock_entry=return_entry,
                    from_location=stock_entry.to_location,
                    to_location=stock_entry.from_location,
                    previous_status=InstanceStatus.IN_TRANSIT,
                    new_status=InstanceStatus.IN_TRANSIT,
                    movement_type='RETURN',
                    moved_by=request.user,
                    remarks=f"✗ REJECTED: {rejection_reason}. Returning to {stock_entry.from_location.name} (awaiting sender acknowledgment)",
                    acknowledged=False,  # Not acknowledged yet
                    requires_acknowledgment=True
                )
                
                rejected_instances_data.append({
                    'id': instance.id,
                    'instance_code': instance.instance_code,
                    'return_entry': return_entry.entry_number,
                    'status': 'IN_TRANSIT',
                    'awaiting_sender_acknowledgment': True,
                    'rejection_reason': rejection_reason,
                    'qr_code': instance.qr_code_data
                })
        
        # Complete original transfer
        stock_entry.status = 'COMPLETED'
        stock_entry.acknowledged_by = request.user
        stock_entry.acknowledged_at = timezone.now()
        stock_entry.save()
        
        return Response({
            'success': True,
            'message': 'Acknowledgment processed',
            'accepted': {
                'count': len(accepted_ids),
                'receipt_entry': acceptance_receipt.entry_number if acceptance_receipt else None,
                'status': 'COMPLETED - Items IN_STORE at receiver',
                'instances': accepted_instances_data
            },
            'rejected': {
                'count': len(rejected_ids),
                'return_entry': return_entry.entry_number if return_entry else None,
                'status': 'PENDING - Items IN_TRANSIT back to sender',
                'awaiting_sender_acknowledgment': True,
                'instances': rejected_instances_data,
                'next_step': f'Sender ({stock_entry.from_location.name}) must acknowledge return'
            }
        })


    # ==================== STEP 2: SENDER ACKNOWLEDGES RETURN ====================

    @action(detail=True, methods=['post'])
    @transaction.atomic
    def acknowledge_return(self, request, pk=None):
        """
        STEP 2: Sender acknowledges receipt of RETURNED/REJECTED items.
        
        - Creates RECEIPT entry
        - Marks instances as IN_STORE
        - Updates inventory
        
        This is called by the ORIGINAL SENDER when rejected items come back.
        """
        return_entry = self.get_object()
        
        # Validate this is a RETURN entry
        if return_entry.entry_type != 'RETURN':
            return Response({
                'error': 'This endpoint is only for RETURN entries',
                'entry_type': return_entry.entry_type
            }, status=status.HTTP_400_BAD_REQUEST)
        
        if return_entry.status != 'PENDING_ACK':
            return Response({
                'error': 'Return is not pending acknowledgment',
                'current_status': return_entry.status
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Get instances to acknowledge
        acknowledged_ids = request.data.get('accepted_instances', [])
        
        if not acknowledged_ids:
            # If no specific IDs, acknowledge all
            acknowledged_ids = list(return_entry.instances.values_list('id', flat=True))
        
        if not acknowledged_ids:
            return Response({
                'error': 'No instances to acknowledge'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        acknowledged_instances = ItemInstance.objects.filter(id__in=acknowledged_ids)
        
        # ✅ CREATE RECEIPT ENTRY (Finally!)
        receipt_entry = StockEntry.objects.create(
            entry_type='RECEIPT',  # Now it's a RECEIPT
            from_location=return_entry.from_location,  # From receiver who rejected
            to_location=return_entry.to_location,      # To original sender
            item=return_entry.item,
            quantity=len(acknowledged_ids),
            purpose=f"Receipt of returned items from return {return_entry.entry_number}",
            remarks=f"✓ {len(acknowledged_ids)} rejected items received back by {request.user.get_full_name()}. Original rejection: {return_entry.remarks}",
            reference_entry=return_entry,
            status='COMPLETED',
            created_by=request.user,
            acknowledged_by=request.user,
            acknowledged_at=timezone.now()
        )
        receipt_entry.instances.set(acknowledged_instances)
        
        # Process each instance
        acknowledged_instances_data = []
        for instance in acknowledged_instances:
            # Mark as IN_STORE at sender location
            instance.current_location = return_entry.to_location
            instance.current_status = InstanceStatus.IN_STORE  # ✅ Finally IN_STORE
            instance.previous_status = InstanceStatus.IN_TRANSIT
            instance.status_changed_by = request.user
            instance.status_changed_at = timezone.now()
            # source_location stays UNCHANGED (was always owned by sender)
            instance.save()
            
            # Regenerate QR code
            instance.generate_qr_code()
            instance.save()
            
            # Create movement record
            InstanceMovement.objects.create(
                instance=instance,
                stock_entry=receipt_entry,  # Link to RECEIPT entry
                from_location=return_entry.from_location,
                to_location=return_entry.to_location,
                previous_status=InstanceStatus.IN_TRANSIT,
                new_status=InstanceStatus.IN_STORE,
                movement_type='RETURN',
                moved_by=request.user,
                remarks=f"✓ RETURN ACKNOWLEDGED - Rejected item received back at {return_entry.to_location.name}. Receipt: {receipt_entry.entry_number}",
                acknowledged=True,
                acknowledged_by=request.user,
                acknowledged_at=timezone.now()
            )
            
            acknowledged_instances_data.append({
                'id': instance.id,
                'instance_code': instance.instance_code,
                'receipt_entry': receipt_entry.entry_number,
                'status': 'IN_STORE',
                'location': return_entry.to_location.name,
                'qr_code': instance.qr_code_data
            })
            
            # Log activity
            UserActivity.objects.create(
                user=request.user,
                action='ACKNOWLEDGE_TRANSFER',
                model='ItemInstance',
                object_id=instance.id,
                details={
                    'instance_code': instance.instance_code,
                    'action': 'RETURN_ACKNOWLEDGED',
                    'receipt_entry': receipt_entry.entry_number,
                    'location': return_entry.to_location.name,
                    'original_rejection': return_entry.remarks
                }
            )
        
        # Mark return entry as COMPLETED
        return_entry.status = 'COMPLETED'
        return_entry.acknowledged_by = request.user
        return_entry.acknowledged_at = timezone.now()
        return_entry.save()
        
        # ✅ UPDATE INVENTORY for sender
        if return_entry.to_location.is_store:
            inv, created = LocationInventory.objects.get_or_create(
                location=return_entry.to_location,
                item=return_entry.item
            )
            inv.update_quantities()
            
            # Log inventory update
            UserActivity.objects.create(
                user=request.user,
                action='STOCK_ENTRY',
                model='LocationInventory',
                object_id=inv.id,
                details={
                    'location': return_entry.to_location.name,
                    'item': return_entry.item.name,
                    'quantity_change': f'+{len(acknowledged_ids)}',
                    'reason': 'Rejected items returned and acknowledged',
                    'receipt_entry': receipt_entry.entry_number,
                    'return_entry': return_entry.entry_number
                }
            )
        
        return Response({
            'success': True,
            'message': 'Return acknowledged successfully',
            'return_entry': return_entry.entry_number,
            'receipt_entry': receipt_entry.entry_number,
            'instances_acknowledged': len(acknowledged_ids),
            'location': return_entry.to_location.name,
            'instances': acknowledged_instances_data,
            'inventory_updated': True,
            'summary': {
                'status': 'COMPLETED',
                'instances_now_in_store': len(acknowledged_ids),
                'available_for_reuse': True
            }
        })


    # ==================== HELPER ENDPOINTS ====================

    @action(detail=False, methods=['get'])
    def pending_returns(self, request):
        """
        Get all RETURN entries pending acknowledgment by sender.
        Shows rejected items waiting to be acknowledged.
        """
        if not hasattr(request.user, 'profile'):
            return Response({'error': 'Profile not found'}, status=404)
        
        profile = request.user.profile
        
        # Get stores user manages (sender stores)
        accessible_stores = profile.get_accessible_stores()
        
        # Get RETURN entries TO these stores (rejected items coming back)
        pending_returns = StockEntry.objects.filter(
            entry_type='RETURN',
            status='PENDING_ACK',
            to_location__in=accessible_stores  # Returns TO sender
        ).select_related(
            'item', 'from_location', 'to_location', 'created_by', 'reference_entry'
        ).prefetch_related('instances')
        
        returns_data = []
        for return_entry in pending_returns:
            instances = return_entry.instances.all()
            
            # Get original transfer details
            original_transfer = return_entry.reference_entry
            
            return_data = {
                'id': return_entry.id,
                'return_entry_number': return_entry.entry_number,
                'entry_date': return_entry.entry_date,
                'from_location': {
                    'id': return_entry.from_location.id,
                    'name': return_entry.from_location.name,
                    'code': return_entry.from_location.code
                },
                'to_location': {
                    'id': return_entry.to_location.id,
                    'name': return_entry.to_location.name,
                    'code': return_entry.to_location.code
                },
                'item': {
                    'id': return_entry.item.id,
                    'name': return_entry.item.name,
                    'code': return_entry.item.code
                },
                'quantity': return_entry.quantity,
                'rejection_reason': return_entry.remarks,
                'original_transfer': original_transfer.entry_number if original_transfer else None,
                'rejected_by': return_entry.created_by.get_full_name() if return_entry.created_by else None,
                'instances': [
                    {
                        'id': inst.id,
                        'instance_code': inst.instance_code,
                        'current_status': inst.current_status,
                        'condition': inst.condition,
                        'condition_notes': inst.condition_notes,
                        'qr_code': inst.qr_code_data
                    }
                    for inst in instances
                ],
                'awaiting_acknowledgment_since': return_entry.created_at,
                'days_pending': (timezone.now() - return_entry.created_at).days,
                'action_required': f'Acknowledge receipt at {return_entry.to_location.name}'
            }
            returns_data.append(return_data)
        
        return Response({
            'user_role': profile.role,
            'stores': LocationMinimalSerializer(accessible_stores, many=True).data,
            'pending_returns_count': len(returns_data),
            'pending_returns': returns_data,
            'instructions': 'Use acknowledge_return endpoint to confirm receipt of returned items'
        })

# ==================== ITEM INSTANCE VIEWSET ====================
class ItemInstanceViewSet(viewsets.ModelViewSet):
    queryset = ItemInstance.objects.all()
    serializer_class = ItemInstanceSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if not hasattr(user, 'profile'):
            return queryset.none()
        
        profile = user.profile
        
        if profile.role == UserRole.STOCK_INCHARGE:
            accessible_stores = profile.get_accessible_stores()
            queryset = queryset.filter(source_location__in=accessible_stores)
        elif profile.role == UserRole.LOCATION_HEAD:
            accessible_locations = profile.get_accessible_locations()
            queryset = queryset.filter(
                Q(source_location__in=accessible_locations) |
                Q(current_location__in=accessible_locations)
            ).distinct()
        
        # Filters
        location = self.request.query_params.get('location')
        item = self.request.query_params.get('item')
        status_filter = self.request.query_params.get('status')
        search = self.request.query_params.get('search')
        source_location = self.request.query_params.get('source_location')
        instance_code = self.request.query_params.get('instance_code')
        
        if location:
            queryset = queryset.filter(current_location_id=location)
        if source_location:
            queryset = queryset.filter(source_location_id=source_location)
        if item:
            queryset = queryset.filter(item_id=item)
        if status_filter:
            queryset = queryset.filter(current_status=status_filter)
        if instance_code:
            queryset = queryset.filter(instance_code__icontains=instance_code)
        if search:
            queryset = queryset.filter(
                Q(instance_code__icontains=search) |
                Q(assigned_to__icontains=search) |
                Q(item__name__icontains=search)
            )
        
        return queryset.select_related('item', 'current_location', 'source_location')
    
    @action(detail=True, methods=['get'])
    def scan_qr(self, request, pk=None):
        """
        Scan individual instance QR code and get comprehensive real-time information.
        This is the PRIMARY endpoint for QR code scanning.
        """
        instance = self.get_object()
        
        # Get complete movement history
        movements = InstanceMovement.objects.filter(
            instance=instance
        ).select_related(
            'from_location', 'to_location', 'moved_by', 'acknowledged_by', 'stock_entry'
        ).order_by('-moved_at')
        
        movements_data = []
        for movement in movements:
            movements_data.append({
                'id': movement.id,
                'date': movement.moved_at,
                'from_location': {
                    'id': movement.from_location.id if movement.from_location else None,
                    'name': movement.from_location.name if movement.from_location else None,
                    'type': movement.from_location.location_type if movement.from_location else None
                },
                'to_location': {
                    'id': movement.to_location.id if movement.to_location else None,
                    'name': movement.to_location.name if movement.to_location else None,
                    'type': movement.to_location.location_type if movement.to_location else None
                },
                'previous_status': movement.previous_status,
                'previous_status_display': movement.get_previous_status_display(),
                'new_status': movement.new_status,
                'new_status_display': movement.get_new_status_display(),
                'movement_type': movement.movement_type,
                'movement_type_display': movement.get_movement_type_display(),
                'moved_by': movement.moved_by.get_full_name() if movement.moved_by else 'System',
                'remarks': movement.remarks,
                'acknowledged': movement.acknowledged,
                'acknowledged_by': movement.acknowledged_by.get_full_name() if movement.acknowledged_by else None,
                'acknowledged_at': movement.acknowledged_at,
                'is_upward_transfer': movement.is_upward_transfer,
                'stock_entry_number': movement.stock_entry.entry_number if movement.stock_entry else None
            })
        
        # Check for pending transfers
        pending_transfer = None
        if instance.current_status == InstanceStatus.IN_TRANSIT:
            pending = StockEntry.objects.filter(
                instances=instance,
                status='PENDING_ACK'
            ).select_related('from_location', 'to_location', 'item', 'created_by').first()
            
            if pending:
                pending_transfer = {
                    'id': pending.id,
                    'entry_number': pending.entry_number,
                    'entry_type': pending.entry_type,
                    'from_location': {
                        'id': pending.from_location.id,
                        'name': pending.from_location.name,
                        'code': pending.from_location.code
                    },
                    'to_location': {
                        'id': pending.to_location.id,
                        'name': pending.to_location.name,
                        'code': pending.to_location.code
                    },
                    'created_by': pending.created_by.get_full_name() if pending.created_by else None,
                    'created_at': pending.created_at,
                    'days_pending': (timezone.now() - pending.created_at).days,
                    'is_upward_transfer': pending.is_upward_transfer,
                    'awaiting_acknowledgment': True
                }
        
        # Get lifecycle summary
        lifecycle = {
            'created': {
                'date': instance.created_at,
                'by': instance.created_by.get_full_name() if instance.created_by else None,
                'location': instance.source_location.name,
                'inspection_certificate': instance.inspection_certificate.certificate_no if instance.inspection_certificate else None
            },
            'current': {
                'status': instance.current_status,
                'status_display': instance.get_current_status_display(),
                'location': instance.current_location.name,
                'location_full_path': instance.current_location.get_full_path(),
                'owner': instance.source_location.name,
                'condition': instance.condition,
                'condition_display': instance.get_condition_display(),
                'last_updated': instance.updated_at
            },
            'statistics': {
                'total_movements': movements.count(),
                'days_since_creation': (timezone.now().date() - instance.created_at.date()).days,
                'is_available': instance.is_available(),
                'is_in_transit': instance.is_in_transit(),
                'is_issued': instance.is_issued(),
                'is_overdue': instance.is_overdue()
            }
        }
        
        # Assignment details if applicable
        assignment = None
        if instance.assigned_to:
            assignment = {
                'assigned_to': instance.assigned_to,
                'assigned_date': instance.assigned_date,
                'expected_return_date': instance.expected_return_date,
                'actual_return_date': instance.actual_return_date,
                'is_overdue': instance.is_overdue(),
                'days_since_assigned': (timezone.now().date() - instance.assigned_date.date()).days if instance.assigned_date else None
            }
        
        # Damage/Repair history if applicable
        maintenance = None
        if instance.current_status in [InstanceStatus.DAMAGED, InstanceStatus.UNDER_REPAIR]:
            maintenance = {
                'damage_reported_date': instance.damage_reported_date,
                'damage_description': instance.damage_description,
                'repair_started_date': instance.repair_started_date,
                'repair_completed_date': instance.repair_completed_date,
                'repair_cost': float(instance.repair_cost) if instance.repair_cost else None,
                'repair_vendor': instance.repair_vendor
            }
        
        return Response({
            'instance': {
                'id': instance.id,
                'instance_code': instance.instance_code,
                'item': {
                    'id': instance.item.id,
                    'name': instance.item.name,
                    'code': instance.item.code,
                    'category': instance.item.category.name if instance.item.category else None,
                    'specifications': instance.item.specifications
                },
                'qr_code_image': instance.qr_code_data,
                'qr_info': instance.get_qr_info(),
                'qr_generated': instance.qr_generated
            },
            'lifecycle': lifecycle,
            'pending_transfer': pending_transfer,
            'assignment': assignment,
            'maintenance': maintenance,
            'movement_history': movements_data,
            'scan_time': timezone.now()
        })
    
    @action(detail=True, methods=['post'])
    def regenerate_qr(self, request, pk=None):
        """
        Manually regenerate QR code for an instance.
        Useful when data is updated.
        """
        instance = self.get_object()
        
        # Regenerate QR code with latest data
        instance.generate_qr_code()
        instance.save()
        
        # Log activity
        UserActivity.objects.create(
            user=request.user,
            action='GENERATE_QR_CODE',
            model='ItemInstance',
            object_id=instance.id,
            details={
                'instance_code': instance.instance_code,
                'current_status': instance.current_status,
                'current_location': instance.current_location.name,
                'regenerated_at': timezone.now().isoformat()
            }
        )
        
        return Response({
            'message': 'QR code regenerated successfully',
            'instance_code': instance.instance_code,
            'qr_code_image': instance.qr_code_data,
            'qr_info': instance.get_qr_info(),
            'updated_at': instance.updated_at
        })
    
    @action(detail=False, methods=['post'])
    def scan_by_code(self, request):
        """
        Scan instance by QR code string or instance code.
        For mobile scanning apps.
        """
        code = request.data.get('code') or request.data.get('instance_code')
        
        if not code:
            return Response({
                'error': 'Code parameter required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Try to find instance by code
        try:
            instance = ItemInstance.objects.get(instance_code=code)
            
            # Use the scan_qr method to get full details
            request_with_pk = type('obj', (object,), {'user': request.user})
            self.kwargs = {'pk': instance.pk}
            
            return self.scan_qr(request, pk=instance.pk)
            
        except ItemInstance.DoesNotExist:
            return Response({
                'error': f'No instance found with code: {code}',
                'suggestions': 'Please check the code and try again'
            }, status=status.HTTP_404_NOT_FOUND)
    
    @action(detail=False, methods=['get'])
    def available_instances(self, request):
        """
        Get all available instances (IN_STORE status) for a location.
        Used when creating transfers.
        """
        location_id = request.query_params.get('location')
        item_id = request.query_params.get('item')
        
        if not location_id:
            return Response({
                'error': 'location parameter required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        queryset = self.get_queryset().filter(
            current_location_id=location_id,
            current_status=InstanceStatus.IN_STORE
        )
        
        if item_id:
            queryset = queryset.filter(item_id=item_id)
        
        instances_data = []
        for instance in queryset:
            instances_data.append({
                'id': instance.id,
                'instance_code': instance.instance_code,
                'item': {
                    'id': instance.item.id,
                    'name': instance.item.name,
                    'code': instance.item.code
                },
                'condition': instance.condition,
                'condition_display': instance.get_condition_display(),
                'purchase_date': instance.purchase_date,
                'warranty_expiry': instance.warranty_expiry,
                'qr_code': instance.qr_code_data,
                'source_location': instance.source_location.name
            })
        
        return Response({
            'location_id': location_id,
            'item_id': item_id,
            'available_count': len(instances_data),
            'instances': instances_data
        })
    
    @action(detail=False, methods=['get'])
    def in_transit_instances(self, request):
        """
        Get all instances currently in transit.
        Shows pending transfers.
        """
        if not hasattr(request.user, 'profile'):
            return Response({'error': 'Profile not found'}, status=404)
        
        profile = request.user.profile
        accessible_locations = profile.get_accessible_locations()
        
        # Get instances in transit related to user's locations
        in_transit = self.get_queryset().filter(
            current_status=InstanceStatus.IN_TRANSIT
        ).filter(
            Q(source_location__in=accessible_locations) |
            Q(current_location__in=accessible_locations)
        ).distinct()
        
        instances_data = []
        for instance in in_transit:
            # Find pending transfer
            pending_transfer = StockEntry.objects.filter(
                instances=instance,
                status='PENDING_ACK'
            ).select_related('from_location', 'to_location').first()
            
            instances_data.append({
                'id': instance.id,
                'instance_code': instance.instance_code,
                'item': {
                    'id': instance.item.id,
                    'name': instance.item.name,
                    'code': instance.item.code
                },
                'from_location': pending_transfer.from_location.name if pending_transfer else None,
                'to_location': pending_transfer.to_location.name if pending_transfer else None,
                'transfer_entry': pending_transfer.entry_number if pending_transfer else None,
                'days_in_transit': (timezone.now() - instance.status_changed_at).days if instance.status_changed_at else None,
                'qr_code': instance.qr_code_data
            })
        
        return Response({
            'in_transit_count': len(instances_data),
            'instances': instances_data
        })
    
    @action(detail=True, methods=['get'])
    def print_qr_label(self, request, pk=None):
        """
        Get QR code with printable label format.
        Includes instance details for physical labels.
        """
        instance = self.get_object()
        
        label_data = {
            'qr_code_image': instance.qr_code_data,
            'instance_code': instance.instance_code,
            'item_name': instance.item.name,
            'item_code': instance.item.code,
            'category': instance.item.category.name if instance.item.category else None,
            'current_location': instance.current_location.name,
            'owner': instance.source_location.name,
            'condition': instance.get_condition_display(),
            'status': instance.get_current_status_display(),
            'purchase_date': instance.purchase_date,
            'warranty_expiry': instance.warranty_expiry,
            'created_at': instance.created_at,
            'label_format': {
                'width': '4 inches',
                'height': '2 inches',
                'qr_size': '1.5 inches',
                'font_size': '10pt'
            }
        }
        
        return Response(label_data)


# ==================== LOCATION INVENTORY VIEWSET ====================
class LocationInventoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = LocationInventory.objects.all()
    serializer_class = LocationInventorySerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if not hasattr(user, 'profile'):
            return queryset.none()
        
        profile = user.profile
        
        # Filter inventory based on user role
        if profile.role == UserRole.SYSTEM_ADMIN or profile.role == UserRole.AUDITOR:
            # System admin and auditor can see all inventory
            pass
        elif profile.role == UserRole.LOCATION_HEAD:
            # Location head sees inventory of stores in their hierarchy
            accessible_stores = self._get_hierarchy_stores(profile)
            queryset = queryset.filter(location__in=accessible_stores)
        elif profile.role == UserRole.STOCK_INCHARGE:
            # Stock incharge sees only their assigned stores
            accessible_stores = profile.get_accessible_stores()
            queryset = queryset.filter(location__in=accessible_stores)
        else:
            queryset = queryset.none()
        
        # Apply filters
        location = self.request.query_params.get('location')
        item = self.request.query_params.get('item')
        
        if location:
            queryset = queryset.filter(location_id=location)
        if item:
            queryset = queryset.filter(item_id=item)
        
        return queryset.select_related('location', 'item')
    
    def _get_hierarchy_stores(self, profile):
        """Get all stores in the location head's hierarchy"""
        stores = []
        
        for location in profile.assigned_locations.all():
            if location.is_standalone:
                # Get main store
                if location.auto_created_store:
                    stores.append(location.auto_created_store)
                
                # Get all descendant stores
                descendants = location.get_descendants(include_self=False)
                descendant_stores = descendants.filter(is_store=True, is_active=True)
                stores.extend(list(descendant_stores))
                
                # For root location, also get stores of immediate standalone children
                if location.parent_location is None:
                    standalone_children = location.get_standalone_children()
                    for child in standalone_children:
                        if child.auto_created_store:
                            stores.append(child.auto_created_store)
                        child_stores = child.get_descendants(include_self=False).filter(
                            is_store=True, is_active=True
                        )
                        stores.extend(list(child_stores))
        
        # Remove duplicates
        store_ids = list(set([s.id for s in stores]))
        return Location.objects.filter(id__in=store_ids)
    
    @action(detail=False, methods=['get'])
    def my_inventory(self, request):
        """Get inventory based on user's role and location access"""
        if not hasattr(request.user, 'profile'):
            return Response({'error': 'Profile not found'}, status=404)
        
        profile = request.user.profile
        
        # Get stores based on role
        if profile.role == UserRole.LOCATION_HEAD:
            stores = self._get_hierarchy_stores(profile)
        elif profile.role == UserRole.STOCK_INCHARGE:
            stores = profile.get_accessible_stores()
        elif profile.role in [UserRole.SYSTEM_ADMIN, UserRole.AUDITOR]:
            stores = Location.objects.filter(is_store=True, is_active=True)
        else:
            stores = Location.objects.none()
        
        inventory = LocationInventory.objects.filter(
            location__in=stores
        ).select_related('location', 'item')
        
        # Group by location
        inventory_by_location = {}
        for inv in inventory:
            loc_id = inv.location.id
            if loc_id not in inventory_by_location:
                inventory_by_location[loc_id] = {
                    'location': LocationMinimalSerializer(inv.location).data,
                    'items': []
                }
            inventory_by_location[loc_id]['items'].append(
                LocationInventorySerializer(inv).data
            )
        
        return Response({
            'user_role': profile.role,
            'stores_count': stores.count(),
            'inventory_by_location': list(inventory_by_location.values())
        })

# ==================== USER ACTIVITY VIEWSET ====================
class UserActivityViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = UserActivity.objects.all()
    serializer_class = UserActivitySerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if not hasattr(user, 'profile'):
            return queryset.none()
        
        if user.profile.role == UserRole.SYSTEM_ADMIN:
            return queryset
        
        return queryset.filter(user=user)