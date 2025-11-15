from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db.models.signals import post_save
from django.dispatch import receiver

class UserRole(models.TextChoices):
    SYSTEM_ADMIN = 'SYSTEM_ADMIN', 'System Admin'
    LOCATION_HEAD = 'LOCATION_HEAD', 'Location Head'
    STOCK_INCHARGE = 'STOCK_INCHARGE', 'Stock Incharge'
    AUDITOR = 'AUDITOR', 'Auditor'

class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=20, choices=UserRole.choices)
    assigned_locations = models.ManyToManyField('inventory.Location', blank=True, related_name='assigned_users')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_profiles')
    phone = models.CharField(max_length=20, blank=True, null=True)
    employee_id = models.CharField(max_length=50, unique=True, null=True, blank=True)
    department = models.ForeignKey('inventory.Location', on_delete=models.SET_NULL, null=True, blank=True, 
                                  related_name='department_users', limit_choices_to={'location_type': 'DEPARTMENT'})
    custom_permissions = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        indexes = [
            models.Index(fields=['role']),
            models.Index(fields=['employee_id']),
        ]
        
    def __str__(self):
        return f"{self.user.username} - {self.get_role_display()}"
    
    def get_main_location(self):
        """
        Get the main/root/standalone location for this user.
        - System Admin: Returns None (has access to all)
        - Location Head: Returns their assigned standalone location
        - Stock Incharge: Returns the root location of their assigned store
        - Auditor: Returns None (has access to all)
        """
        if self.role in [UserRole.SYSTEM_ADMIN, UserRole.AUDITOR]:
            return None
        
        assigned_locs = self.assigned_locations.all()
        
        if not assigned_locs.exists():
            return None
        
        if self.role == UserRole.LOCATION_HEAD:
            # Location Head should be assigned to a standalone location
            standalone = assigned_locs.filter(parent_location__isnull=True, is_store=False).first()
            if standalone:
                return standalone
            
            # If not directly assigned to standalone, traverse up from first assigned
            first_assigned = assigned_locs.first()
            return self._traverse_to_root(first_assigned)
        
        elif self.role == UserRole.STOCK_INCHARGE:
            # Stock Incharge is assigned to a store, find its root
            assigned_store = assigned_locs.filter(is_store=True).first()
            
            if not assigned_store:
                return None
            
            return self._traverse_to_root(assigned_store)
        
        return None
    
    def _traverse_to_root(self, location):
        """Helper method to traverse up the location hierarchy to find the root"""
        if not location:
            return None
        
        current = location
        
        # Traverse up until we find a location with no parent
        while current.parent_location:
            current = current.parent_location
        
        # Verify it's a valid standalone location (not a store)
        if not current.is_store:
            return current
        
        return None
    
    def has_location_access(self, location):
        """Check if user has access to a location"""
        if self.role == UserRole.SYSTEM_ADMIN:
            return True
        
        # Check direct assignment
        if self.assigned_locations.filter(id=location.id).exists():
            return True
        
        # Check if location is a descendant of any assigned location
        from inventory.models import Location
        
        def is_descendant(loc, potential_parent_ids):
            """Recursively check if loc is a descendant of any potential parent"""
            if loc.id in potential_parent_ids:
                return True
            if loc.parent_location:
                return is_descendant(loc.parent_location, potential_parent_ids)
            return False
        
        assigned_location_ids = set(self.assigned_locations.values_list('id', flat=True))
        return is_descendant(location, assigned_location_ids)
    
    def get_accessible_locations(self):
        """Get all locations user can access (including descendants)"""
        if self.role == UserRole.SYSTEM_ADMIN:
            from inventory.models import Location
            return Location.objects.all()
        
        from inventory.models import Location
        
        # Get directly assigned locations
        assigned_locations = self.assigned_locations.all()
        accessible_ids = set(assigned_locations.values_list('id', flat=True))
        
        # Get main location
        main_location = self.get_main_location()
        if main_location:
            accessible_ids.add(main_location.id)
        
        # Get all descendants recursively
        def get_all_descendants(location):
            descendants = set()
            children = location.child_locations.all()
            for child in children:
                descendants.add(child.id)
                descendants.update(get_all_descendants(child))
            return descendants
        
        for location in assigned_locations:
            accessible_ids.update(get_all_descendants(location))
        
        # Add descendants of main location
        if main_location:
            accessible_ids.update(get_all_descendants(main_location))
        
        return Location.objects.filter(id__in=accessible_ids)
    
    def get_accessible_stores(self):
        """Get only stores that user is directly assigned to or manages"""
        if self.role == UserRole.SYSTEM_ADMIN:
            from inventory.models import Location
            return Location.objects.filter(is_store=True)
        
        if self.role == UserRole.AUDITOR:
            from inventory.models import Location
            return Location.objects.filter(is_store=True)
        
        # For Location Head and Stock Incharge, return ONLY directly assigned stores
        # NOT all stores in the hierarchy
        if self.role == UserRole.LOCATION_HEAD:
            # Location Head: return stores from their assigned locations hierarchy
            assigned_standalone_locations = self.assigned_locations.filter(
                parent_location__isnull=True, 
                is_store=False
            )
            accessible_stores = set()
            
            for standalone_loc in assigned_standalone_locations:
                # Get all stores under this standalone location
                stores = standalone_loc.get_all_stores()
                accessible_stores.update(stores)
            
            from inventory.models import Location
            return Location.objects.filter(id__in=[store.id for store in accessible_stores])
        
        elif self.role == UserRole.STOCK_INCHARGE:
            # Stock Incharge: return ONLY the stores they are directly assigned to
            return self.assigned_locations.filter(is_store=True)
        
        # Fallback
        from inventory.models import Location
        return Location.objects.none()
    
    def can_create_user(self, target_role):
        """Check if user can create users with target role"""
        if self.role == UserRole.SYSTEM_ADMIN:
            return True
        
        if self.role == UserRole.LOCATION_HEAD:
            # Location Head can only create Stock Incharge
            return target_role == UserRole.STOCK_INCHARGE
        
        return False
    
    def can_create_location(self, parent_location=None):
        """Check if user can create a location"""
        if self.role == UserRole.SYSTEM_ADMIN:
            return True
        
        if self.role == UserRole.LOCATION_HEAD:
            # Can create root locations or children of assigned locations
            if parent_location is None:
                return True
            return self.has_location_access(parent_location)
        
        return False
    
    def can_edit_location(self, location):
        """Check if user can edit a location"""
        if self.role == UserRole.SYSTEM_ADMIN:
            return True
        
        if self.role == UserRole.LOCATION_HEAD:
            return self.has_location_access(location)
        
        return False
    
    def has_permission(self, permission_key):
        """Check if user has a custom permission"""
        if self.role == UserRole.SYSTEM_ADMIN:
            return True
        return self.custom_permissions.get(permission_key, False)
    
    def save(self, *args, **kwargs):
        if not self.employee_id:
            last_profile = UserProfile.objects.order_by('-id').first()
            next_id = (last_profile.id + 1) if last_profile else 1
            self.employee_id = f"EMP{next_id:05d}"
        super().save(*args, **kwargs)

class PermissionTemplate(models.Model):
    name = models.CharField(max_length=100, unique=True)
    role = models.CharField(max_length=20, choices=UserRole.choices)
    permissions = models.JSONField(default=dict)
    description = models.TextField(blank=True, null=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.name} ({self.get_role_display()})"

class UserActivity(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='activities', null=True, blank=True)
    action = models.CharField(max_length=100)
    model = models.CharField(max_length=50)
    object_id = models.IntegerField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['user', '-created_at']),
        ]
    
    def __str__(self):
        username = self.user.username if self.user else 'System'
        return f"{username} - {self.action} - {self.created_at}"