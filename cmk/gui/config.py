#!/usr/bin/python
# -*- encoding: utf-8; py-indent-offset: 4 -*-
# +------------------------------------------------------------------+
# |             ____ _               _        __  __ _  __           |
# |            / ___| |__   ___  ___| | __   |  \/  | |/ /           |
# |           | |   | '_ \ / _ \/ __| |/ /   | |\/| | ' /            |
# |           | |___| | | |  __/ (__|   <    | |  | | . \            |
# |            \____|_| |_|\___|\___|_|\_\___|_|  |_|_|\_\           |
# |                                                                  |
# | Copyright Mathias Kettner 2014             mk@mathias-kettner.de |
# +------------------------------------------------------------------+
#
# This file is part of Check_MK.
# The official homepage is at http://mathias-kettner.de/check_mk.
#
# check_mk is free software;  you can redistribute it and/or modify it
# under the  terms of the  GNU General Public License  as published by
# the Free Software Foundation in version 2.  check_mk is  distributed
# in the hope that it will be useful, but WITHOUT ANY WARRANTY;  with-
# out even the implied warranty of  MERCHANTABILITY  or  FITNESS FOR A
# PARTICULAR PURPOSE. See the  GNU General Public License for more de-
# tails. You should have  received  a copy of the  GNU  General Public
# License along with GNU Make; see the file  COPYING.  If  not,  write
# to the Free Software Foundation, Inc., 51 Franklin St,  Fifth Floor,
# Boston, MA 02110-1301 USA.

import sys
import errno
import os
import pprint
import glob
import copy

import cmk.gui.utils as utils
import cmk.gui.i18n
from cmk.gui.i18n import _
from cmk.gui.globals import html
import cmk.gui.log as log
import cmk.paths
import cmk.store as store

from cmk.gui.exceptions import MKConfigError, MKAuthException

import cmk.gui.plugins.config

# This import is added for static analysis tools like pylint to make them
# know about all shipped config options. The default config options are
# later handled with the default_config dict and _load_default_config()
from cmk.gui.plugins.config.base import *

if not cmk.is_raw_edition():
    from cmk.gui.cee.plugins.config.cee import *

if cmk.is_managed_edition():
    from cmk.gui.cme.plugins.config.cme import *

#   .--Declarations--------------------------------------------------------.
#   |       ____            _                 _   _                        |
#   |      |  _ \  ___  ___| | __ _ _ __ __ _| |_(_) ___  _ __  ___        |
#   |      | | | |/ _ \/ __| |/ _` | '__/ _` | __| |/ _ \| '_ \/ __|       |
#   |      | |_| |  __/ (__| | (_| | | | (_| | |_| | (_) | | | \__ \       |
#   |      |____/ \___|\___|_|\__,_|_|  \__,_|\__|_|\___/|_| |_|___/       |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   |  Declarations of global variables and constants                      |
#   '----------------------------------------------------------------------'

sites           = {}
multisite_users = {}
admin_users     = []

# hard coded in various permissions
builtin_role_ids = [ "user", "admin", "guest" ]

# Base directory of dynamic configuration
config_dir = cmk.paths.var_dir + "/web"

# Detect modification in configuration
modification_timestamps = []

# Stores the initial configuration values
default_config = {}

# Global table of available permissions. Plugins may add their own
# permissions by calling declare_permission()
permissions_by_name              = {}
permissions_by_order             = []
permission_sections              = {}
permission_declaration_functions = []

# Constants for BI
ALL_HOSTS = '(.*)'
HOST_STATE = ('__HOST_STATE__',)
HIDDEN = ('__HIDDEN__',)
class FOREACH_HOST(object):
    pass
class FOREACH_CHILD(object):
    pass
class FOREACH_CHILD_WITH(object):
    pass
class FOREACH_PARENT(object):
    pass
class FOREACH_SERVICE(object):
    pass
class REMAINING(object):
    pass
class DISABLED(object):
    pass
class HARD_STATES(object):
    pass
class DT_AGGR_WARN(object):
    pass

# Has to be declared here once since the functions can be assigned in
# bi.py and also in multisite.mk. "Double" declarations are no problem
# here since this is a dict (List objects have problems with duplicate
# definitions).
aggregation_functions = {}


#.
#   .--Functions-----------------------------------------------------------.
#   |             _____                 _   _                              |
#   |            |  ___|   _ _ __   ___| |_(_) ___  _ __  ___              |
#   |            | |_ | | | | '_ \ / __| __| |/ _ \| '_ \/ __|             |
#   |            |  _|| |_| | | | | (__| |_| | (_) | | | \__ \             |
#   |            |_|   \__,_|_| |_|\___|\__|_|\___/|_| |_|___/             |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   |  Helper functions for config parsing, login, etc.                    |
#   '----------------------------------------------------------------------'


def initialize():
    clear_user_login()
    load_config()
    log.set_log_levels(log_levels)
    cmk.gui.i18n.set_user_localizations(user_localizations)


# Read in a multisite.d/*.mk file
def include(filename):
    if not filename.startswith("/"):
        filename = cmk.paths.default_config_dir + "/" + filename

    # Config file is obligatory. An empty example is installed
    # during setup.sh. Better signal an error then simply ignore
    # Absence.
    try:
        lm = os.stat(filename).st_mtime
        execfile(filename, globals(), globals())
        modification_timestamps.append((filename, lm))
    except Exception, e:
        raise MKConfigError(_("Cannot read configuration file %s: %s:") % (filename, e))


# Load multisite.mk and all files in multisite.d/. This will happen
# for *each* HTTP request.
# FIXME: Optimize this to cache the config etc. until either the config files or plugins
# have changed. We could make this being cached for multiple requests just like the
# plugins of other modules. This may save significant time in case of small requests like
# the graph ajax page or similar.
def load_config():
    global modification_timestamps, sites
    modification_timestamps = []

    # Set default values for all user-changable configuration settings
    _initialize_with_default_config()

    # Initialze sites with default site configuration. Need to do it here to
    # override possibly deleted sites
    sites = default_single_site_configuration()

    # First load main file
    include("multisite.mk")

    # Load also recursively all files below multisite.d
    conf_dir = cmk.paths.default_config_dir + "/multisite.d"
    filelist = []
    if os.path.isdir(conf_dir):
        for root, _directories, files in os.walk(conf_dir):
            for filename in files:
                if filename.endswith(".mk"):
                    filelist.append(root + "/" + filename)

    filelist.sort()
    for p in filelist:
        include(p)

    # Prevent problem when user has deleted all sites from his configuration
    # and sites is {}. We assume a default single site configuration in
    # that case.
    if not sites:
        sites = default_single_site_configuration()

    migrate_old_sample_config_tag_groups(wato_host_tags, wato_aux_tags)


def _initialize_with_default_config():
    vars_before_plugins = all_nonfunction_vars(globals())
    load_plugins(True)
    vars_after_plugins = all_nonfunction_vars(globals())
    _load_default_config(vars_before_plugins, vars_after_plugins)

    _apply_default_config()


def _apply_default_config():
    for k, v in default_config.items():
        if isinstance(v, (dict, list)):
            v = copy.deepcopy(v)
        globals()[k] = v


def _load_default_config(vars_before_plugins, vars_after_plugins):
    default_config.clear()
    _load_default_config_from_module_plugins()
    _load_default_config_from_legacy_plugins(vars_before_plugins, vars_after_plugins)


def _load_default_config_from_module_plugins():
    # TODO: Find a better solution for this. Probably refactor declaration of default
    # config option.
    config_plugin_vars = {}
    for module in _config_plugin_modules():
        config_plugin_vars.update(module.__dict__)

    for k, v in config_plugin_vars.items():
        if k[0] == "_":
            continue

        if isinstance(v, (dict, list)):
            v = copy.deepcopy(v)

        default_config[k] = v


def _load_default_config_from_legacy_plugins(vars_before_plugins, vars_after_plugins):
    new_vars = vars_after_plugins.difference(vars_before_plugins)
    default_config.update(dict([ (k, copy.deepcopy(globals()[k])) for k in new_vars ]))


def _config_plugin_modules():
    return [module
            for name, module in sys.modules.items()
            if (name.startswith("cmk.gui.plugins.config.") or
                name.startswith("cmk.gui.cee.plugins.config.") or
                name.startswith("cmk.gui.cme.plugins.config.")) and
               module is not None]


def reporting_available():
    try:
        # Check the existance of one arbitrary config variable from the
        # reporting module
        _dummy = reporting_filename
        return True
    except:
        return False


def combined_graphs_available():
    try:
        _dummy = have_combined_graphs
        return True
    except:
        return False


def hide_language(lang):
    return lang in hide_languages


def all_nonfunction_vars(var_dict):
    return set([ name for name,value in var_dict.items()
                 if name[0] != '_' and type(value) != type(lambda:0) ])


def get_language(default=None):
    if default == None:
        return default_language
    return default


def tag_alias(tag):
    for entry in host_tag_groups():
        tag_id, _title, tags = entry[:3]
        for t in tags:
            if t[0] == tag:
                return t[1]

    for tag_id, alias in aux_tags():
        if tag_id == tag:
            return alias


def tag_group_title(tag):
    for entry in host_tag_groups():
        _tag_id, title, tags = entry[:3]
        for t in tags:
            if t[0] == tag:
                return title

#.
#   .--Permissions---------------------------------------------------------.
#   |        ____                     _         _                          |
#   |       |  _ \ ___ _ __ _ __ ___ (_)___ ___(_) ___  _ __  ___          |
#   |       | |_) / _ \ '__| '_ ` _ \| / __/ __| |/ _ \| '_ \/ __|         |
#   |       |  __/  __/ |  | | | | | | \__ \__ \ | (_) | | | \__ \         |
#   |       |_|   \___|_|  |_| |_| |_|_|___/___/_|\___/|_| |_|___/         |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   | Declarations of permissions and roles                                |
#   '----------------------------------------------------------------------'

def declare_permission(name, title, description, defaults):
    perm = {
        "name"        : name,
        "title"       : title,
        "description" : description,
        "defaults"    : defaults,
    }

    # Detect if this permission has already been declared before
    # The dict value is replaced automatically but the list value
    # to be replaced -> INPLACE!
    # FIXME: permissions_by_order is bad. Remove this and add a "sort"
    # attribute to the permissions_by_name dict. This would be much cleaner.
    replaced = False
    for index, test_perm in enumerate(permissions_by_order):
        if test_perm['name'] == perm['name']:
            permissions_by_order[index] = perm
            replaced = True

    if not replaced:
        permissions_by_order.append(perm)

    permissions_by_name[name] = perm


def declare_permission_section(name, title, prio = 0, do_sort = False):
    # Prio can be a number which is used for sorting. Higher numbers will
    # be listed first, e.g. in the edit dialogs
    permission_sections[name] = (prio, title, do_sort)


# Some module have a non-fixed list of permissions. For example for
# each user defined view there is also a permission. This list is
# not known at the time of the loading of the module - though. For
# that purpose module can register functions. These functions should
# just call declare_permission(). They are being called in the correct
# situations.
def declare_dynamic_permissions(func):
    permission_declaration_functions.append(func)


# This function needs to be called by all code that needs access
# to possible dynamic permissions
def load_dynamic_permissions():
    for func in permission_declaration_functions:
        func()


def permission_exists(pname):
    return pname in permissions_by_name


def get_role_permissions():
    role_permissions = {}
    # Loop all permissions
    # and for each permission loop all roles
    # and check whether it has the permission or not

    roleids = roles.keys()
    for perm in permissions_by_order:
        for role_id in roleids:
            if not role_id in role_permissions:
                role_permissions[role_id] = []

            if _may_with_roles([role_id], perm['name']):
                role_permissions[role_id].append(perm['name'])
    return role_permissions


def _may_with_roles(some_role_ids, pname):
    # If at least one of the given roles has this permission, it's fine
    for role_id in some_role_ids:
        role = roles[role_id]

        he_may = role.get("permissions", {}).get(pname)
        # Handle compatibility with permissions without "general." that
        # users might have saved in their own custom roles.
        if he_may == None and pname.startswith("general."):
            he_may = role.get("permissions", {}).get(pname[8:])

        if he_may == None: # not explicitely listed -> take defaults
            if "basedon" in role:
                base_role_id = role["basedon"]
            else:
                base_role_id = role_id
            if pname not in permissions_by_name:
                return False # Permission unknown. Assume False. Functionality might be missing
            perm = permissions_by_name[pname]
            he_may = base_role_id in perm["defaults"]
        if he_may:
            return True
    return False



#.
#   .--User Login----------------------------------------------------------.
#   |           _   _                 _                _                   |
#   |          | | | |___  ___ _ __  | |    ___   __ _(_)_ __              |
#   |          | | | / __|/ _ \ '__| | |   / _ \ / _` | | '_ \             |
#   |          | |_| \__ \  __/ |    | |__| (_) | (_| | | | | |            |
#   |           \___/|___/\___|_|    |_____\___/ \__, |_|_| |_|            |
#   |                                            |___/                     |
#   +----------------------------------------------------------------------+
#   | Managing the currently logged in user                                |
#   '----------------------------------------------------------------------'
# TODO: Shouldn't this be moved to e.g. login.py or userdb.py?

# This objects intention is currently only to handle the currently logged in user after authentication.
# But maybe this can be used for managing all user objects in future.
# TODO: Cleanup accesses to module global vars and functions
class LoggedInUser(object):
    def __init__(self, user_id):
        self.id = user_id

        self._load_confdir()
        self._load_roles()
        self._load_attributes()
        self._load_permissions()
        self._load_site_config()


    # TODO: Clean up that baserole_* stuff?
    def _load_roles(self):
        # Determine the roles of the user. If the user is listed in
        # users, admin_users or guest_users in multisite.mk then we
        # give him the according roles. If the user has an explicit
        # profile in multisite_users (e.g. due to WATO), we rather
        # use that profile. Remaining (unknown) users get the default_user_role.
        # That can be set to None -> User has no permissions at all.
        self.role_ids = self._gather_roles()

        # Get base roles (admin/user/guest)
        self._load_base_roles()

        # Get best base roles and use as "the" role of the user
        if "admin" in self.baserole_ids:
            self.baserole_id = "admin"
        elif "user" in self.baserole_ids:
            self.baserole_id = "user"
        else:
            self.baserole_id = "guest"


    def _gather_roles(self):
        return roles_of_user(self.id)


    def _load_base_roles(self):
        base_roles = set([])
        for r in self.role_ids:
            if r in builtin_role_ids:
                base_roles.add(r)
            else:
                base_roles.add(roles[r]["basedon"])

        self.baserole_ids = list(base_roles)


    def _load_attributes(self):
        self.attributes = self.load_file("cached_profile", None)
        if self.attributes == None:
            if self.id in multisite_users:
                self.attributes = multisite_users[self.id]
            else:
                self.attributes = {
                    "roles" : self.role_ids,
                }

        self.alias = self.attributes.get("alias", self.id)
        self.email = self.attributes.get("email", self.id)


    def _load_permissions(self):
        # Prepare cache of already computed permissions
        # Make sure, admin can restore permissions in any case!
        if self.id in admin_users:
            self.permissions = {
                "general.use" : True, # use Multisite
                "wato.use"    : True, # enter WATO
                "wato.edit"   : True, # make changes in WATO...
                "wato.users"  : True, # ... with access to user management
            }
        else:
            self.permissions = {}


    def _load_confdir(self):
        self.confdir = config_dir + "/" + self.id.encode("utf-8")
        store.mkdir(self.confdir)


    def _load_site_config(self):
        self.siteconf = self.load_file("siteconfig", {})


    def save_site_config(self):
        self.save_file("siteconfig", self.siteconf)


    def get_attribute(self, key, deflt=None):
        return self.attributes.get(key, deflt)


    def set_attribute(self, key, value):
        self.attributes[key] = value


    def unset_attribute(self, key):
        try:
            del self.attributes[key]
        except KeyError:
            pass


    def language(self, default=None):
        return self.get_attribute("language", get_language(default))


    def contact_groups(self):
        return self.get_attribute("contactgroups", [])


    def load_stars(self):
        return set(self.load_file("favorites", []))


    def save_stars(self, stars):
        self.save_file("favorites", list(stars))


    def is_site_disabled(self, site_id):
        siteconf = self.siteconf.get(site_id, {})
        return siteconf.get("disabled", False)


    def authorized_sites(self, unfiltered_sites=None):
        if unfiltered_sites is None:
            unfiltered_sites = allsites().items()

        authorized_sites = self.get_attribute("authorized_sites")
        if authorized_sites is None:
            return unfiltered_sites

        return [ (site_id, site) for site_id, site in unfiltered_sites
                      if site_id in authorized_sites ]


    def authorized_login_sites(self):
        login_site_ids = get_login_slave_sites()
        login_sites = [ (site_id, site) for site_id, site in allsites().items()
                             if site_id in login_site_ids ]
        return self.authorized_sites(login_sites)


    def may(self, pname):
        if pname in self.permissions:
            return self.permissions[pname]
        he_may = _may_with_roles(user.role_ids, pname)
        self.permissions[pname] = he_may
        return he_may


    def need_permission(self, pname):
        if not self.may(pname):
            perm = permissions_by_name[pname]
            raise MKAuthException(_("We are sorry, but you lack the permission "
                                  "for this operation. If you do not like this "
                                  "then please ask you administrator to provide you with "
                                  "the following permission: '<b>%s</b>'.") % perm["title"])


    def load_file(self, name, deflt, lock=False):
        # In some early error during login phase there are cases where it might
        # happen that a user file is requested but the user is not yet
        # set. We have all information to set it, then do it.
        if not user:
            return deflt # No user known at this point of time

        path = self.confdir + "/" + name + ".mk"
        return store.load_data_from_file(path, deflt, lock)


    def save_file(self, name, content, unlock=False):
        save_user_file(name, content, self.id, unlock)


    def file_modified(self, name):
        if self.confdir == None:
            return 0

        try:
            return os.stat(self.confdir + "/" + name + ".mk").st_mtime
        except OSError, e:
            if e.errno == errno.ENOENT:
                return 0
            else:
                raise



# Login a user that has all permissions. This is needed for making
# Livestatus queries from unauthentiated page handlers
# TODO: Can we somehow get rid of this?
class LoggedInSuperUser(LoggedInUser):
    def __init__(self):
        super(LoggedInSuperUser, self).__init__(None)
        self.alias = "Superuser for unauthenticated pages"
        self.email = "admin"


    def _gather_roles(self):
        return [ "admin" ]


    def _load_confdir(self):
        self.confdir = None


    def _load_site_config(self):
        self.siteconf = {}


    def load_file(self, name, deflt, lock=False):
        return deflt



class LoggedInNobody(LoggedInUser):
    def __init__(self):
        super(LoggedInNobody, self).__init__(None)
        self.alias = "Unauthenticated user"
        self.email = "nobody"


    def _gather_roles(self):
        return []


    def _load_confdir(self):
        self.confdir = None


    def _load_site_config(self):
        self.siteconf = {}


    def load_file(self, name, deflt, lock=False):
        return deflt



def clear_user_login():
    _set_user(LoggedInNobody())


def set_user_by_id(user_id):
    _set_user(LoggedInUser(user_id))


def set_super_user():
    _set_user(LoggedInSuperUser())


def _set_user(_user):
    global user
    user = _user


# This holds the currently logged in user object
user = LoggedInNobody()

#.
#   .--User Handling-------------------------------------------------------.
#   |    _   _                 _   _                 _ _ _                 |
#   |   | | | |___  ___ _ __  | | | | __ _ _ __   __| | (_)_ __   __ _     |
#   |   | | | / __|/ _ \ '__| | |_| |/ _` | '_ \ / _` | | | '_ \ / _` |    |
#   |   | |_| \__ \  __/ |    |  _  | (_| | | | | (_| | | | | | | (_| |    |
#   |    \___/|___/\___|_|    |_| |_|\__,_|_| |_|\__,_|_|_|_| |_|\__, |    |
#   |                                                            |___/     |
#   +----------------------------------------------------------------------+
#   | General user handling of all users, not only the currently logged    |
#   | in user. These functions are mostly working with the loaded multisite|
#   | configuration data (multisite_users, admin_users, ...), so they are  |
#   | more related to this module than to the userdb module.               |
#   '----------------------------------------------------------------------'

def roles_of_user(user_id):
    def existing_role_ids(role_ids):
        return [
            role_id for role_id in role_ids
            if role_id in roles
        ]

    if user_id in multisite_users:
        return existing_role_ids(multisite_users[user_id]["roles"])
    elif user_id in admin_users:
        return [ "admin" ]
    elif user_id in guest_users:
        return [ "guest" ]
    elif users != None and user_id in users:
        return [ "user" ]
    elif os.path.exists(config_dir + "/" + user_id.encode("utf-8") + "/automation.secret"):
        return [ "guest" ] # unknown user with automation account
    elif 'roles' in default_user_profile:
        return existing_role_ids(default_user_profile['roles'])
    elif default_user_role:
        return existing_role_ids([ default_user_role ])
    return []


def alias_of_user(user_id):
    if user_id in multisite_users:
        return multisite_users[user_id].get("alias", user_id)
    return user_id


def user_may(user_id, pname):
    return _may_with_roles(roles_of_user(user_id), pname)


# TODO: Check all calls for arguments (changed optional user to 3rd positional)
def save_user_file(name, data, user, unlock=False):
    path = config_dir + "/" + user.encode("utf-8") + "/" + name + ".mk"
    store.mkdir(os.path.dirname(path))
    store.save_data_to_file(path, data)


#.
#   .--Host tags-----------------------------------------------------------.
#   |              _   _           _     _                                 |
#   |             | | | | ___  ___| |_  | |_ __ _  __ _ ___                |
#   |             | |_| |/ _ \/ __| __| | __/ _` |/ _` / __|               |
#   |             |  _  | (_) \__ \ |_  | || (_| | (_| \__ \               |
#   |             |_| |_|\___/|___/\__|  \__\__,_|\__, |___/               |
#   |                                             |___/                    |
#   +----------------------------------------------------------------------+
#   |  Helper functions for dealing with host tags                         |
#   '----------------------------------------------------------------------'

class BuiltinTags(object):
    def host_tags(self):
        return [
            ("agent", "%s/%s" % (_("Data sources"), _("Check_MK Agent")), [
                    ("cmk-agent",      _("Contact either Check_MK Agent or use datasource program"), ["tcp"]),
                    ("all-agents",     _("Contact Check_MK agent and all enabled datasource programs"), ["tcp"]),
                    ("special-agents", _("Use all enabled datasource programs"), ["tcp"]),
                    ("no-agent",       _("No agent"), []),
                ],
                ["!ping"],
            ),
            ("snmp", "%s/%s" % (_("Data sources"), _("SNMP")), [
                    ("no-snmp",        _("No SNMP"), []),
                    ("snmp-v2",        _("SNMP v2 or v3"), ["snmp"]),
                    ("snmp-v1",        _("SNMP v1"), ["snmp"]),
                ],
                ["!ping"],
            ),
            ("address_family", "%s/%s " % (_("Address"), _("IP Address Family")), [
                    ("ip-v4-only", _("IPv4 only"), ["ip-v4"]),
                    ("ip-v6-only", _("IPv6 only"), ["ip-v6"]),
                    ("ip-v4v6",    _("IPv4/IPv6 dual-stack"), ["ip-v4", "ip-v6"]),
                    ("no-ip",      _("No IP"),     []),
                ]
            ),
        ]


    def aux_tags(self):
        return [
            ("ip-v4", "%s/%s" % (_("Address"), _("IPv4"))),
            ("ip-v6", "%s/%s" % (_("Address"), _("IPv6"))),
            ("snmp",  "%s/%s" % (_("Data sources"), _("Monitor via SNMP"))),
            ("tcp",   "%s/%s" % (_("Data sources"), _("Monitor via Check_MK Agent"))),
            ("ping",  "%s/%s" % (_("Data sources"), _("Only ping this device"))),
        ]


    def get_effective_tag_groups(self, tag_groups):
        """Extend the given tag group definitions with the builtin tag groups
        and return the extended list"""
        tag_groups = tag_groups[:]
        tag_group_ids = set([ tg[0] for tg in tag_groups ])

        for tag_group in self.host_tags():
            if tag_group[0] not in tag_group_ids:
                tag_groups.append(tag_group)

        return tag_groups


    def get_effective_aux_tags(self, aux_tag_list):
        aux_tags = aux_tag_list[:]
        aux_tag_ids = set([ at[0] for at in aux_tag_list ])

        for aux_tag in self.aux_tags():
            if aux_tag[0] not in aux_tag_ids:
                aux_tags.append(aux_tag)

        return aux_tags


# Previous to 1.5 the "Agent type" tag group was created as sample config and was not
# a builtin tag group (which can not be modified by the user). With werk #5535 we changed
# the tag scheme and need to deal with the user config (which might extend the original tag group).
# Use two strategies:
#
# a) Check whether or not the tag group has been modified. If not, simply remove it from the user
#    config and use the builtin tag group in the future.
# b) Extend the tag group in the user configuration with the tag configuration we need for 1.5.
def migrate_old_sample_config_tag_groups(host_tags, aux_tags):
    remove_old_sample_config_tag_groups(host_tags, aux_tags)
    extend_user_modified_tag_groups(host_tags)


def remove_old_sample_config_tag_groups(host_tags, aux_tags):
    legacy_tag_group_default = ('agent', u'Agent type',
        [
            ('cmk-agent', u'Check_MK Agent (Server)', ['tcp']),
            ('snmp-only', u'SNMP (Networking device, Appliance)', ['snmp']),
            ('snmp-v1',   u'Legacy SNMP device (using V1)', ['snmp']),
            ('snmp-tcp',  u'Dual: Check_MK Agent + SNMP', ['snmp', 'tcp']),
            ('ping',      u'No Agent', []),
        ],
    )

    try:
        host_tags.remove(legacy_tag_group_default)

        # Former tag choices (see above) are added as aux tags to allow the user to migrate
        # these tags and the objects that use them
        aux_tags.insert(0, ("snmp-only", "Data sources/Legacy: SNMP (Networking device, Appliance)"))
        aux_tags.insert(0, ("snmp-tcp", "Data sources/Legacy: Dual: Check_MK Agent + SNMP"))
    except ValueError:
        pass # Not there or modified

    legacy_aux_tag_ids = [
        'snmp',
        'tcp',
    ]

    for aux_tag in aux_tags[:]:
        if aux_tag[0] in legacy_aux_tag_ids:
            aux_tags.remove(aux_tag)


def extend_user_modified_tag_groups(host_tags):
    """This method supports migration from <1.5 to 1.5 in case the user has a customized "Agent type" tag group
    See help of migrate_old_sample_config_tag_groups() and werk #5535 and #6446 for further information.

    Disclaimer: The host_tags data structure is a mess which will hopefully be cleaned up during 1.6 development.
    Basically host_tags is a list of configured tag groups. Each tag group is represented by a tuple like this:

    # tag_group_id, tag_group_title, tag_choices
    ('agent', u'Agent type',
        [
            # tag_id, tag_title, aux_tag_ids
            ('cmk-agent', u'Check_MK Agent (Server)', ['tcp']),
            ('snmp-only', u'SNMP (Networking device, Appliance)', ['snmp']),
            ('snmp-v1',   u'Legacy SNMP device (using V1)', ['snmp']),
            ('snmp-tcp',  u'Dual: Check_MK Agent + SNMP', ['snmp', 'tcp']),
            ('ping',      u'No Agent', []),
        ],
    )
    """
    tag_group = None
    for this_tag_group in host_tags:
        if this_tag_group[0] == "agent":
            tag_group = this_tag_group

    if tag_group is None:
        return # Tag group does not exist

    # Mark all existing tag choices as legacy to help the user that this should be cleaned up
    for index, tag_choice in enumerate(tag_group[2][:]):
        if tag_choice[0] in [ "no-agent", "special-agents", "all-agents", "cmk-agent" ]:
            continue # Don't prefix the standard choices

        if tag_choice[1].startswith("Legacy: "):
            continue # Don't prefix already prefixed choices

        tag_choice_list = list(tag_choice)
        tag_choice_list[1] = "Legacy: %s" % tag_choice_list[1]
        tag_group[2][index] = tuple(tag_choice_list)

    tag_choices = [ c[0] for c in tag_group[2] ]

    if "no-agent" not in tag_choices:
        tag_group[2].insert(0, ("no-agent",       _("No agent"), []))

    if "special-agents" not in tag_choices:
        tag_group[2].insert(0, ("special-agents", _("Use all enabled datasource programs"), ["tcp"]))

    if "all-agents" not in tag_choices:
        tag_group[2].insert(0, ("all-agents",     _("Contact Check_MK agent and all enabled datasource programs"), ["tcp"]))

    if "cmk-agent" not in tag_choices:
        tag_group[2].insert(0, ("cmk-agent",      _("Contact either Check_MK Agent or use datasource program"), ["tcp"]))
    else:
        # Change title of cmk-agent tag choice and move to top
        for index, tag_choice in enumerate(tag_group[2]):
            if tag_choice[0] == "cmk-agent":
                tag_choice_list = list(tag_group[2].pop(index))
                tag_choice_list[1] = _("Contact either Check_MK Agent or use datasource program")
                tag_group[2].insert(0, tuple(tag_choice_list))
                break


def host_tag_groups():
    """Returns the effective set of tag groups defined. This includes
    the implicitly declared builtin host tags. This function must be used by
    the GUI code to get the tag group definitions."""
    return BuiltinTags().get_effective_tag_groups(wato_host_tags)


def aux_tags():
    """Returns the effective set of auxiliary tags defined. This includes
    the implicitly declared builtin host tags. This function must be used by
    the GUI code to get the auxiliay tag definitions."""
    return BuiltinTags().get_effective_aux_tags(wato_aux_tags)


#.
#   .--Sites---------------------------------------------------------------.
#   |                        ____  _ _                                     |
#   |                       / ___|(_) |_ ___  ___                          |
#   |                       \___ \| | __/ _ \/ __|                         |
#   |                        ___) | | ||  __/\__ \                         |
#   |                       |____/|_|\__\___||___/                         |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   |  The config module provides some helper functions for sites.         |
#   '----------------------------------------------------------------------'

def omd_site():
    return os.environ["OMD_SITE"]

def url_prefix():
    return "/%s/" % omd_site()

use_siteicons = False

def default_single_site_configuration():
    return {
        omd_site(): {
            'alias'        : _("Local site %s") % omd_site(),
            'disable_wato' : True,
            'disabled'     : False,
            'insecure'     : False,
            'multisiteurl' : '',
            'persist'      : False,
            'replicate_ec' : False,
            'replication'  : '',
            'timeout'      : 10,
            'user_login'   : True,
    }}

sites = {}

def sitenames():
    return sites.keys()

# TODO: Cleanup: Make clear that this function is used by the status GUI (and not WATO)
# and only returns the currently enabled sites. Or should we redeclare the "disabled" state
# to disable the sites at all?
# TODO: Rename this!
# TODO: All site listing functions should return the same data structure, e.g. a list of
#       pairs (site_id, site)
def allsites():
    return dict( [(name, site(name))
                  for name in sitenames()
                  if not site(name).get("disabled", False)
                     and site(name)['socket'] != 'disabled' ] )


def configured_sites():
    return [(site_id, site(site_id)) for site_id in sitenames() ]


def has_wato_slave_sites():
    return bool(wato_slave_sites())


def is_wato_slave_site():
    return _has_distributed_wato_file() and not has_wato_slave_sites()


def _has_distributed_wato_file():
    return os.path.exists(cmk.paths.check_mk_config_dir + "/distributed_wato.mk") \
        and os.stat(cmk.paths.check_mk_config_dir + "/distributed_wato.mk").st_size != 0


def get_login_sites():
    """Returns the WATO slave sites a user may login and the local site"""
    return get_login_slave_sites() + [ omd_site() ]


# TODO: All site listing functions should return the same data structure, e.g. a list of
#       pairs (site_id, site)
def get_login_slave_sites():
    """Returns a list of site ids which are WATO slave sites and users can login"""
    sites = []
    for site_id, site in wato_slave_sites():
        if site.get('user_login', True) and not site_is_local(site_id):
            sites.append(site_id)
    return sites


def wato_slave_sites():
    return [ (site_id, site) for site_id, site in sites.items()
                                                  if site.get("replication") ]

def sorted_sites():
    sitenames = []
    for site_id, site in user.authorized_sites():
        sitenames.append((site_id, site['alias']))
    sitenames = sorted(sitenames, key=lambda k: k[1], cmp = lambda a,b: cmp(a.lower(), b.lower()))

    return sitenames


def site(site_id):
    s = dict(sites.get(site_id, {}))
    # Now make sure that all important keys are available.
    # Add missing entries by supplying default values.
    s.setdefault("alias", site_id)
    s.setdefault("socket", "unix:" + cmk.paths.livestatus_unix_socket)
    s.setdefault("url_prefix", "../") # relative URL from /check_mk/
    if type(s["socket"]) == tuple and s["socket"][0] == "proxy":
        s["cache"] = s["socket"][1].get("cache", True)
        s["socket"] = "unix:" + cmk.paths.livestatus_unix_socket + "proxy/" + site_id
    else:
        s["cache"] = False
    s["id"] = site_id
    return s


def site_is_local(site_name):
    s = sites.get(site_name, {})
    sock = s.get("socket")

    if not sock or sock == "unix:" + cmk.paths.livestatus_unix_socket:
        return True

    if type(s["socket"]) == tuple and s["socket"][0] == "proxy" \
       and s["socket"][1]["socket"] is None:
        return True

    return False


def default_site():
    for site_name, _site in sites.items():
        if site_is_local(site_name):
            return site_name
    return None


def is_single_local_site():
    if len(sites) > 1:
        return False
    elif len(sites) == 0:
        return True

    # Also use Multisite mode if the one and only site is not local
    sitename = sites.keys()[0]
    return site_is_local(sitename)


def site_attribute_default_value():
    def_site = default_site()
    authorized_site_ids = [x[0] for x in user.authorized_sites(unfiltered_sites=configured_sites())]
    if def_site and def_site in authorized_site_ids:
        return def_site


def site_attribute_choices():
    authorized_site_ids = [x[0] for x in user.authorized_sites(unfiltered_sites=configured_sites())]
    return site_choices(filter_func=lambda site_id, site: site_id in authorized_site_ids)


def site_choices(filter_func=None):
    choices = []
    for site_id, site in sites.items():
        if filter_func and not filter_func(site_id, site):
            continue

        title = site_id
        if site.get("alias"):
            title += " - " + site["alias"]

        choices.append((site_id, title))

    return sorted(choices, key=lambda s: s[1])


def get_event_console_site_choices():
    return site_choices(filter_func=lambda site_id, site: site_is_local(site_id) or site.get("replicate_ec"))

#.
#   .--Plugins-------------------------------------------------------------.
#   |                   ____  _             _                              |
#   |                  |  _ \| |_   _  __ _(_)_ __  ___                    |
#   |                  | |_) | | | | |/ _` | | '_ \/ __|                   |
#   |                  |  __/| | |_| | (_| | | | | \__ \                   |
#   |                  |_|   |_|\__,_|\__, |_|_| |_|___/                   |
#   |                                 |___/                                |
#   +----------------------------------------------------------------------+
#   |  Handling of our own plugins. In plugins other software pieces can   |
#   |  declare defaults for configuration variables.                       |
#   '----------------------------------------------------------------------'

def load_plugins(force):
    utils.load_web_plugins("config", globals())

    # Make sure, builtin roles are present, even if not modified and saved with WATO.
    for br in builtin_role_ids:
        roles.setdefault(br, {})


def theme_choices():
    return [
        ("classic", _("Classic")),
        ("facelift", _("Modern")),
    ]


def get_page_heading():
    if "%s" in page_heading:
        return page_heading % (site(omd_site()).get('alias', _("GUI")))
    return page_heading
