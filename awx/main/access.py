# Copyright (c) 2013 AnsibleWorks, Inc.
# All Rights Reserved.

# Python
import sys
import logging

# Django
from django.db.models import Q
from django.contrib.auth.models import User

# Django REST Framework
from rest_framework.exceptions import PermissionDenied

# AWX
from awx.main.utils import *
from awx.main.models import *
from awx.main.licenses import LicenseReader

__all__ = ['get_user_queryset', 'check_user_access']

PERMISSION_TYPES = [
    PERM_INVENTORY_ADMIN,
    PERM_INVENTORY_READ,
    PERM_INVENTORY_WRITE,
    PERM_INVENTORY_DEPLOY,
    PERM_INVENTORY_CHECK,
]

PERMISSION_TYPES_ALLOWING_INVENTORY_READ = [
    PERM_INVENTORY_ADMIN,
    PERM_INVENTORY_WRITE,
    PERM_INVENTORY_READ,
]

PERMISSION_TYPES_ALLOWING_INVENTORY_WRITE = [
    PERM_INVENTORY_ADMIN,
    PERM_INVENTORY_WRITE,
]

PERMISSION_TYPES_ALLOWING_INVENTORY_ADMIN = [
    PERM_INVENTORY_ADMIN,
]

logger = logging.getLogger('awx.main.access')

access_registry = {
    # <model_class>: [<access_class>, ...],
    # ...
}

def register_access(model_class, access_class):
    access_classes = access_registry.setdefault(model_class, [])
    access_classes.append(access_class)

def get_user_queryset(user, model_class):
    '''
    Return a queryset for the given model_class containing only the instances
    that should be visible to the given user.
    '''
    querysets = []
    for access_class in access_registry.get(model_class, []):
        access_instance = access_class(user)
        querysets.append(access_instance.get_queryset())
    if not querysets:
        return model_class.objects.none()
    elif len(querysets) == 1:
        return querysets[0]
    else:
        queryset = model_class.objects.all()
        for qs in querysets:
            queryset = queryset.filter(pk__in=qs.values_list('pk', flat=True))
        return queryset

def check_user_access(user, model_class, action, *args, **kwargs):
    '''
    Return True if user can perform action against model_class with the
    provided parameters.
    '''
    for access_class in access_registry.get(model_class, []):
        access_instance = access_class(user)
        access_method = getattr(access_instance, 'can_%s' % action, None)
        if not access_method:
            continue
        result = access_method(*args, **kwargs)
        logger.debug('%s.%s %r returned %r', access_instance.__class__.__name__,
                     access_method.__name__, args, result)
        if result:
            return result
    return False

class BaseAccess(object):
    '''
    Base class for checking user access to a given model.  Subclasses should
    define the model attribute, override the get_queryset method to return only
    the instances the user should be able to view, and override/define can_*
    methods to verify a user's permission to perform a particular action.
    '''

    model = None

    def __init__(self, user):
        self.user = user

    def get_queryset(self):
        if self.user.is_superuser:
            return self.model.objects.all()
        else:
            return self.model.objects.none()

    def can_read(self, obj):
        return self.user.is_superuser

    def can_add(self, data):
        return self.user.is_superuser

    def can_change(self, obj, data):
        return self.user.is_superuser

    def can_write(self, obj, data):
        # Alias for change.
        return self.can_change(obj, data)

    def can_admin(self, obj, data):
        # Alias for can_change.  Can be overridden if admin vs. user change
        # permissions need to be different.
        return self.can_change(obj, data)

    def can_delete(self, obj):
        return self.user.is_superuser

    def can_attach(self, obj, sub_obj, relationship, data,
                   skip_sub_obj_read_check=False):
        if skip_sub_obj_read_check:
            return self.can_change(obj, None)
        else:
            return bool(self.can_change(obj, None) and
                        self.user.can_access(type(sub_obj), 'read', sub_obj))

    def can_unattach(self, obj, sub_obj, relationship):
        return self.can_change(obj, None)

class UserAccess(BaseAccess):
    '''
    I can see user records when:
     - I'm a superuser.
     - I'm that user.
     - I'm their org admin.
     - I'm on a team with that user.
    I can change some fields for a user (mainly password) when I am that user.
    I can change all fields for a user (admin access) or delete when:
     - I'm a superuser.
     - I'm their org admin.
    '''

    model = User

    def get_queryset(self):
        qs = self.model.objects.filter(is_active=True).distinct()
        if self.user.is_superuser:
            return qs
        return qs.filter(
            Q(pk=self.user.pk) |
            Q(organizations__in=self.user.admin_of_organizations.all()) |
            Q(teams__in=self.user.teams.all())
        ).distinct()

    def can_read(self, obj):
         # A user can be read if they are on the same team or can be changed.
         matching_teams = self.user.teams.filter(users__in=[self.user]).count()
         return bool(matching_teams or self.can_change(obj, None))

    def can_add(self, data):
        # TODO: reuse. make helper functions like "is user an org admin"
        # apply throughout permissions code
        return bool(self.user.is_superuser or
                    self.user.admin_of_organizations.count())

    def can_change(self, obj, data):
        # A user can be changed if they are themselves, or by org admins or
        # superusers.  Change permission implies changing only certain fields
        # that a user should be able to edit for themselves.
        return bool(self.user == obj or self.can_admin(obj, data))

    def can_admin(self, obj, data):
        # Admin implies changing all user fields.
        if self.user.is_superuser:
            return True
        return bool(obj.organizations.filter(admins__in=[self.user]).count())

    def can_delete(self, obj):
        if obj == self.user:
            # cannot delete yourself
            return False
        super_users = User.objects.filter(is_active=True, is_superuser=True)
        if obj.is_superuser and super_users.count() == 1:
            # cannot delete the last active superuser
            return False
        return bool(self.user.is_superuser or 
                    obj.organizations.filter(admins__in=[self.user]).count())

class OrganizationAccess(BaseAccess):
    '''
    I can see organizations when:
     - I am a superuser.
     - I am an admin or user in that organization.
    I can change or delete organizations when:
     - I am a superuser.
     - I'm an admin of that organization.
    '''

    model = Organization

    def get_queryset(self):
        qs = self.model.objects.distinct()
        if self.user.is_superuser:
            return qs
        return qs.filter(Q(admins__in=[self.user]) | Q(users__in=[self.user]))

    def can_read(self, obj):
        return bool(self.can_change(obj, None) or
                    self.user in obj.users.all())

    def can_change(self, obj, data):
        return bool(self.user.is_superuser or
                    self.user in obj.admins.all())

    def can_delete(self, obj):
        return self.can_change(obj, None)

class InventoryAccess(BaseAccess):
    '''
    I can see inventory when:
     - I'm a superuser.
     - I'm an org admin of the inventory's org.
     - I have read, write or admin permissions on it.
    I can change inventory when:
     - I'm a superuser.
     - I'm an org admin of the inventory's org.
     - I have write or admin permissions on it.
    I can delete inventory when:
     - I'm a superuser.
     - I'm an org admin of the inventory's org.
     - I have admin permissions on it.
    '''

    model = Inventory

    def get_queryset(self, allowed=None):
        allowed = allowed or PERMISSION_TYPES_ALLOWING_INVENTORY_READ
        qs = Inventory.objects.filter(active=True).distinct()
        if self.user.is_superuser:
            return qs
        admin_of = qs.filter(organization__admins__in=[self.user]).distinct()
        has_user_perms = qs.filter(
            permissions__user__in=[self.user],
            permissions__permission_type__in=allowed,
        ).distinct()
        has_team_perms = qs.filter(
            permissions__team__users__in=[self.user],
            permissions__permission_type__in=allowed,
        ).distinct()
        return admin_of | has_user_perms | has_team_perms

    def has_permission_types(self, obj, allowed):
        return bool(obj and self.get_queryset(allowed).filter(pk=obj.pk).count())

    def can_read(self, obj):
        return self.has_permission_types(obj, PERMISSION_TYPES_ALLOWING_INVENTORY_READ)

    def can_add(self, data):
        # If no data is specified, just checking for generic add permission?
        if not data:
            return bool(self.user.is_superuser or self.user.admin_of_organizations.count())
        # Otherwise, verify that the user has access to change the parent
        # organization of this inventory.
        if self.user.is_superuser:
            return True
        else:
            org = get_object_or_400(Organization, pk=data.get('organization', None))
            if self.user.can_access(Organization, 'change', org, None):
                return True
        return False

    def can_change(self, obj, data):
        # Verify that the user has access to the given organization.
        if data and 'organization' in data and not self.can_add(data):
            return False
        return self.has_permission_types(obj, PERMISSION_TYPES_ALLOWING_INVENTORY_WRITE)

    def can_admin(self, obj, data):
        # Verify that the user has access to the given organization.
        if data and 'organization' in data and not self.can_add(data):
            return False
        return self.has_permission_types(obj, PERMISSION_TYPES_ALLOWING_INVENTORY_ADMIN)

    def can_delete(self, obj):
        return self.can_admin(obj, None)

class HostAccess(BaseAccess):
    '''
    I can see hosts whenever I can see their inventory.
    I can change or delete hosts whenver I can change their inventory.
    '''

    model = Host

    def get_queryset(self):
        qs = self.model.objects.filter(active=True).distinct()
        inventories_qs = self.user.get_queryset(Inventory)
        return qs.filter(inventory__in=inventories_qs)

    def can_read(self, obj):
        return obj and self.user.can_access(Inventory, 'read', obj.inventory)

    def can_add(self, data):
        if not data or not 'inventory' in data:
            return False

        # Checks for admin or change permission on inventory.
        inventory = get_object_or_400(Inventory, pk=data.get('inventory', None))
        if not self.user.can_access(Inventory, 'change', inventory, None):
           return False

        # Check to see if we have enough licenses
        reader = LicenseReader()
        validation_info = reader.from_file()

        if 'test' in sys.argv:# and 'free_instances' in validation_info:
            # this hack is in here so the test code can function
            # but still go down *most* of the license code path.
            validation_info['free_instances'] = 99999999

        if validation_info.get('free_instances', 0) > 0:
            # BOOKMARK
            return True
        instances = validation_info.get('available_instances', 0)
        raise PermissionDenied("license range of %s instances has been exceed" % instances)

    def can_change(self, obj, data):
        # Prevent moving a host to a different inventory.
        if obj and data and obj.inventory.pk != data.get('inventory', None):
            raise PermissionDenied('Unable to change inventory on a host')
        # Checks for admin or change permission on inventory, controls whether
        # the user can edit variable data.
        return obj and self.user.can_access(Inventory, 'change', obj.inventory, None)

class GroupAccess(BaseAccess):
    '''
    I can see groups whenever I can see their inventory.
    I can change or delete groups whenever I can change their inventory.
    '''

    model = Group

    def get_queryset(self):
        qs = self.model.objects.filter(active=True).distinct()
        inventories_qs = self.user.get_queryset(Inventory)
        return qs.filter(inventory__in=inventories_qs)

    def can_read(self, obj):
        return obj and self.user.can_access(Inventory, 'read', obj.inventory)

    def can_add(self, data):
        if not data or not 'inventory' in data:
            return False
        # Checks for admin or change permission on inventory.
        inventory = get_object_or_400(Inventory, pk=data.get('inventory', None))
        return self.user.can_access(Inventory, 'change', inventory, None)

    def can_change(self, obj, data):
        # Checks for admin or change permission on inventory, controls whether
        # the user can attach subgroups or edit variable data.
        return obj and self.user.can_access(Inventory, 'change', obj.inventory, None)

    def can_attach(self, obj, sub_obj, relationship, data,
                   skip_sub_obj_read_check=False):
        if not super(GroupAccess, self).can_attach(obj, sub_obj, relationship,
                                                   data, skip_sub_obj_read_check):
            return False

        # Prevent group from being assigned as its own (grand)child.
        if type(obj) == type(sub_obj):
            parent_pks = set(obj.all_parents.values_list('pk', flat=True))
            parent_pks.add(obj.pk)
            child_pks = set(sub_obj.all_children.values_list('pk', flat=True))
            child_pks.add(sub_obj.pk)
            if parent_pks & child_pks:
                return False

        return True

class CredentialAccess(BaseAccess):
    '''
    I can see credentials when:
     - I'm a superuser.
     - It's a user credential and it's my credential.
     - It's a user credential and I'm an admin of an organization where that
       user is a member of admin of the organization.
     - It's a team credential and I'm an admin of the team's organization.
     - It's a team credential and I'm a member of the team.
    '''

    model = Credential

    def get_queryset(self):
        qs = self.model.objects.filter(active=True).distinct()
        if self.user.is_superuser:
            return qs
        orgs_as_admin = self.user.admin_of_organizations.all()
        return qs.filter(
            Q(user=self.user) |
            Q(user__organizations__in=orgs_as_admin) |
            Q(user__admin_of_organizations__in=orgs_as_admin) |
            Q(team__organization__in=orgs_as_admin) |
            Q(team__users__in=[self.user])
        )

    def can_read(self, obj):
        return obj and self.can_change(obj, None)

    def can_add(self, data):
        if self.user.is_superuser:
            return True
        if 'user' in data:
            user_obj = User.objects.get(pk=data['user'])
            return self.user.can_access(User, 'change', user_obj, None)
        if 'team' in data:
            team_obj = Team.objects.get(pk=data['team'])
            return self.user.can_access(Team, 'change', team_obj, None)

    def can_change(self, obj, data):
        if self.user.is_superuser:
            return True
        if self.user == obj.user:
            return True
        if obj.user:
            if (obj.user.organizations.filter(admins__in = [self.user]).count()):
                return True
        if obj.team:
            if self.user in obj.team.organization.admins.all():
                return True
        return False

    def can_delete(self, obj):
        if obj.user is None and obj.team is None:
            # unassociated credentials may be marked deleted by anyone
            return True
        return self.can_change(obj, None)

class TeamAccess(BaseAccess):

    model = Team

    def get_queryset(self):
        # I can see a team when:
        # - I'm a superuser.
        # - I'm an admin of the team's organization.
        # - I'm a member of that team.
        qs = self.model.objects.filter(active=True).distinct()
        if self.user.is_superuser:
            return qs
        return qs.filter(
            Q(organization__admins__in=[self.user]) |
            Q(users__in=[self.user])
        )

    def can_add(self, data):
        if self.user.is_superuser:
            return True
        if Organization.objects.filter(admins__in = [self.user]).count():
            # team assignment to organizations is handled elsewhere, this just creates
            # a blank team
            return True
        return False

    def can_read(self, obj):
        if self.can_change(obj, None):
            return True
        if obj.users.filter(pk__in = [ self.user.pk ]).count():
            return True
        return False

    def can_change(self, obj, data):
        # FIXME -- audit when this is called explicitly, if any
        if self.user.is_superuser:
            return True
        if self.user in obj.organization.admins.all():
            return True
        return False

    def can_delete(self, obj):
        return self.can_change(obj, None)

class ProjectAccess(BaseAccess):

    model = Project

    def get_queryset(self):
        # I can see projects when:
        # - I am a superuser
        # - I am an admin or user in that organization...
        # FIXME
        base = Project.objects.distinct()
        if self.user.is_superuser:
            return base.all()
        my_teams = Team.objects.filter(users__in = [ self.user])
        my_orgs  = Organization.objects.filter(admins__in = [ self.user ])
        return base.filter(
            teams__in = my_teams
        ).distinct() | base.filter(
            organizations__in = my_orgs
        ).distinct()

    def can_read(self, obj):
        if self.can_change(obj, None):
            return True
        # and also if I happen to be on a team inside the project
        # FIXME: add this too
        return False

    def can_change(self, obj, data):
        if self.user.is_superuser:
            return True
        if obj.created_by == self.user:
            return True
        organizations = Organization.objects.filter(admins__in = [ self.user ], projects__in = [ obj ])
        for org in organizations:
            if org in project.organizations():
                return True
        return False

    def can_delete(self, obj):
        return self.can_change(obj, None)

class PermissionAccess(BaseAccess):

    model = Permission

    def get_queryset(self):
        return self.model.objects.distinct() # FIXME

    def can_read(self, obj):
        # a permission can be seen by the assigned user or team
        # or anyone who can administrate that permission
        if obj.user and obj.user == self.user:
            return True
        if obj.team and obj.team.users.filter(pk = self.user.pk).count() > 0:
            return True
        return self.can_change(obj, None)

    def can_change(self, obj, data):
        if self.user.is_superuser:
            return True
        # a permission can be administrated by a super
        # or if a user permission, that an admin of a user's organization
        # or if a team permission, an admin of that team's organization
        if obj.user and obj.user.organizations.filter(admins__in = [self.user]).count() > 0:
            return True
        if obj.team and obj.team.organization.admins.filter(user=self.user).count() > 0:
            return True
        return False

    def can_delete(self, obj):
        return self.can_change(obj, None)

class JobTemplateAccess(BaseAccess):

    model = JobTemplate

    def get_queryset(self):
        ''' 
        I can see job templates when I am a superuser, or I am an admin of the
        project's orgs, or if I'm in a team on the project.  This does not mean
        I would be able to launch a job from the template or edit the template.
        '''
        # FIXME: Don't think this is quite right...
        qs = self.model.objects.all()
        if self.user.is_superuser:
            return qs.all()
        qs = qs.filter(active=True).filter(
            Q(project__organizations__admins__in=[self.user]) |
            Q(project__teams__users__in=[self.user])
        ).distinct()
        #print qs.values_list('name', flat=True)
        return qs

    def can_read(self, obj):
        # you can only see the job templates that you have permission to launch.
        data = dict(
            inventory = obj.inventory.pk,
            project = obj.project.pk,
            job_type = obj.job_type
        )
        return self.can_add(data)

    def can_add(self, data):
        ''' 
        a user can create a job template if they are a superuser, an org admin of any org
        that the project is a member, or if they have user or team based permissions tying
        the project to the inventory source for the given action.
   
        users who are able to create deploy jobs can also make check (dry run) jobs
        '''

        if self.user.is_superuser:
            return True
        if not data or '_method' in data:  # FIXME: So the browseable API will work?
            return True
        project = Project.objects.get(pk=data['project'])
        inventory = Inventory.objects.get(pk=data['inventory'])

        admin_of_orgs = project.organizations.filter(admins__in = [ self.user ])
        if admin_of_orgs.count() > 0:
            return True
        job_type = data['job_type']

        has_launch_permission = False
        user_permissions = Permission.objects.filter(inventory=inventory, project=project, user=self.user)
        for perm in user_permissions:
            if job_type == PERM_INVENTORY_CHECK:
                # if you have run permissions, you can also create check jobs
                has_launch_permission = True
            elif job_type == PERM_INVENTORY_DEPLOY and perm.permission_type == PERM_INVENTORY_DEPLOY:
                # you need explicit run permissions to make run jobs
                has_launch_permission = True
        team_permissions = Permission.objects.filter(inventory=inventory, project=project, team__users__in = [self.user])
        for perm in team_permissions:
            if job_type == PERM_INVENTORY_CHECK:
                # if you have run permissions, you can also create check jobs
                has_launch_permission = True
            elif job_type == PERM_INVENTORY_DEPLOY and perm.permission_type == PERM_INVENTORY_DEPLOY:
                # you need explicit run permissions to make run jobs
                has_launch_permission = True

        if not has_launch_permission:
            return False

        # make sure user owns the credentials they are using
        if data.has_key('credential'):
            has_credential = False
            credential = Credential.objects.get(pk=data['credential'])
            if credential.team and credential.team.users.filter(id = self.user.pk).count():
               has_credential = True
            if credential.user and credential.user == self.user:
               has_credential = True
            if not has_credential:
               return False

        # shouldn't really matter with permissions given, but make sure the user
        # is also currently on the team in case they were added a per-user permission and then removed
        # from the project.
        if project.teams.filter(users__in = [ self.user ]).count():
            return False

        return True

    def can_change(self, obj, data):
        '''
        '''
        return self.user.is_superuser # FIXME

class JobAccess(BaseAccess):

    model = Job

    def get_queryset(self):
        return self.model.objects.distinct() # FIXME

    def can_change(self, obj, data):
        return self.user.is_superuser and obj.status == 'new'

    def can_start(self, obj):
        return False # FIXME

    def can_cancel(self, obj):
        return False # FIXME

class JobHostSummaryAccess(BaseAccess):

    model = JobHostSummary

    def get_queryset(self):
        return self.model.objects.distinct() # FIXME

class JobEventAccess(BaseAccess):

    model = JobEvent

    def get_queryset(self):
        return self.model.objects.distinct() # FIXME

register_access(User, UserAccess)
register_access(Organization, OrganizationAccess)
register_access(Inventory, InventoryAccess)
register_access(Host, HostAccess)
register_access(Group, GroupAccess)
register_access(Credential, CredentialAccess)
register_access(Team, TeamAccess)
register_access(Project, ProjectAccess)
register_access(Permission, PermissionAccess)
register_access(JobTemplate, JobTemplateAccess)
register_access(Job, JobAccess)
register_access(JobHostSummary, JobHostSummaryAccess)
register_access(JobEvent, JobEventAccess)
