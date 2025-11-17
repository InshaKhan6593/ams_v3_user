# user_management/models.py - ENHANCED VERSION
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
    
    # ENHANCED: assigned_locations with specific rules:
    # - LOCATION_HEAD: must be assigned to standalone locations
    # - STOCK_INCHARGE: must be assigned to stores
    assigned_locations = models.ManyToManyField(
        'inventory.Location', 
        blank=True, 
        related_name='assigned_users',
        help_text="Locations this user is directly assigned to manage"
    )
    
    created_by = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='created_profiles'
    )
    phone = models.CharField(max_length=20, blank=True, null=True)
    employee_id = models.CharField(max_length=50, unique=True, null=True, blank=True)
    department = models.ForeignKey(
        'inventory.Location', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='department_users'
    )
    custom_permissions = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        indexes = [
            models.Index(fields=['role']),
            models.Index(fields=['employee_id']),
            models.Index(fields=['is_active']),
        ]
        
    def __str__(self):
        return f"{self.user.username} - {self.get_role_display()}"
    
    def clean(self):
        """Enhanced validation with standalone location rules"""
        super().clean()
        
        # Skip M2M validation during creation
        if not self.pk:
            return
        
        # RULE 1: Location Head must be assigned to standalone locations
        if self.pk and self.role == UserRole.LOCATION_HEAD:
            assigned_locs = self.assigned_locations.all()
            if assigned_locs.exists():
                non_standalone = assigned_locs.filter(is_standalone=False)
                if non_standalone.exists():
                    raise ValidationError({
                        'assigned_locations': f"Location Head must be assigned to standalone locations only. "
                        f"The following are not standalone: {', '.join([loc.name for loc in non_standalone])}"
                    })
        
        # RULE 2: Stock Incharge must be assigned to stores
        if self.pk and self.role == UserRole.STOCK_INCHARGE:
            assigned_locs = self.assigned_locations.all()
            if assigned_locs.exists():
                non_stores = assigned_locs.filter(is_store=False)
                if non_stores.exists():
                    raise ValidationError({
                        'assigned_locations': f"Stock Incharge must be assigned to store locations only. "
                        f"The following are not stores: {', '.join([loc.name for loc in non_stores])}"
                    })
        
        # RULE 3: System Admin must be superuser
        if self.role == UserRole.SYSTEM_ADMIN and not self.user.is_superuser:
            raise ValidationError({
                'role': "Only superusers can be assigned as System Administrators."
            })
    
    def get_responsible_location(self):
        """
        Get the location this user is primarily responsible for.
        - SYSTEM_ADMIN: Root location (Main University)
        - LOCATION_HEAD: Their assigned standalone location
        - STOCK_INCHARGE: The store they're assigned to
        - AUDITOR: Root location (Main University)
        """
        if self.role in [UserRole.SYSTEM_ADMIN, UserRole.AUDITOR]:
            return self._get_root_location()
        
        if self.role == UserRole.LOCATION_HEAD:
            # Get first standalone location
            return self.assigned_locations.filter(is_standalone=True).first()
        
        if self.role == UserRole.STOCK_INCHARGE:
            # Get first assigned store
            return self.assigned_locations.filter(is_store=True).first()
        
        return None
    
    def get_main_location(self):
        """Alias for get_responsible_location for backward compatibility"""
        return self.get_responsible_location()
    
    def _get_root_location(self):
        """Get the root location (Main University)"""
        from inventory.models import Location
        return Location.objects.filter(parent_location__isnull=True).first()
    
    def has_location_access(self, location):
        """
        Enhanced access check with standalone location awareness.
        Returns True if user has access to the given location.
        """
        if self.role == UserRole.SYSTEM_ADMIN:
            return True
        
        if self.role == UserRole.AUDITOR:
            return True
        
        responsible_loc = self.get_responsible_location()
        if not responsible_loc:
            return False
        
        # Direct assignment
        if self.assigned_locations.filter(id=location.id).exists():
            return True
        
        # For Location Head: access to all descendants of their standalone location
        if self.role == UserRole.LOCATION_HEAD:
            if location == responsible_loc:
                return True
            return location.is_descendant_of(responsible_loc)
        
        # For Stock Incharge: access to locations in same standalone hierarchy
        if self.role == UserRole.STOCK_INCHARGE:
            # Stock Incharge can access locations under the same parent standalone
            location_parent_standalone = location.get_parent_standalone()
            store_parent_standalone = responsible_loc.get_parent_standalone() if responsible_loc.is_store else None
            
            if location_parent_standalone and store_parent_standalone:
                return location_parent_standalone == store_parent_standalone
        
        return False
    
    def get_accessible_locations(self):
        """
        Get all locations user can access.
        - SYSTEM_ADMIN/AUDITOR: All active locations
        - LOCATION_HEAD: Their standalone location + all descendants
        - STOCK_INCHARGE: All locations under the same parent standalone
        """
        from inventory.models import Location
        
        if self.role in [UserRole.SYSTEM_ADMIN, UserRole.AUDITOR]:
            return Location.objects.filter(is_active=True)
        
        responsible_loc = self.get_responsible_location()
        if not responsible_loc:
            return Location.objects.none()
        
        if self.role == UserRole.LOCATION_HEAD:
            # All descendants including self
            return responsible_loc.get_descendants(include_self=True)
        
        if self.role == UserRole.STOCK_INCHARGE:
            # All locations under the same parent standalone
            parent_standalone = responsible_loc.get_parent_standalone()
            if parent_standalone:
                return parent_standalone.get_descendants(include_self=True)
        
        return Location.objects.none()
    
    def get_accessible_stores(self):
        """
        Get only stores that user can access.
        - SYSTEM_ADMIN/AUDITOR: All active stores
        - LOCATION_HEAD: All stores in their hierarchy
        - STOCK_INCHARGE: Their assigned stores + main store of hierarchy
        """
        from inventory.models import Location
        
        if self.role in [UserRole.SYSTEM_ADMIN, UserRole.AUDITOR]:
            return Location.objects.filter(is_store=True, is_active=True)
        
        if self.role == UserRole.LOCATION_HEAD:
            accessible_locations = self.get_accessible_locations()
            return accessible_locations.filter(is_store=True)
        
        if self.role == UserRole.STOCK_INCHARGE:
            # Assigned stores + main store of parent standalone
            assigned_stores = self.assigned_locations.filter(is_store=True, is_active=True)
            
            # Also include main store of the parent standalone location
            responsible_loc = self.get_responsible_location()
            if responsible_loc:
                parent_standalone = responsible_loc.get_parent_standalone()
                if parent_standalone:
                    main_store = parent_standalone.get_main_store()
                    if main_store:
                        # Combine assigned stores with main store
                        store_ids = list(assigned_stores.values_list('id', flat=True))
                        if main_store.id not in store_ids:
                            store_ids.append(main_store.id)
                        return Location.objects.filter(id__in=store_ids, is_active=True)
            
            return assigned_stores
        
        return Location.objects.none()
    
    def get_standalone_locations(self):
        """
        Get standalone locations user can access.
        Useful for creating sub-locations or assigning users.
        """
        accessible_locations = self.get_accessible_locations()
        return accessible_locations.filter(is_standalone=True)
    
    def can_create_user(self, target_role):
        """
        Check if user can create users with target role.
        - SYSTEM_ADMIN: Can create any role
        - LOCATION_HEAD: Can create STOCK_INCHARGE for stores in their hierarchy
        """
        if self.role == UserRole.SYSTEM_ADMIN:
            return True
        
        if self.role == UserRole.LOCATION_HEAD:
            return target_role == UserRole.STOCK_INCHARGE
        
        return False
    
    def can_assign_location_to_user(self, location, target_role):
        """
        Check if user can assign a specific location to a user with target_role.
        - SYSTEM_ADMIN: Can assign any location
        - LOCATION_HEAD: Can assign stores within their hierarchy to STOCK_INCHARGE
        """
        if self.role == UserRole.SYSTEM_ADMIN:
            return True
        
        if self.role == UserRole.LOCATION_HEAD:
            if target_role != UserRole.STOCK_INCHARGE:
                return False
            
            # Location must be a store in their hierarchy
            if not location.is_store:
                return False
            
            return self.has_location_access(location)
        
        return False
    
    def can_create_location(self, parent_location=None):
        """
        Check if user can create a location under parent_location.
        - SYSTEM_ADMIN: Can create anywhere including root
        - LOCATION_HEAD: Can create under their standalone location
        - STOCK_INCHARGE: Can create locations under their store's parent
        """
        if self.role == UserRole.SYSTEM_ADMIN:
            return True
        
        if parent_location is None:
            # Only SYSTEM_ADMIN can create root location
            return False
        
        if self.role == UserRole.LOCATION_HEAD:
            # Can create under their standalone location or its descendants
            return self.has_location_access(parent_location)
        
        if self.role == UserRole.STOCK_INCHARGE:
            # Can create under locations in their hierarchy
            return self.has_location_access(parent_location)
        
        return False
    
    def can_edit_location(self, location):
        """Check if user can edit a location"""
        if self.role == UserRole.SYSTEM_ADMIN:
            return True
        
        return self.has_location_access(location)
    
    def can_delete_location(self, location):
        """
        Check if user can delete a location.
        Additional restrictions:
        - Cannot delete if has children
        - Cannot delete if has instances
        """
        if self.role != UserRole.SYSTEM_ADMIN:
            return False
        
        # Prevent deletion of locations with children
        if location.child_locations.exists():
            return False
        
        # Prevent deletion of locations with instances
        from inventory.models import ItemInstance
        if ItemInstance.objects.filter(
            models.Q(source_location=location) | 
            models.Q(current_location=location)
        ).exists():
            return False
        
        return True
    
    def can_create_item(self):
        """Check if user can create items"""
        return self.role in [
            UserRole.SYSTEM_ADMIN,
            UserRole.LOCATION_HEAD,
            UserRole.STOCK_INCHARGE
        ]
    
    def get_item_default_locations(self):
        """
        Get standalone locations that can be used as item default locations.
        Items must belong to standalone locations.
        """
        if self.role in [UserRole.SYSTEM_ADMIN, UserRole.AUDITOR]:
            from inventory.models import Location
            return Location.objects.filter(is_standalone=True, is_active=True)
        
        if self.role == UserRole.LOCATION_HEAD:
            # Location Head can create items for their standalone location
            responsible_loc = self.get_responsible_location()
            if responsible_loc and responsible_loc.is_standalone:
                from inventory.models import Location
                return Location.objects.filter(id=responsible_loc.id)
        
        if self.role == UserRole.STOCK_INCHARGE:
            # Stock Incharge creates items for their parent standalone location
            responsible_loc = self.get_responsible_location()
            if responsible_loc:
                parent_standalone = responsible_loc.get_parent_standalone()
                if parent_standalone:
                    from inventory.models import Location
                    return Location.objects.filter(id=parent_standalone.id)
        
        from inventory.models import Location
        return Location.objects.none()
    
    def can_view_inspection_certificates(self):
        """Check if user can view inspection certificates"""
        return self.role in [
            UserRole.SYSTEM_ADMIN,
            UserRole.LOCATION_HEAD, 
            UserRole.STOCK_INCHARGE,
            UserRole.AUDITOR
        ]
    
    def can_create_inspection_certificates(self):
        """
        Check if user can create inspection certificates.
        Only Location Heads of standalone locations can create.
        """
        if self.role == UserRole.SYSTEM_ADMIN:
            return True
        
        if self.role == UserRole.LOCATION_HEAD:
            # Must be assigned to a standalone location
            responsible_loc = self.get_responsible_location()
            return responsible_loc and responsible_loc.is_standalone
        
        return False
    
    def can_manage_stock(self):
        """Check if user can manage stock entries"""
        return self.role in [
            UserRole.SYSTEM_ADMIN,
            UserRole.STOCK_INCHARGE
        ]
    
    def is_main_store_incharge(self):
        """
        Check if this Stock Incharge manages a main store.
        Main store incharges have special permission to issue UP the hierarchy.
        """
        if self.role != UserRole.STOCK_INCHARGE:
            return False
        
        # Check if any assigned store is a main store
        return self.assigned_locations.filter(
            is_store=True,
            is_main_store=True,
            is_active=True
        ).exists()
    
    def can_issue_to_parent_standalone(self):
        """
        Check if user can issue items to parent standalone location.
        Only main store incharges can do this.
        """
        if not self.is_main_store_incharge():
            return False
        
        # Get the main store they manage
        main_store = self.assigned_locations.filter(
            is_store=True,
            is_main_store=True,
            is_active=True
        ).first()
        
        if not main_store:
            return False
        
        return main_store.can_issue_to_parent_standalone()
    
    def get_parent_standalone_for_issuance(self):
        """
        Get the parent standalone location this main store incharge can issue to.
        Returns None if not applicable.
        """
        if not self.is_main_store_incharge():
            return None
        
        main_store = self.assigned_locations.filter(
            is_store=True,
            is_main_store=True,
            is_active=True
        ).first()
        
        if not main_store:
            return None
        
        return main_store.get_parent_standalone_for_issuance()
    
    def can_audit(self):
        """Check if user can perform audit actions"""
        return self.role in [UserRole.SYSTEM_ADMIN, UserRole.AUDITOR]
    
    def has_permission(self, permission_key):
        """Check if user has a custom permission"""
        if self.role == UserRole.SYSTEM_ADMIN:
            return True
        
        permission_mapping = {
            'view_inspection_certificates': self.can_view_inspection_certificates(),
            'create_inspection_certificates': self.can_create_inspection_certificates(),
            'manage_stock': self.can_manage_stock(),
            'audit': self.can_audit(),
            'create_users': self.can_create_user(UserRole.STOCK_INCHARGE),
            'create_locations': self.can_create_location(),
            'create_items': self.can_create_item(),
            'issue_to_parent_standalone': self.can_issue_to_parent_standalone(),
            'is_main_store_incharge': self.is_main_store_incharge(),
        }
        
        if permission_key in permission_mapping:
            return permission_mapping[permission_key]
        
        return self.custom_permissions.get(permission_key, False)
    
    def get_permissions_summary(self):
        """Get comprehensive permissions summary for frontend"""
        responsible_loc = self.get_responsible_location()
        parent_standalone_for_issuance = self.get_parent_standalone_for_issuance()
        
        return {
            'role': self.role,
            'role_display': self.get_role_display(),
            'can_create_users': self.can_create_user(UserRole.STOCK_INCHARGE),
            'can_create_locations': self.can_create_location(),
            'can_create_items': self.can_create_item(),
            'can_manage_stock': self.can_manage_stock(),
            'can_audit': self.can_audit(),
            'can_view_inspection_certificates': self.can_view_inspection_certificates(),
            'can_create_inspection_certificates': self.can_create_inspection_certificates(),
            'is_main_store_incharge': self.is_main_store_incharge(),
            'can_issue_to_parent_standalone': self.can_issue_to_parent_standalone(),
            'responsible_location': {
                'id': responsible_loc.id if responsible_loc else None,
                'name': responsible_loc.name if responsible_loc else None,
                'type': responsible_loc.location_type if responsible_loc else None,
                'is_standalone': responsible_loc.is_standalone if responsible_loc else None,
                'is_store': responsible_loc.is_store if responsible_loc else None,
                'is_main_store': responsible_loc.is_main_store if responsible_loc else None,
            } if responsible_loc else None,
            'parent_standalone_for_issuance': {
                'id': parent_standalone_for_issuance.id if parent_standalone_for_issuance else None,
                'name': parent_standalone_for_issuance.name if parent_standalone_for_issuance else None,
                'code': parent_standalone_for_issuance.code if parent_standalone_for_issuance else None,
            } if parent_standalone_for_issuance else None,
            'accessible_stores_count': self.get_accessible_stores().count(),
            'accessible_locations_count': self.get_accessible_locations().count(),
            'accessible_standalone_count': self.get_standalone_locations().count(),
        }
    
    def save(self, *args, **kwargs):
        # Generate employee ID if not set
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
    
    class Meta:
        indexes = [
            models.Index(fields=['role']),
            models.Index(fields=['name']),
        ]
    
    def __str__(self):
        return f"{self.name} ({self.get_role_display()})"
    
    def clean(self):
        """Validate permission template"""
        super().clean()
        
        if not isinstance(self.permissions, dict):
            raise ValidationError({'permissions': 'Permissions must be a JSON object'})

class UserActivity(models.Model):
    ACTION_CHOICES = [
        ('LOGIN', 'User Login'),
        ('LOGOUT', 'User Logout'),
        ('CREATE_USER', 'Create User'),
        ('UPDATE_USER', 'Update User'),
        ('DELETE_USER', 'Delete User'),
        ('CREATE_LOCATION', 'Create Location'),
        ('UPDATE_LOCATION', 'Update Location'),
        ('DELETE_LOCATION', 'Delete Location'),
        ('CREATE_ITEM', 'Create Item'),
        ('UPDATE_ITEM', 'Update Item'),
        ('DELETE_ITEM', 'Delete Item'),
        ('CREATE_INSPECTION_CERTIFICATE', 'Create Inspection Certificate'),
        ('UPDATE_INSPECTION_CERTIFICATE', 'Update Inspection Certificate'),
        ('STOCK_ENTRY', 'Stock Entry'),
        ('INSTANCE_MOVEMENT', 'Instance Movement'),
        ('ACKNOWLEDGE_TRANSFER', 'Acknowledge Transfer'),
        ('GENERATE_QR_CODE', 'Generate QR Code'),
        ('UPWARD_TRANSFER', 'Upward Transfer to Parent Standalone'),
    ]
    
    user = models.ForeignKey(
        User, 
        on_delete=models.CASCADE, 
        related_name='activities', 
        null=True, 
        blank=True
    )
    action = models.CharField(max_length=50, choices=ACTION_CHOICES)
    model = models.CharField(max_length=50)
    object_id = models.IntegerField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['action']),
            models.Index(fields=['model']),
        ]
        verbose_name_plural = 'User Activities'
    
    def __str__(self):
        username = self.user.username if self.user else 'System'
        return f"{username} - {self.get_action_display()} - {self.created_at.strftime('%Y-%m-%d %H:%M')}"
    
    @classmethod
    def log_activity(cls, user, action, model, object_id=None, details=None, ip_address=None, user_agent=None):
        """Helper method to log user activities"""
        return cls.objects.create(
            user=user,
            action=action,
            model=model,
            object_id=object_id,
            details=details or {},
            ip_address=ip_address,
            user_agent=user_agent
        )

# Signal to create user profile when user is created
@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(user=instance)

@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    if hasattr(instance, 'profile'):
        instance.profile.save()