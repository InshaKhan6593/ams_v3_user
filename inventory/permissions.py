from rest_framework import permissions
from user_management.models import UserRole


class CanManageUsers(permissions.BasePermission):
    """
    Permission for user management operations.
    Location Head can create Stock Incharge users for stores within their location.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        if not hasattr(request.user, 'profile'):
            return False
        
        profile = request.user.profile
        
        # System Admin can manage all users
        if profile.role == UserRole.SYSTEM_ADMIN:
            return True
        
        # Location Head can create Stock Incharge users
        if profile.role == UserRole.LOCATION_HEAD:
            if view.action in ['list', 'retrieve', 'create', 'my_profile', 'my_permissions', 
                              'reset_password', 'toggle_active']:
                return True
            return False
        
        # Others can only view their own profile
        if view.action in ['retrieve', 'my_profile', 'my_permissions']:
            return True
        
        return False
    
    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return False
        
        if not hasattr(request.user, 'profile'):
            return False
        
        profile = request.user.profile
        
        # System Admin has full access
        if profile.role == UserRole.SYSTEM_ADMIN:
            return True
        
        # Users can view/edit their own profile
        if obj.user == request.user:
            return True
        
        # Location Head can manage users they created (Stock Incharge)
        if profile.role == UserRole.LOCATION_HEAD:
            return obj.created_by == request.user
        
        return False


class HasLocationAccess(permissions.BasePermission):
    """
    Permission for location-based operations.
    - System Admin: Full access to all locations
    - Location Head: Can create and manage locations within their hierarchy
    - Stock Incharge: Can create locations within their assigned main locations (stores)
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        if not hasattr(request.user, 'profile'):
            return False
        
        profile = request.user.profile
        
        # System Admin and Auditor have global access
        if profile.role in [UserRole.SYSTEM_ADMIN, UserRole.AUDITOR]:
            return True
        
        # Safe methods (GET, HEAD, OPTIONS) allowed for authenticated users
        if request.method in permissions.SAFE_METHODS:
            return True
        
        # Location Head can create and manage locations
        if profile.role == UserRole.LOCATION_HEAD:
            if view.action in ['create', 'update', 'partial_update', 'destroy']:
                return True
        
        # Stock Incharge can create locations (within their main location)
        if profile.role == UserRole.STOCK_INCHARGE:
            if view.action in ['create', 'update', 'partial_update']:
                return True
        
        return False
    
    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return False
        
        if not hasattr(request.user, 'profile'):
            return False
        
        profile = request.user.profile
        
        # System Admin and Auditor have full access
        if profile.role in [UserRole.SYSTEM_ADMIN, UserRole.AUDITOR]:
            return True
        
        # Check location access
        if not profile.has_location_access(obj):
            return False
        
        # Safe methods allowed if user has access
        if request.method in permissions.SAFE_METHODS:
            return True
        
        # Location Head can modify locations they have access to
        if profile.role == UserRole.LOCATION_HEAD:
            return True
        
        # Stock Incharge can modify locations within their assigned stores/main location
        if profile.role == UserRole.STOCK_INCHARGE:
            # Stock Incharge can modify locations that are under their main location
            main_location = profile.get_main_location()
            if main_location:
                # Check if this location is under the main location hierarchy
                return obj.is_descendant_of(main_location) or obj.id == main_location.id
            return False
        
        return False

class CanManageItems(permissions.BasePermission):
    """
    Permission for item management.
    - System Admin: Full access to all items
    - Location Head: Can create items for their main location (root/no parent)
    - Stock Incharge: Can create items
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        if not hasattr(request.user, 'profile'):
            return False
        
        profile = request.user.profile
        
        # System Admin has full access
        if profile.role == UserRole.SYSTEM_ADMIN:
            return True
        
        # All can list/retrieve
        if view.action in ['list', 'retrieve']:
            return True
        
        # Location Head and Stock Incharge can create items
        if view.action == 'create':
            return profile.role in [UserRole.LOCATION_HEAD, UserRole.STOCK_INCHARGE]
        
        # Only System Admin can update/delete
        if view.action in ['update', 'partial_update', 'destroy']:
            return profile.role == UserRole.SYSTEM_ADMIN
        
        return False
    
    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return False
        
        if not hasattr(request.user, 'profile'):
            return False
        
        profile = request.user.profile
        
        # System Admin has full access
        if profile.role == UserRole.SYSTEM_ADMIN:
            return True
        
        # Safe methods allowed for all
        if request.method in permissions.SAFE_METHODS:
            return True
        
        # Only System Admin can modify items
        return False


class InspectionCertificatePermission(permissions.BasePermission):
    """
    Permission for inspection certificate operations.
    Flow: Location Head creates → Stock Incharge (main store) reviews → Auditor approves
    """
    
    def has_permission(self, request, view):
        """
        Check if user has permission to perform the ACTION.
        """
        if not request.user or not request.user.is_authenticated:
            return False
        
        if not hasattr(request.user, 'profile'):
            return False
        
        profile = request.user.profile
        
        # System Admin can do everything
        if profile.role == UserRole.SYSTEM_ADMIN:
            return True
        
        # All authenticated users with profiles can list/retrieve/dashboard
        if view.action in ['list', 'retrieve', 'dashboard_stats']:
            return True
        
        # Create action
        if view.action == 'create':
            return profile.role in [UserRole.LOCATION_HEAD, UserRole.SYSTEM_ADMIN]
        
        # Update actions - check basic permission, detailed check in has_object_permission
        if view.action in ['update', 'partial_update']:
            return profile.role in [UserRole.LOCATION_HEAD, UserRole.STOCK_INCHARGE, 
                                   UserRole.AUDITOR, UserRole.SYSTEM_ADMIN]
        
        # Custom action permissions
        if view.action == 'submit_to_stock_incharge':
            return profile.role in [UserRole.LOCATION_HEAD, UserRole.SYSTEM_ADMIN]
        
        if view.action == 'submit_stock_details':
            return profile.role in [UserRole.STOCK_INCHARGE, UserRole.SYSTEM_ADMIN]
        
        if view.action == 'submit_audit_review':
            return profile.role in [UserRole.AUDITOR, UserRole.SYSTEM_ADMIN]
        
        if view.action == 'reject':
            return profile.role in [UserRole.AUDITOR, UserRole.SYSTEM_ADMIN]
        
        return False
    
    def has_object_permission(self, request, view, obj):
        """
        Check if user has permission to access THIS SPECIFIC OBJECT.
        """
        if not request.user or not request.user.is_authenticated:
            return False
        
        if not hasattr(request.user, 'profile'):
            return False
        
        profile = request.user.profile
        
        # System Admin has full access
        if profile.role == UserRole.SYSTEM_ADMIN:
            return True
        
        # For list and retrieve, check location access
        if view.action in ['list', 'retrieve', 'dashboard_stats']:
            # Auditor can view all
            if profile.role == UserRole.AUDITOR:
                return True
            
            # Others need location access
            has_dept_access = profile.has_location_access(obj.department)
            main_store = obj.get_main_store()
            has_store_access = main_store and profile.has_location_access(main_store)
            
            if profile.role == UserRole.LOCATION_HEAD:
                return has_dept_access
            elif profile.role == UserRole.STOCK_INCHARGE:
                return has_store_access
            
            return False
        
        # CUSTOM ACTIONS
        if view.action == 'submit_to_stock_incharge':
            # Location Head submitting to Stock Incharge
            if obj.stage != 'INITIATED':
                return False
            return profile.has_location_access(obj.department)
        
        if view.action == 'submit_stock_details':
            # Stock Incharge submitting stock details
            if obj.stage != 'STOCK_DETAILS':
                return False
            main_store = obj.get_main_store()
            return main_store and profile.has_location_access(main_store)
        
        if view.action == 'submit_audit_review':
            # Auditor completing the certificate
            return obj.stage == 'AUDIT_REVIEW'
        
        if view.action == 'reject':
            # Auditor rejecting (at any stage except completed/rejected)
            return obj.stage not in ['COMPLETED', 'REJECTED']
        
        # UPDATE/PARTIAL_UPDATE actions
        if view.action in ['update', 'partial_update']:
            # Check based on role and stage
            if profile.role == UserRole.AUDITOR:
                return obj.stage == 'AUDIT_REVIEW'
            
            if profile.role == UserRole.STOCK_INCHARGE:
                if obj.stage != 'STOCK_DETAILS':
                    return False
                main_store = obj.get_main_store()
                return main_store and profile.has_location_access(main_store)
            
            if profile.role == UserRole.LOCATION_HEAD:
                if obj.stage != 'INITIATED':
                    return False
                return profile.has_location_access(obj.department)
        
        return False


class IsSystemAdminOrReadOnly(permissions.BasePermission):
    """
    Only System Admin can edit, others have read-only access
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        if not hasattr(request.user, 'profile'):
            return False
        
        # Safe methods allowed for all authenticated users
        if request.method in permissions.SAFE_METHODS:
            return True
        
        # Only System Admin can modify
        return request.user.profile.role == UserRole.SYSTEM_ADMIN


class CanManageStockEntry(permissions.BasePermission):
    """
    Permission for managing stock entries
    """
    
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        if not hasattr(request.user, 'profile'):
            return False
        
        profile = request.user.profile
        
        # System Admin and Auditor can view all
        if profile.role in [UserRole.SYSTEM_ADMIN, UserRole.AUDITOR]:
            return True
        
        # Stock Incharge and Location Head can create/view their own
        if profile.role in [UserRole.STOCK_INCHARGE, UserRole.LOCATION_HEAD]:
            return True
        
        return False
    
    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return False
        
        if not hasattr(request.user, 'profile'):
            return False
        
        profile = request.user.profile
        
        # System Admin can do everything
        if profile.role == UserRole.SYSTEM_ADMIN:
            return True
        
        # For acknowledge_receipt and acknowledge_return actions
        if view.action in ['acknowledge_receipt', 'acknowledge_return']:
            # User must have access to the destination location (to_location)
            if obj.to_location:
                return profile.has_location_access(obj.to_location)
            return False
        
        # Auditor can view all
        if profile.role == UserRole.AUDITOR:
            return request.method in permissions.SAFE_METHODS
        
        # Stock Incharge can manage entries in their stores
        if profile.role == UserRole.STOCK_INCHARGE:
            accessible_stores = profile.get_accessible_stores()
            
            # Can manage if from_location or to_location is in their stores
            if obj.from_location and obj.from_location in accessible_stores:
                return True
            if obj.to_location and obj.to_location in accessible_stores:
                return True
        
        # Location Head can view entries related to their locations
        if profile.role == UserRole.LOCATION_HEAD:
            accessible_locations = profile.get_accessible_locations()
            
            if request.method in permissions.SAFE_METHODS:
                if obj.from_location and obj.from_location in accessible_locations:
                    return True
                if obj.to_location and obj.to_location in accessible_locations:
                    return True
        
        return False