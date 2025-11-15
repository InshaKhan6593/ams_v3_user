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

# ==================== LOCATION MODELS ====================
class LocationType(models.TextChoices):
    DEPARTMENT = 'DEPARTMENT', 'Department'
    BUILDING = 'BUILDING', 'Building'
    STORE = 'STORE', 'Store'
    ROOM = 'ROOM', 'Room'
    LAB = 'LAB', 'Lab'
    JUNKYARD = 'JUNKYARD', 'Junkyard'
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
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['code']),
            models.Index(fields=['location_type']),
            models.Index(fields=['is_store']),
            models.Index(fields=['is_active']),
        ]

    def __str__(self):
        return f'{self.name} ({self.code})'
    
    def get_full_path(self):
        """Get hierarchical path of location"""
        path = [self.name]
        parent = self.parent_location
        while parent:
            path.insert(0, parent.name)
            parent = parent.parent_location
        return ' > '.join(path)
    
    def get_main_store(self):
        """Get the main auto-created store for this location"""
        if self.is_store:
            return self
        
        # If this location has an auto-created store, return it
        if self.auto_created_store:
            return self.auto_created_store
        
        # Otherwise, check parent location recursively
        if self.parent_location:
            return self.parent_location.get_main_store()
        
        return None
    
    def get_all_stores(self):
        """Get all stores under this location including auto-created store"""
        if self.is_store:
            return Location.objects.filter(id=self.id)
        
        store_ids = set()
        
        # Include auto-created store
        if self.auto_created_store:
            store_ids.add(self.auto_created_store.id)
        
        # Get all child stores recursively
        def get_child_stores(location):
            children = location.child_locations.all()
            for child in children:
                if child.is_store:
                    store_ids.add(child.id)
                get_child_stores(child)
        
        get_child_stores(self)
        return Location.objects.filter(id__in=store_ids)
    
    def is_standalone(self):
        """Check if location is standalone (no parent) and not a store"""
        return self.parent_location is None and not self.is_store
    
    def can_have_auto_store(self):
        """Check if location can have an auto-created store"""
        return self.is_standalone() and not self.is_store and not self.auto_created_store
    
    def get_depth(self):
        """Get depth level in hierarchy"""
        depth = 0
        parent = self.parent_location
        while parent:
            depth += 1
            parent = parent.parent_location
        return depth
    
    def get_root_location(self):
        """Get the root/top-level location"""
        root = self
        while root.parent_location:
            root = root.parent_location
        return root
    
    def clean(self):
        """Validate location data"""
        super().clean()
        
        # Store locations cannot have children
        if self.is_store and self.pk:
            if self.child_locations.exists():
                raise ValidationError("Store locations cannot have child locations")
        
        # Prevent circular parent references
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
        
        # Auto-created stores must have parent
        if self.is_auto_created and not self.parent_location:
            raise ValidationError("Auto-created stores must have a parent location")
        
        # Stores cannot be parents
        if self.parent_location and self.parent_location.is_store:
            raise ValidationError("Store locations cannot be parent locations")


# Signal to auto-create store for standalone locations
@receiver(post_save, sender=Location)
def auto_create_store_for_location(sender, instance, created, **kwargs):
    """
    Automatically create a store location for standalone non-parent locations
    """
    if created and instance.can_have_auto_store():
        # Generate store code and name
        store_code = f"{instance.code}-STORE"
        store_name = f"{instance.name} - Central Store"
        
        # Create the store
        store = Location.objects.create(
            name=store_name,
            code=store_code,
            parent_location=instance,
            location_type=LocationType.STORE,
            is_store=True,
            is_auto_created=True,
            description=f"Auto-created central store for {instance.name}",
            address=instance.address,
            in_charge=instance.in_charge,
            contact_number=instance.contact_number,
            is_active=True
        )
        
        # Link back to parent
        instance.auto_created_store = store
        instance.save(update_fields=['auto_created_store'])
        
        # Log activity if possible
        try:
            from user_management.models import UserActivity
            UserActivity.objects.create(
                user=None,
                action='AUTO_CREATE_STORE',
                model='Location',
                object_id=store.id,
                details={
                    'parent_location': instance.name,
                    'store_name': store_name,
                    'store_code': store_code
                }
            )
        except:
            pass  # Fail silently if UserActivity doesn't exist yet

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
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = 'Categories'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} - {self.code}"

class Item(models.Model):
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=50, unique=True)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='items')
    description = models.TextField(blank=True, null=True)
    acct_unit = models.CharField(max_length=255, help_text="Accounting unit/measurement")
    specifications = models.TextField(blank=True, null=True)
    default_location = models.ForeignKey(
        Location, 
        on_delete=models.PROTECT,
        related_name='default_items'
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
    
    def update_total_quantity(self):
        """Update total quantity based on instances"""
        self.total_quantity = self.instances.count()
        self.save(update_fields=['total_quantity'])


# ==================== INSPECTION CERTIFICATE MODELS ====================
class InspectionStage(models.TextChoices):
    INITIATED = 'INITIATED', 'Initiated - Basic Info'
    STOCK_DETAILS = 'STOCK_DETAILS', 'Stock Details Entry'
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
    department = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        related_name='department_certificates',
        help_text="The location where this certificate is initiated"
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
        
        # Once submitted to next stage, previous user cannot edit
        if stage == InspectionStage.INITIATED:
            # Can edit only if still in INITIATED stage
            return (self.stage == InspectionStage.INITIATED and
                   user_profile.role == UserRole.LOCATION_HEAD and 
                   user_profile.has_location_access(self.department))
        
        elif stage == InspectionStage.STOCK_DETAILS:
            # Can edit only if in STOCK_DETAILS stage
            main_store = self.get_main_store()
            return (self.stage == InspectionStage.STOCK_DETAILS and
                   user_profile.role == UserRole.STOCK_INCHARGE and 
                   main_store and 
                   user_profile.has_location_access(main_store))
        
        elif stage == InspectionStage.AUDIT_REVIEW:
            # Can edit only if in AUDIT_REVIEW stage
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
    
    # Stock register details (added by Stock Incharge for each item)
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
        from django.core.exceptions import ValidationError
        if (self.accepted_quantity + self.rejected_quantity) > self.tendered_quantity:
            raise ValidationError(
                "Accepted + Rejected quantity cannot exceed tendered quantity"
            )

# ==================== ITEM INSTANCE MODEL ====================
# ==================== ADD/UPDATE THESE IN YOUR models.py ====================

# UPDATED InstanceStatus - Replace your existing one
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


# UPDATED ItemInstance Model - Replace your existing ItemInstance class
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
    
    # Location tracking
    source_location = models.ForeignKey(
        'Location', 
        on_delete=models.PROTECT, 
        related_name='source_instances'
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
    
    # Purchase info
    purchase_date = models.DateField(null=True, blank=True)
    warranty_expiry = models.DateField(null=True, blank=True)
    
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
    
    def generate_qr_code(self):
        import qrcode
        from io import BytesIO
        import base64
        
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr_data = {
            'instance_code': self.instance_code,
            'item_name': self.item.name,
            'item_code': self.item.code,
        }
        qr.add_data(str(qr_data))
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format='PNG')
        img_str = base64.b64encode(buffer.getvalue()).decode()
        
        self.qr_code_data = f"data:image/png;base64,{img_str}"
        self.qr_generated = True
    
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

# ADD THESE SIGNALS at the end of models.py
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
    except Exception as e:
        pass


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
    except:
        pass

# ==================== STOCK ENTRY MODEL ====================
class StockEntry(models.Model):
    ENTRY_TYPE_CHOICES = [
        ('RECEIPT', 'Receipt'),
        ('ISSUE', 'Issue'),
        ('CORRECTION', 'Correction'),
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
        related_name='outgoing_entries'
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
        ]

    def __str__(self):
        return f"{self.entry_number} ({self.entry_type})"
    
    def save(self, *args, **kwargs):
        if not self.entry_number:
            self.entry_number = self.generate_entry_number()
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
    in_store_quantity = models.PositiveIntegerField(default=0)  # ADD THIS
    in_transit_quantity = models.PositiveIntegerField(default=0)
    in_use_quantity = models.PositiveIntegerField(default=0)
    temporary_issued_quantity = models.PositiveIntegerField(default=0)
    under_repair_quantity = models.PositiveIntegerField(default=0)  # ADD THIS
    damaged_quantity = models.PositiveIntegerField(default=0)  # ADD THIS
    lost_quantity = models.PositiveIntegerField(default=0)  # ADD THIS
    condemned_quantity = models.PositiveIntegerField(default=0)  # ADD THIS
    disposed_quantity = models.PositiveIntegerField(default=0)  # ADD THIS
    
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
        # CRITICAL: Count instances where THIS location is the SOURCE
        # These are instances that "belong" to this inventory
        source_instances = ItemInstance.objects.filter(
            item=self.item,
            source_location=self.location
        )
        
        self.total_quantity = source_instances.count()
        
        # Count by status for instances sourced from this location
        self.in_store_quantity = source_instances.filter(
            current_status=InstanceStatus.IN_STORE,
            current_location=self.location  # Must also be physically here
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
        
        # Available = instances currently at this location in IN_STORE status
        # regardless of source (includes items transferred here)
        self.available_quantity = ItemInstance.objects.filter(
            item=self.item,
            current_location=self.location,
            current_status=InstanceStatus.IN_STORE
        ).count()
        
        self.save()
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

    class Meta:
        ordering = ['-moved_at']
        indexes = [
            models.Index(fields=['instance', '-moved_at']),
        ]

    def __str__(self):
        return f"{self.instance.instance_code}: {self.get_previous_status_display()} → {self.get_new_status_display()}"