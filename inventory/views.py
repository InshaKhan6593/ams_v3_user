# views.py - ENHANCED VERSION (Part 1: Auth & User Management)
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
                }
                
                # Log activity
                UserActivity.objects.create(
                    user=user,
                    action='LOGIN',
                    model='User',
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

# views.py - ENHANCED VERSION (Part 2: Location ViewSet)
# This continues from part 1

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
        """Get locations this store can issue to (including upward for main stores)"""
        location = self.get_object()
        
        if not location.is_store:
            return Response({
                'error': 'Only stores can have issuance targets'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Get all locations in same hierarchy
        parent_standalone = location.get_parent_standalone()
        if not parent_standalone:
            return Response({
                'targets': [],
                'can_issue_upward': False
            })
        
        targets = parent_standalone.get_descendants(include_self=True)
        
        # Check if can issue upward
        can_issue_upward = location.can_issue_to_parent_standalone()
        upward_target = None
        if can_issue_upward:
            upward_target = location.get_parent_standalone_for_issuance()
        
        response_data = {
            'location': LocationMinimalSerializer(location).data,
            'parent_standalone': LocationMinimalSerializer(parent_standalone).data,
            'hierarchy_targets': LocationMinimalSerializer(targets, many=True).data,
            'can_issue_upward': can_issue_upward,
            'upward_target': LocationMinimalSerializer(upward_target).data if upward_target else None
        }
        
        return Response(response_data)

# Continuing with remaining viewsets...

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
            # Get departments where user's store is the main store
            accessible_stores = profile.get_accessible_stores()
            department_ids = set()
            
            for store in accessible_stores:
                if store.is_main_store and store.parent_location:
                    # This is a main store, get its parent standalone
                    parent_standalone = store.get_parent_standalone()
                    if parent_standalone:
                        department_ids.add(parent_standalone.id)
            
            if department_ids:
                queryset = queryset.filter(department_id__in=department_ids)
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
        
        if not profile.can_create_inspection_certificates():
            raise PermissionDenied("Only Location Heads of standalone locations can create inspection certificates")
        
        department = serializer.validated_data.get('department')
        if department and not profile.has_location_access(department):
            raise PermissionDenied("You don't have access to this department")
        
        main_store = department.get_main_store()
        if not main_store:
            raise PermissionDenied(f"Department {department.name} does not have a main store configured")
        
        serializer.save()
    
    @action(detail=True, methods=['post'])
    @transaction.atomic
    def submit_to_stock_incharge(self, request, pk=None):
        """Location Head submits certificate to Stock Incharge of main store"""
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
        
        inspection.transition_stage('STOCK_DETAILS', request.user)
        
        return Response({
            'message': f'Certificate submitted to Stock Incharge of {main_store.name}',
            'new_stage': inspection.stage,
            'main_store': main_store.name,
            'certificate': InspectionCertificateSerializer(inspection, context={'request': request}).data
        })
    
    @action(detail=True, methods=['post'])
    @transaction.atomic
    def submit_stock_details(self, request, pk=None):
        """Stock Incharge submits stock details"""
        inspection = self.get_object()
        
        if inspection.stage != 'STOCK_DETAILS':
            return Response({
                'error': f'Certificate must be in STOCK_DETAILS stage, currently in {inspection.stage}'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Update with submitted data
        if request.data:
            serializer = self.get_serializer(inspection, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
        
        items_count = inspection.inspection_items.count()
        if items_count == 0:
            return Response({
                'error': 'At least one inspection item is required. Please add items first.',
            }, status=status.HTTP_400_BAD_REQUEST)
        
        inspection.transition_stage('AUDIT_REVIEW', request.user)
        
        return Response({
            'message': f'Stock details with {items_count} items submitted successfully',
            'new_stage': inspection.stage,
            'items_count': items_count,
            'certificate': InspectionCertificateSerializer(inspection, context={'request': request}).data
        })
    
    @action(detail=True, methods=['post'])
    @transaction.atomic
    def submit_audit_review(self, request, pk=None):
        """Auditor completes audit and creates stock entries in main store"""
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
        
        # Update audit details
        serializer = self.get_serializer(inspection, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        
        inspection.transition_stage('COMPLETED', request.user)
        
        # Create instances in main store
        created_instances_count = self._create_stock_from_inspection(inspection, request.user, main_store)
        
        return Response({
            'message': f'Audit completed successfully. Created {created_instances_count} instances in {main_store.name}.',
            'new_stage': inspection.stage,
            'instances_created': created_instances_count,
            'main_store': main_store.name,
            'certificate': InspectionCertificateSerializer(inspection, context={'request': request}).data
        })
    
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
        """Get options for creating stock entries with upward transfer support"""
        if not hasattr(request.user, 'profile'):
            return Response({'error': 'Profile not found'}, status=status.HTTP_404_NOT_FOUND)
        
        profile = request.user.profile
        
        # From locations: accessible stores
        from_locations = profile.get_accessible_stores()
        
        # To locations: all accessible locations
        accessible_locations = profile.get_accessible_locations()
        
        # Check if user is main store incharge (can issue upward)
        can_issue_upward = profile.can_issue_to_parent_standalone()
        parent_standalone_for_upward = profile.get_parent_standalone_for_issuance()
        
        # Get available items
        available_items = Item.objects.filter(
            instances__source_location__in=from_locations
        ).distinct()
        
        response_data = {
            'from_locations': LocationMinimalSerializer(from_locations, many=True).data,
            'to_locations': LocationMinimalSerializer(accessible_locations, many=True).data,
            'available_items': ItemMinimalSerializer(available_items, many=True).data,
            'user_role': profile.role,
            'can_issue_upward': can_issue_upward,
            'parent_standalone_for_upward': LocationMinimalSerializer(
                parent_standalone_for_upward
            ).data if parent_standalone_for_upward else None
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
        """Acknowledge receipt of stock transfer (including upward transfers)"""
        stock_entry = self.get_object()
        
        if stock_entry.status != 'PENDING_ACK':
            return Response({'error': 'Entry is not pending acknowledgment'}, status=status.HTTP_400_BAD_REQUEST)
        
        accepted_ids = request.data.get('accepted_instances', [])
        rejected_ids = request.data.get('rejected_instances', [])
        
        # Process accepted: Transfer ownership to destination
        accepted_instances = ItemInstance.objects.filter(id__in=accepted_ids)
        for instance in accepted_instances:
            instance.source_location = stock_entry.to_location  # Transfer ownership
            instance.current_location = stock_entry.to_location
            instance.current_status = InstanceStatus.IN_STORE
            instance.save()
        
        # Process rejected: Create return entry
        if rejected_ids:
            rejected_instances = ItemInstance.objects.filter(id__in=rejected_ids)
            return_entry = StockEntry.objects.create(
                entry_type='RECEIPT',
                from_location=stock_entry.to_location,
                to_location=stock_entry.from_location,
                item=stock_entry.item,
                quantity=len(rejected_ids),
                purpose=f"Rejected items from transfer {stock_entry.entry_number}",
                reference_entry=stock_entry,
                status='PENDING_ACK',
                created_by=request.user
            )
            return_entry.instances.set(rejected_instances)
        
        stock_entry.status = 'COMPLETED'
        stock_entry.acknowledged_by = request.user
        stock_entry.acknowledged_at = timezone.now()
        stock_entry.save()
        
        # Update inventories
        self._update_inventories(stock_entry)
        
        message = f'Receipt acknowledged successfully. Accepted: {len(accepted_ids)}'
        if rejected_ids:
            message += f', Rejected: {len(rejected_ids)} (returning to sender)'
        
        return Response({
            'message': message,
            'accepted_count': len(accepted_ids),
            'rejected_count': len(rejected_ids)
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
        
        if location:
            queryset = queryset.filter(current_location_id=location)
        if source_location:
            queryset = queryset.filter(source_location_id=source_location)
        if item:
            queryset = queryset.filter(item_id=item)
        if status_filter:
            queryset = queryset.filter(current_status=status_filter)
        if search:
            queryset = queryset.filter(
                Q(instance_code__icontains=search) |
                Q(assigned_to__icontains=search) |
                Q(item__name__icontains=search)
            )
        
        return queryset.select_related('item', 'current_location', 'source_location')

# ==================== LOCATION INVENTORY VIEWSET ====================
class LocationInventoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = LocationInventory.objects.all()
    serializer_class = LocationInventorySerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        location = self.request.query_params.get('location')
        item = self.request.query_params.get('item')
        
        if location:
            queryset = queryset.filter(location_id=location)
        if item:
            queryset = queryset.filter(item_id=item)
        
        return queryset

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