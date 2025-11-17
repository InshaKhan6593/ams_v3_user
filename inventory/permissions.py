# permissions.py - ENHANCED VERSION
from rest_framework import permissions
from user_management.models import UserRole


class CanManageUsers(permissions.BasePermission):
    """
    Permission for user management operations.
    - SYSTEM_ADMIN: Can manage all users
    - LOCATION_HEAD: Can create Stock Incharge for stores in their hierarchy
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
    Permission for location-based operations with standalone location awareness.
    - SYSTEM_ADMIN: Full access to all locations
    - LOCATION_HEAD: Can create and manage locations within their standalone hierarchy
    - STOCK_INCHARGE: Can create locations within their hierarchy
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
        
        # Stock Incharge can create locations (within their hierarchy)
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
        
        # Stock Incharge can modify locations within their hierarchy
        if profile.role == UserRole.STOCK_INCHARGE:
            return True
        
        return False


class CanManageItems(permissions.BasePermission):
    """
    Permission for item management with standalone location awareness.
    - SYSTEM_ADMIN: Full access to all items
    - LOCATION_HEAD: Can create items for their standalone location
    - STOCK_INCHARGE: Can create items for their parent standalone location
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
            return profile.can_create_item()
        
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
    Permission for inspection certificate operations with standalone location awareness.
    Flow: Location Head (standalone) creates → Stock Incharge (main store) reviews → Auditor approves
    """
    
    def has_permission(self, request, view):
        """Check if user has permission to perform the ACTION."""
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
        
        # Create action - only Location Head of standalone locations
        if view.action == 'create':
            return profile.can_create_inspection_certificates()
        
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
        """Check if user has permission to access THIS SPECIFIC OBJECT."""
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
    """Only System Admin can edit, others have read-only access"""
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
    Permission for managing stock entries with upward transfer support.
    - SYSTEM_ADMIN: Full access
    - STOCK_INCHARGE: Can create/manage entries for their stores
    - Main Store Incharge: Can also issue upward to parent standalone
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
        
        # Stock Incharge can create/view their own
        if profile.role == UserRole.STOCK_INCHARGE:
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
        
        # For acknowledge actions
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
            
            # Special case: Upward transfers to parent standalone
            # Stock Incharge of main store can view entries targeting parent standalone
            if obj.is_upward_transfer and profile.is_main_store_incharge():
                return True
        
        return False