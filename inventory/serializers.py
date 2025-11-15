from rest_framework import serializers
from django.utils import timezone
from django.contrib.auth.models import User
from inventory.models import *
from user_management.models import UserProfile, UserRole, UserActivity
import json
from django.db import transaction

# ==================== USER SERIALIZERS ====================
class UserSerializer(serializers.ModelSerializer):
    role = serializers.CharField(source='profile.role', read_only=True)
    role_display = serializers.CharField(source='profile.get_role_display', read_only=True)
    assigned_locations = serializers.SerializerMethodField()
    accessible_stores = serializers.SerializerMethodField()
    full_name = serializers.SerializerMethodField()
    
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'full_name', 
                  'role', 'role_display', 'assigned_locations', 'accessible_stores', 'is_active']
    
    def get_full_name(self, obj):
        return obj.get_full_name() or obj.username
    
    def get_assigned_locations(self, obj):
        if hasattr(obj, 'profile'):
            return LocationMinimalSerializer(obj.profile.assigned_locations.all(), many=True).data
        return []
    
    def get_accessible_stores(self, obj):
        if hasattr(obj, 'profile'):
            return LocationMinimalSerializer(obj.profile.get_accessible_stores(), many=True).data
        return []


class UserProfileSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    username = serializers.CharField(write_only=True, required=False)
    password = serializers.CharField(write_only=True, required=False, style={'input_type': 'password'})
    email = serializers.EmailField(write_only=True, required=False)
    first_name = serializers.CharField(write_only=True, required=False)
    last_name = serializers.CharField(write_only=True, required=False)
    assigned_locations_data = serializers.SerializerMethodField()
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    department_name = serializers.CharField(source='department.name', read_only=True)
    role_display = serializers.CharField(source='get_role_display', read_only=True)
    can_create_users = serializers.SerializerMethodField()
    can_create_locations = serializers.SerializerMethodField()
    main_location = serializers.SerializerMethodField()
    
    class Meta:
        model = UserProfile
        fields = '__all__'
        read_only_fields = ['created_by', 'employee_id', 'created_at', 'updated_at']
    
    def get_assigned_locations_data(self, obj):
        return LocationMinimalSerializer(obj.assigned_locations.all(), many=True).data
    
    def get_can_create_users(self, obj):
        return obj.role in [UserRole.SYSTEM_ADMIN, UserRole.LOCATION_HEAD]
    
    def get_can_create_locations(self, obj):
        return obj.role in [UserRole.SYSTEM_ADMIN, UserRole.LOCATION_HEAD, UserRole.STOCK_INCHARGE]
    
    def get_main_location(self, obj):
        main_loc = obj.get_main_location()
        if main_loc:
            return LocationMinimalSerializer(main_loc).data
        return None

    def validate_assigned_locations(self, value):
        """
        Validate assigned locations based on user role being created
        """
        request = self.context.get('request')
        current_user = request.user if request else None
        
        # Get the role being created/updated
        role = self.initial_data.get('role') or (self.instance.role if self.instance else None)
        
        if not role:
            return value
        
        # For Location Head: only allow standalone locations
        if role == UserRole.LOCATION_HEAD:
            for location in value:
                if not location.is_standalone():
                    raise serializers.ValidationError(
                        f"{location.name} is not a standalone location. "
                        f"Location Heads can only be assigned to standalone locations (top-level locations with no parent)."
                    )
        
        # For Stock Incharge: only allow store locations
        elif role == UserRole.STOCK_INCHARGE:
            for location in value:
                if not location.is_store:
                    raise serializers.ValidationError(
                        f"{location.name} is not a store location. "
                        f"Stock Incharge can only be assigned to store locations."
                    )
        
        # Additional validation for Location Head creating Stock Incharge
        if current_user and hasattr(current_user, 'profile'):
            current_profile = current_user.profile
            
            if current_profile.role == UserRole.LOCATION_HEAD:
                if role == UserRole.STOCK_INCHARGE:
                    # Validate each location is accessible to the Location Head
                    for location in value:
                        if not location.is_store:
                            raise serializers.ValidationError({
                                'assigned_locations': f"{location.name} is not a store location"
                            })
                        if not current_profile.has_location_access(location):
                            raise serializers.ValidationError({
                                'assigned_locations': f"You don't have access to {location.name}"
                            })
        
        return value
    
    
    @transaction.atomic
    def create(self, validated_data):
        username = validated_data.pop('username', None)
        password = validated_data.pop('password', None)
        email = validated_data.pop('email', None)
        first_name = validated_data.pop('first_name', None)
        last_name = validated_data.pop('last_name', None)
        assigned_locations = validated_data.pop('assigned_locations', [])
        
        request = self.context.get('request')
        current_user = request.user if request else None
        
        if current_user and hasattr(current_user, 'profile'):
            current_profile = current_user.profile
            target_role = validated_data.get('role')
            
            if not current_profile.can_create_user(target_role):
                raise serializers.ValidationError({
                    'role': f"{current_profile.get_role_display()} cannot create {target_role} users"
                })
            
            if current_profile.role == UserRole.LOCATION_HEAD:
                if target_role != UserRole.STOCK_INCHARGE:
                    raise serializers.ValidationError({
                        'role': "Location Heads can only create Stock Incharge users"
                    })
                
                # Validate assigned locations for Stock Incharge
                if not assigned_locations:
                    raise serializers.ValidationError({
                        'assigned_locations': "At least one store must be assigned to Stock Incharge"
                    })
        
        if not username:
            raise serializers.ValidationError({'username': "Username is required"})
        if not password:
            raise serializers.ValidationError({'password': "Password is required"})
        
        if User.objects.filter(username=username).exists():
            raise serializers.ValidationError({'username': "Username already exists"})
        
        # Create Django User
        user = User.objects.create_user(
            username=username,
            password=password,
            email=email or f"{username}@inventory.local",
            first_name=first_name or '',
            last_name=last_name or ''
        )
        
        # Create UserProfile
        validated_data['user'] = user
        validated_data['created_by'] = current_user
        profile = UserProfile.objects.create(**validated_data)
        
        # Assign locations to the profile
        if assigned_locations:
            profile.assigned_locations.set(assigned_locations)
            profile.save()
        
        # Log activity
        if current_user:
            UserActivity.objects.create(
                user=current_user,
                action='CREATE_USER',
                model='UserProfile',
                object_id=profile.id,
                details={
                    'username': username,
                    'role': profile.role,
                    'assigned_locations': [loc.name for loc in assigned_locations]
                }
            )
        
        return profile
    
    @transaction.atomic
    def update(self, instance, validated_data):
        validated_data.pop('username', None)
        validated_data.pop('password', None)
        
        email = validated_data.pop('email', None)
        first_name = validated_data.pop('first_name', None)
        last_name = validated_data.pop('last_name', None)
        
        if email:
            instance.user.email = email
        if first_name is not None:
            instance.user.first_name = first_name
        if last_name is not None:
            instance.user.last_name = last_name
        instance.user.save()
        
        assigned_locations = validated_data.pop('assigned_locations', None)
        
        request = self.context.get('request')
        if request and hasattr(request.user, 'profile'):
            current_profile = request.user.profile
            
            if current_profile.role == UserRole.LOCATION_HEAD:
                if instance.created_by != request.user and instance.user != request.user:
                    raise serializers.ValidationError({
                        'error': "You can only modify users you created or your own profile"
                    })
                
                if assigned_locations:
                    # Validate each location
                    for location in assigned_locations:
                        if not location.is_store:
                            raise serializers.ValidationError({
                                'assigned_locations': f"{location.name} is not a store location"
                            })
                        if not current_profile.has_location_access(location):
                            raise serializers.ValidationError({
                                'assigned_locations': f"You don't have access to {location.name}"
                            })
        
        # Update profile fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        
        # CRITICAL FIX: Update assigned locations
        if assigned_locations is not None:
            instance.assigned_locations.set(assigned_locations)
            instance.save()  # Save after setting locations
        
        # Log activity
        if request:
            UserActivity.objects.create(
                user=request.user,
                action='UPDATE_USER',
                model='UserProfile',
                object_id=instance.id,
                details={
                    'username': instance.user.username,
                    'assigned_locations': [loc.name for loc in (assigned_locations or [])]
                }
            )
        
        return instance  # Changed from 'profile' to 'instance'


# ==================== LOCATION SERIALIZERS ====================
class LocationMinimalSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = ['id', 'name', 'code', 'location_type', 'is_store', 'is_auto_created', 'parent_location']


class LocationSerializer(serializers.ModelSerializer):
    parent_location_name = serializers.CharField(source='parent_location.name', read_only=True)
    full_path = serializers.SerializerMethodField()
    total_items = serializers.SerializerMethodField()
    stores_count = serializers.SerializerMethodField()
    all_stores = serializers.SerializerMethodField()
    auto_created_store_data = serializers.SerializerMethodField()
    main_store = serializers.SerializerMethodField()
    depth = serializers.SerializerMethodField()
    is_standalone = serializers.SerializerMethodField()
    
    class Meta:
        model = Location
        fields = '__all__'
        read_only_fields = ['auto_created_store', 'is_auto_created', 'created_at', 'updated_at']
    
    def get_full_path(self, obj):
        return obj.get_full_path()
    
    def get_total_items(self, obj):
        return ItemInstance.objects.filter(current_location=obj).count()
    
    def get_stores_count(self, obj):
        return obj.get_all_stores().count()
    
    def get_all_stores(self, obj):
        return LocationMinimalSerializer(obj.get_all_stores(), many=True).data
    
    def get_auto_created_store_data(self, obj):
        if obj.auto_created_store:
            return LocationMinimalSerializer(obj.auto_created_store).data
        return None
    
    def get_main_store(self, obj):
        main_store = obj.get_main_store()
        if main_store:
            return LocationMinimalSerializer(main_store).data
        return None
    
    def get_depth(self, obj):
        return obj.get_depth()
    
    def get_is_standalone(self, obj):
        return obj.is_standalone()
    
    def validate(self, data):
        request = self.context.get('request')
        parent_location = data.get('parent_location')
        
        if data.get('is_store') and parent_location:
            if parent_location.is_store:
                raise serializers.ValidationError({
                    'parent_location': "Store locations cannot be parent locations"
                })
        
        if request and hasattr(request.user, 'profile'):
            profile = request.user.profile
            
            if profile.role == UserRole.LOCATION_HEAD:
                if parent_location and not profile.has_location_access(parent_location):
                    raise serializers.ValidationError({
                        'parent_location': "You don't have access to create sub-locations under this location"
                    })
            
            elif profile.role == UserRole.STOCK_INCHARGE:
                if not parent_location:
                    raise serializers.ValidationError({
                        'parent_location': "Stock Incharge must specify a parent location"
                    })
                
                main_location = profile.get_main_location()
                
                if not main_location:
                    raise serializers.ValidationError({
                        'error': "Could not determine your main location"
                    })
                
                if parent_location.id == main_location.id:
                    pass
                else:
                    current = parent_location
                    is_valid = False
                    while current:
                        if current.id == main_location.id:
                            is_valid = True
                            break
                        current = current.parent_location
                    
                    if not is_valid:
                        raise serializers.ValidationError({
                            'parent_location': f"You can only create locations within '{main_location.name}' or its sub-locations"
                        })
        
        return data


# ==================== CATEGORY SERIALIZERS ====================
class CategoryMinimalSerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ['id', 'name', 'code']


class CategorySerializer(serializers.ModelSerializer):
    parent_category_name = serializers.CharField(source='parent_category.name', read_only=True)
    items_count = serializers.SerializerMethodField()
    
    class Meta:
        model = Category
        fields = '__all__'
    
    def get_items_count(self, obj):
        return obj.items.count()


# ==================== ITEM SERIALIZERS ====================
class ItemMinimalSerializer(serializers.ModelSerializer):
    class Meta:
        model = Item
        fields = ['id', 'name', 'code', 'category']


class ItemSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    default_location_name = serializers.CharField(source='default_location.name', read_only=True)
    total_instances = serializers.SerializerMethodField()
    available_quantity = serializers.SerializerMethodField()
    
    class Meta:
        model = Item
        fields = '__all__'
    
    def get_total_instances(self, obj):
        return obj.instances.count()
    
    def get_available_quantity(self, obj):
        return obj.instances.filter(current_status='IN_STORE').count()


# ==================== INSPECTION SERIALIZERS ====================
class InspectionItemSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source='item.name', read_only=True)
    item_code = serializers.CharField(source='item.code', read_only=True)
    item_unit = serializers.CharField(source='item.acct_unit', read_only=True)
    total_value = serializers.SerializerMethodField()
    
    class Meta:
        model = InspectionItem
        exclude = ['inspection_certificate']
    
    def get_total_value(self, obj):
        if obj.unit_price:
            return float(obj.accepted_quantity * obj.unit_price)
        return None
    
    def validate(self, data):
        tendered = data.get('tendered_quantity', 0)
        accepted = data.get('accepted_quantity', 0)
        rejected = data.get('rejected_quantity', 0)
        
        if (accepted + rejected) > tendered:
            raise serializers.ValidationError(
                "Accepted + Rejected quantity cannot exceed tendered quantity"
            )
        
        return data


class InspectionCertificateSerializer(serializers.ModelSerializer):
    inspection_items = InspectionItemSerializer(many=True, required=False)
    department_name = serializers.CharField(source='department.name', read_only=True)
    department_full_path = serializers.SerializerMethodField()
    main_store = serializers.SerializerMethodField()
    main_store_name = serializers.SerializerMethodField()
    
    initiated_by_name = serializers.CharField(source='initiated_by.get_full_name', read_only=True)
    stock_filled_by_name = serializers.CharField(source='stock_filled_by.get_full_name', read_only=True)
    auditor_reviewed_by_name = serializers.CharField(source='auditor_reviewed_by.get_full_name', read_only=True)
    rejected_by_name = serializers.CharField(source='rejected_by.get_full_name', read_only=True)
    
    can_edit = serializers.SerializerMethodField()
    can_submit = serializers.SerializerMethodField()
    can_reject = serializers.SerializerMethodField()
    editable_fields = serializers.SerializerMethodField()
    next_stage = serializers.SerializerMethodField()
    stage_progress = serializers.SerializerMethodField()
    stage_display = serializers.CharField(source='get_stage_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    is_locked_for_location_head = serializers.SerializerMethodField()
    is_locked_for_stock_incharge = serializers.SerializerMethodField()
    
    total_items_count = serializers.SerializerMethodField()
    total_accepted = serializers.SerializerMethodField()
    total_rejected = serializers.SerializerMethodField()
    total_value = serializers.SerializerMethodField()
    
    class Meta:
        model = InspectionCertificate
        fields = '__all__'
        read_only_fields = [
            'certificate_no', 'stage', 'stage_history', 'initiated_by', 'initiated_at',
            'stock_filled_by', 'stock_filled_at', 'auditor_reviewed_by', 'auditor_reviewed_at',
            'rejected_by', 'rejected_at', 'created_by', 'acknowledged_by', 'acknowledged_at',
            'created_at', 'updated_at'
        ]
    
    def get_department_full_path(self, obj):
        return obj.department.get_full_path() if obj.department else None
    
    def get_main_store(self, obj):
        main_store = obj.get_main_store()
        if main_store:
            return LocationMinimalSerializer(main_store).data
        return None
    
    def get_main_store_name(self, obj):
        main_store = obj.get_main_store()
        return main_store.name if main_store else None
    
    def get_is_locked_for_location_head(self, obj):
        request = self.context.get('request')
        if request and hasattr(request.user, 'profile'):
            profile = request.user.profile
            if profile.role == UserRole.LOCATION_HEAD:
                return obj.stage != 'INITIATED'
        return False
    
    def get_is_locked_for_stock_incharge(self, obj):
        request = self.context.get('request')
        if request and hasattr(request.user, 'profile'):
            profile = request.user.profile
            if profile.role == UserRole.STOCK_INCHARGE:
                return obj.stage not in ['STOCK_DETAILS']
        return False
    
    def get_can_edit(self, obj):
        request = self.context.get('request')
        if request and hasattr(request.user, 'profile'):
            return obj.can_edit_stage(request.user)
        return False
    
    def get_can_submit(self, obj):
        request = self.context.get('request')
        if not request or not hasattr(request.user, 'profile'):
            return False
        
        profile = request.user.profile
        
        if obj.stage == 'INITIATED' and profile.role == UserRole.LOCATION_HEAD:
            return profile.has_location_access(obj.department)
        elif obj.stage == 'STOCK_DETAILS' and profile.role == UserRole.STOCK_INCHARGE:
            main_store = obj.get_main_store()
            return main_store and profile.has_location_access(main_store)
        elif obj.stage == 'AUDIT_REVIEW' and profile.role == UserRole.AUDITOR:
            return True
        
        return False
    
    def get_can_reject(self, obj):
        request = self.context.get('request')
        if not request or not hasattr(request.user, 'profile'):
            return False
        
        profile = request.user.profile
        return (obj.stage != 'COMPLETED' and obj.stage != 'REJECTED' and 
                profile.role in [UserRole.AUDITOR, UserRole.SYSTEM_ADMIN])
    
    def get_editable_fields(self, obj):
        request = self.context.get('request')
        if not request or not hasattr(request.user, 'profile'):
            return []
        
        profile = request.user.profile
        
        if not obj.can_edit_stage(request.user):
            return []
        
        if obj.stage == 'INITIATED':
            return ['contractor_name', 'contractor_address', 'contract_no', 'contract_date',
                    'indenter', 'indent_no', 'department', 'date', 'date_of_delivery',
                    'delivery_type', 'remarks', 'certificate_image']
        
        elif obj.stage == 'STOCK_DETAILS':
            return ['inspection_items', 'inspected_by', 'date_of_inspection',
                    'consignee_name', 'consignee_designation']
        
        elif obj.stage == 'AUDIT_REVIEW':
            return ['dead_stock_register_no', 'dead_stock_page_no', 'central_store_entry_date',
                    'finance_check_date', 'supporting_documents']
        
        return []
    
    def get_next_stage(self, obj):
        stage_flow = {
            'INITIATED': 'STOCK_DETAILS',
            'STOCK_DETAILS': 'AUDIT_REVIEW',
            'AUDIT_REVIEW': 'COMPLETED'
        }
        return stage_flow.get(obj.stage)
    
    def get_stage_progress(self, obj):
        stage_weights = {
            'INITIATED': 25,
            'STOCK_DETAILS': 50,
            'AUDIT_REVIEW': 75,
            'COMPLETED': 100,
            'REJECTED': 0
        }
        return stage_weights.get(obj.stage, 0)
    
    def get_total_items_count(self, obj):
        return obj.get_total_items()
    
    def get_total_accepted(self, obj):
        return obj.get_total_accepted()
    
    def get_total_rejected(self, obj):
        return obj.get_total_rejected()
    
    def get_total_value(self, obj):
        total = sum(
            item.accepted_quantity * (item.unit_price or 0) 
            for item in obj.inspection_items.all()
        )
        return float(total) if total else None
    
    @transaction.atomic
    def create(self, validated_data):
        request = self.context.get('request')
        user = request.user if request else None
        
        inspection_items_data = validated_data.pop('inspection_items', [])
        
        if user:
            validated_data['initiated_by'] = user
            validated_data['initiated_at'] = timezone.now()
            validated_data['created_by'] = user
        
        inspection_cert = InspectionCertificate.objects.create(**validated_data)
        
        for item_data in inspection_items_data:
            InspectionItem.objects.create(
                inspection_certificate=inspection_cert,
                **item_data
            )
        
        if user:
            UserActivity.objects.create(
                user=user,
                action='CREATE_INSPECTION_CERTIFICATE',
                model='InspectionCertificate',
                object_id=inspection_cert.id,
                details={'certificate_no': inspection_cert.certificate_no}
            )
        
        return inspection_cert
    
    @transaction.atomic
    def update(self, instance, validated_data):
        request = self.context.get('request')
        
        if request and hasattr(request.user, 'profile'):
            profile = request.user.profile
            
            if profile.role == UserRole.LOCATION_HEAD:
                if instance.stage != 'INITIATED':
                    raise serializers.ValidationError({
                        'error': f"Location Head cannot edit certificate after {instance.get_stage_display()} stage"
                    })
                
                if 'inspection_items' in validated_data:
                    raise serializers.ValidationError({
                        'inspection_items': "Location Head cannot add items. Items are filled by Stock Incharge."
                    })
            
            if profile.role == UserRole.STOCK_INCHARGE:
                if instance.stage != 'STOCK_DETAILS':
                    raise serializers.ValidationError({
                        'error': "Stock Incharge can only edit in STOCK_DETAILS stage"
                    })
            
            if profile.role == UserRole.AUDITOR:
                if instance.stage != 'AUDIT_REVIEW':
                    raise serializers.ValidationError({
                        'error': "Auditor can only edit in AUDIT_REVIEW stage"
                    })
        
        if instance.stage == 'COMPLETED':
            raise serializers.ValidationError({
                'error': "Completed certificates cannot be updated"
            })
        
        inspection_items_data = validated_data.pop('inspection_items', None)
        
        validated_data.pop('created_by', None)
        validated_data.pop('initiated_by', None)
        
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        
        if inspection_items_data is not None:
            if request and hasattr(request.user, 'profile'):
                profile = request.user.profile
                if profile.role not in [UserRole.STOCK_INCHARGE, UserRole.SYSTEM_ADMIN]:
                    raise serializers.ValidationError({
                        'inspection_items': "Only Stock Incharge can manage items"
                    })
            
            instance.inspection_items.all().delete()
            for item_data in inspection_items_data:
                InspectionItem.objects.create(
                    inspection_certificate=instance,
                    **item_data
                )
        
        return instance
    
class InspectionCertificateMinimalSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source='department.name', read_only=True)
    contractor_name = serializers.CharField(read_only=True)
    certificate_no = serializers.CharField(read_only=True)
    
    class Meta:
        model = InspectionCertificate
        fields = ['id', 'certificate_no', 'date', 'contractor_name', 'department_name', 'stage']


# ==================== ITEM INSTANCE SERIALIZERS ====================
class ItemInstanceSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source='item.name', read_only=True)
    item_code = serializers.CharField(source='item.code', read_only=True)
    current_location_name = serializers.CharField(source='current_location.name', read_only=True)
    source_location_name = serializers.CharField(source='source_location.name', read_only=True)
    location_path = serializers.SerializerMethodField()
    
    # Enhanced status fields
    status_display = serializers.CharField(source='get_current_status_display', read_only=True)
    previous_status_display = serializers.CharField(source='get_previous_status_display', read_only=True)
    status_changed_by_name = serializers.CharField(source='status_changed_by.get_full_name', read_only=True)
    
    # Inspection certificate details
    inspection_certificate_details = InspectionCertificateMinimalSerializer(
        source='inspection_certificate', 
        read_only=True
    )
    
    # QR Code and Image
    qr_code_image = serializers.SerializerMethodField()
    
    # Availability flags
    is_available = serializers.SerializerMethodField()
    is_in_transit = serializers.SerializerMethodField()
    is_issued = serializers.SerializerMethodField()
    is_overdue = serializers.SerializerMethodField()
    
    # Assignment tracking
    days_since_assigned = serializers.SerializerMethodField()
    days_until_return = serializers.SerializerMethodField()
    
    condition_display = serializers.CharField(source='get_condition_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    
    class Meta:
        model = ItemInstance
        fields = '__all__'
        read_only_fields = ['instance_code', 'qr_code_data', 'qr_generated', 
                           'previous_status', 'status_changed_at', 'status_changed_by']
    
    def get_location_path(self, obj):
        return obj.current_location.get_full_path()
    
    def get_qr_code_image(self, obj):
        """Return QR code image data if available"""
        if obj.qr_code_data:
            return obj.qr_code_data
        return None
    
    def get_is_available(self, obj):
        return obj.is_available()
    
    def get_is_in_transit(self, obj):
        return obj.is_in_transit()
    
    def get_is_issued(self, obj):
        return obj.is_issued()
    
    def get_is_overdue(self, obj):
        return obj.is_overdue()
    
    def get_days_since_assigned(self, obj):
        if obj.assigned_date:
            delta = timezone.now().date() - obj.assigned_date.date()
            return delta.days
        return None
    
    def get_days_until_return(self, obj):
        if obj.expected_return_date and not obj.actual_return_date:
            delta = obj.expected_return_date - timezone.now().date()
            return delta.days
        return None


class InstanceMovementSerializer(serializers.ModelSerializer):
    instance_code = serializers.CharField(source='instance.instance_code', read_only=True)
    from_location_name = serializers.CharField(source='from_location.name', read_only=True)
    to_location_name = serializers.CharField(source='to_location.name', read_only=True)
    moved_by_name = serializers.SerializerMethodField()
    previous_status_display = serializers.CharField(source='get_previous_status_display', read_only=True)
    new_status_display = serializers.CharField(source='get_new_status_display', read_only=True)
    movement_type_display = serializers.CharField(source='get_movement_type_display', read_only=True)
    acknowledged_by_name = serializers.CharField(source='acknowledged_by.get_full_name', read_only=True)
    
    class Meta:
        model = InstanceMovement
        fields = '__all__'
    
    def get_moved_by_name(self, obj):
        return obj.moved_by.get_full_name() if obj.moved_by else 'System'


# ==================== STOCK ENTRY SERIALIZERS ====================
class StockEntrySerializer(serializers.ModelSerializer):
    instances_details = ItemInstanceSerializer(source='instances', many=True, read_only=True)
    instances = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=ItemInstance.objects.all(),
        required=False
    )
    item_name = serializers.CharField(source='item.name', read_only=True)
    from_location_name = serializers.CharField(source='from_location.name', read_only=True)
    to_location_name = serializers.CharField(source='to_location.name', read_only=True)
    created_by_name = serializers.SerializerMethodField()
    acknowledged_by_name = serializers.SerializerMethodField()
    is_overdue = serializers.SerializerMethodField()
    entry_type_display = serializers.CharField(source='get_entry_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    # Enhanced fields
    requires_acknowledgment = serializers.SerializerMethodField()
    is_transfer = serializers.SerializerMethodField()
    instances_count = serializers.SerializerMethodField()
    
    class Meta:
        model = StockEntry
        fields = '__all__'
        read_only_fields = ['entry_number', 'created_at', 'updated_at', 'created_by']
    
    def get_created_by_name(self, obj):
        return obj.created_by.get_full_name() if obj.created_by else None
    
    def get_acknowledged_by_name(self, obj):
        return obj.acknowledged_by.get_full_name() if obj.acknowledged_by else None
    
    def get_is_overdue(self, obj):
        if obj.is_temporary and obj.expected_return_date and not obj.actual_return_date:
            return timezone.now().date() > obj.expected_return_date
        return False
    
    def get_requires_acknowledgment(self, obj):
        return (obj.entry_type == 'ISSUE' and 
                obj.to_location and 
                obj.to_location.is_store and 
                obj.status == 'PENDING_ACK')
    
    def get_is_transfer(self, obj):
        return (obj.entry_type == 'ISSUE' and 
                obj.to_location and 
                obj.to_location.is_store)
    
    def get_instances_count(self, obj):
        return obj.instances.count()
    
    def validate(self, data):
        entry_type = data.get('entry_type')
        from_location = data.get('from_location')
        to_location = data.get('to_location')
        is_temporary = data.get('is_temporary', False)
        
        if entry_type == 'RECEIPT':
            if not to_location:
                raise serializers.ValidationError({'to_location': "Receipt must have a destination location"})
            if not to_location.is_store:
                raise serializers.ValidationError({'to_location': "Receipt destination must be a store location"})
        
        elif entry_type == 'ISSUE':
            if not from_location:
                raise serializers.ValidationError({'from_location': "Issue must have a source location"})
            if not from_location.is_store:
                raise serializers.ValidationError({'from_location': "Issue source must be a store location"})
            if not to_location:
                raise serializers.ValidationError({'to_location': "Issue must have a destination location"})
            
            if is_temporary:
                if not data.get('expected_return_date'):
                    raise serializers.ValidationError({
                        'expected_return_date': "Expected return date required for temporary issues"
                    })
                if not data.get('temporary_recipient'):
                    raise serializers.ValidationError({
                        'temporary_recipient': "Recipient name required for temporary issues"
                    })
        
        elif entry_type == 'CORRECTION':
            if not data.get('reference_entry'):
                raise serializers.ValidationError({
                    'reference_entry': "Correction must have a reference entry"
                })
        
        return data
    
    def validate_instances(self, value):
        """Validate instances are available for issue"""
        request = self.context.get('request')
        if request and request.method == 'POST':
            entry_type = self.initial_data.get('entry_type')
            
            if entry_type == 'ISSUE':
                unavailable = []
                for instance in value:
                    if not instance.is_available():
                        unavailable.append(instance.instance_code)
                
                if unavailable:
                    raise serializers.ValidationError(
                        f"Following instances are not available: {', '.join(unavailable)}"
                    )
        
        return value
    
    @transaction.atomic
    def create(self, validated_data):
        instances_data = validated_data.pop('instances', [])
        auto_create = validated_data.pop('auto_create_instances', False)
        
        request = self.context.get('request', None)
        user = getattr(request, 'user', None) if request else None
        if user and not user.is_anonymous:
            validated_data['created_by'] = user
        
        stock_entry = StockEntry.objects.create(**validated_data)
        
        if auto_create and not instances_data:
            if stock_entry.entry_type == 'RECEIPT':
                created_instances = []
                for i in range(stock_entry.quantity):
                    instance = ItemInstance.objects.create(
                        item=stock_entry.item,
                        source_location=stock_entry.to_location,
                        current_location=stock_entry.to_location,
                        current_status='IN_STORE',
                        condition='NEW',
                        purchase_date=timezone.now().date(),
                        inspection_certificate=stock_entry.inspection_certificate,
                        created_by=user
                    )
                    created_instances.append(instance)
                stock_entry.instances.set(created_instances)
        elif instances_data:
            stock_entry.instances.set(instances_data)
        
        return stock_entry


# ==================== INVENTORY SERIALIZERS ====================
class LocationInventorySerializer(serializers.ModelSerializer):
    location_name = serializers.CharField(source='location.name', read_only=True)
    item_name = serializers.CharField(source='item.name', read_only=True)
    item_code = serializers.CharField(source='item.code', read_only=True)
    
    # Enhanced fields
    available_quantity = serializers.IntegerField(read_only=True)
    needs_reorder = serializers.SerializerMethodField()
    reorder_level = serializers.IntegerField(source='item.reorder_level', read_only=True)
    status_breakdown = serializers.SerializerMethodField()
    utilization_percentage = serializers.SerializerMethodField()
    
    class Meta:
        model = LocationInventory
        fields = '__all__'
        read_only_fields = ['last_updated']
    
    def get_needs_reorder(self, obj):
        return obj.available_quantity < obj.item.reorder_level
    
    def get_status_breakdown(self, obj):
        """Get breakdown by status"""
        return {
            'in_store': obj.in_store_quantity,
            'in_transit': obj.in_transit_quantity,
            'in_use': obj.in_use_quantity,
            'temporary_issued': obj.temporary_issued_quantity,
            'under_repair': obj.under_repair_quantity,
            'damaged': obj.damaged_quantity,
            'lost': obj.lost_quantity,
            'condemned': obj.condemned_quantity,
            'disposed': obj.disposed_quantity,
        }
    
    def get_utilization_percentage(self, obj):
        """Calculate utilization percentage"""
        if obj.total_quantity > 0:
            utilized = obj.total_quantity - obj.available_quantity
            return round((utilized / obj.total_quantity) * 100, 2)
        return 0.0


# ==================== ACTIVITY SERIALIZERS ====================
class UserActivitySerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source='user.get_full_name', read_only=True)
    
    class Meta:
        model = UserActivity
        fields = '__all__'

