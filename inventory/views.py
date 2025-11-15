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
# Replace your CustomTokenObtainPairView in views.py with this:

class CustomTokenObtainPairView(TokenObtainPairView):
    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == 200:
            user = User.objects.get(username=request.data['username'])
            if hasattr(user, 'profile'):
                # Get accessible stores as full objects, not just IDs
                accessible_stores = user.profile.get_accessible_stores()
                
                response.data['user'] = {
                    'id': user.id,
                    'username': user.username,
                    'email': user.email,
                    'full_name': user.get_full_name() or user.username,
                    'role': user.profile.role,
                    'role_display': user.profile.get_role_display(),
                    'assigned_locations': list(user.profile.assigned_locations.values_list('id', flat=True)),
                    # FIXED: Return full store objects instead of just IDs
                    'accessible_stores': LocationMinimalSerializer(accessible_stores, many=True).data,
                    'can_create_users': user.profile.role in [UserRole.SYSTEM_ADMIN, UserRole.LOCATION_HEAD],
                    'can_create_locations': user.profile.role in [UserRole.SYSTEM_ADMIN, UserRole.LOCATION_HEAD, UserRole.STOCK_INCHARGE],
                    'can_create_items': user.profile.role in [UserRole.SYSTEM_ADMIN, UserRole.LOCATION_HEAD, UserRole.STOCK_INCHARGE]
                }
            
            UserActivity.objects.create(
                user=user,
                action='LOGIN',
                model='User',
                ip_address=self.get_client_ip(request)
            )
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
        """Get current user's permissions"""
        if not hasattr(request.user, 'profile'):
            return Response({'error': 'Profile not found'}, status=status.HTTP_404_NOT_FOUND)
        
        profile = request.user.profile
        
        return Response({
            'role': profile.role,
            'role_display': profile.get_role_display(),
            'can_create_users': profile.can_create_user(UserRole.STOCK_INCHARGE),
            'can_create_stock_incharge': profile.role in [UserRole.SYSTEM_ADMIN, UserRole.LOCATION_HEAD],
            'can_create_locations': profile.role in [UserRole.SYSTEM_ADMIN, UserRole.LOCATION_HEAD, UserRole.STOCK_INCHARGE],
            'can_create_items': profile.role in [UserRole.SYSTEM_ADMIN, UserRole.LOCATION_HEAD, UserRole.STOCK_INCHARGE],
            'accessible_locations': LocationMinimalSerializer(profile.get_accessible_locations(), many=True).data,
            'accessible_stores': LocationMinimalSerializer(profile.get_accessible_stores(), many=True).data,
            'is_global_viewer': profile.role in [UserRole.SYSTEM_ADMIN, UserRole.AUDITOR],
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
        my_locations = self.request.query_params.get('my_locations')
        
        if profile.role in [UserRole.SYSTEM_ADMIN, UserRole.AUDITOR]:
            pass
        elif my_locations == 'true':
            accessible_locations = profile.get_accessible_locations()
            assigned_location_ids = profile.assigned_locations.values_list('id', flat=True)
            queryset = queryset.filter(
                Q(id__in=accessible_locations.values_list('id', flat=True)) |
                Q(id__in=assigned_location_ids)
            ).distinct()
        
        # Filters
        location_type = self.request.query_params.get('type')
        is_store = self.request.query_params.get('is_store')
        is_standalone = self.request.query_params.get('is_standalone')
        parent_id = self.request.query_params.get('parent')
        search = self.request.query_params.get('search')
        
        if location_type:
            queryset = queryset.filter(location_type=location_type)
        if is_store is not None:
            queryset = queryset.filter(is_store=is_store.lower() == 'true')
        if is_standalone is not None:
            if is_standalone.lower() == 'true':
                queryset = queryset.filter(parent_location__isnull=True, is_store=False)
        if parent_id:
            queryset = queryset.filter(parent_location_id=parent_id)
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) |
                Q(code__icontains=search) |
                Q(address__icontains=search)
            )
        
        return queryset.select_related('parent_location')
    
    def perform_create(self, serializer):
        """Create location with permission checks"""
        if not hasattr(self.request.user, 'profile'):
            raise PermissionDenied("User profile required")
        
        profile = self.request.user.profile
        parent_location = serializer.validated_data.get('parent_location')
        
        if profile.role == UserRole.SYSTEM_ADMIN:
            serializer.save(created_by=self.request.user)
            UserActivity.objects.create(
                user=self.request.user,
                action='CREATE_LOCATION',
                model='Location',
                object_id=serializer.instance.id,
                details={
                    'location_name': serializer.instance.name,
                    'location_code': serializer.instance.code,
                    'parent': parent_location.name if parent_location else None
                }
            )
            return
        
        if profile.role == UserRole.LOCATION_HEAD:
            if parent_location:
                if not profile.has_location_access(parent_location):
                    raise PermissionDenied("You don't have access to this parent location")
            
            serializer.save(created_by=self.request.user)
            UserActivity.objects.create(
                user=self.request.user,
                action='CREATE_LOCATION',
                model='Location',
                object_id=serializer.instance.id,
                details={
                    'location_name': serializer.instance.name,
                    'location_code': serializer.instance.code,
                    'parent': parent_location.name if parent_location else None
                }
            )
            return
        
        if profile.role == UserRole.STOCK_INCHARGE:
            main_location = profile.get_main_location()
            
            if not main_location:
                raise PermissionDenied("No main location configured for your profile")
            
            if not parent_location:
                raise PermissionDenied("Stock Incharge must specify a parent location")
            
            if parent_location.id != main_location.id and not parent_location.is_descendant_of(main_location):
                raise PermissionDenied("You can only create locations under your main location hierarchy")
            
            serializer.save(created_by=self.request.user)
            UserActivity.objects.create(
                user=self.request.user,
                action='CREATE_LOCATION',
                model='Location',
                object_id=serializer.instance.id,
                details={
                    'location_name': serializer.instance.name,
                    'location_code': serializer.instance.code,
                    'parent': parent_location.name if parent_location else None,
                    'main_location': main_location.name
                }
            )
            return
        
        raise PermissionDenied("You don't have permission to create locations")
    
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
        children = location.child_locations.filter(is_active=True)
        serializer = LocationSerializer(children, many=True, context={'request': request})
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def inventory_summary(self, request, pk=None):
        """Get inventory summary for location"""
        location = self.get_object()
        
        if not location.is_store:
            return Response({'error': 'Only store locations have inventory'}, 
                          status=status.HTTP_400_BAD_REQUEST)
        
        inventory_items = LocationInventory.objects.filter(location=location)
        
        return Response({
            'location': LocationMinimalSerializer(location).data,
            'total_items': inventory_items.count(),
            'total_quantity': sum(inv.total_quantity for inv in inventory_items),
            'available_quantity': sum(inv.available_quantity for inv in inventory_items),
            'in_use_quantity': sum(inv.in_use_quantity for inv in inventory_items),
            'in_transit_quantity': sum(inv.in_transit_quantity for inv in inventory_items),
            'items': LocationInventorySerializer(inventory_items, many=True).data
        })
    
    @action(detail=False, methods=['get'])
    def standalone_locations(self, request):
        """
        Get all standalone locations (no parent, not stores)
        For assigning Location Heads
        """
        standalone = Location.objects.filter(
            parent_location__isnull=True,
            is_store=False,
            is_active=True
        ).order_by('name')
        
        serializer = LocationMinimalSerializer(standalone, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def store_locations(self, request):
        """
        Get all store locations
        For assigning Stock Incharge
        """
        user = request.user
        
        if not hasattr(user, 'profile'):
            return Response({'error': 'Profile not found'}, status=status.HTTP_404_NOT_FOUND)
        
        profile = user.profile
        
        # System Admin sees all stores
        if profile.role == UserRole.SYSTEM_ADMIN:
            stores = Location.objects.filter(is_store=True, is_active=True)
        # Location Head sees only their accessible stores
        elif profile.role == UserRole.LOCATION_HEAD:
            stores = profile.get_accessible_stores()
        else:
            stores = Location.objects.none()
        
        serializer = LocationMinimalSerializer(stores, many=True)
        return Response(serializer.data)


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
        
        if category:
            queryset = queryset.filter(category_id=category)
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
        
        profile = self.request.user.profile
        
        if profile.role not in [UserRole.SYSTEM_ADMIN, UserRole.LOCATION_HEAD, UserRole.STOCK_INCHARGE]:
            raise PermissionDenied("You don't have permission to create items")
        
        serializer.save(created_by=self.request.user)
        
        UserActivity.objects.create(
            user=self.request.user,
            action='CREATE_ITEM',
            model='Item',
            object_id=serializer.instance.id,
            details={
                'item_name': serializer.instance.name,
                'item_code': serializer.instance.code
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
            managed_stores = profile.assigned_locations.filter(is_store=True)
            department_ids = set()
            
            for store in managed_stores:
                if store.is_auto_created and store.parent_location:
                    department_ids.add(store.parent_location.id)
                
                departments_with_auto_store = Location.objects.filter(auto_created_store=store)
                department_ids.update(departments_with_auto_store.values_list('id', flat=True))
                
                if store.parent_location and not store.parent_location.is_store:
                    department_ids.add(store.parent_location.id)
            
            if department_ids:
                queryset = queryset.filter(department_id__in=department_ids)
            else:
                queryset = queryset.filter(
                    stage='STOCK_DETAILS',
                    department__auto_created_store__in=managed_stores
                ) if managed_stores.exists() else queryset.none()
        else:
            user_locations = profile.get_accessible_locations()
            assigned_locations = profile.assigned_locations.all()
            
            queryset = queryset.filter(
                Q(department__in=user_locations) |
                Q(department__in=assigned_locations)
            ).distinct()
        
        # Filters
        stage = self.request.query_params.get('stage')
        status_filter = self.request.query_params.get('status')
        department = self.request.query_params.get('department')
        my_tasks = self.request.query_params.get('my_tasks')
        
        if stage:
            queryset = queryset.filter(stage=stage)
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        if department:
            queryset = queryset.filter(department_id=department)
        
        if my_tasks == 'true':
            if profile.role == UserRole.LOCATION_HEAD:
                queryset = queryset.filter(
                    stage='INITIATED', 
                    department__in=profile.assigned_locations.all()
                )
            elif profile.role == UserRole.STOCK_INCHARGE:
                queryset = queryset.filter(stage='STOCK_DETAILS')
            elif profile.role == UserRole.AUDITOR:
                queryset = queryset.filter(stage='AUDIT_REVIEW')
        
        return queryset
    
    def perform_create(self, serializer):
        """Create inspection certificate"""
        if not hasattr(self.request.user, 'profile'):
            raise PermissionDenied("User profile required")
        
        profile = self.request.user.profile
        
        if profile.role not in [UserRole.LOCATION_HEAD, UserRole.SYSTEM_ADMIN]:
            raise PermissionDenied("Only Location Heads can initiate inspection certificates")
        
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
        """Location Head submits certificate to Stock Incharge"""
        inspection = self.get_object()
        
        if not hasattr(request.user, 'profile'):
            raise PermissionDenied("User profile required")
        
        profile = request.user.profile
        
        if profile.role not in [UserRole.LOCATION_HEAD, UserRole.SYSTEM_ADMIN]:
            raise PermissionDenied("Only Location Head can submit to Stock Incharge")
        
        if inspection.stage != 'INITIATED':
            return Response({
                'error': f'Certificate must be in INITIATED stage, currently in {inspection.stage}'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        if not profile.has_location_access(inspection.department):
            raise PermissionDenied("You don't have access to this department")
        
        main_store = inspection.get_main_store()
        if not main_store:
            return Response({
                'error': 'No main store found for this department'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        required_fields = ['contractor_name', 'contract_no', 'indenter', 'indent_no', 'date']
        missing_fields = []
        for field in required_fields:
            if not getattr(inspection, field):
                missing_fields.append(field)
        
        if missing_fields:
            return Response({
                'error': 'Please fill all required fields before submitting',
                'missing_fields': missing_fields
            }, status=status.HTTP_400_BAD_REQUEST)
        
        if request.data and len(request.data) > 0:
            update_data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)
            if 'inspection_items' in update_data:
                del update_data['inspection_items']
            
            if update_data:
                serializer = self.get_serializer(inspection, data=update_data, partial=True)
                serializer.is_valid(raise_exception=True)
                serializer.save()
        
        inspection.transition_stage('STOCK_DETAILS', request.user)
        
        UserActivity.objects.create(
            user=request.user,
            action='SUBMIT_TO_STOCK_INCHARGE',
            model='InspectionCertificate',
            object_id=inspection.id,
            details={
                'certificate_no': inspection.certificate_no,
                'new_stage': inspection.stage,
                'main_store': main_store.name
            }
        )
        
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
        
        if not hasattr(request.user, 'profile'):
            raise PermissionDenied("User profile required")
        
        profile = request.user.profile
        
        if profile.role not in [UserRole.STOCK_INCHARGE, UserRole.SYSTEM_ADMIN]:
            raise PermissionDenied("Only Stock Incharge can submit stock details")
        
        if inspection.stage != 'STOCK_DETAILS':
            return Response({
                'error': f'Certificate must be in STOCK_DETAILS stage, currently in {inspection.stage}'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        main_store = inspection.get_main_store()
        if not main_store:
            return Response({
                'error': 'No main store found for this department'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        if not profile.has_location_access(main_store):
            raise PermissionDenied("You don't have access to the main store for this department")
        
        if request.data and len(request.data) > 0:
            serializer = self.get_serializer(inspection, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
        
        items_count = inspection.inspection_items.count()
        if items_count == 0:
            return Response({
                'error': 'At least one inspection item is required. Please add items first.',
            }, status=status.HTTP_400_BAD_REQUEST)
        
        inspection.transition_stage('AUDIT_REVIEW', request.user)
        
        UserActivity.objects.create(
            user=request.user,
            action='SUBMIT_STOCK_DETAILS',
            model='InspectionCertificate',
            object_id=inspection.id,
            details={
                'certificate_no': inspection.certificate_no,
                'new_stage': inspection.stage,
                'main_store': main_store.name,
                'items_count': items_count
            }
        )
        
        return Response({
            'message': f'Stock details with {items_count} items submitted successfully',
            'new_stage': inspection.stage,
            'items_count': items_count,
            'certificate': InspectionCertificateSerializer(inspection, context={'request': request}).data
        })
    
    @action(detail=True, methods=['post'])
    @transaction.atomic
    def submit_audit_review(self, request, pk=None):
        """Auditor completes audit and creates stock entries"""
        inspection = self.get_object()
        
        if not hasattr(request.user, 'profile'):
            raise PermissionDenied("User profile required")
        
        profile = request.user.profile
        
        if profile.role not in [UserRole.AUDITOR, UserRole.SYSTEM_ADMIN]:
            raise PermissionDenied("Only Auditors can submit audit review")
        
        if inspection.stage != 'AUDIT_REVIEW':
            return Response({
                'error': f'Certificate must be in AUDIT_REVIEW stage, currently in {inspection.stage}'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        main_store = inspection.get_main_store()
        if not main_store:
            return Response({
                'error': 'No main store found for this department'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        serializer = self.get_serializer(inspection, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        
        inspection.transition_stage('COMPLETED', request.user)
        
        created_instances_count = self._create_stock_from_inspection(inspection, request.user, main_store)
        
        return Response({
            'message': f'Audit completed successfully. Created {created_instances_count} instances in {main_store.name}.',
            'new_stage': inspection.stage,
            'instances_created': created_instances_count,
            'main_store': main_store.name,
            'certificate': InspectionCertificateSerializer(inspection, context={'request': request}).data
        })
    
    @action(detail=True, methods=['post'])
    @transaction.atomic
    def reject(self, request, pk=None):
        """Reject inspection certificate"""
        inspection = self.get_object()
        reason = request.data.get('reason')
        
        if not reason:
            return Response({'error': 'Rejection reason required'}, status=status.HTTP_400_BAD_REQUEST)
        
        if not hasattr(request.user, 'profile'):
            raise PermissionDenied("User profile required")
        
        profile = request.user.profile
        
        if profile.role not in [UserRole.AUDITOR, UserRole.SYSTEM_ADMIN]:
            raise PermissionDenied("Only Auditors can reject certificates")
        
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
    
    def _create_stock_from_inspection(self, inspection, user, main_store):
        """Create stock entries and instances after audit approval"""
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
            
            for instance in created_instances:
                InstanceMovement.objects.create(
                    instance=instance,
                    stock_entry=receipt_entry,
                    from_location=None,
                    to_location=main_store,
                    previous_status='NEW',
                    new_status='IN_STORE',
                    moved_by=user,
                    remarks=f"Initial receipt from inspection {inspection.certificate_no}"
                )
            
            inv, created = LocationInventory.objects.get_or_create(
                location=main_store,
                item=insp_item.item
            )
            inv.update_quantities()
        
        return total_instances
    
    @action(detail=False, methods=['get'])
    def dashboard_stats(self, request):
        """Get dashboard statistics"""
        user = request.user
        if not hasattr(user, 'profile'):
            return Response({'error': 'Profile not found'}, status=status.HTTP_404_NOT_FOUND)
        
        profile = user.profile
        queryset = self.get_queryset()
        
        stats = {
            'total': queryset.count(),
            'initiated': queryset.filter(stage='INITIATED').count(),
            'stock_details': queryset.filter(stage='STOCK_DETAILS').count(),
            'audit_review': queryset.filter(stage='AUDIT_REVIEW').count(),
            'completed': queryset.filter(stage='COMPLETED').count(),
            'rejected': queryset.filter(stage='REJECTED').count(),
        }
        
        if profile.role == UserRole.LOCATION_HEAD:
            stats['my_pending'] = queryset.filter(
                stage='INITIATED',
                department__in=profile.assigned_locations.all()
            ).count()
        elif profile.role == UserRole.STOCK_INCHARGE:
            stats['my_pending'] = queryset.filter(stage='STOCK_DETAILS').count()
        elif profile.role == UserRole.AUDITOR:
            stats['my_pending'] = queryset.filter(stage='AUDIT_REVIEW').count()
        else:
            stats['my_pending'] = 0
        
        return Response(stats)


# ==================== ITEM INSTANCE VIEWSET ====================
# ==================== ITEM INSTANCE VIEWSET ====================
class ItemInstanceViewSet(viewsets.ModelViewSet):
    queryset = ItemInstance.objects.all()
    serializer_class = ItemInstanceSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        # Apply role-based filtering
        if not hasattr(user, 'profile'):
            return queryset.none()
        
        profile = user.profile
        
        if profile.role == UserRole.STOCK_INCHARGE:
            # Stock Incharge can only see instances from their accessible stores
            accessible_stores = profile.get_accessible_stores()
            queryset = queryset.filter(source_location__in=accessible_stores)
        elif profile.role == UserRole.LOCATION_HEAD:
            # Location Head can see instances from their accessible locations
            accessible_locations = profile.get_accessible_locations()
            queryset = queryset.filter(
                Q(source_location__in=accessible_locations) |
                Q(current_location__in=accessible_locations)
            ).distinct()
        # SYSTEM_ADMIN and AUDITOR can see all instances (no additional filtering)
        
        # Filters
        location = self.request.query_params.get('location')
        item = self.request.query_params.get('item')
        status_filter = self.request.query_params.get('status')
        search = self.request.query_params.get('search')
        qr_scan = self.request.query_params.get('qr_scan')
        available_only = self.request.query_params.get('available_only')
        overdue = self.request.query_params.get('overdue')
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
                Q(item__name__icontains=search) |
                Q(item__code__icontains=search)
            )
        if qr_scan:
            queryset = queryset.filter(instance_code=qr_scan)
        if available_only == 'true':
            queryset = queryset.filter(current_status=InstanceStatus.IN_STORE)
        if overdue == 'true':
            queryset = queryset.filter(
                current_status=InstanceStatus.TEMPORARY_ISSUED,
                expected_return_date__lt=timezone.now().date(),
                actual_return_date__isnull=True
            )
        
        # Include related data for better performance
        return queryset.select_related(
            'item', 
            'current_location', 
            'source_location', 
            'inspection_certificate',
            'inspection_certificate__department',
            'status_changed_by',
            'created_by'
        ).prefetch_related('stock_entries')
    
    def retrieve(self, request, *args, **kwargs):
        """Get single instance with full details"""
        instance = self.get_object()
        
        # Check access permissions
        if not self._check_instance_access(request.user, instance):
            return Response(
                {'error': 'You do not have permission to access this instance'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        serializer = self.get_serializer(instance)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def movement_history(self, request, pk=None):
        """Get movement history for a specific instance"""
        instance = self.get_object()
        
        # Check access permissions
        if not self._check_instance_access(request.user, instance):
            return Response(
                {'error': 'You do not have permission to access this instance'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        movements = InstanceMovement.objects.filter(instance=instance).select_related(
            'from_location', 'to_location', 'moved_by', 'acknowledged_by'
        ).order_by('-moved_at')
        
        serializer = InstanceMovementSerializer(movements, many=True)
        
        return Response({
            'instance': self.get_serializer(instance).data,
            'movements': serializer.data,
            'total_movements': movements.count()
        })
    
    @action(detail=True, methods=['get'])
    def inspection_details(self, request, pk=None):
        """Get detailed inspection certificate information for this instance"""
        instance = self.get_object()
        
        # Check access permissions
        if not self._check_instance_access(request.user, instance):
            return Response(
                {'error': 'You do not have permission to access this instance'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        if not instance.inspection_certificate:
            return Response({
                'message': 'No inspection certificate associated with this instance',
                'inspection_certificate': None,
                'inspection_items': []
            })
        
        # Get full inspection certificate details
        inspection = instance.inspection_certificate
        inspection_serializer = InspectionCertificateSerializer(
            inspection, 
            context={'request': request}
        )
        
        # Get inspection items for this item
        inspection_items = InspectionItem.objects.filter(
            inspection_certificate=inspection,
            item=instance.item
        )
        items_serializer = InspectionItemSerializer(inspection_items, many=True)
        
        return Response({
            'inspection_certificate': inspection_serializer.data,
            'inspection_items': items_serializer.data,
            'total_items_count': inspection_items.count(),
            'total_accepted': sum(item.accepted_quantity for item in inspection_items),
            'total_rejected': sum(item.rejected_quantity for item in inspection_items)
        })
    
    @action(detail=True, methods=['get'])
    def stock_entries(self, request, pk=None):
        """Get all stock entries related to this instance"""
        instance = self.get_object()
        
        # Check access permissions
        if not self._check_instance_access(request.user, instance):
            return Response(
                {'error': 'You do not have permission to access this instance'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        stock_entries = instance.stock_entries.all().select_related(
            'from_location', 'to_location', 'item', 'created_by'
        ).order_by('-entry_date')
        
        serializer = StockEntrySerializer(stock_entries, many=True, context={'request': request})
        
        return Response({
            'instance': self.get_serializer(instance).data,
            'stock_entries': serializer.data,
            'total_entries': stock_entries.count()
        })
    
    @action(detail=True, methods=['post'])
    def generate_qr_code(self, request, pk=None):
        """Force generate QR code for instance"""
        instance = self.get_object()
        
        # Check access permissions
        if not self._check_instance_access(request.user, instance):
            return Response(
                {'error': 'You do not have permission to access this instance'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        try:
            instance.generate_qr_code()
            instance.save()
            
            UserActivity.objects.create(
                user=request.user,
                action='GENERATE_QR_CODE',
                model='ItemInstance',
                object_id=instance.id,
                details={
                    'instance_code': instance.instance_code,
                    'item_name': instance.item.name
                }
            )
            
            return Response({
                'message': 'QR code generated successfully',
                'qr_code_data': instance.qr_code_data,
                'instance': self.get_serializer(instance).data
            })
            
        except Exception as e:
            return Response(
                {'error': f'Failed to generate QR code: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=False, methods=['get'])
    def scan_qr(self, request):
        """Scan QR code and get instance details"""
        instance_code = request.query_params.get('code')
        
        if not instance_code:
            return Response({'error': 'Instance code required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            instance = ItemInstance.objects.get(instance_code=instance_code)
            
            # Check access permissions
            if not self._check_instance_access(request.user, instance):
                return Response(
                    {'error': 'You do not have permission to access this instance'}, 
                    status=status.HTTP_403_FORBIDDEN
                )
            
            serializer = self.get_serializer(instance)
            
            movements = InstanceMovement.objects.filter(instance=instance).select_related(
                'from_location', 'to_location', 'moved_by'
            ).order_by('-moved_at')[:10]
            movement_serializer = InstanceMovementSerializer(movements, many=True)
            
            return Response({
                'instance': serializer.data,
                'recent_movements': movement_serializer.data
            })
        except ItemInstance.DoesNotExist:
            return Response({'error': 'Instance not found'}, status=status.HTTP_404_NOT_FOUND)
    
    @action(detail=True, methods=['post'])
    @transaction.atomic
    def change_status(self, request, pk=None):
        """Manually change instance status"""
        instance = self.get_object()
        
        # Check access permissions
        if not self._check_instance_access(request.user, instance):
            return Response(
                {'error': 'You do not have permission to modify this instance'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        new_status = request.data.get('status')
        notes = request.data.get('notes', '')
        new_location = request.data.get('location')
        
        if not new_status:
            return Response({'error': 'Status is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        if new_status not in dict(InstanceStatus.choices):
            return Response({'error': 'Invalid status'}, status=status.HTTP_400_BAD_REQUEST)
        
        location_obj = None
        if new_location:
            try:
                location_obj = Location.objects.get(id=new_location)
            except Location.DoesNotExist:
                return Response({'error': 'Invalid location'}, status=status.HTTP_400_BAD_REQUEST)
        
        instance.change_status(
            new_status=new_status,
            user=request.user,
            location=location_obj,
            notes=notes
        )
        
        if instance.current_location.is_store:
            inv, _ = LocationInventory.objects.get_or_create(
                location=instance.current_location,
                item=instance.item
            )
            inv.update_quantities()
        
        UserActivity.objects.create(
            user=request.user,
            action='CHANGE_INSTANCE_STATUS',
            model='ItemInstance',
            object_id=instance.id,
            details={
                'instance_code': instance.instance_code,
                'old_status': instance.previous_status,
                'new_status': new_status
            }
        )
        
        return Response({
            'message': 'Status changed successfully',
            'instance': self.get_serializer(instance).data
        })
    
    @action(detail=True, methods=['post'])
    @transaction.atomic
    def mark_damaged(self, request, pk=None):
        """Mark instance as damaged"""
        instance = self.get_object()
        
        # Check access permissions
        if not self._check_instance_access(request.user, instance):
            return Response(
                {'error': 'You do not have permission to modify this instance'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        damage_description = request.data.get('damage_description', '')
        condition = request.data.get('condition', 'DAMAGED')
        
        instance.damage_reported_date = timezone.now().date()
        instance.damage_description = damage_description
        instance.condition = condition
        instance.change_status(
            new_status=InstanceStatus.DAMAGED,
            user=request.user,
            notes=f"Marked as damaged: {damage_description}"
        )
        
        UserActivity.objects.create(
            user=request.user,
            action='MARK_DAMAGED',
            model='ItemInstance',
            object_id=instance.id,
            details={
                'instance_code': instance.instance_code,
                'damage_description': damage_description
            }
        )
        
        return Response({
            'message': 'Instance marked as damaged',
            'instance': self.get_serializer(instance).data
        })
    
    @action(detail=True, methods=['post'])
    @transaction.atomic
    def send_for_repair(self, request, pk=None):
        """Send instance for repair"""
        instance = self.get_object()
        
        # Check access permissions
        if not self._check_instance_access(request.user, instance):
            return Response(
                {'error': 'You do not have permission to modify this instance'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        repair_vendor = request.data.get('repair_vendor', '')
        notes = request.data.get('notes', '')
        
        instance.repair_started_date = timezone.now().date()
        instance.repair_vendor = repair_vendor
        instance.change_status(
            new_status=InstanceStatus.UNDER_REPAIR,
            user=request.user,
            notes=f"Sent for repair to {repair_vendor}. {notes}"
        )
        
        UserActivity.objects.create(
            user=request.user,
            action='SEND_FOR_REPAIR',
            model='ItemInstance',
            object_id=instance.id,
            details={
                'instance_code': instance.instance_code,
                'repair_vendor': repair_vendor
            }
        )
        
        return Response({
            'message': 'Instance sent for repair',
            'instance': self.get_serializer(instance).data
        })
    
    @action(detail=True, methods=['post'])
    @transaction.atomic
    def complete_repair(self, request, pk=None):
        """Complete repair and return to store"""
        instance = self.get_object()
        
        # Check access permissions
        if not self._check_instance_access(request.user, instance):
            return Response(
                {'error': 'You do not have permission to modify this instance'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        if instance.current_status != InstanceStatus.UNDER_REPAIR:
            return Response({'error': 'Instance is not under repair'}, status=status.HTTP_400_BAD_REQUEST)
        
        repair_cost = request.data.get('repair_cost')
        condition = request.data.get('condition', 'GOOD')
        notes = request.data.get('notes', '')
        
        instance.repair_completed_date = timezone.now().date()
        instance.repair_cost = repair_cost
        instance.condition = condition
        instance.change_status(
            new_status=InstanceStatus.IN_STORE,
            user=request.user,
            notes=f"Repair completed. {notes}"
        )
        
        if instance.current_location.is_store:
            inv, _ = LocationInventory.objects.get_or_create(
                location=instance.current_location,
                item=instance.item
            )
            inv.update_quantities()
        
        UserActivity.objects.create(
            user=request.user,
            action='COMPLETE_REPAIR',
            model='ItemInstance',
            object_id=instance.id,
            details={
                'instance_code': instance.instance_code,
                'repair_cost': str(repair_cost) if repair_cost else None
            }
        )
        
        return Response({
            'message': 'Repair completed',
            'instance': self.get_serializer(instance).data
        })
    
    @action(detail=True, methods=['post'])
    @transaction.atomic
    def dispose(self, request, pk=None):
        """Dispose instance"""
        instance = self.get_object()
        
        # Check access permissions
        if not self._check_instance_access(request.user, instance):
            return Response(
                {'error': 'You do not have permission to modify this instance'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        disposal_reason = request.data.get('disposal_reason', '')
        
        if not disposal_reason:
            return Response({'error': 'Disposal reason is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        instance.disposal_date = timezone.now().date()
        instance.disposal_reason = disposal_reason
        instance.disposal_approved_by = request.user
        instance.change_status(
            new_status=InstanceStatus.DISPOSED,
            user=request.user,
            notes=f"Disposed: {disposal_reason}"
        )
        
        if instance.source_location.is_store:
            inv, _ = LocationInventory.objects.get_or_create(
                location=instance.source_location,
                item=instance.item
            )
            inv.update_quantities()
        
        UserActivity.objects.create(
            user=request.user,
            action='DISPOSE_INSTANCE',
            model='ItemInstance',
            object_id=instance.id,
            details={
                'instance_code': instance.instance_code,
                'disposal_reason': disposal_reason
            }
        )
        
        return Response({
            'message': 'Instance disposed successfully',
            'instance': self.get_serializer(instance).data
        })
    
    @action(detail=False, methods=['get'])
    def status_summary(self, request):
        """Get summary of instances by status"""
        location_id = request.query_params.get('location')
        item_id = request.query_params.get('item')
        
        queryset = self.get_queryset()
        
        if location_id:
            queryset = queryset.filter(current_location_id=location_id)
        if item_id:
            queryset = queryset.filter(item_id=item_id)
        
        summary = queryset.values('current_status').annotate(count=Count('id')).order_by('current_status')
        
        status_data = {}
        for item in summary:
            status_key = item['current_status']
            status_data[status_key] = {
                'count': item['count'],
                'display': dict(InstanceStatus.choices).get(status_key, status_key)
            }
        
        overdue_count = queryset.filter(
            current_status=InstanceStatus.TEMPORARY_ISSUED,
            expected_return_date__lt=timezone.now().date(),
            actual_return_date__isnull=True
        ).count()
        
        return Response({
            'status_summary': status_data,
            'total_instances': queryset.count(),
            'overdue_temporary_issued': overdue_count
        })
    
    def _check_instance_access(self, user, instance):
        """Check if user has access to this instance"""
        if not hasattr(user, 'profile'):
            return False
        
        profile = user.profile
        
        if profile.role in [UserRole.SYSTEM_ADMIN, UserRole.AUDITOR]:
            return True
        
        if profile.role == UserRole.STOCK_INCHARGE:
            accessible_stores = profile.get_accessible_stores()
            return instance.source_location in accessible_stores
        
        if profile.role == UserRole.LOCATION_HEAD:
            accessible_locations = profile.get_accessible_locations()
            return (instance.source_location in accessible_locations or 
                   instance.current_location in accessible_locations)
        
        return False


# ==================== STOCK ENTRY VIEWSET ====================
# ==================== STOCK ENTRY VIEWSET ====================
class StockEntryViewSet(viewsets.ModelViewSet):
    queryset = StockEntry.objects.all()
    serializer_class = StockEntrySerializer
    permission_classes = [IsAuthenticated]  # CHANGED: Removed CanManageStockEntry
    
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if not hasattr(user, 'profile'):
            return queryset.none()
        
        profile = user.profile
        
        # For acknowledge actions, allow access to entries where user has access to destination
        if self.action in ['acknowledge_receipt', 'acknowledge_return']:
            # For acknowledgment, user needs access to to_location
            accessible_stores = profile.get_accessible_stores()
            return queryset.filter(to_location__in=accessible_stores)
        
        # Normal filtering for other actions
        if profile.role not in [UserRole.SYSTEM_ADMIN, UserRole.AUDITOR]:
            user_locations = profile.get_accessible_locations()
            queryset = queryset.filter(
                Q(from_location__in=user_locations) |
                Q(to_location__in=user_locations)
            ).distinct()
        
        entry_type = self.request.query_params.get('entry_type')
        status_filter = self.request.query_params.get('status')
        pending_ack = self.request.query_params.get('pending_ack')
        search = self.request.query_params.get('search')
        
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
        if search:
            queryset = queryset.filter(entry_number__icontains=search)
        
        return queryset
    
    def get_permissions(self):
        """
        Override permissions per action
        """
        if self.action in ['acknowledge_receipt', 'acknowledge_return']:
            return [IsAuthenticated()]
        return [IsAuthenticated(), CanManageStockEntry()]
    
    @transaction.atomic
    def perform_create(self, serializer):
        """Create stock entry with automatic status updates"""
        stock_entry = serializer.save(created_by=self.request.user)
        
        if stock_entry.entry_type == 'ISSUE':
            self._process_issue_entry(stock_entry, self.request.user)
        elif stock_entry.entry_type == 'RECEIPT':
            self._process_receipt_entry(stock_entry, self.request.user)
        elif stock_entry.entry_type == 'CORRECTION':
            self._process_correction_entry(stock_entry, self.request.user)
        
        return stock_entry
    
    def _process_issue_entry(self, stock_entry, user):
        """
        Process issue entry with proper status based on destination:
        - Store to Store: Mark as IN_TRANSIT (PENDING_ACK), requires acknowledgment
        - Store to Non-Store: Mark as TEMPORARY_ISSUED or IN_USE (COMPLETED)
        """
        instances = stock_entry.instances.all()
        
        # Check if destination is a store
        if stock_entry.to_location.is_store:
            # Store to Store Transfer: IN_TRANSIT
            target_status = InstanceStatus.IN_TRANSIT
            stock_entry.status = 'PENDING_ACK'
            movement_type = 'TRANSFER'
            requires_ack = True
        else:
            # Store to Room/Lab: IN_USE or TEMPORARY_ISSUED
            if stock_entry.is_temporary:
                target_status = InstanceStatus.TEMPORARY_ISSUED
            else:
                target_status = InstanceStatus.IN_USE
            stock_entry.status = 'COMPLETED'  # Completed immediately for non-store
            movement_type = 'ISSUE'
            requires_ack = False
        
        stock_entry.save()
        
        for instance in instances:
            prev_status = instance.current_status
            prev_location = instance.current_location
            
            instance.current_location = stock_entry.to_location
            instance.assigned_to = stock_entry.temporary_recipient or stock_entry.to_location.name
            
            if stock_entry.is_temporary:
                instance.expected_return_date = stock_entry.expected_return_date
                instance.assigned_date = timezone.now()
            
            instance.change_status(
                new_status=target_status,
                user=user,
                location=stock_entry.to_location,
                notes=stock_entry.remarks or f"Issued from {stock_entry.from_location.name}"
            )
            
            InstanceMovement.objects.create(
                instance=instance,
                stock_entry=stock_entry,
                from_location=prev_location,
                to_location=stock_entry.to_location,
                previous_status=prev_status,
                new_status=target_status,
                movement_type=movement_type,
                moved_by=user,
                requires_acknowledgment=requires_ack,
                remarks=stock_entry.remarks
            )
        
        self._update_inventories(stock_entry)

    def _process_receipt_entry(self, stock_entry, user):
        """
        Process receipt entry (return to store):
        - Mark instances as IN_STORE
        - Update inventory
        - Status: COMPLETED immediately
        """
        instances = stock_entry.instances.all()
        
        stock_entry.status = 'COMPLETED'
        stock_entry.save()
        
        for instance in instances:
            prev_status = instance.current_status
            prev_location = instance.current_location
            
            # If it was temporary issued, mark return date
            if prev_status == InstanceStatus.TEMPORARY_ISSUED:
                instance.actual_return_date = timezone.now().date()
            
            instance.change_status(
                new_status=InstanceStatus.IN_STORE,
                user=user,
                location=stock_entry.to_location,
                notes=stock_entry.remarks or f"Returned from {prev_location.name}"
            )
            
            InstanceMovement.objects.create(
                instance=instance,
                stock_entry=stock_entry,
                from_location=prev_location,
                to_location=stock_entry.to_location,
                previous_status=prev_status,
                new_status=InstanceStatus.IN_STORE,
                movement_type='RETURN',
                moved_by=user,
                requires_acknowledgment=False,
                remarks=stock_entry.remarks or "Return to store"
            )
        
        self._update_inventories(stock_entry)
    
    def _process_correction_entry(self, stock_entry, user):
        """Process correction entry"""
        if stock_entry.reference_entry:
            ref_entry = stock_entry.reference_entry
            ref_instances = ref_entry.instances.all()
            
            for instance in ref_instances:
                last_movement = InstanceMovement.objects.filter(
                    instance=instance,
                    stock_entry=ref_entry
                ).first()
                
                if last_movement:
                    instance.current_location = last_movement.from_location or instance.source_location
                    instance.change_status(
                        new_status=last_movement.previous_status,
                        user=user,
                        location=instance.current_location,
                        notes="Correction: Reverting"
                    )
        
        new_instances = stock_entry.instances.all()
        for instance in new_instances:
            new_status = InstanceStatus.IN_STORE if stock_entry.to_location.is_store else InstanceStatus.IN_USE
            
            instance.change_status(
                new_status=new_status,
                user=user,
                location=stock_entry.to_location,
                notes="Correction entry"
            )
        
        stock_entry.status = 'COMPLETED'
        stock_entry.save()
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
        """Acknowledge receipt of stock transfer (Store to Store)"""
        stock_entry = self.get_object()
        
        if stock_entry.status != 'PENDING_ACK':
            return Response({'error': 'Entry is not pending acknowledgment'}, status=status.HTTP_400_BAD_REQUEST)
        
        if stock_entry.entry_type != 'ISSUE':
            return Response({'error': 'Only issue entries can be acknowledged'}, status=status.HTTP_400_BAD_REQUEST)
        
        if not hasattr(request.user, 'profile'):
            raise PermissionDenied("User profile required")
        
        profile = request.user.profile
        
        # Manual permission check
        if not profile.has_location_access(stock_entry.to_location):
            raise PermissionDenied("You don't have access to acknowledge this entry")
        
        accepted_ids = request.data.get('accepted_instances', [])
        rejected_ids = request.data.get('rejected_instances', [])
        
        entry_instance_ids = set(stock_entry.instances.values_list('id', flat=True))
        accepted_set = set(accepted_ids)
        rejected_set = set(rejected_ids)
        
        if not accepted_set.issubset(entry_instance_ids):
            return Response({'error': 'Some accepted instances do not belong to this entry'}, status=status.HTTP_400_BAD_REQUEST)
        
        if not rejected_set.issubset(entry_instance_ids):
            return Response({'error': 'Some rejected instances do not belong to this entry'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Process accepted: IN_TRANSIT → IN_STORE at destination
        # CRITICAL: Change source_location to destination (ownership transfer)
        accepted_instances = ItemInstance.objects.filter(id__in=accepted_ids)
        for instance in accepted_instances:
            # Transfer ownership to destination store
            old_source = instance.source_location
            instance.source_location = stock_entry.to_location  # NEW: Transfer ownership
            instance.current_location = stock_entry.to_location
            instance.current_status = InstanceStatus.IN_STORE
            instance.status_changed_at = timezone.now()
            instance.status_changed_by = request.user
            instance.save()
            
            # Create movement record
            InstanceMovement.objects.create(
                instance=instance,
                stock_entry=stock_entry,
                from_location=stock_entry.from_location,
                to_location=stock_entry.to_location,
                previous_status=InstanceStatus.IN_TRANSIT,
                new_status=InstanceStatus.IN_STORE,
                movement_type='TRANSFER',
                moved_by=request.user,
                requires_acknowledgment=False,
                acknowledged=True,
                acknowledged_by=request.user,
                acknowledged_at=timezone.now(),
                remarks=f"Transfer acknowledged - ownership transferred from {old_source.name} to {stock_entry.to_location.name}"
            )
        
        # Process rejected: Create return entry back to source
        rejected_instances = ItemInstance.objects.filter(id__in=rejected_ids)
        
        if rejected_instances.exists():
            return_entry = StockEntry.objects.create(
                entry_type='RECEIPT',
                from_location=stock_entry.to_location,
                to_location=stock_entry.from_location,
                item=stock_entry.item,
                quantity=len(rejected_ids),
                purpose=f"Rejected items from transfer {stock_entry.entry_number}",
                remarks=f"Rejected during acknowledgment by {request.user.get_full_name() or request.user.username}",
                reference_entry=stock_entry,
                status='PENDING_ACK',
                created_by=request.user
            )
            
            for instance in rejected_instances:
                # Keep source_location as original (not transferring ownership)
                instance.current_location = stock_entry.from_location
                instance.current_status = InstanceStatus.IN_TRANSIT
                instance.status_changed_at = timezone.now()
                instance.status_changed_by = request.user
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
                    requires_acknowledgment=True,
                    remarks="Rejected during acknowledgment - returning to sender"
                )
            
            return_entry.instances.set(rejected_instances)
        
        # Update original entry
        stock_entry.status = 'COMPLETED'
        stock_entry.acknowledged_by = request.user
        stock_entry.acknowledged_at = timezone.now()
        stock_entry.remarks = (
            f"{stock_entry.remarks or ''}\n"
            f"Accepted: {len(accepted_ids)}, Rejected: {len(rejected_ids)}"
        )
        stock_entry.save()
        
        # Update inventories for BOTH stores
        # Source store (from_location) - quantities should decrease
        if stock_entry.from_location.is_store:
            from_inv, _ = LocationInventory.objects.get_or_create(
                location=stock_entry.from_location,
                item=stock_entry.item
            )
            from_inv.update_quantities()
        
        # Destination store (to_location) - quantities should increase
        if stock_entry.to_location.is_store:
            to_inv, _ = LocationInventory.objects.get_or_create(
                location=stock_entry.to_location,
                item=stock_entry.item
            )
            to_inv.update_quantities()
        
        UserActivity.objects.create(
            user=request.user,
            action='ACKNOWLEDGE_STOCK_ENTRY',
            model='StockEntry',
            object_id=stock_entry.id,
            details={
                'entry_number': stock_entry.entry_number,
                'accepted_count': len(accepted_ids),
                'rejected_count': len(rejected_ids)
            }
        )
        
        message = f'Receipt acknowledged successfully. Accepted: {len(accepted_ids)}'
        if rejected_ids:
            message += f', Rejected: {len(rejected_ids)} (returning to sender)'
        
        return Response({
            'message': message,
            'accepted_count': len(accepted_ids),
            'rejected_count': len(rejected_ids),
            'stock_entry': StockEntrySerializer(stock_entry, context={'request': request}).data
        })
    
    
    @action(detail=True, methods=['post'])
    @transaction.atomic
    def acknowledge_return(self, request, pk=None):
        """Acknowledge receipt of returned items (original sender receiving rejected items back)"""
        stock_entry = self.get_object()
        
        if stock_entry.status != 'PENDING_ACK':
            return Response({'error': 'Entry is not pending acknowledgment'}, status=status.HTTP_400_BAD_REQUEST)
        
        if stock_entry.entry_type != 'RECEIPT':
            return Response({'error': 'Only receipt entries can be acknowledged as returns'}, status=status.HTTP_400_BAD_REQUEST)
        
        if not stock_entry.reference_entry:
            return Response({'error': 'This is not a return entry'}, status=status.HTTP_400_BAD_REQUEST)
        
        if not hasattr(request.user, 'profile'):
            raise PermissionDenied("User profile required")
        
        profile = request.user.profile
        
        # Manual permission check
        if not profile.has_location_access(stock_entry.to_location):
            raise PermissionDenied("You don't have access to acknowledge this return")
        
        # Update all instances: IN_TRANSIT → IN_STORE at source
        # Source location remains the same (never changed ownership)
        for instance in stock_entry.instances.all():
            instance.current_location = stock_entry.to_location
            instance.current_status = InstanceStatus.IN_STORE
            instance.status_changed_at = timezone.now()
            instance.status_changed_by = request.user
            instance.save()
            
            # Update movement
            movement = InstanceMovement.objects.filter(
                instance=instance,
                stock_entry=stock_entry,
                requires_acknowledgment=True
            ).first()
            
            if movement:
                movement.acknowledged = True
                movement.acknowledged_by = request.user
                movement.acknowledged_at = timezone.now()
                movement.save()
        
        # Complete the return entry
        stock_entry.status = 'COMPLETED'
        stock_entry.acknowledged_by = request.user
        stock_entry.acknowledged_at = timezone.now()
        stock_entry.save()
        
        # Update inventory for the original source store
        if stock_entry.to_location.is_store:
            inv, _ = LocationInventory.objects.get_or_create(
                location=stock_entry.to_location,
                item=stock_entry.item
            )
            inv.update_quantities()
        
        UserActivity.objects.create(
            user=request.user,
            action='ACKNOWLEDGE_RETURN',
            model='StockEntry',
            object_id=stock_entry.id,
            details={
                'entry_number': stock_entry.entry_number,
                'returned_count': stock_entry.instances.count(),
                'original_transfer': stock_entry.reference_entry.entry_number
            }
        )
        
        return Response({
            'message': f'Return acknowledged successfully. {stock_entry.instances.count()} items back in inventory.',
            'returned_count': stock_entry.instances.count(),
            'stock_entry': StockEntrySerializer(stock_entry, context={'request': request}).data
        })

    @action(detail=True, methods=['post'])
    @transaction.atomic
    def return_temporary_issued(self, request, pk=None):
        """Return temporarily issued items"""
        stock_entry = self.get_object()
        
        if not stock_entry.is_temporary:
            return Response({'error': 'Entry is not marked as temporary'}, status=status.HTTP_400_BAD_REQUEST)
        
        returned_instance_ids = request.data.get('returned_instances', [])
        condition_updates = request.data.get('condition_updates', {})
        
        if not returned_instance_ids:
            return Response({'error': 'No instances specified for return'}, status=status.HTTP_400_BAD_REQUEST)
        
        returned_instances = ItemInstance.objects.filter(
            id__in=returned_instance_ids,
            current_status=InstanceStatus.TEMPORARY_ISSUED
        )
        
        for instance in returned_instances:
            if str(instance.id) in condition_updates:
                instance.condition = condition_updates[str(instance.id)]
            
            instance.actual_return_date = timezone.now().date()
            instance.change_status(
                new_status=InstanceStatus.IN_STORE,
                user=request.user,
                location=stock_entry.from_location,
                notes="Temporary issue returned"
            )
            
            InstanceMovement.objects.create(
                instance=instance,
                stock_entry=stock_entry,
                from_location=stock_entry.to_location,
                to_location=stock_entry.from_location,
                previous_status=InstanceStatus.TEMPORARY_ISSUED,
                new_status=InstanceStatus.IN_STORE,
                movement_type='RETURN',
                moved_by=request.user,
                remarks="Temporary issue returned"
            )
        
        if stock_entry.from_location.is_store:
            inv, _ = LocationInventory.objects.get_or_create(
                location=stock_entry.from_location,
                item=stock_entry.item
            )
            inv.update_quantities()
        
        return Response({
            'message': f'{returned_instances.count()} instances returned successfully',
            'returned_count': returned_instances.count()
        })
# ==================== LOCATION INVENTORY VIEWSET ====================
class LocationInventoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = LocationInventory.objects.all()
    serializer_class = LocationInventorySerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        location = self.request.query_params.get('location')
        item = self.request.query_params.get('item')
        low_stock = self.request.query_params.get('low_stock')
        
        if location:
            queryset = queryset.filter(location_id=location)
        if item:
            queryset = queryset.filter(item_id=item)
        if low_stock == 'true':
            queryset = queryset.filter(
                available_quantity__lt=F('item__reorder_level')
            )
        
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