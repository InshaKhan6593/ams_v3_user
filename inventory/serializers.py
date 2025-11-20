# serializers.py - ENHANCED VERSION
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
    user = serializers.SerializerMethodField(read_only=True)
    username = serializers.CharField(write_only=True, required=False)
    password = serializers.CharField(write_only=True, required=False, style={'input_type': 'password'})
    email = serializers.EmailField(write_only=True, required=False)
    first_name = serializers.CharField(write_only=True, required=False)
    last_name = serializers.CharField(write_only=True, required=False)
    assigned_locations_data = serializers.SerializerMethodField()
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    department_name = serializers.CharField(source='department.name', read_only=True)
    role_display = serializers.CharField(source='get_role_display', read_only=True)
    
    # Enhanced fields
    responsible_location = serializers.SerializerMethodField()
    allowed_assignment_locations = serializers.SerializerMethodField()
    permissions_summary = serializers.SerializerMethodField()
    accessible_stores_count = serializers.SerializerMethodField()
    accessible_locations_count = serializers.SerializerMethodField()
    accessible_standalone_count = serializers.SerializerMethodField()
    is_main_store_incharge = serializers.SerializerMethodField()
    can_issue_upward = serializers.SerializerMethodField()
    parent_standalone_for_issuance = serializers.SerializerMethodField()
    
    class Meta:
        model = UserProfile
        fields = [
            'id', 'user', 'role', 'role_display', 'assigned_locations', 'assigned_locations_data',
            'created_by', 'created_by_name', 'phone', 'employee_id', 'department', 'department_name',
            'custom_permissions', 'is_active', 'created_at', 'updated_at',
            # Write-only user fields
            'username', 'password', 'email', 'first_name', 'last_name',
            # Enhanced fields
            'responsible_location', 'allowed_assignment_locations', 'permissions_summary',
            'accessible_stores_count', 'accessible_locations_count', 'accessible_standalone_count',
            'is_main_store_incharge', 'can_issue_upward', 'parent_standalone_for_issuance'
        ]
        read_only_fields = ['created_by', 'employee_id', 'created_at', 'updated_at']
    
    def get_user(self, obj):
        """Get user data"""
        return {
            'id': obj.user.id,
            'username': obj.user.username,
            'email': obj.user.email,
            'first_name': obj.user.first_name,
            'last_name': obj.user.last_name,
            'full_name': obj.user.get_full_name() or obj.user.username,
            'is_active': obj.user.is_active
        }
    
    def get_assigned_locations_data(self, obj):
        return LocationMinimalSerializer(obj.assigned_locations.all(), many=True).data
    
    def get_responsible_location(self, obj):
        """Get the location this user is responsible for"""
        responsible_loc = obj.get_responsible_location()
        if responsible_loc:
            return {
                'id': responsible_loc.id,
                'name': responsible_loc.name,
                'type': responsible_loc.location_type,
                'code': responsible_loc.code,
                'is_store': responsible_loc.is_store,
                'is_standalone': responsible_loc.is_standalone,
                'is_main_store': responsible_loc.is_main_store if responsible_loc.is_store else False,
                'parent_location': responsible_loc.parent_location_id
            }
        return None
    
    def get_allowed_assignment_locations(self, obj):
        """Get locations that can be assigned to this user based on their role"""
        request = self.context.get('request')
        if not request or not hasattr(request.user, 'profile'):
            return []
        
        current_user_profile = request.user.profile
        
        if obj.role == UserRole.LOCATION_HEAD:
            # For Location Head, can assign standalone locations
            accessible_locations = current_user_profile.get_accessible_locations()
            assignable_locations = accessible_locations.filter(is_standalone=True).distinct()
            return LocationMinimalSerializer(assignable_locations, many=True).data
        
        elif obj.role == UserRole.STOCK_INCHARGE:
            # For Stock Incharge, can assign stores
            accessible_stores = current_user_profile.get_accessible_stores()
            return LocationMinimalSerializer(accessible_stores, many=True).data
        
        return []
    
    def get_permissions_summary(self, obj):
        """Get comprehensive permissions summary"""
        return obj.get_permissions_summary()
    
    def get_accessible_stores_count(self, obj):
        return obj.get_accessible_stores().count()
    
    def get_accessible_locations_count(self, obj):
        return obj.get_accessible_locations().count()
    
    def get_accessible_standalone_count(self, obj):
        return obj.get_standalone_locations().count()
    
    def get_is_main_store_incharge(self, obj):
        return obj.is_main_store_incharge()
    
    def get_can_issue_upward(self, obj):
        return obj.can_issue_to_parent_standalone()
    
    def get_parent_standalone_for_issuance(self, obj):
        parent = obj.get_parent_standalone_for_issuance()
        if parent:
            return {
                'id': parent.id,
                'name': parent.name,
                'code': parent.code,
                'is_standalone': parent.is_standalone
            }
        return None
    
    def validate_assigned_locations(self, value):
        """Validate assigned locations based on user role"""
        request = self.context.get('request')
        current_user = request.user if request else None
        
        # Get the role being created/updated
        role = self.initial_data.get('role') or (self.instance.role if self.instance else None)
        
        if not role:
            return value
        
        # For Location Head: only allow standalone locations
        if role == UserRole.LOCATION_HEAD:
            non_standalone = []
            for location in value:
                if not location.is_standalone:
                    non_standalone.append(f"{location.name} ({location.location_type})")
            
            if non_standalone:
                raise serializers.ValidationError(
                    f"Location Head can only be assigned to standalone locations. "
                    f"The following are not standalone: {', '.join(non_standalone)}"
                )
        
        # For Stock Incharge: only allow store locations
        elif role == UserRole.STOCK_INCHARGE:
            non_store_locations = []
            for location in value:
                if not location.is_store:
                    non_store_locations.append(f"{location.name} ({location.location_type})")
            
            if non_store_locations:
                raise serializers.ValidationError(
                    f"Stock Incharge can only be assigned to store locations. "
                    f"The following are not stores: {', '.join(non_store_locations)}"
                )
        
        # Additional validation for Location Head creating Stock Incharge
        if current_user and hasattr(current_user, 'profile'):
            current_profile = current_user.profile
            
            if current_profile.role == UserRole.LOCATION_HEAD:
                if role == UserRole.STOCK_INCHARGE:
                    # Validate each location is accessible to the Location Head
                    inaccessible_locations = []
                    for location in value:
                        if not location.is_store:
                            inaccessible_locations.append(f"{location.name} (not a store)")
                        elif not current_profile.has_location_access(location):
                            inaccessible_locations.append(f"{location.name} (no access)")
                    
                    if inaccessible_locations:
                        raise serializers.ValidationError(
                            f"You don't have access to assign these locations: {', '.join(inaccessible_locations)}"
                        )
        
        return value
    
    def validate_role(self, value):
        """Validate role assignment"""
        request = self.context.get('request')
        if request and hasattr(request.user, 'profile'):
            current_profile = request.user.profile
            
            # Check if current user can assign this role
            if not current_profile.can_create_user(value):
                raise serializers.ValidationError(
                    f"You don't have permission to create users with role: {value}"
                )
        
        return value
    
    @transaction.atomic
    def create(self, validated_data):
        # Extract user creation data
        username = validated_data.pop('username', None)
        password = validated_data.pop('password', None)
        email = validated_data.pop('email', None)
        first_name = validated_data.pop('first_name', None)
        last_name = validated_data.pop('last_name', None)
        assigned_locations = validated_data.pop('assigned_locations', [])
        
        request = self.context.get('request')
        current_user = request.user if request else None
        
        # Validate required fields
        if not username:
            raise serializers.ValidationError({'username': "Username is required"})
        if not password:
            raise serializers.ValidationError({'password': "Password is required"})
        
        # Validate permissions
        if current_user and hasattr(current_user, 'profile'):
            current_profile = current_user.profile
            target_role = validated_data.get('role')
            
            if not current_profile.can_create_user(target_role):
                raise serializers.ValidationError({
                    'role': f"{current_profile.get_role_display()} cannot create {target_role} users"
                })
            
            # Location Head specific validations
            if current_profile.role == UserRole.LOCATION_HEAD:
                if target_role != UserRole.STOCK_INCHARGE:
                    raise serializers.ValidationError({
                        'role': "Location Heads can only create Stock Incharge users"
                    })
                
                if not assigned_locations:
                    raise serializers.ValidationError({
                        'assigned_locations': "At least one store must be assigned to Stock Incharge"
                    })
        
        # Check if user already exists
        if User.objects.filter(username=username).exists():
            raise serializers.ValidationError({
                'username': f"User with username '{username}' already exists"
            })
        
        # Create new Django User
        try:
            user = User.objects.create_user(
                username=username,
                password=password,
                email=email or f"{username}@inventory.local",
                first_name=first_name or '',
                last_name=last_name or ''
            )
        except Exception as e:
            raise serializers.ValidationError({
                'error': f"Failed to create user: {str(e)}"
            })
        
        # Get the profile that was created by the signal
        profile = user.profile
        
        # Update the profile with the validated data
        for field, value in validated_data.items():
            if field != 'user':
                setattr(profile, field, value)
        
        profile.created_by = current_user
        profile.save()
        
        # Set the many-to-many relationship AFTER saving
        if assigned_locations:
            profile.assigned_locations.set(assigned_locations)
        
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
        # Remove user-related fields that we'll handle separately
        validated_data.pop('username', None)
        validated_data.pop('password', None)
        
        email = validated_data.pop('email', None)
        first_name = validated_data.pop('first_name', None)
        last_name = validated_data.pop('last_name', None)
        assigned_locations = validated_data.pop('assigned_locations', None)
        
        # Update user fields if provided
        if email:
            instance.user.email = email
        if first_name is not None:
            instance.user.first_name = first_name
        if last_name is not None:
            instance.user.last_name = last_name
        instance.user.save()
        
        request = self.context.get('request')
        if request and hasattr(request.user, 'profile'):
            current_profile = request.user.profile
            
            # Permission checks for updates
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
        
        # Update assigned locations AFTER saving the instance
        if assigned_locations is not None:
            instance.assigned_locations.set(assigned_locations)
        
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
        
        return instance


# ==================== LOCATION SERIALIZERS ====================
class LocationMinimalSerializer(serializers.ModelSerializer):
    is_standalone = serializers.BooleanField(read_only=True)
    is_main_store = serializers.BooleanField(read_only=True)
    
    class Meta:
        model = Location
        fields = ['id', 'name', 'code', 'location_type', 'is_store', 'is_auto_created', 
                 'is_main_store', 'parent_location', 'is_standalone', 'is_root_location']


# Location Serializer - FIXED VERSION
# Add this to your serializers.py, replacing the LocationSerializer class

class LocationSerializer(serializers.ModelSerializer):
    parent_location_name = serializers.CharField(source='parent_location.name', read_only=True)
    full_path = serializers.SerializerMethodField()
    total_items = serializers.SerializerMethodField()
    stores_count = serializers.SerializerMethodField()
    all_stores = serializers.SerializerMethodField()
    auto_created_store_data = serializers.SerializerMethodField()
    main_store = serializers.SerializerMethodField()
    depth = serializers.SerializerMethodField()
    is_standalone = serializers.BooleanField()
    same_hierarchy_locations = serializers.SerializerMethodField()
    parent_standalone = serializers.SerializerMethodField()
    
    # Enhanced fields
    can_have_sub_locations = serializers.SerializerMethodField()
    allowed_location_types = serializers.SerializerMethodField()
    can_be_assigned_to_location_head = serializers.SerializerMethodField()
    can_issue_to_parent_standalone = serializers.SerializerMethodField()
    parent_standalone_for_issuance = serializers.SerializerMethodField()
    
    class Meta:
        model = Location
        fields = '__all__'
        read_only_fields = ['hierarchy_level', 'hierarchy_path', 'auto_created_store', 
                           'is_auto_created', 'created_at', 'updated_at', 'is_root_location']
    
    def get_full_path(self, obj):
        return obj.get_full_path()
    
    def get_total_items(self, obj):
        if obj.is_store:
            return ItemInstance.objects.filter(source_location=obj).count()
        return 0
    
    def get_stores_count(self, obj):
        if obj.is_store:
            return 0
        return obj.get_all_stores().count()
    
    def get_all_stores(self, obj):
        stores = obj.get_all_stores()
        return LocationMinimalSerializer(stores, many=True).data
    
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
    
    def get_same_hierarchy_locations(self, obj):
        # For performance, return minimal data
        root = obj.get_root_location()
        if root:
            return {'root_id': root.id, 'root_name': root.name}
        return None
    
    def get_parent_standalone(self, obj):
        parent_standalone = obj.get_parent_standalone()
        if parent_standalone:
            return {
                'id': parent_standalone.id,
                'name': parent_standalone.name,
                'code': parent_standalone.code,
                'is_standalone': parent_standalone.is_standalone
            }
        return None
    
    def get_can_have_sub_locations(self, obj):
        return obj.can_have_sub_locations()
    
    def get_allowed_location_types(self, obj):
        """Get allowed location types that can be created under this location"""
        if not obj.can_have_sub_locations():
            return []
        
        # Define hierarchy rules
        type_hierarchy = {
            'ROOT': [LocationType.DEPARTMENT, LocationType.BUILDING, LocationType.JUNKYARD, 
                    LocationType.OFFICE, LocationType.OTHER],
            'DEPARTMENT': [LocationType.STORE, LocationType.ROOM, LocationType.LAB, 
                         LocationType.OFFICE, LocationType.AV_HALL, LocationType.AUDITORIUM],
            'BUILDING': [LocationType.STORE, LocationType.ROOM, LocationType.LAB, 
                        LocationType.OFFICE, LocationType.AV_HALL, LocationType.AUDITORIUM],
            'OFFICE': [LocationType.STORE, LocationType.ROOM],
            'OTHER': [LocationType.STORE, LocationType.ROOM, LocationType.LAB],
        }
        
        parent_type = 'ROOT' if obj.is_root_location else obj.location_type
        return type_hierarchy.get(parent_type, [LocationType.STORE, LocationType.ROOM])
    
    def get_can_be_assigned_to_location_head(self, obj):
        """Check if this location can be assigned to a Location Head"""
        return obj.is_standalone
    
    def get_can_issue_to_parent_standalone(self, obj):
        """Check if this store can issue to parent standalone"""
        return obj.can_issue_to_parent_standalone() if obj.is_store else False
    
    def get_parent_standalone_for_issuance(self, obj):
        """Get the parent standalone this store can issue to"""
        if obj.is_store:
            parent = obj.get_parent_standalone_for_issuance()
            if parent:
                return {
                    'id': parent.id,
                    'name': parent.name,
                    'code': parent.code
                }
        return None
    
    def validate(self, data):
        request = self.context.get('request')
        parent_location = data.get('parent_location')
        location_type = data.get('location_type')
        is_store = data.get('is_store', False)
        is_standalone = data.get('is_standalone', False)
        
        # FIX: Auto-set is_store = True if location_type is STORE
        if location_type == LocationType.STORE:
            data['is_store'] = True
            is_store = True
            # Force is_standalone to False for stores
            data['is_standalone'] = False
            is_standalone = False
        
        if not request or not hasattr(request.user, 'profile'):
            raise serializers.ValidationError("User authentication required")
        
        profile = request.user.profile
        
        # Check if user can create location
        if not profile.can_create_location(parent_location):
            raise serializers.ValidationError({
                'parent_location': "You don't have permission to create locations under this parent"
            })
        
        # Validate stores cannot be standalone
        if is_store and is_standalone:
            raise serializers.ValidationError({
                'is_standalone': "Store locations cannot be marked as standalone"
            })
        
        # Validate location type hierarchy
        if parent_location:
            serializer = LocationSerializer(parent_location, context=self.context)
            allowed_types = serializer.get_allowed_location_types(parent_location)
            if location_type not in allowed_types:
                raise serializers.ValidationError({
                    'location_type': f"Cannot create {location_type} under {parent_location.location_type}. "
                                   f"Allowed types: {', '.join(allowed_types)}"
                })
        
        # Validate root location rules
        if not parent_location:
            # Only SYSTEM_ADMIN can create root
            if profile.role != UserRole.SYSTEM_ADMIN:
                raise serializers.ValidationError({
                    'parent_location': "Only System Admin can create root location"
                })
            
            # Root must be standalone
            if not is_standalone:
                raise serializers.ValidationError({
                    'is_standalone': "Root location must be standalone"
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


class CategoryDepreciationSerializer(serializers.Serializer):
    """Separate serializer for depreciation calculations"""
    opening_value = serializers.DecimalField(max_digits=15, decimal_places=2, required=True)
    years = serializers.IntegerField(default=1, min_value=1, max_value=50)
    
    def calculate(self, category):
        opening_value = self.validated_data['opening_value']
        years = self.validated_data['years']
        return category.calculate_wdv_depreciation(opening_value, years)


# ==================== ITEM SERIALIZERS ====================
class ItemMinimalSerializer(serializers.ModelSerializer):
    class Meta:
        model = Item
        fields = ['id', 'name', 'code', 'category']


class ItemSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    default_location_name = serializers.CharField(source='default_location.name', read_only=True)
    default_location_is_standalone = serializers.BooleanField(source='default_location.is_standalone', read_only=True)
    total_instances = serializers.SerializerMethodField()
    available_quantity = serializers.SerializerMethodField()
    
    class Meta:
        model = Item
        fields = '__all__'
    
    def get_total_instances(self, obj):
        return obj.instances.count()
    
    def get_available_quantity(self, obj):
        return obj.instances.filter(current_status='IN_STORE').count()
    
    def validate_default_location(self, value):
        """Validate that default_location is standalone"""
        if not value.is_standalone:
            raise serializers.ValidationError(
                "Items must belong to a standalone location (Department, Main University, etc.)"
            )
        return value


# ==================== INSPECTION SERIALIZERS ====================
# InspectionItem Serializer - UPDATED VERSION
# Replace the InspectionItemSerializer in serializers.py

# InspectionItem Serializer - 4-STAGE WORKFLOW VERSION
# Replace the InspectionItemSerializer in serializers.py

# InspectionItem Serializer - FIXED VERSION (No initial_data error)
# Replace the InspectionItemSerializer in serializers.py

class InspectionItemSerializer(serializers.ModelSerializer):
    # CRITICAL: Make id writable so we can update existing items
    id = serializers.IntegerField(required=False)

    item_name = serializers.CharField(source='item.name', read_only=True)
    item_code = serializers.CharField(source='item.code', read_only=True)
    item_unit = serializers.CharField(source='item.acct_unit', read_only=True)
    total_value = serializers.SerializerMethodField()
    
    # Add read-only flags to show which fields should be editable by which role
    can_edit_basic_details = serializers.SerializerMethodField()
    can_edit_stock_register = serializers.SerializerMethodField()
    can_edit_central_register = serializers.SerializerMethodField()
    
    class Meta:
        model = InspectionItem
        exclude = ['inspection_certificate']
    
    def get_total_value(self, obj):
        if obj.unit_price:
            return float(obj.accepted_quantity * obj.unit_price)
        return None
    
    def get_can_edit_basic_details(self, obj):
        """
        Basic item details can be edited by Location Head in INITIATED stage
        """
        request = self.context.get('request')
        if not request or not hasattr(request.user, 'profile'):
            return False
        
        profile = request.user.profile
        cert = obj.inspection_certificate
        
        if profile.role == UserRole.LOCATION_HEAD and cert.stage == 'INITIATED':
            if profile.has_location_access(cert.department):
                return True
        
        if profile.role == UserRole.SYSTEM_ADMIN:
            return True
        
        return False
        
        if profile.role == UserRole.SYSTEM_ADMIN:
            return True
        
        return False
    
    def get_can_edit_stock_register(self, obj):
        """
        Stock register details can be edited by department store incharge
        in STOCK_DETAILS stage (NON-ROOT ONLY)
        """
        request = self.context.get('request')
        if not request or not hasattr(request.user, 'profile'):
            return False
        
        profile = request.user.profile
        cert = obj.inspection_certificate
        
        # STOCK_DETAILS stage should only exist for non-root certificates
        if profile.role == UserRole.STOCK_INCHARGE and cert.stage == 'STOCK_DETAILS':
            main_store = cert.get_main_store()
            if main_store and profile.has_location_access(main_store):
                # Must NOT be central store incharge
                if not profile.is_main_store_incharge():
                    return True
        
        if profile.role == UserRole.SYSTEM_ADMIN:
            return True
        
        return False
    
    def get_can_edit_central_register(self, obj):
        """
        Central register details can be edited by central store incharge
        in CENTRAL_REGISTER stage
        """
        request = self.context.get('request')
        if not request or not hasattr(request.user, 'profile'):
            return False
        
        profile = request.user.profile
        cert = obj.inspection_certificate
        
        if profile.role == UserRole.STOCK_INCHARGE and cert.stage == 'CENTRAL_REGISTER':
            # MUST be central store incharge
            if profile.is_main_store_incharge():
                return True
        
        if profile.role == UserRole.SYSTEM_ADMIN:
            return True
        
        return False
    
    def validate(self, data):
        """
        Validate quantities and ensure proper field editing based on workflow stage
        """
        tendered = data.get('tendered_quantity', 0)
        accepted = data.get('accepted_quantity', 0)
        rejected = data.get('rejected_quantity', 0)
        
        if (accepted + rejected) > tendered:
            raise serializers.ValidationError(
                "Accepted + Rejected quantity cannot exceed tendered quantity"
            )
        
        # Validate that only appropriate fields are being edited based on stage
        request = self.context.get('request')
        if request and hasattr(request.user, 'profile'):
            profile = request.user.profile
            
            # Get the inspection certificate
            inspection_cert = None
            
            if self.instance:
                inspection_cert = self.instance.inspection_certificate
            elif hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'instance'):
                inspection_cert = self.parent.instance
            
            if inspection_cert:
                # STAGE 1: Location Head - Can edit basic details, NOT register fields
                if profile.role == UserRole.LOCATION_HEAD and inspection_cert.stage == 'INITIATED':
                    register_fields = [
                        'stock_register_no', 'stock_register_page_no', 'stock_entry_date',
                        'central_register_no', 'central_register_page_no'
                    ]
                    if any(field in data for field in register_fields):
                        raise serializers.ValidationError({
                            'error': 'Location Head cannot edit register fields. These will be filled in later stages.'
                        })
                
                # STAGE 2: Department Store Incharge - Can ONLY edit stock register fields (NON-ROOT)
                elif profile.role == UserRole.STOCK_INCHARGE and inspection_cert.stage == 'STOCK_DETAILS':
                    if not profile.is_main_store_incharge():
                        # Can only edit stock register fields
                        allowed_fields = ['stock_register_no', 'stock_register_page_no', 'stock_entry_date']
                        basic_fields = [
                            'item', 'tendered_quantity', 'accepted_quantity', 
                            'rejected_quantity', 'unit_price', 'remarks'
                        ]
                        central_fields = ['central_register_no', 'central_register_page_no']
                        
                        if any(field in data for field in basic_fields):
                            raise serializers.ValidationError({
                                'error': 'Stock Incharge cannot edit basic item details in STOCK_DETAILS stage.'
                            })
                        if any(field in data for field in central_fields):
                            raise serializers.ValidationError({
                                'error': 'Stock Incharge cannot edit central register details in STOCK_DETAILS stage.'
                            })
                
                # STAGE 3: Central Store Incharge - Can ONLY edit central register fields
                elif profile.role == UserRole.STOCK_INCHARGE and inspection_cert.stage == 'CENTRAL_REGISTER':
                    if profile.is_main_store_incharge():
                        # Can only edit central register fields
                        allowed_fields = ['central_register_no', 'central_register_page_no']
                        basic_fields = [
                            'item', 'tendered_quantity', 'accepted_quantity', 
                            'rejected_quantity', 'unit_price', 'remarks'
                        ]
                        stock_fields = ['stock_register_no', 'stock_register_page_no', 'stock_entry_date']
                        
                        if any(field in data for field in basic_fields):
                            raise serializers.ValidationError({
                                'error': 'Central Store Incharge cannot edit basic item details.'
                            })
                        if any(field in data for field in stock_fields):
                            raise serializers.ValidationError({
                                'error': 'Central Store Incharge cannot edit stock register details in CENTRAL_REGISTER stage.'
                            })
                
                # STAGE 4: Auditor - Cannot edit item fields at all
                elif profile.role == UserRole.AUDITOR and inspection_cert.stage == 'AUDIT_REVIEW':
                    if data:
                        raise serializers.ValidationError({
                            'error': 'Auditor cannot edit item details in AUDIT_REVIEW stage.'
                        })
        
        return data


# InspectionCertificate Serializer - UPDATED VERSION
# Replace the InspectionCertificateSerializer in serializers.py

# InspectionCertificate Serializer - 4-STAGE WORKFLOW VERSION
# Replace the InspectionCertificateSerializer in serializers.py

# InspectionCertificate Serializer - FIXED STAGE VALIDATION
# Replace the InspectionCertificateSerializer in serializers.py

class InspectionCertificateSerializer(serializers.ModelSerializer):
    inspection_items = InspectionItemSerializer(many=True, required=False)
    department_name = serializers.CharField(source='department.name', read_only=True)
    department_full_path = serializers.SerializerMethodField()
    department_is_standalone = serializers.BooleanField(source='department.is_standalone', read_only=True)
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
    
    total_items_count = serializers.SerializerMethodField()
    total_accepted = serializers.SerializerMethodField()
    total_rejected = serializers.SerializerMethodField()
    total_value = serializers.SerializerMethodField()
    
    # New fields for 4-stage workflow
    is_main_store_incharge = serializers.SerializerMethodField()
    central_store_info = serializers.SerializerMethodField()
    current_user_role_display = serializers.SerializerMethodField()
    
    class Meta:
        model = InspectionCertificate
        fields = '__all__'
        read_only_fields = [
            'certificate_no', 'stage', 'stage_history', 'initiated_by', 'initiated_at',
            'stock_filled_by', 'stock_filled_at', 'auditor_reviewed_by', 'auditor_reviewed_at',
            'rejected_by', 'rejected_at', 'created_by', 'acknowledged_by', 'acknowledged_at',
            'created_at', 'updated_at', 'is_root_department', 'workflow_type', 'current_handler'  # â† ADD THIS
        ]

    # Add these to the serializer fields list
    is_root_department = serializers.SerializerMethodField()
    workflow_type = serializers.SerializerMethodField()
    workflow_stages = serializers.SerializerMethodField()
    current_stage_info = serializers.SerializerMethodField()
    current_handler = serializers.SerializerMethodField()

    def get_current_handler(self, obj):
        """Get who should handle the current stage"""
        is_root = obj.department.parent_location is None
        
        handlers = {
            'INITIATED': 'Location Head',
            'STOCK_DETAILS': 'Department Store Incharge',
            'CENTRAL_REGISTER': 'Central Store Incharge',
            'AUDIT_REVIEW': 'Auditor',
            'COMPLETED': 'Completed',
            'REJECTED': 'Rejected'
        }
        
        handler = handlers.get(obj.stage, 'Unknown')
        
        # For root certs in CENTRAL_REGISTER, clarify it's the same as dept store
        if is_root and obj.stage == 'CENTRAL_REGISTER':
            handler += ' (Root Main Store)'
        
        return handler

    def get_is_root_department(self, obj):
        """
        Check if this certificate's department is the root location.
        Root location has no parent_location.
        """
        if obj.department:
            is_root = obj.department.parent_location is None
            # Debug logging
            print(f"Serializer - Department: {obj.department.name}, Parent: {obj.department.parent_location}, Is Root: {is_root}")
            return is_root
        return False
    
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
    
    def get_is_main_store_incharge(self, obj):
        """Check if current user is root/central main store incharge"""
        request = self.context.get('request')
        if request and hasattr(request.user, 'profile'):
            return request.user.profile.is_main_store_incharge()
        return False
    
    def get_central_store_info(self, obj):
        """Get information about the central/root store"""
        request = self.context.get('request')
        if request and hasattr(request.user, 'profile'):
            profile = request.user.profile
            if profile.is_main_store_incharge():
                # Get the root location's main store
                from inventory.models import Location
                root = Location.objects.filter(parent_location__isnull=True).first()
                if root:
                    main_store = root.get_main_store()
                    if main_store:
                        return {
                            'id': main_store.id,
                            'name': main_store.name,
                            'code': main_store.code
                        }
        return None
    
    def get_current_user_role_display(self, obj):
        """Get current user's role for frontend display"""
        request = self.context.get('request')
        if request and hasattr(request.user, 'profile'):
            profile = request.user.profile
            role_display = profile.get_role_display()
            if profile.role == UserRole.STOCK_INCHARGE and profile.is_main_store_incharge():
                role_display += " (Central Store)"
            return role_display
        return None
    
    def get_can_edit(self, obj):
        """
        CRITICAL FIX: Department Store Incharge can edit in STOCK_DETAILS stage
        """
        request = self.context.get('request')
        if request and hasattr(request.user, 'profile'):
            profile = request.user.profile
            
            # System Admin can always edit
            if profile.role == UserRole.SYSTEM_ADMIN:
                return True
            
            # Stage 1: Location Head
            if obj.stage == 'INITIATED' and profile.role == UserRole.LOCATION_HEAD:
                return profile.has_location_access(obj.department)
            
            # Stage 2: Department Store Incharge (CRITICAL FIX)
            elif obj.stage == 'STOCK_DETAILS' and profile.role == UserRole.STOCK_INCHARGE:
                # Must NOT be central store incharge
                if profile.is_main_store_incharge():
                    return False
                
                # Must have access to department's main store
                main_store = obj.get_main_store()
                return main_store and profile.has_location_access(main_store)
            
            # Stage 3: CENTRAL_REGISTER - Central Store Incharge
            elif obj.stage == 'CENTRAL_REGISTER' and profile.role == UserRole.STOCK_INCHARGE:
                return profile.is_main_store_incharge()
            
            # Stage 4: AUDIT_REVIEW - Auditor
            elif obj.stage == 'AUDIT_REVIEW' and profile.role == UserRole.AUDITOR:
                return True
        
        return False
    
    def get_can_submit(self, obj):
        """
        Proper 4-STAGE + 3-STAGE workflow submission permissions
        """
        request = self.context.get('request')
        if not request or not hasattr(request.user, 'profile'):
            return False
        
        profile = request.user.profile
        
        # System admin can submit at any stage
        if profile.role == UserRole.SYSTEM_ADMIN:
            return True
        
        # Stage 1: Location Head can submit from INITIATED
        if obj.stage == 'INITIATED' and profile.role == UserRole.LOCATION_HEAD:
            return profile.has_location_access(obj.department)
        
        # Stage 2: Department Store Incharge can submit from STOCK_DETAILS (NON-ROOT ONLY)
        elif obj.stage == 'STOCK_DETAILS' and profile.role == UserRole.STOCK_INCHARGE:
            # Must NOT be central store incharge
            if profile.is_main_store_incharge():
                return False
            
            # Must have access to the department's main store
            main_store = obj.get_main_store()
            return main_store and profile.has_location_access(main_store)
        
        # Stage 3: Central Store Incharge can submit from CENTRAL_REGISTER
        elif obj.stage == 'CENTRAL_REGISTER' and profile.role == UserRole.STOCK_INCHARGE:
            # MUST be central store incharge
            return profile.is_main_store_incharge()
        
        # Stage 4: Auditor can submit from AUDIT_REVIEW
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
        """
        UPDATED Workflow - Editable Fields by Stage
        - Stage 1 (INITIATED): Location Head - basic info + consignee fields
        - Stage 2 (STOCK_DETAILS): Dept Stock Incharge - only register details
        - Stage 3 (CENTRAL_REGISTER): Central Store - register + central_store_entry_date
        - Stage 4 (AUDIT_REVIEW): Auditor - only finance_check_date
        """
        request = self.context.get('request')
        if not request or not hasattr(request.user, 'profile'):
            return []
        
        profile = request.user.profile
        
        if not self.get_can_edit(obj):
            return []
        
        is_root_cert = obj.department.parent_location is None
        
        # Stage 1: Location Head fills basic info + consignee fields
        if obj.stage == 'INITIATED':
            return [
                'contractor_name', 'contractor_address', 'contract_no', 'contract_date',
                'indenter', 'indent_no', 'department', 'date', 'date_of_delivery',
                'delivery_type', 'remarks', 'certificate_image',
                'inspection_items',
                # Consignee fields now in Stage 1
                'inspected_by', 'date_of_inspection', 'consignee_name', 'consignee_designation'
            ]
        
        # Stage 2: Department Store Incharge fills ONLY stock register details
        elif obj.stage == 'STOCK_DETAILS':
            if not is_root_cert and profile.role == UserRole.STOCK_INCHARGE and not profile.is_main_store_incharge():
                return [
                    'inspection_items',  # For stock register fields in items only
                ]
            return []
        
        # Stage 3: Central Store Incharge fills central register + entry date
        elif obj.stage == 'CENTRAL_REGISTER':
            if profile.role == UserRole.STOCK_INCHARGE and profile.is_main_store_incharge():
                return [
                    'inspection_items',  # For central register fields in items
                    'central_store_entry_date'  # Added central store entry date
                ]
            return []
        
        # Stage 4: Auditor fills ONLY finance_check_date
        elif obj.stage == 'AUDIT_REVIEW':
            if profile.role == UserRole.AUDITOR:
                return [
                    'finance_check_date'  # Only finance check date now
                ]
            return []
        
        return []
    
    def get_next_stage(self, obj):
        """Get the next stage name based on certificate type"""
        is_root_cert = obj.department.parent_location is None
        
        stage_flow = {
            'INITIATED': {
                'root': 'Central Register Entry',
                'non_root': 'Stock Details Entry'
            },
            'STOCK_DETAILS': {
                'root': None,  # Root certs skip this
                'non_root': 'Central Register Entry'
            },
            'CENTRAL_REGISTER': {
                'root': 'Auditor Review',
                'non_root': 'Auditor Review'
            },
            'AUDIT_REVIEW': {
                'root': 'Completion',
                'non_root': 'Completion'
            }
        }
        
        stage_info = stage_flow.get(obj.stage, {})
        key = 'root' if is_root_cert else 'non_root'
        return stage_info.get(key, 'Next Stage')
    
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
    
    def get_is_root_department(self, obj):
        """Check if this certificate's department is root location"""
        return obj.department.parent_location is None

    def get_workflow_type(self, obj):
        """Determine workflow type based on department"""
        is_root = obj.department.parent_location is None
        return '3-stage (Root)' if is_root else '4-stage (Department)'

    def get_workflow_stages(self, obj):
        """Get list of workflow stages based on department type"""
        is_root = obj.department.parent_location is None if obj.department else False
        
        if is_root:
            return [
                {'stage': 'INITIATED', 'label': 'Stage 1: Location Head', 'description': 'Basic Info & Items'},
                {'stage': 'AUDIT_REVIEW', 'label': 'Stage 2: Central Store', 'description': 'Central Register'},
                {'stage': 'COMPLETED', 'label': 'Stage 3: Auditor', 'description': 'Final Verification'}
            ]
        else:
            return [
                {'stage': 'INITIATED', 'label': 'Stage 1: Location Head', 'description': 'Basic Info & Items'},
                {'stage': 'STOCK_DETAILS', 'label': 'Stage 2: Dept Store', 'description': 'Stock Register'},
                {'stage': 'AUDIT_REVIEW', 'label': 'Stage 3: Central Store', 'description': 'Central Register'},
                {'stage': 'COMPLETED', 'label': 'Stage 4: Auditor', 'description': 'Final Verification'}
            ]

    def get_current_stage_info(self, obj):
        """Get detailed info about current stage"""
        request = self.context.get('request')
        if not request or not hasattr(request.user, 'profile'):
            return None
        
        profile = request.user.profile
        is_root = obj.department.parent_location is None
        
        stage_info = {
            'stage': obj.stage,
            'stage_display': obj.get_stage_display(),
            'is_root_flow': is_root,
            'workflow_type': '3-stage (Root)' if is_root else '4-stage (Non-Root)',
        }
        
        # Determine what action is needed and by whom
        if obj.stage == 'INITIATED':
            stage_info['action_needed'] = 'Submit to Stock Incharge'
            stage_info['action_by'] = 'Location Head'
            stage_info['next_stage'] = 'Central Store Registration' if is_root else 'Department Store Registration'
            stage_info['can_edit'] = profile.role in [UserRole.LOCATION_HEAD, UserRole.SYSTEM_ADMIN]
            
        elif obj.stage == 'STOCK_DETAILS':
            stage_info['action_needed'] = 'Submit Stock Details'
            stage_info['action_by'] = 'Department Store Incharge'
            stage_info['next_stage'] = 'Central Store Registration'
            # Only dept store incharge can edit (not central store)
            stage_info['can_edit'] = (
                profile.role == UserRole.STOCK_INCHARGE and 
                not profile.is_main_store_incharge()
            ) or profile.role == UserRole.SYSTEM_ADMIN
            
        elif obj.stage == 'AUDIT_REVIEW':
            # Check if central register is filled
            has_central_register = obj.inspection_items.filter(
                central_register_no__isnull=False
            ).exists()
            
            if not has_central_register:
                stage_info['action_needed'] = 'Fill Central Register'
                stage_info['action_by'] = 'Central Store Incharge'
                stage_info['next_stage'] = 'Auditor Review'
                stage_info['can_edit'] = (
                    profile.role == UserRole.STOCK_INCHARGE and 
                    profile.is_main_store_incharge()
                ) or profile.role == UserRole.SYSTEM_ADMIN
            else:
                stage_info['action_needed'] = 'Complete Audit'
                stage_info['action_by'] = 'Auditor'
                stage_info['next_stage'] = 'Completed'
                stage_info['can_edit'] = profile.role in [UserRole.AUDITOR, UserRole.SYSTEM_ADMIN]
        
        return stage_info
    
    def validate_department(self, value):
        """Validate that department is standalone"""
        if not value.is_standalone:
            raise serializers.ValidationError(
                "Inspection certificates must be for standalone locations only (Departments, Main University, etc.)"
            )
        return value
    
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
                details={
                    'certificate_no': inspection_cert.certificate_no,
                    'department': inspection_cert.department.name
                }
            )
        
        return inspection_cert
    
    # Replace the update method in InspectionCertificateSerializer class

    @transaction.atomic
    def update(self, instance, validated_data):
        request = self.context.get('request')
        
        if request and hasattr(request.user, 'profile'):
            profile = request.user.profile
            
            # STAGE 1: Location Head can only edit in INITIATED stage
            if profile.role == UserRole.LOCATION_HEAD:
                if instance.stage != 'INITIATED':
                    raise serializers.ValidationError({
                        'error': f"Location Head cannot edit certificate after {instance.get_stage_display()} stage"
                    })
            
            # STAGE 2: Stock Incharge can edit in STOCK_DETAILS stage
            elif profile.role == UserRole.STOCK_INCHARGE:
                if instance.stage == 'STOCK_DETAILS':
                    # Check if they have access to the department's main store
                    main_store = instance.get_main_store()
                    if not main_store or not profile.has_location_access(main_store):
                        raise serializers.ValidationError({
                            'error': "You don't have access to this certificate's location"
                        })
                    # Make sure they're not the central store incharge
                    if profile.is_main_store_incharge():
                        raise serializers.ValidationError({
                            'error': "Central store incharge should not fill stock details. This is for the department's store incharge."
                        })
                
                # STAGE 3: Central Store Incharge can edit in CENTRAL_REGISTER
                elif instance.stage == 'CENTRAL_REGISTER':
                    # Only central store incharge can edit here
                    if not profile.is_main_store_incharge():
                        raise serializers.ValidationError({
                            'error': "Only central store incharge can edit in CENTRAL_REGISTER stage"
                        })
                else:
                    raise serializers.ValidationError({
                        'error': "Stock Incharge can only edit in their designated stage"
                    })
            
            # STAGE 3B: Auditor can only edit in AUDIT_REVIEW
            elif profile.role == UserRole.AUDITOR:
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
        
        # Handle inspection items update
        if inspection_items_data is not None:
            if request and hasattr(request.user, 'profile'):
                profile = request.user.profile
                
                # Only Location Head, Stock Incharge, and System Admin can manage items
                if profile.role not in [UserRole.LOCATION_HEAD, UserRole.STOCK_INCHARGE, UserRole.SYSTEM_ADMIN]:
                    raise serializers.ValidationError({
                        'inspection_items': "Only Location Head and Stock Incharge can manage items"
                    })
            
            # CRITICAL FIX: Don't create new items, only update existing ones
            for item_data in inspection_items_data:
                item_id = item_data.get('id')
                
                if item_id:
                    # Update existing item
                    try:
                        item = InspectionItem.objects.get(id=item_id, inspection_certificate=instance)
                        
                        # Control which fields can be updated based on stage and role
                        if instance.stage == 'INITIATED':
                            # Location Head can update all item fields except register numbers
                            allowed_fields = [
                                'item', 'tendered_quantity', 'accepted_quantity', 
                                'rejected_quantity', 'unit_price', 'make_type', 'remarks'
                            ]
                        elif instance.stage == 'STOCK_DETAILS':
                            # Stock Incharge can only update stock register fields
                            allowed_fields = [
                                'stock_register_no', 'stock_register_page_no', 'stock_entry_date'
                            ]
                        elif instance.stage == 'CENTRAL_REGISTER' and profile.role == UserRole.STOCK_INCHARGE:
                            # Central Store Incharge can only update central register fields
                            allowed_fields = [
                                'central_register_no', 'central_register_page_no'
                            ]
                        else:
                            allowed_fields = []
                        
                        # Update only allowed fields
                        for field, value in item_data.items():
                            if field != 'id' and field in allowed_fields:
                                setattr(item, field, value)
                        item.save()
                        
                    except InspectionItem.DoesNotExist:
                        # Item doesn't exist - only allow creation in INITIATED stage
                        if instance.stage == 'INITIATED':
                            # Remove 'id' before creating
                            item_data_without_id = {k: v for k, v in item_data.items() if k != 'id'}
                            InspectionItem.objects.create(
                                inspection_certificate=instance,
                                **item_data_without_id
                            )
                else:
                    # No ID provided - create new item (only in INITIATED stage)
                    if instance.stage == 'INITIATED':
                        # Ensure we have the item field
                        if 'item' not in item_data:
                            continue
                        
                        # Check if item already exists for this certificate
                        existing_item = InspectionItem.objects.filter(
                            inspection_certificate=instance,
                            item_id=item_data.get('item')
                        ).first()
                        
                        if existing_item:
                            # Update existing item instead of creating
                            allowed_fields = [
                                'item', 'tendered_quantity', 'accepted_quantity', 
                                'rejected_quantity', 'unit_price', 'make_type', 'remarks'
                            ]
                            for field, value in item_data.items():
                                if field in allowed_fields:
                                    setattr(existing_item, field, value)
                            existing_item.save()
                        else:
                            # Create new item
                            InspectionItem.objects.create(
                                inspection_certificate=instance,
                                **{k: v for k, v in item_data.items() if k != 'id'}
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
    qr_info = serializers.SerializerMethodField()
    
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
        read_only_fields = ['instance_code', 'qr_code_data', 'qr_generated', 'qr_data_json',
                           'previous_status', 'status_changed_at', 'status_changed_by']
    
    def get_location_path(self, obj):
        return obj.current_location.get_full_path()
    
    def get_qr_code_image(self, obj):
        if obj.qr_code_data:
            return obj.qr_code_data
        return None
    
    def get_qr_info(self, obj):
        return obj.get_qr_info()
    
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
# serializers.py - FIXED VERSION with Return Acknowledgment Support
# This file includes proper serialization for the new acknowledge_return endpoint

from rest_framework import serializers
from django.utils import timezone
from django.contrib.auth.models import User
from inventory.models import *
from user_management.models import UserProfile, UserRole, UserActivity
import json
from django.db import transaction

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
    requires_acknowledgment = serializers.BooleanField(read_only=True)
    is_cross_location = serializers.BooleanField(read_only=True)
    is_upward_transfer = serializers.BooleanField(read_only=True)
    is_transfer = serializers.SerializerMethodField()
    instances_count = serializers.SerializerMethodField()
    can_acknowledge = serializers.SerializerMethodField()
    
    # NEW: Return acknowledgment fields
    is_return_entry = serializers.SerializerMethodField()
    can_acknowledge_return = serializers.SerializerMethodField()
    original_transfer_info = serializers.SerializerMethodField()
    
    class Meta:
        model = StockEntry
        fields = '__all__'
        read_only_fields = ['entry_number', 'created_at', 'updated_at', 'created_by', 
                           'requires_acknowledgment', 'is_cross_location', 'is_upward_transfer']
    
    def get_created_by_name(self, obj):
        return obj.created_by.get_full_name() if obj.created_by else None
    
    def get_acknowledged_by_name(self, obj):
        return obj.acknowledged_by.get_full_name() if obj.acknowledged_by else None
    
    def get_is_overdue(self, obj):
        if obj.is_temporary and obj.expected_return_date and not obj.actual_return_date:
            return timezone.now().date() > obj.expected_return_date
        return False
    
    def get_is_transfer(self, obj):
        return (obj.entry_type == 'ISSUE' and 
                obj.to_location and 
                obj.to_location.is_store)
    
    def get_instances_count(self, obj):
        return obj.instances.count()
    
    def get_can_acknowledge(self, obj):
        """Check if user can acknowledge receipt (for ISSUE transfers)"""
        request = self.context.get('request')
        if not request or not hasattr(request.user, 'profile'):
            return False
        
        if not obj.requires_acknowledgment or obj.status != 'PENDING_ACK':
            return False
        
        # Only for ISSUE type entries
        if obj.entry_type != 'ISSUE':
            return False
        
        profile = request.user.profile
        
        # User must have access to the destination location
        if obj.to_location:
            return profile.has_location_access(obj.to_location)
        
        return False
    
    def get_is_return_entry(self, obj):
        """Check if this is a RETURN entry"""
        return obj.entry_type == 'RETURN'
    
    def get_can_acknowledge_return(self, obj):
        """Check if user can acknowledge return (for RETURN entries)"""
        request = self.context.get('request')
        if not request or not hasattr(request.user, 'profile'):
            return False
        
        # Only for RETURN entries
        if obj.entry_type != 'RETURN':
            return False
        
        if obj.status != 'PENDING_ACK':
            return False
        
        profile = request.user.profile
        
        # User must have access to the destination location (original sender)
        if obj.to_location:
            return profile.has_location_access(obj.to_location)
        
        return False
    
    def get_original_transfer_info(self, obj):
        """Get information about the original transfer (for RETURN entries)"""
        if obj.entry_type == 'RETURN' and obj.reference_entry:
            return {
                'entry_number': obj.reference_entry.entry_number,
                'entry_type': obj.reference_entry.entry_type,
                'from_location': obj.reference_entry.from_location.name if obj.reference_entry.from_location else None,
                'to_location': obj.reference_entry.to_location.name if obj.reference_entry.to_location else None,
                'created_at': obj.reference_entry.created_at,
                'created_by': obj.reference_entry.created_by.get_full_name() if obj.reference_entry.created_by else None
            }
        return None
    
    def validate(self, data):
        entry_type = data.get('entry_type')
        from_location = data.get('from_location')
        to_location = data.get('to_location')
        is_temporary = data.get('is_temporary', False)
        
        request = self.context.get('request')
        if request and hasattr(request.user, 'profile'):
            profile = request.user.profile
            
            # Stock Incharge validation
            if profile.role == UserRole.STOCK_INCHARGE:
                accessible_stores = profile.get_accessible_stores()
                
                if entry_type == 'ISSUE':
                    # Validate from_location is an accessible store
                    if from_location and from_location not in accessible_stores:
                        raise serializers.ValidationError({
                            'from_location': 'You can only issue from stores you manage'
                        })
                    
                    # CRITICAL FIX: Check if this is an upward transfer
                    if from_location and to_location and from_location.is_main_store:
                        parent_standalone_for_issuance = from_location.get_parent_standalone_for_issuance()
                        
                        # If to_location is the parent standalone, resolve to its main store
                        if to_location == parent_standalone_for_issuance:
                            # This is a valid upward transfer - get the main store
                            target_main_store = to_location.get_main_store()
                            if not target_main_store:
                                raise serializers.ValidationError({
                                    'to_location': f'{to_location.name} does not have a main store'
                                })
                            # Replace to_location with the main store
                            data['to_location'] = target_main_store
                        elif not profile.has_location_access(to_location):
                            # Regular transfer - validate access
                            raise serializers.ValidationError({
                                'to_location': f'You can only issue to locations within your hierarchy or to parent standalone location'
                            })
                
                elif entry_type == 'RECEIPT':
                    # Validate to_location is an accessible store
                    if to_location and to_location not in accessible_stores:
                        raise serializers.ValidationError({
                            'to_location': 'You can only receive to stores you manage'
                        })
        
        # Existing validation
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
            
            # Check transfer permissions (after resolving standalone to main store)
            if from_location and to_location:
                # Check if upward transfer
                if from_location.is_main_store:
                    parent_standalone_for_issuance = from_location.get_parent_standalone_for_issuance()
                    # Get parent standalone of to_location
                    to_parent_standalone = to_location.get_parent_standalone()
                    
                    # Valid if transferring to parent standalone's main store
                    if to_parent_standalone == parent_standalone_for_issuance:
                        # Valid upward transfer
                        pass
                    elif not from_location.can_transfer_to(to_location):
                        raise serializers.ValidationError({
                            'to_location': f"Transfer from {from_location.name} to {to_location.name} is not allowed"
                        })
                elif not from_location.can_transfer_to(to_location):
                    raise serializers.ValidationError({
                        'to_location': f"Transfer from {from_location.name} to {to_location.name} is not allowed"
                    })
            
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
                        inspection_certificate=stock_entry.inspection_certificate,
                        source_location=stock_entry.to_location,
                        current_location=stock_entry.to_location,
                        current_status='IN_STORE',
                        condition='NEW',
                        purchase_date=timezone.now().date(),
                        created_by=user
                    )
                    created_instances.append(instance)
                stock_entry.instances.set(created_instances)
        elif instances_data:
            stock_entry.instances.set(instances_data)
        
        return stock_entry


# ==================== ITEM INSTANCE SERIALIZERS ====================
class ItemInstanceSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source='item.name', read_only=True)
    item_code = serializers.CharField(source='item.code', read_only=True)
    category_name = serializers.CharField(source='item.category.name', read_only=True)
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
    qr_info = serializers.SerializerMethodField()
    
    # Availability flags
    is_available = serializers.SerializerMethodField()
    is_in_transit = serializers.SerializerMethodField()
    is_issued = serializers.SerializerMethodField()
    is_overdue = serializers.SerializerMethodField()
    
    # Assignment tracking
    days_since_assigned = serializers.SerializerMethodField()
    days_until_return = serializers.SerializerMethodField()
    
    # Return/rejection tracking
    is_rejected = serializers.SerializerMethodField()
    rejection_details = serializers.SerializerMethodField()
    
    # ==================== DEPRECIATION FIELDS ====================
    depreciation_rate = serializers.SerializerMethodField()
    age_in_years = serializers.SerializerMethodField()
    current_book_value = serializers.SerializerMethodField()
    accumulated_depreciation = serializers.SerializerMethodField()
    depreciation_info = serializers.SerializerMethodField()
    depreciation_schedule = serializers.SerializerMethodField()
    
    condition_display = serializers.CharField(source='get_condition_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    
    class Meta:
        model = ItemInstance
        fields = '__all__'
        read_only_fields = ['instance_code', 'qr_code_data', 'qr_generated', 'qr_data_json',
                           'previous_status', 'status_changed_at', 'status_changed_by']
    
    def get_location_path(self, obj):
        return obj.current_location.get_full_path()
    
    def get_qr_code_image(self, obj):
        if obj.qr_code_data:
            return obj.qr_code_data
        return None
    
    def get_qr_info(self, obj):
        return obj.get_qr_info()
    
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
    
    def get_is_rejected(self, obj):
        """Check if instance is part of a rejected/return entry"""
        if obj.current_status == InstanceStatus.IN_TRANSIT:
            return_entry = StockEntry.objects.filter(
                instances=obj,
                entry_type='RETURN',
                status='PENDING_ACK'
            ).exists()
            return return_entry
        return False
    
    def get_rejection_details(self, obj):
        """Get details about rejection if applicable"""
        if obj.current_status == InstanceStatus.IN_TRANSIT:
            return_entry = StockEntry.objects.filter(
                instances=obj,
                entry_type='RETURN',
                status='PENDING_ACK'
            ).select_related('from_location', 'to_location', 'created_by').first()
            
            if return_entry:
                return {
                    'return_entry_number': return_entry.entry_number,
                    'rejected_by_location': return_entry.from_location.name if return_entry.from_location else None,
                    'returning_to': return_entry.to_location.name if return_entry.to_location else None,
                    'rejection_reason': return_entry.remarks,
                    'rejected_at': return_entry.created_at,
                    'days_pending_return': (timezone.now() - return_entry.created_at).days
                }
        return None
    
    # ==================== DEPRECIATION SERIALIZER METHODS ====================
    
    def get_depreciation_rate(self, obj):
        """Get depreciation rate from item's category"""
        return float(obj.get_depreciation_rate())
    
    def get_age_in_years(self, obj):
        """Get age of instance in years"""
        return round(obj.get_age_in_years(), 2)
    
    def get_current_book_value(self, obj):
        """Get current depreciated book value"""
        value = obj.get_current_book_value()
        return float(value) if value else None
    
    def get_accumulated_depreciation(self, obj):
        """Get total accumulated depreciation"""
        value = obj.get_accumulated_depreciation()
        return float(value) if value else None
    
    def get_depreciation_info(self, obj):
        """Get comprehensive depreciation information"""
        return obj.get_depreciation_info()
    
    def get_depreciation_schedule(self, obj):
        """
        Get depreciation schedule for next 5 years.
        Only include if explicitly requested to avoid overhead.
        """
        request = self.context.get('request')
        if request and request.query_params.get('include_schedule') == 'true':
            schedule = obj.get_depreciation_schedule(years=5)
            # Convert Decimal to float for JSON serialization
            return [
                {
                    'year': item['year'],
                    'opening_value': float(item['opening_value']),
                    'depreciation_rate': float(item['depreciation_rate']),
                    'depreciation_amount': float(item['depreciation_amount']),
                    'closing_value': float(item['closing_value'])
                }
                for item in schedule
            ]
        return None


# Note: Keep all other serializers from the original file unchanged
# (UserSerializer, UserProfileSerializer, LocationSerializer, CategorySerializer, 
#  ItemSerializer, InspectionCertificateSerializer, InspectionItemSerializer, 
#  LocationInventorySerializer, InstanceMovementSerializer, UserActivitySerializer, etc.)


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