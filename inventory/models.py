# models.py - ENHANCED VERSION
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from django.core.validators import MinValueValidator
from django.core.exceptions import ValidationError
import qrcode
from io import BytesIO
import base64
from django.dispatch import receiver
from django.db.models.signals import post_save, post_delete
from django.db import transaction
import json

# ==================== LOCATION MODELS ====================
class LocationType(models.TextChoices):
    DEPARTMENT = 'DEPARTMENT', 'Department'
    BUILDING = 'BUILDING', 'Building'
    STORE = 'STORE', 'Store'
    ROOM = 'ROOM', 'Room'
    LAB = 'LAB', 'Lab'
    JUNKYARD = 'JUNKYARD', 'Junkyard'
    OFFICE = 'OFFICE', 'Office'
    AV_HALL = 'AV_HALL', 'AV Hall'
    AUDITORIUM = 'AUDITORIUM', 'Auditorium'
    OTHER = 'OTHER', 'Other'

class Location(models.Model):
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=255, unique=True)
    parent_location = models.ForeignKey(
        'self', 
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='child_locations'
    )
    location_type = models.CharField(
        max_length=20,
        choices=LocationType.choices
    )
    is_store = models.BooleanField(default=False)
    description = models.TextField(null=True, blank=True)
    address = models.TextField(null=True, blank=True)
    in_charge = models.CharField(max_length=150, null=True, blank=True)
    contact_number = models.CharField(max_length=20, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    
    # ENHANCED: Standalone indicates this location can have sub-locations
    # and will get its own main store automatically
    is_standalone = models.BooleanField(
        default=False, 
        help_text="If true, this location can have sub-locations and will get a main store"
    )
    
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_locations'
    )
    
    # Auto-created store tracking
    auto_created_store = models.OneToOneField(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='parent_location_ref'
    )
    is_auto_created = models.BooleanField(default=False)
    is_main_store = models.BooleanField(
        default=False, 
        help_text="Indicates if this is the main store for its parent standalone location"
    )
    
    # Hierarchy tracking
    hierarchy_level = models.PositiveIntegerField(default=0, editable=False)
    hierarchy_path = models.CharField(max_length=765, blank=True, editable=False)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['code']),
            models.Index(fields=['location_type']),
            models.Index(fields=['is_store']),
            models.Index(fields=['is_active']),
            models.Index(fields=['is_standalone']),
            models.Index(fields=['is_main_store']),
            models.Index(fields=['hierarchy_level']),
            models.Index(fields=['hierarchy_path']),
        ]

    def __str__(self):
        return f'{self.name} ({self.code})'
    
    def clean(self):
        """Enhanced validation for standalone locations and main stores"""
        super().clean()
        
        # RULE 1: Only one root location allowed (parent=None)
        if not self.parent_location and self.pk:
            existing_root = Location.objects.filter(
                parent_location__isnull=True
            ).exclude(pk=self.pk).exists()
            if existing_root:
                raise ValidationError(
                    "Only one root location (Main University) is allowed. "
                    "All other locations must have a parent."
                )
        
        # RULE 2: Root location must be standalone
        if not self.parent_location and not self.is_standalone:
            raise ValidationError(
                "Root location (Main University) must be marked as standalone"
            )
        
        # RULE 3: Store locations cannot be standalone
        if self.is_store and self.is_standalone:
            raise ValidationError("Store locations cannot be marked as standalone")
        
        # RULE 4: Store locations cannot have children
        if self.is_store and self.pk:
            if self.child_locations.exists():
                raise ValidationError("Store locations cannot have child locations")
        
        # RULE 5: Only one main store per standalone location
        if self.is_main_store and self.is_store:
            if self.pk:
                parent_standalone = self.get_parent_standalone()
                if parent_standalone:
                    existing_main_stores = Location.objects.filter(
                        is_main_store=True,
                        is_store=True,
                        is_active=True,
                        parent_location=parent_standalone
                    ).exclude(pk=self.pk)
                    
                    if existing_main_stores.exists():
                        raise ValidationError(
                            f"There can only be one main store for {parent_standalone.name}. "
                            f"'{existing_main_stores.first().name}' is already the main store."
                        )
        
        # RULE 6: Prevent circular parent references
        if self.parent_location:
            parent = self.parent_location
            visited = set()
            while parent:
                if parent.id in visited:
                    raise ValidationError("Circular parent reference detected")
                visited.add(parent.id)
                if parent.id == self.id:
                    raise ValidationError("Circular parent reference detected")
                parent = parent.parent_location
        
        # RULE 7: Auto-created stores must have parent
        if self.is_auto_created and not self.parent_location:
            raise ValidationError("Auto-created stores must have a parent location")
        
        # RULE 8: Stores cannot be parents
        if self.parent_location and self.parent_location.is_store:
            raise ValidationError("Store locations cannot be parent locations")
        
        # RULE 9: Only stores can be main stores
        if self.is_main_store and not self.is_store:
            raise ValidationError("Only store locations can be marked as main stores")
        
        # RULE 10: Main stores must be auto-created for standalone locations
        if self.is_main_store and not self.is_auto_created and self.parent_location:
            if not self.parent_location.is_standalone:
                raise ValidationError("Main stores can only be created for standalone locations")
    
    def save(self, *args, **kwargs):
        # Calculate hierarchy level and path
        if self.parent_location:
            self.hierarchy_level = self.parent_location.hierarchy_level + 1
            self.hierarchy_path = f"{self.parent_location.hierarchy_path}/{self.code}"
        else:
            # Root location
            self.hierarchy_level = 0
            self.hierarchy_path = self.code
        
        super().save(*args, **kwargs)
    
    def get_full_path(self):
        """Get hierarchical path of location"""
        path = [self.name]
        parent = self.parent_location
        while parent:
            path.insert(0, parent.name)
            parent = parent.parent_location
        return ' > '.join(path)
    
    def get_depth(self):
        """Calculate the depth of this location in the hierarchy"""
        return self.hierarchy_level
    
    def get_parent_standalone(self):
        """
        Get the parent standalone location for this location.
        This is crucial for determining permissions and main store.
        """
        if self.is_standalone:
            return self
        
        if not self.parent_location:
            return None
        
        # Traverse up until we find a standalone location
        current = self.parent_location
        while current:
            if current.is_standalone:
                return current
            current = current.parent_location
        
        return None
    
    def get_main_store(self):
        """
        Get the main store for this location's hierarchy.
        - If this is standalone, return its auto-created main store
        - If not standalone, find parent standalone and return its main store
        """
        if self.is_store and self.is_main_store:
            return self
        
        # If this is standalone, return its auto-created store
        if self.is_standalone and self.auto_created_store:
            return self.auto_created_store
        
        # Otherwise, find parent standalone location
        parent_standalone = self.get_parent_standalone()
        if parent_standalone and parent_standalone.auto_created_store:
            return parent_standalone.auto_created_store
        
        return None
    
    def get_all_stores(self):
        """Get all stores under this location including auto-created main store"""
        stores = []
        
        # Include auto-created main store
        if self.auto_created_store:
            stores.append(self.auto_created_store)
        
        # Get all child stores recursively
        def get_child_stores(location):
            children = location.child_locations.filter(is_active=True)
            for child in children:
                if child.is_store:
                    stores.append(child)
                else:
                    # Only recurse into non-store locations
                    get_child_stores(child)
        
        get_child_stores(self)
        return Location.objects.filter(id__in=[store.id for store in stores])
    
    def is_descendant_of(self, location):
        """Check if this location is a descendant of the given location"""
        return self.hierarchy_path.startswith(f"{location.hierarchy_path}/")
    
    def get_root_location(self):
        """Get the root location (Main University)"""
        if not self.parent_location:
            return self
        
        # Use hierarchy_path to find root efficiently
        root_code = self.hierarchy_path.split('/')[0]
        return Location.objects.filter(code=root_code, parent_location__isnull=True).first()
    
    def get_descendants(self, include_self=False):
        """Get all descendants using hierarchy_path for efficient querying"""
        if include_self:
            return Location.objects.filter(
                hierarchy_path__startswith=self.hierarchy_path,
                is_active=True
            )
        return Location.objects.filter(
            hierarchy_path__startswith=f"{self.hierarchy_path}/",
            is_active=True
        )
    
    def get_immediate_children(self):
        """Get immediate children only"""
        return self.child_locations.filter(is_active=True)
    
    def get_standalone_children(self):
        """Get only standalone children (departments, buildings, etc.)"""
        return self.child_locations.filter(is_standalone=True, is_active=True)
    
    def can_have_sub_locations(self):
        """
        Determine if this location type can have sub-locations.
        Only standalone locations can have meaningful sub-locations.
        """
        return self.is_standalone
    
    def can_transfer_to(self, target_location):
        """
        Enhanced transfer logic:
        - Main store can transfer UP to parent standalone location
        - Main store can transfer to any location within its hierarchy
        - Regular stores can transfer within same hierarchy only
        """
        if not self.is_store:
            return False
        
        # Get parent standalone locations
        self_parent_standalone = self.get_parent_standalone()
        target_parent_standalone = target_location.get_parent_standalone()
        
        # Same hierarchy: always allowed
        if self_parent_standalone == target_parent_standalone:
            return True
        
        # SPECIAL RULE: Main store can transfer UP to parent standalone location
        if self.is_main_store and self_parent_standalone:
            # Check if target is the direct parent standalone location
            if self_parent_standalone.parent_location:
                parent_of_parent_standalone = self_parent_standalone.parent_location.get_parent_standalone()
                if target_location == parent_of_parent_standalone:
                    return True
        
        return False
    
    def can_issue_to_parent_standalone(self):
        """
        Check if this store (must be main store) can issue to parent standalone location.
        This is the special permission for main stores.
        """
        if not self.is_store or not self.is_main_store:
            return False
        
        parent_standalone = self.get_parent_standalone()
        if not parent_standalone or not parent_standalone.parent_location:
            return False
        
        # Can issue to parent's parent standalone
        return True
    
    def get_parent_standalone_for_issuance(self):
        """
        Get the parent standalone location this main store can issue to.
        Returns None if not applicable.
        """
        if not self.can_issue_to_parent_standalone():
            return None
        
        parent_standalone = self.get_parent_standalone()
        if parent_standalone and parent_standalone.parent_location:
            return parent_standalone.parent_location.get_parent_standalone()
        
        return None

    @property
    def is_root_location(self):
        """Check if this is the root location (Main University)"""
        return self.parent_location is None

# Signal to auto-create main store for standalone locations
@receiver(post_save, sender=Location)
def auto_create_store_for_standalone(sender, instance, created, **kwargs):
    """
    Automatically create a main store for standalone locations.
    This includes the root location (Main University) and any department/building marked as standalone.
    """
    if created and instance.is_standalone and not instance.is_store:
        # Generate store code and name
        store_code = f"{instance.code}-MAIN-STORE"
        store_name = f"{instance.name} - Main Store"
        
        # Create the main store
        store = Location.objects.create(
            name=store_name,
            code=store_code,
            parent_location=instance,
            location_type=LocationType.STORE,
            is_store=True,
            is_auto_created=True,
            is_main_store=True,
            is_standalone=False,
            description=f"Auto-created main store for {instance.name}",
            address=instance.address,
            in_charge=instance.in_charge,
            contact_number=instance.contact_number,
            is_active=True,
            created_by=instance.created_by
        )
        
        # Link back to parent
        instance.auto_created_store = store
        instance.save(update_fields=['auto_created_store'])
        
        # Log activity if possible
        try:
            from user_management.models import UserActivity
            UserActivity.objects.create(
                user=instance.created_by,
                action='AUTO_CREATE_MAIN_STORE',
                model='Location',
                object_id=store.id,
                details={
                    'parent_location': instance.name,
                    'store_name': store_name,
                    'store_code': store_code,
                    'is_main_store': True,
                    'is_standalone_parent': True
                }
            )
        except:
            pass

# ==================== CATEGORY AND ITEM MODELS ====================
class Category(models.Model):
    name = models.CharField(max_length=255, unique=True)
    code = models.CharField(max_length=20, unique=True)
    description = models.TextField(blank=True, null=True)
    parent_category = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='subcategories'
    )
    depreciation_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0.00,
        help_text="Annual depreciation rate in percentage (e.g., 10.00 for 10%)"
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = 'Categories'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} - {self.code}"

    def calculate_wdv_depreciation(self, opening_value, years=1):
        """
        Calculate depreciation using Written Down Value (WDV) method.
        
        Args:
            opening_value (Decimal): The opening/current value of the asset
            years (int): Number of years to calculate depreciation for
            
        Returns:
            dict: Contains depreciation amount, closing value, and accumulated depreciation
        """
        from decimal import Decimal
        
        if self.depreciation_rate == 0:
            return {
                'depreciation_amount': Decimal('0.00'),
                'closing_value': opening_value,
                'accumulated_depreciation': Decimal('0.00')
            }
        
        rate = self.depreciation_rate / Decimal('100')
        current_value = Decimal(str(opening_value))
        total_depreciation = Decimal('0.00')
        
        for _ in range(years):
            year_depreciation = current_value * rate
            total_depreciation += year_depreciation
            current_value -= year_depreciation
        
        return {
            'depreciation_amount': total_depreciation,
            'closing_value': current_value,
            'accumulated_depreciation': total_depreciation
        }

    def get_year_wise_depreciation(self, opening_value, years=5):
        """
        Get year-by-year depreciation breakdown using WDV method.
        
        Args:
            opening_value (Decimal): The initial value of the asset
            years (int): Number of years to project
            
        Returns:
            list: Year-wise depreciation details
        """
        from decimal import Decimal
        
        schedule = []
        rate = self.depreciation_rate / Decimal('100')
        current_value = Decimal(str(opening_value))
        
        for year in range(1, years + 1):
            year_depreciation = current_value * rate
            closing_value = current_value - year_depreciation
            
            schedule.append({
                'year': year,
                'opening_value': current_value,
                'depreciation_rate': self.depreciation_rate,
                'depreciation_amount': year_depreciation,
                'closing_value': closing_value
            })
            
            current_value = closing_value
        
        return schedule

class Item(models.Model):
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=50, unique=True)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='items')
    description = models.TextField(blank=True, null=True)
    acct_unit = models.CharField(max_length=255, help_text="Accounting unit/measurement")
    specifications = models.TextField(blank=True, null=True)
    
    # ENHANCED: default_location must be a standalone location
    default_location = models.ForeignKey(
        Location, 
        on_delete=models.PROTECT,
        related_name='default_items',
        limit_choices_to={'is_standalone': True},
        help_text="Must be a standalone location (Department, Main University, etc.)"
    )
    
    total_quantity = models.PositiveIntegerField(default=0)
    reorder_level = models.PositiveIntegerField(default=0)
    reorder_quantity = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_items'
    )

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['code']),
            models.Index(fields=['category']),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"
    
    def clean(self):
        """Validate that default_location is standalone"""
        if self.default_location and not self.default_location.is_standalone:
            raise ValidationError({
                'default_location': "Items must belong to a standalone location (Department, Main University, etc.)"
            })
    
    def update_total_quantity(self):
        """Update total quantity based on instances"""
        self.total_quantity = self.instances.count()
        self.save(update_fields=['total_quantity'])

# ==================== INSPECTION CERTIFICATE MODELS ====================
class InspectionStage(models.TextChoices):
    INITIATED = 'INITIATED', 'Initiated - Basic Info'
    STOCK_DETAILS = 'STOCK_DETAILS', 'Stock Details Entry'
    CENTRAL_REGISTER = 'CENTRAL_REGISTER', 'Central Register Entry'  # ← NEW STAGE
    AUDIT_REVIEW = 'AUDIT_REVIEW', 'Audit Review'
    COMPLETED = 'COMPLETED', 'Completed'
    REJECTED = 'REJECTED', 'Rejected'

class InspectionCertificate(models.Model):
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('IN_PROGRESS', 'In Progress'),
        ('CONFIRMED', 'Confirmed'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
    ]

    # Basic Info (Stage 1 - Location Head)
    certificate_no = models.CharField(max_length=50, unique=True, editable=False)
    date = models.DateField()
    contract_no = models.CharField(max_length=100)
    contract_date = models.DateField(null=True, blank=True)
    contractor_name = models.CharField(max_length=255)
    contractor_address = models.TextField(blank=True, null=True)
    indenter = models.CharField(max_length=150)
    indent_no = models.CharField(max_length=100)
    
    # ENHANCED: department must be standalone location
    department = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        related_name='department_certificates',
        limit_choices_to={'is_standalone': True},
        help_text="Must be a standalone location (Department, Main University, etc.)"
    )
    
    date_of_delivery = models.DateField(null=True, blank=True)
    delivery_type = models.CharField(
        max_length=20, 
        choices=[('PART', 'Part'), ('FULL', 'Full')],
        default='FULL'
    )
    remarks = models.TextField(blank=True, null=True)

    # Stock Details (Stage 2 - Stock Incharge)
    inspected_by = models.CharField(max_length=150, blank=True, null=True)
    date_of_inspection = models.DateField(null=True, blank=True)
    consignee_name = models.CharField(max_length=150, blank=True, null=True)
    consignee_designation = models.CharField(max_length=150, blank=True, null=True)

    # Audit/Finance Details (Stage 3 - Auditor)
    dead_stock_register_no = models.CharField(max_length=100, blank=True, null=True)
    dead_stock_page_no = models.CharField(max_length=100, blank=True, null=True)
    central_store_entry_date = models.DateField(null=True, blank=True)
    finance_check_date = models.DateField(null=True, blank=True)

    # File Attachments
    certificate_image = models.ImageField(
        upload_to='inspection_certificates/%Y/%m/',
        null=True,
        blank=True
    )
    supporting_documents = models.FileField(
        upload_to='inspection_documents/%Y/%m/',
        null=True,
        blank=True
    )

    # Workflow Tracking
    stage = models.CharField(
        max_length=20, 
        choices=InspectionStage.choices, 
        default=InspectionStage.INITIATED
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='IN_PROGRESS')

    # User Tracking for Each Stage
    initiated_by = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='initiated_certificates'
    )
    initiated_at = models.DateTimeField(null=True, blank=True)
    
    stock_filled_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stock_filled_certificates'
    )
    stock_filled_at = models.DateTimeField(null=True, blank=True)
    
    auditor_reviewed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='auditor_reviewed_certificates'
    )
    auditor_reviewed_at = models.DateTimeField(null=True, blank=True)
    
    rejected_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='rejected_certificates'
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, null=True)
    rejection_stage = models.CharField(
        max_length=20,
        choices=InspectionStage.choices,
        null=True,
        blank=True
    )

    # Legacy fields for compatibility
    created_by = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='created_certificates'
    )
    acknowledged_by = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='acknowledged_certificates'
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    # Metadata
    stage_history = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date', '-created_at']
        indexes = [
            models.Index(fields=['certificate_no']),
            models.Index(fields=['status']),
            models.Index(fields=['stage']),
            models.Index(fields=['date']),
        ]

    def __str__(self):
        return f"IC-{self.certificate_no} ({self.stage})"

    def save(self, *args, **kwargs):
        if not self.certificate_no:
            now = timezone.now()
            year_month = now.strftime('%Y%m')
            last_cert = InspectionCertificate.objects.filter(
                certificate_no__startswith=f"IC-{year_month}"
            ).order_by('-certificate_no').first()
            
            if last_cert and last_cert.certificate_no:
                try:
                    last_seq = int(last_cert.certificate_no.split('-')[-1])
                    next_seq = last_seq + 1
                except (ValueError, IndexError):
                    next_seq = 1
            else:
                next_seq = 1
            
            self.certificate_no = f"IC-{year_month}-{next_seq:05d}"
        
        if self.stage == InspectionStage.REJECTED:
            self.status = 'CANCELLED'
        elif self.stage == InspectionStage.COMPLETED:
            self.status = 'COMPLETED'
        elif self.stage in [InspectionStage.INITIATED, InspectionStage.STOCK_DETAILS, InspectionStage.AUDIT_REVIEW]:
            self.status = 'IN_PROGRESS'
        
        super().save(*args, **kwargs)
    
    def clean(self):
        """Validate that department is standalone"""
        if self.department and not self.department.is_standalone:
            raise ValidationError({
                'department': "Inspection certificates must be for standalone locations only"
            })

    def get_main_store(self):
        """Get the main store for this certificate's department"""
        return self.department.get_main_store()

    def can_edit_stage(self, user, stage=None):
        """Check if user can edit at current or specified stage"""
        if not user or not hasattr(user, 'profile'):
            return False
        
        if stage is None:
            stage = self.stage
        
        from user_management.models import UserRole
        user_profile = user.profile
        
        if user_profile.role == UserRole.SYSTEM_ADMIN:
            return True
        
        if stage == InspectionStage.INITIATED:
            return (self.stage == InspectionStage.INITIATED and
                   user_profile.role == UserRole.LOCATION_HEAD and 
                   user_profile.has_location_access(self.department))
        
        elif stage == InspectionStage.STOCK_DETAILS:
            main_store = self.get_main_store()
            return (self.stage == InspectionStage.STOCK_DETAILS and
                   user_profile.role == UserRole.STOCK_INCHARGE and 
                   main_store and 
                   user_profile.has_location_access(main_store))
        
        elif stage == InspectionStage.AUDIT_REVIEW:
            return (self.stage == InspectionStage.AUDIT_REVIEW and
                   user_profile.role == UserRole.AUDITOR)
        
        return False

    def transition_stage(self, new_stage, user, rejection_reason=None):
        """Transition inspection certificate to a new stage"""
        from user_management.models import UserActivity
        
        old_stage = self.stage
        
        history_entry = {
            'from_stage': old_stage,
            'to_stage': new_stage,
            'user_id': user.id,
            'user_name': user.get_full_name() or user.username,
            'timestamp': timezone.now().isoformat(),
            'rejection_reason': rejection_reason if new_stage == InspectionStage.REJECTED else None
        }
        
        if not isinstance(self.stage_history, list):
            self.stage_history = []
        self.stage_history.append(history_entry)
        
        self.stage = new_stage
        
        if new_stage == InspectionStage.STOCK_DETAILS:
            if not self.stock_filled_by:
                self.stock_filled_by = user
                self.stock_filled_at = timezone.now()
        
        elif new_stage == InspectionStage.COMPLETED:
            self.auditor_reviewed_by = user
            self.auditor_reviewed_at = timezone.now()
            self.acknowledged_by = user
            self.acknowledged_at = timezone.now()
        
        elif new_stage == InspectionStage.REJECTED:
            self.rejected_by = user
            self.rejected_at = timezone.now()
            self.rejection_reason = rejection_reason
            self.rejection_stage = old_stage
        
        self.save()
        
        UserActivity.objects.create(
            user=user,
            action=f'STAGE_TRANSITION_{new_stage}',
            model='InspectionCertificate',
            object_id=self.id,
            details={
                'certificate_no': self.certificate_no,
                'from_stage': old_stage,
                'to_stage': new_stage
            }
        )

    def get_total_items(self):
        return self.inspection_items.count()
    
    def get_total_accepted(self):
        return sum(item.accepted_quantity for item in self.inspection_items.all())
    
    def get_total_rejected(self):
        return sum(item.rejected_quantity for item in self.inspection_items.all())

class InspectionItem(models.Model):
    inspection_certificate = models.ForeignKey(
        InspectionCertificate, 
        on_delete=models.CASCADE, 
        related_name='inspection_items'
    )
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name='inspection_entries')
    tendered_quantity = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])
    accepted_quantity = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])
    rejected_quantity = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])
    remarks = models.TextField(blank=True, null=True)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    
    # Stock register details
    stock_register_no = models.CharField(max_length=100, blank=True, null=True)
    stock_register_page_no = models.CharField(max_length=50, blank=True, null=True)
    stock_entry_date = models.DateField(null=True, blank=True)
    
    # Central store register details
    central_register_no = models.CharField(max_length=100, blank=True, null=True)
    central_register_page_no = models.CharField(max_length=50, blank=True, null=True)

    class Meta:
        unique_together = [['inspection_certificate', 'item']]

    def __str__(self):
        return f"{self.item.name} - {self.inspection_certificate.certificate_no}"
    
    def clean(self):
        if (self.accepted_quantity + self.rejected_quantity) > self.tendered_quantity:
            raise ValidationError(
                "Accepted + Rejected quantity cannot exceed tendered quantity"
            )

# ==================== ITEM INSTANCE MODEL ====================
class InstanceStatus(models.TextChoices):
    IN_STORE = 'IN_STORE', 'In Store'
    IN_TRANSIT = 'IN_TRANSIT', 'In Transit'
    IN_USE = 'IN_USE', 'In Use'
    TEMPORARY_ISSUED = 'TEMPORARY_ISSUED', 'Temporary Issued'
    UNDER_REPAIR = 'UNDER_REPAIR', 'Under Repair'
    DAMAGED = 'DAMAGED', 'Damaged'
    LOST = 'LOST', 'Lost'
    CONDEMNED = 'CONDEMNED', 'Condemned'
    DISPOSED = 'DISPOSED', 'Disposed'

class ItemInstance(models.Model):
    item = models.ForeignKey('Item', on_delete=models.PROTECT, related_name='instances')
    inspection_certificate = models.ForeignKey(
        'InspectionCertificate', 
        on_delete=models.PROTECT, 
        null=True, 
        blank=True,
        related_name='instances'
    )
    instance_code = models.CharField(max_length=50, unique=True, editable=False)
    
    # Enhanced Status tracking
    current_status = models.CharField(
        max_length=20, 
        choices=InstanceStatus.choices, 
        default=InstanceStatus.IN_STORE
    )
    previous_status = models.CharField(
        max_length=20, 
        choices=InstanceStatus.choices, 
        null=True,
        blank=True
    )
    status_changed_at = models.DateTimeField(null=True, blank=True)
    status_changed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='status_changed_instances'
    )
    
    # Source and current location
    source_location = models.ForeignKey(
        'Location', 
        on_delete=models.PROTECT, 
        related_name='source_instances',
        limit_choices_to={'is_store': True}
    )
    current_location = models.ForeignKey(
        'Location', 
        on_delete=models.PROTECT, 
        related_name='current_instances'
    )
    
    # Assignment tracking
    assigned_to = models.CharField(max_length=150, null=True, blank=True)
    assigned_date = models.DateTimeField(null=True, blank=True)
    expected_return_date = models.DateField(null=True, blank=True)
    actual_return_date = models.DateField(null=True, blank=True)
    
    # Condition tracking
    condition = models.CharField(
        max_length=50,
        choices=[
            ('NEW', 'New'),
            ('EXCELLENT', 'Excellent'),
            ('GOOD', 'Good'),
            ('FAIR', 'Fair'),
            ('POOR', 'Poor'),
            ('DAMAGED', 'Damaged'),
            ('BEYOND_REPAIR', 'Beyond Repair')
        ],
        default='NEW'
    )
    condition_notes = models.TextField(blank=True, null=True)
    
    # Damage/Repair tracking
    damage_reported_date = models.DateField(null=True, blank=True)
    damage_description = models.TextField(blank=True, null=True)
    repair_started_date = models.DateField(null=True, blank=True)
    repair_completed_date = models.DateField(null=True, blank=True)
    repair_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    repair_vendor = models.CharField(max_length=255, blank=True, null=True)
    
    # Disposal tracking
    disposal_date = models.DateField(null=True, blank=True)
    disposal_reason = models.TextField(blank=True, null=True)
    disposal_approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_disposals'
    )
    
    # QR Code
    qr_code_data = models.TextField(blank=True, null=True)
    qr_generated = models.BooleanField(default=False)
    
    # Attachments
    attachment = models.FileField(
        upload_to='instance_attachments/%Y/%m/',
        null=True,
        blank=True
    )
    
    # Purchase/Financial info
    purchase_date = models.DateField(null=True, blank=True)
    purchase_value = models.DecimalField(
        max_digits=15, 
        decimal_places=2, 
        null=True, 
        blank=True,
        help_text="Original purchase value/cost"
    )
    warranty_expiry = models.DateField(null=True, blank=True)
    
    # Enhanced QR data
    qr_data_json = models.JSONField(default=dict, blank=True)
    
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_instances'
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['instance_code']),
            models.Index(fields=['current_status']),
            models.Index(fields=['current_location']),
            models.Index(fields=['source_location']),
            models.Index(fields=['item']),
        ]

    def __str__(self):
        return f"{self.instance_code} ({self.get_current_status_display()})"
    
    def save(self, *args, **kwargs):
        if not self.instance_code:
            self.instance_code = self.generate_instance_code()
        
        if not self.qr_generated:
            self.generate_qr_code()
        
        is_new = self.pk is None
        super().save(*args, **kwargs)
        
        if is_new:
            self.item.update_total_quantity()

    def generate_instance_code(self):
        year = timezone.now().year
        last_instance = ItemInstance.objects.filter(
            item=self.item,
            instance_code__startswith=f"{self.item.code}-{year}"
        ).order_by('-id').first()

        if last_instance:
            try:
                last_seq = int(last_instance.instance_code.split('-')[-1])
                new_seq = last_seq + 1
            except (ValueError, IndexError):
                new_seq = 1
        else:
            new_seq = 1
        
        return f"{self.item.code}-{year}-{new_seq:04d}"
    
    # ==================== DEPRECIATION METHODS ====================
    
    def get_age_in_years(self):
        """Calculate the age of the instance in years"""
        from datetime import date
        
        if not self.purchase_date:
            return 0
        
        today = date.today()
        age_days = (today - self.purchase_date).days
        age_years = age_days / 365.25
        
        return age_years
    
    def get_depreciation_rate(self):
        """Get depreciation rate from category"""
        if self.item and self.item.category:
            return self.item.category.depreciation_rate
        return 0
    
    def get_current_book_value(self):
        """
        Calculate current book value using category's WDV method.
        Returns the depreciated value based on purchase_value and age.
        """
        from decimal import Decimal
        
        # If no purchase value, return 0
        if not self.purchase_value:
            return Decimal('0.00')
        
        # If no depreciation rate, return original value
        depreciation_rate = self.get_depreciation_rate()
        if depreciation_rate == 0:
            return self.purchase_value
        
        # Calculate years elapsed (rounded down)
        age_years = self.get_age_in_years()
        years_elapsed = int(age_years)
        
        # If purchased recently (less than 1 year), no depreciation yet
        if years_elapsed == 0:
            return self.purchase_value
        
        # Use category's WDV calculation method
        result = self.item.category.calculate_wdv_depreciation(
            opening_value=self.purchase_value,
            years=years_elapsed
        )
        
        return result['closing_value']
    
    def get_accumulated_depreciation(self):
        """
        Get total depreciation accumulated since purchase.
        """
        from decimal import Decimal
        
        if not self.purchase_value:
            return Decimal('0.00')
        
        current_value = self.get_current_book_value()
        return self.purchase_value - current_value
    
    def get_depreciation_schedule(self, years=5):
        """
        Get year-wise depreciation schedule for this instance.
        Useful for reporting and forecasting.
        """
        if not self.purchase_value or not self.item.category:
            return []
        
        return self.item.category.get_year_wise_depreciation(
            opening_value=self.purchase_value,
            years=years
        )
    
    def get_depreciation_info(self):
        """
        Get comprehensive depreciation information for this instance.
        Returns a dictionary with all depreciation-related data.
        """
        from decimal import Decimal
        
        if not self.purchase_value:
            return {
                'purchase_value': None,
                'current_book_value': Decimal('0.00'),
                'accumulated_depreciation': Decimal('0.00'),
                'depreciation_rate': self.get_depreciation_rate(),
                'age_in_years': self.get_age_in_years(),
                'purchase_date': self.purchase_date,
                'has_depreciation_data': False
            }
        
        current_value = self.get_current_book_value()
        accumulated = self.get_accumulated_depreciation()
        
        return {
            'purchase_value': float(self.purchase_value),
            'current_book_value': float(current_value),
            'accumulated_depreciation': float(accumulated),
            'depreciation_rate': float(self.get_depreciation_rate()),
            'age_in_years': round(self.get_age_in_years(), 2),
            'years_elapsed': int(self.get_age_in_years()),
            'purchase_date': str(self.purchase_date) if self.purchase_date else None,
            'depreciation_percentage': round((accumulated / self.purchase_value * 100), 2) if self.purchase_value > 0 else 0,
            'has_depreciation_data': True
        }
    
    # ==================== OTHER METHODS ====================
    
    def generate_qr_code(self):
        """Generate QR code with comprehensive instance information including depreciation"""
        import qrcode
        from io import BytesIO
        import base64
        
        depreciation_info = self.get_depreciation_info() if self.purchase_value else None
        
        qr_data = {
            'instance_code': self.instance_code,
            'item_name': self.item.name,
            'item_code': self.item.code,
            'item_description': self.item.description,
            'category': self.item.category.name if self.item.category else None,
            'specifications': self.item.specifications,
            'current_status': self.current_status,
            'current_status_display': self.get_current_status_display(),
            'current_location': self.current_location.name,
            'current_location_code': self.current_location.code,
            'source_location': self.source_location.name,
            'source_location_code': self.source_location.code,
            'condition': self.condition,
            'purchase_date': str(self.purchase_date) if self.purchase_date else None,
            'purchase_value': str(self.purchase_value) if self.purchase_value else None,
            'current_book_value': str(depreciation_info['current_book_value']) if depreciation_info else None,
            'warranty_expiry': str(self.warranty_expiry) if self.warranty_expiry else None,
            'created_at': str(self.created_at),
            'inspection_certificate': self.inspection_certificate.certificate_no if self.inspection_certificate else None,
            'assigned_to': self.assigned_to,
            'expected_return_date': str(self.expected_return_date) if self.expected_return_date else None,
            'last_updated': str(self.updated_at)
        }
        
        self.qr_data_json = qr_data
        
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(json.dumps(qr_data, indent=2))
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format='PNG')
        img_str = base64.b64encode(buffer.getvalue()).decode()
        
        self.qr_code_data = f"data:image/png;base64,{img_str}"
        self.qr_generated = True
    
    def get_qr_info(self):
        """Get comprehensive QR code information"""
        if not self.qr_data_json:
            self.generate_qr_code()
            self.save()
        return self.qr_data_json
    
    def change_status(self, new_status, user, location=None, notes=None):
        """Change instance status with proper tracking"""
        old_status = self.current_status
        old_location = self.current_location
        
        self.previous_status = old_status
        self.current_status = new_status
        self.status_changed_at = timezone.now()
        self.status_changed_by = user
        
        if location:
            self.current_location = location
        
        # Status-specific logic
        if new_status == InstanceStatus.TEMPORARY_ISSUED:
            self.assigned_date = timezone.now()
        elif new_status == InstanceStatus.IN_STORE and old_status == InstanceStatus.TEMPORARY_ISSUED:
            self.actual_return_date = timezone.now().date()
        elif new_status == InstanceStatus.UNDER_REPAIR:
            if not self.repair_started_date:
                self.repair_started_date = timezone.now().date()
        elif new_status == InstanceStatus.DAMAGED:
            if not self.damage_reported_date:
                self.damage_reported_date = timezone.now().date()
        elif new_status == InstanceStatus.DISPOSED:
            self.disposal_date = timezone.now().date()
        
        self.save()
        
        # Create movement record
        InstanceMovement.objects.create(
            instance=self,
            from_location=old_location,
            to_location=self.current_location,
            previous_status=old_status,
            new_status=new_status,
            moved_by=user,
            remarks=notes or f"Status changed from {old_status} to {new_status}"
        )
        
        return self
    
    def is_available(self):
        return self.current_status == InstanceStatus.IN_STORE
    
    def is_in_transit(self):
        return self.current_status == InstanceStatus.IN_TRANSIT
    
    def is_issued(self):
        return self.current_status in [InstanceStatus.IN_USE, InstanceStatus.TEMPORARY_ISSUED]
    
    def is_overdue(self):
        if (self.current_status == InstanceStatus.TEMPORARY_ISSUED and 
            self.expected_return_date and not self.actual_return_date):
            return timezone.now().date() > self.expected_return_date
        return False

# ==================== STOCK ENTRY MODEL ====================
class StockEntry(models.Model):
    ENTRY_TYPE_CHOICES = [
        ('RECEIPT', 'Receipt'),
        ('ISSUE', 'Issue'),
        ('CORRECTION', 'Correction'),
        ('RETURN', 'Return'),
    ]

    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('PENDING_ACK', 'Pending Acknowledgment'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
    ]
    
    entry_type = models.CharField(max_length=20, choices=ENTRY_TYPE_CHOICES)
    entry_number = models.CharField(max_length=50, unique=True, blank=True)
    entry_date = models.DateTimeField(default=timezone.now)
    from_location = models.ForeignKey(
        Location, 
        on_delete=models.PROTECT, 
        null=True, 
        blank=True, 
        related_name='outgoing_entries',
        limit_choices_to={'is_store': True}
    )
    to_location = models.ForeignKey(
        Location, 
        on_delete=models.PROTECT, 
        null=True, 
        blank=True, 
        related_name='incoming_entries'
    )
    item = models.ForeignKey(Item, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    instances = models.ManyToManyField(ItemInstance, blank=True, related_name='stock_entries')
    reference_entry = models.ForeignKey(
        'self', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='correction_entries'
    )
    is_temporary = models.BooleanField(default=False)
    expected_return_date = models.DateField(null=True, blank=True)
    actual_return_date = models.DateField(null=True, blank=True)
    temporary_recipient = models.CharField(max_length=150, null=True, blank=True)
    inspection_certificate = models.ForeignKey(
        InspectionCertificate, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='stock_entries'
    )
    
    # Enhanced transfer tracking
    requires_acknowledgment = models.BooleanField(default=False)
    is_cross_location = models.BooleanField(default=False)
    is_upward_transfer = models.BooleanField(
        default=False,
        help_text="True if this is a main store issuing UP to parent standalone location"
    )
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT')
    created_by = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='created_entries'
    )
    acknowledged_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='acknowledged_entries'
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    remarks = models.TextField(blank=True, null=True)
    purpose = models.CharField(max_length=255, blank=True, null=True)
    auto_create_instances = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'Stock Entries'
        ordering = ['-entry_date', '-created_at']
        indexes = [
            models.Index(fields=['entry_number']),
            models.Index(fields=['entry_type']),
            models.Index(fields=['status']),
            models.Index(fields=['entry_date']),
            models.Index(fields=['requires_acknowledgment']),
            models.Index(fields=['is_upward_transfer']),
        ]

    def __str__(self):
        return f"{self.entry_number} ({self.entry_type})"
    
    def save(self, *args, **kwargs):
        if not self.entry_number:
            self.entry_number = self.generate_entry_number()
        
        # Determine if acknowledgment is required
        self.requires_acknowledgment = self._check_acknowledgment_required()
        self.is_cross_location = self._check_cross_location()
        self.is_upward_transfer = self._check_upward_transfer()
        
        super().save(*args, **kwargs)
    
    def generate_entry_number(self):
        today = timezone.now().strftime('%Y%m%d')
        last_entry = StockEntry.objects.filter(
            entry_type=self.entry_type
        ).order_by('-id').first()
        
        last_seq = 0
        if last_entry and last_entry.entry_number:
            try:
                last_seq = int(last_entry.entry_number.split('-')[-1])
            except (ValueError, IndexError):
                last_seq = 0
        
        return f"{self.entry_type}-{today}-{last_seq + 1:04d}"
    
    def _check_acknowledgment_required(self):
        """Determine if this entry requires acknowledgment"""
        if self.entry_type == 'ISSUE':
            # Store-to-store transfers require acknowledgment
            if (self.from_location and self.from_location.is_store and 
                self.to_location and self.to_location.is_store):
                return True
            
            # Upward transfers (main store to parent standalone) require acknowledgment
            if self._check_upward_transfer():
                return True
        
        elif self.entry_type == 'RETURN':
            # Returns to store require acknowledgment
            if self.to_location and self.to_location.is_store:
                return True
        
        return False
    
    def _check_cross_location(self):
        """Check if this is a cross-location transfer"""
        if self.from_location and self.to_location:
            from_parent_standalone = self.from_location.get_parent_standalone()
            to_parent_standalone = self.to_location.get_parent_standalone()
            return from_parent_standalone != to_parent_standalone
        return False
    
    def _check_upward_transfer(self):
        """Check if this is an upward transfer (main store to parent standalone)"""
        if self.entry_type != 'ISSUE' or not self.from_location or not self.to_location:
            return False
        
        # Check if from_location is a main store
        if not (self.from_location.is_store and self.from_location.is_main_store):
            return False
        
        # Check if to_location is the parent standalone location
        from_parent_standalone = self.from_location.get_parent_standalone()
        if not from_parent_standalone:
            return False
        
        # Get parent of from_parent_standalone
        if from_parent_standalone.parent_location:
            parent_of_parent_standalone = from_parent_standalone.parent_location.get_parent_standalone()
            return self.to_location == parent_of_parent_standalone
        
        return False

# ==================== INVENTORY MODEL ====================
class LocationInventory(models.Model):
    location = models.ForeignKey(
        Location, 
        on_delete=models.CASCADE, 
        related_name='inventory',
        limit_choices_to={'is_store': True}
    )
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='location_inventory')
    
    # Status quantity fields
    total_quantity = models.PositiveIntegerField(default=0)
    available_quantity = models.PositiveIntegerField(default=0)
    in_store_quantity = models.PositiveIntegerField(default=0)
    in_transit_quantity = models.PositiveIntegerField(default=0)
    in_use_quantity = models.PositiveIntegerField(default=0)
    temporary_issued_quantity = models.PositiveIntegerField(default=0)
    under_repair_quantity = models.PositiveIntegerField(default=0)
    damaged_quantity = models.PositiveIntegerField(default=0)
    lost_quantity = models.PositiveIntegerField(default=0)
    condemned_quantity = models.PositiveIntegerField(default=0)
    disposed_quantity = models.PositiveIntegerField(default=0)
    
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [['location', 'item']]
        verbose_name_plural = 'Location Inventories'
        indexes = [
            models.Index(fields=['location', 'item']),
        ]

    def __str__(self):
        return f"{self.location.name} - {self.item.name}: {self.total_quantity} units"
    
    def update_quantities(self):
        """Update all quantity fields based on instances"""
        source_instances = ItemInstance.objects.filter(
            item=self.item,
            source_location=self.location
        )
        
        self.total_quantity = source_instances.count()
        
        self.in_store_quantity = source_instances.filter(
            current_status=InstanceStatus.IN_STORE,
            current_location=self.location
        ).count()
        
        self.in_transit_quantity = source_instances.filter(
            current_status=InstanceStatus.IN_TRANSIT
        ).count()
        
        self.in_use_quantity = source_instances.filter(
            current_status=InstanceStatus.IN_USE
        ).count()
        
        self.temporary_issued_quantity = source_instances.filter(
            current_status=InstanceStatus.TEMPORARY_ISSUED
        ).count()
        
        self.under_repair_quantity = source_instances.filter(
            current_status=InstanceStatus.UNDER_REPAIR
        ).count()
        
        self.damaged_quantity = source_instances.filter(
            current_status=InstanceStatus.DAMAGED
        ).count()
        
        self.lost_quantity = source_instances.filter(
            current_status=InstanceStatus.LOST
        ).count()
        
        self.condemned_quantity = source_instances.filter(
            current_status=InstanceStatus.CONDEMNED
        ).count()
        
        self.disposed_quantity = source_instances.filter(
            current_status=InstanceStatus.DISPOSED
        ).count()
        
        self.available_quantity = ItemInstance.objects.filter(
            item=self.item,
            current_location=self.location,
            current_status=InstanceStatus.IN_STORE
        ).count()
        
        self.save()

# ==================== INSTANCE MOVEMENT MODEL ====================
class InstanceMovement(models.Model):
    instance = models.ForeignKey(
        ItemInstance, 
        on_delete=models.CASCADE, 
        related_name='movements'
    )
    stock_entry = models.ForeignKey(
        StockEntry,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='movements'
    )
    from_location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        related_name='outgoing_movements',
        null=True,
        blank=True
    )
    to_location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        related_name='incoming_movements',
        null=True,
        blank=True
    )
    previous_status = models.CharField(max_length=20, choices=InstanceStatus.choices)
    new_status = models.CharField(max_length=20, choices=InstanceStatus.choices)
    movement_type = models.CharField(
        max_length=20,
        choices=[
            ('ISSUE', 'Issue'),
            ('RETURN', 'Return'),
            ('TRANSFER', 'Transfer'),
            ('UPWARD_TRANSFER', 'Upward Transfer'),
            ('REPAIR', 'Send to Repair'),
            ('REPAIR_RETURN', 'Return from Repair'),
            ('DAMAGE', 'Mark as Damaged'),
            ('DISPOSAL', 'Disposal'),
            ('CORRECTION', 'Correction'),
        ],
        default='TRANSFER'
    )
    moved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    moved_at = models.DateTimeField(default=timezone.now)
    remarks = models.TextField(blank=True, null=True)
    
    # Acknowledgment tracking
    requires_acknowledgment = models.BooleanField(default=False)
    acknowledged = models.BooleanField(default=False)
    acknowledged_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='acknowledged_movements'
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    
    # Enhanced tracking
    is_cross_location = models.BooleanField(default=False)
    is_upward_transfer = models.BooleanField(default=False)

    class Meta:
        ordering = ['-moved_at']
        indexes = [
            models.Index(fields=['instance', '-moved_at']),
            models.Index(fields=['requires_acknowledgment']),
            models.Index(fields=['is_upward_transfer']),
        ]

    def __str__(self):
        return f"{self.instance.instance_code}: {self.get_previous_status_display()} → {self.get_new_status_display()}"

# ==================== SIGNALS ====================
@receiver(post_save, sender=ItemInstance)
def update_inventory_on_instance_change(sender, instance, created, **kwargs):
    """Auto-update inventory when instance changes"""
    try:
        if instance.source_location and instance.source_location.is_store:
            inv, _ = LocationInventory.objects.get_or_create(
                location=instance.source_location,
                item=instance.item
            )
            inv.update_quantities()
        
        if (instance.current_location and instance.current_location.is_store and 
            instance.current_location != instance.source_location):
            inv, _ = LocationInventory.objects.get_or_create(
                location=instance.current_location,
                item=instance.item
            )
            inv.update_quantities()
    except Exception as e:
        print(f"Error updating inventory: {e}")

@receiver(post_delete, sender=ItemInstance)
def update_inventory_on_delete(sender, instance, **kwargs):
    """Update item total quantity when an instance is deleted"""
    try:
        instance.item.update_total_quantity()
        if instance.source_location and instance.source_location.is_store:
            inv = LocationInventory.objects.filter(
                location=instance.source_location,
                item=instance.item
            ).first()
            if inv:
                inv.update_quantities()
    except Exception as e:
        print(f"Error updating inventory on delete: {e}")

@receiver(post_save, sender=StockEntry)
def update_inventory_on_stock_entry(sender, instance, created, **kwargs):
    """Update inventory when stock entry is completed"""
    try:
        if instance.status == 'COMPLETED':
            for location in [instance.from_location, instance.to_location]:
                if location and location.is_store:
                    inv, _ = LocationInventory.objects.get_or_create(
                        location=location,
                        item=instance.item
                    )
                    inv.update_quantities()
    except Exception as e:
        print(f"Error updating inventory from stock entry: {e}")