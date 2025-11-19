# permissions.py - ENHANCED VERSION with Download Permission
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
                              'reset_password', 'toggle_active', 'my_pending_tasks', 
                              'my_item_default_locations']:
                return True
            return False
        
        # Others can only view their own profile and pending tasks
        if view.action in ['retrieve', 'my_profile', 'my_permissions', 'my_pending_tasks',
                          'my_item_default_locations']:
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


# permissions.py - CRITICAL FIX for 4-Stage Workflow with Download Permission
class InspectionCertificatePermission(permissions.BasePermission):
    """
    Permission for inspection certificate operations with 4-STAGE workflow.
    
    4-STAGE WORKFLOW (Non-Root Departments):
    Stage 1 (INITIATED): Location Head creates & adds items
    Stage 2 (STOCK_DETAILS): Department Store Incharge fills stock register
    Stage 3 (CENTRAL_REGISTER): Central Store Incharge fills central register
    Stage 4 (AUDIT_REVIEW): Auditor completes
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
        
        # All authenticated users with profiles can list/retrieve/dashboard/creation_options
        if view.action in ['list', 'retrieve', 'dashboard_stats', 'creation_options', 'download_pdf']:
            return True
        
        # Create action - only Location Head of standalone locations
        if view.action == 'create':
            return profile.can_create_inspection_certificates()
        
        # Update actions - check basic permission
        if view.action in ['update', 'partial_update']:
            return profile.role in [UserRole.LOCATION_HEAD, UserRole.STOCK_INCHARGE, 
                                   UserRole.AUDITOR, UserRole.SYSTEM_ADMIN]
        
        # Custom action permissions
        if view.action == 'submit_to_stock_incharge':
            # Stage 1 → Stage 2: Only Location Head
            return profile.role in [UserRole.LOCATION_HEAD, UserRole.SYSTEM_ADMIN]
        
        if view.action == 'submit_stock_details':
            # Stage 2 → Stage 3: Only Department Store Incharge
            return profile.role in [UserRole.STOCK_INCHARGE, UserRole.SYSTEM_ADMIN]
        
        if view.action == 'submit_central_register':
            # Stage 3 → Stage 4: Only Central Store Incharge
            return profile.role in [UserRole.STOCK_INCHARGE, UserRole.SYSTEM_ADMIN]
        
        if view.action == 'submit_audit_review':
            # Stage 4 → Complete: Auditor
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
        
        # Check if this is a root department certificate
        is_root_cert = obj.department.parent_location is None
        
        # For list and retrieve, check location access
        if view.action in ['list', 'retrieve', 'dashboard_stats', 'download_pdf']:
            # Auditor can view all
            if profile.role == UserRole.AUDITOR:
                return True
            
            # Location Head needs department access
            if profile.role == UserRole.LOCATION_HEAD:
                return profile.has_location_access(obj.department)
            
            # Stock Incharge needs store access
            elif profile.role == UserRole.STOCK_INCHARGE:
                main_store = obj.get_main_store()
                
                # Department store incharge can view if they have access to the main store
                has_dept_store_access = main_store and profile.has_location_access(main_store)
                
                # OR they're the central store incharge (can view all certificates in CENTRAL_REGISTER or AUDIT_REVIEW)
                is_central_store = profile.is_main_store_incharge()
                
                # Can view if they have dept store access OR they're central store (for CENTRAL_REGISTER/AUDIT_REVIEW stage)
                if has_dept_store_access:
                    return True
                if is_central_store and obj.stage in ['CENTRAL_REGISTER', 'AUDIT_REVIEW', 'COMPLETED']:
                    return True
                
                return False
            
            return False
        
        # CUSTOM ACTIONS - 4-STAGE WORKFLOW
        if view.action == 'submit_to_stock_incharge':
            # Stage 1 → Stage 2: Location Head submitting
            if obj.stage != 'INITIATED':
                return False
            return profile.has_location_access(obj.department)
        
        if view.action == 'submit_stock_details':
            # Stage 2 → Stage 3: Department Store Incharge submitting
            if obj.stage != 'STOCK_DETAILS':
                return False
            
            # CRITICAL FIX: Must be department store incharge, NOT central
            if profile.is_main_store_incharge():
                # Central store should NOT submit stock details
                return False
            
            # Check if user has access to the department's main store
            main_store = obj.get_main_store()
            return main_store and profile.has_location_access(main_store)
        
        if view.action == 'submit_central_register':
            # Stage 3 → Stage 4: Central Store Incharge submitting
            if obj.stage != 'CENTRAL_REGISTER':
                return False
            
            # Must be central store incharge
            if profile.role == UserRole.STOCK_INCHARGE:
                return profile.is_main_store_incharge()
            
            return False
        
        if view.action == 'submit_audit_review':
            # Stage 4 → Complete: Auditor
            if obj.stage != 'AUDIT_REVIEW':
                return False
            
            if profile.role == UserRole.AUDITOR:
                return True
            
            return False
        
        if view.action == 'reject':
            # Auditor can reject at any stage except completed/rejected
            return obj.stage not in ['COMPLETED', 'REJECTED']
        
        # UPDATE/PARTIAL_UPDATE actions - CRITICAL: Stage-based permissions
        if view.action in ['update', 'partial_update']:
            # Stage 1: ONLY Location Head
            if obj.stage == 'INITIATED':
                if profile.role == UserRole.LOCATION_HEAD:
                    return profile.has_location_access(obj.department)
                # CRITICAL: Stock Incharge CANNOT edit in INITIATED stage
                elif profile.role == UserRole.STOCK_INCHARGE:
                    return False
                return False
            
            # Stage 2: ONLY Department Store Incharge (NOT central store)
            elif obj.stage == 'STOCK_DETAILS':
                if profile.role == UserRole.STOCK_INCHARGE:
                    # CRITICAL FIX: Must NOT be central store incharge
                    if profile.is_main_store_incharge():
                        return False
                    
                    # Must have access to the department's main store
                    main_store = obj.get_main_store()
                    return main_store and profile.has_location_access(main_store)
                return False
            
            # Stage 3: CENTRAL_REGISTER - Central Store Incharge ONLY
            elif obj.stage == 'CENTRAL_REGISTER':
                if profile.role == UserRole.STOCK_INCHARGE:
                    # CRITICAL: Must BE central store incharge
                    return profile.is_main_store_incharge()
                return False
            
            # Stage 4: AUDIT_REVIEW - Auditor ONLY
            elif obj.stage == 'AUDIT_REVIEW':
                if profile.role == UserRole.AUDITOR:
                    return True
                return False
        
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
    Permission for managing stock entries with upward transfer and return acknowledgment support.
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
        
        # NEW: For acknowledge_return action
        if view.action == 'acknowledge_return':
            # User must have access to the destination location (to_location)
            # This is the original sender receiving back the rejected items
            if obj.entry_type == 'RETURN' and obj.to_location:
                return profile.has_location_access(obj.to_location)
            return False
        
        # For acknowledge_receipt action (original logic)
        if view.action == 'acknowledge_receipt':
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