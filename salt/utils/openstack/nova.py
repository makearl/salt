# -*- coding: utf-8 -*-
'''
Nova class
'''
from __future__ import with_statement

# Import third party libs
HAS_NOVA = False
try:
    from novaclient.v1_1 import client
    import novaclient.auth_plugin
    import novaclient.exceptions
    import novaclient.extension
    HAS_NOVA = True
except ImportError:
    pass

# Import python libs
import time
import logging

# Import salt libs
import salt.utils
from salt.cloud.exceptions import SaltCloudSystemExit

# Get logging started
log = logging.getLogger(__name__)


def check_nova():
    return HAS_NOVA


class NovaServer(object):
    def __init__(self, name, server, password=None):
        '''
        Make output look like libcloud output for consistency
        '''
        self.name = name
        self.id = server['id']
        self.image = server['image']['id']
        self.size = server['flavor']['id']
        self.state = server['status']
        self._uuid = None
        self.extra = {
            'metadata': server['metadata'],
            'access_ip': server['accessIPv4']
        }

        if 'addresses' in server and 'public' in server['addresses']:
            self.public_ips = [
                ip['addr'] for ip in server['addresses']['public']
            ]
            self.private_ips = [
                ip['addr'] for ip in server['addresses']['private']
            ]

        if password:
            self.extra['password'] = password

    def __str__(self):
        return self.__dict__


def get_entry(dict_, key, value):
    for entry in dict_:
        if entry[key] == value:
            return entry
    raise SaltCloudSystemExit('Unable to find {0} in {1}.'.format(key, dict_))


def sanatize_novaclient(kwargs):
    variables = (
        'username', 'api_key', 'project_id', 'auth_url', 'insecure',
        'timeout', 'proxy_tenant_id', 'proxy_token', 'region_name',
        'endpoint_type', 'extensions', 'service_type', 'service_name',
        'volume_service_name', 'timings', 'bypass_url', 'os_cache',
        'no_cache', 'http_log_debug', 'auth_system', 'auth_plugin',
        'auth_token', 'cacert', 'tenant_id'
    )
    ret = {}
    for var in kwargs.keys():
        if var in variables:
            ret[var] = kwargs[var]

    return ret


# Function alias to not shadow built-ins
class SaltNova(object):
    '''
    Class for all novaclient functions
    '''
    def __init__(
        self,
        username,
        project_id,
        auth_url,
        region_name=None,
        password=None,
        os_auth_plugin=None,
        **kwargs
    ):
        '''
        Set up nova credentials
        '''
        if not HAS_NOVA:
            return None

        self.kwargs = kwargs.copy()
        self.kwargs['username'] = username
        self.kwargs['project_id'] = project_id
        self.kwargs['auth_url'] = auth_url
        self.kwargs['region_name'] = region_name
        self.kwargs['service_type'] = 'compute'
        if not os_auth_plugin is None:
            novaclient.auth_plugin.discover_auth_systems()
            auth_plugin = novaclient.auth_plugin.load_plugin(os_auth_plugin)
            self.kwargs['auth_plugin'] = auth_plugin
            self.kwargs['auth_system'] = os_auth_plugin

        if not 'api_key' in self.kwargs.keys():
            self.kwargs['api_key'] = password
        extensions = []
        if 'extensions' in kwargs:
            exts = []
            for key, item in self.kwargs['extensions'].items():
                mod = __import__(item.replace('-', '_'))
                exts.append(
                    novaclient.extension.Extension(key, mod)
                )
            self.kwargs['extensions'] = exts

        self.kwargs = sanatize_novaclient(self.kwargs)

        with client.Client(**self.kwargs) as conn:
            try:
                conn.client.authenticate()
            except novaclient.exceptions.AmbiguousEndpoints:
                raise SaltCloudSystemExit(
                    "Nova provider requires a 'region_name' to be specified"
                )

            self.kwargs['auth_token'] = conn.client.auth_token
            self.catalog = \
                conn.client.service_catalog.catalog['access']['serviceCatalog']

        if not region_name is None:
            servers_endpoints = get_entry(self.catalog, 'type', 'compute')['endpoints']
            self.kwargs['bypass_url'] = get_entry(
                servers_endpoints,
                'region',
                region_name.upper()
            )['publicURL']

        self.compute_conn = client.Client(**self.kwargs)

        if not region_name is None:
            servers_endpoints = get_entry(
                self.catalog,
                'type',
                'volume'
            )['endpoints']
            self.kwargs['bypass_url'] = get_entry(
                servers_endpoints,
                'region',
                region_name.upper()
            )['publicURL']

        self.kwargs['service_type'] = 'volume'
        self.volume_conn = client.Client(**self.kwargs)

    def get_catalog(self):
        '''
        Return service catalog
        '''
        return self.catalog

    def server_show_libcloud(self, uuid):
        '''
        Make output look like libcloud output for consistency
        '''
        server_info = self.server_show(uuid)
        server = server_info.values()[0]
        server_name = server_info.keys()[0]
        if not hasattr(self, 'password'):
            self.password = None
        ret = NovaServer(server_name, server, self.password)

        return ret

    def boot(self, name, flavor_id=0, image_id=0, timeout=300, **kwargs):
        '''
        Boot a cloud server.
        '''
        nt_ks = self.compute_conn
        response = nt_ks.servers.create(
            name=name, flavor=flavor_id, image=image_id, **kwargs
        )
        self.uuid = response.id
        self.password = response.adminPass

        start = time.time()
        trycount = 0
        while True:
            trycount += 1
            try:
                return self.server_show_libcloud(self.uuid)
            except Exception as exc:
                log.debug(
                    'Server information not yet available: {0}'.format(exc)
                )
                time.sleep(1)
                if time.time() - start > timeout:
                    log.error('Timed out after {0} seconds '
                              'while waiting for data'.format(timeout))
                    return False

                log.debug(
                    'Retrying server_show() (try {0})'.format(trycount)
                )

    def show_instance(self, name):
        '''
        Find a server by its name (libcloud)
        '''
        return self.server_list().get(name, {})

    def server_by_name(self, name):
        '''
        Find a server by its name
        '''
        return self.server_show_libcloud(
            self.server_list().get(name, {}).get('id', '')
        )

    def _volume_get(self, volume_id):
        '''
        Organize information about a volume from the volume_id
        '''
        nt_ks = self.volume_conn
        volume = nt_ks.volumes.get(volume_id)
        response = {'name': volume.display_name,
                    'size': volume.size,
                    'id': volume.id,
                    'description': volume.display_description,
                    'attachments': volume.attachments,
                    'status': volume.status
                    }
        return response

    def volume_list(self, search_opts=None):
        '''
        List all block volumes
        '''
        nt_ks = self.volume_conn
        volumes = nt_ks.volumes.list(search_opts=search_opts)
        response = {}
        for volume in volumes:
            response[volume.display_name] = {
                'name': volume.display_name,
                'size': volume.size,
                'id': volume.id,
                'description': volume.display_description,
                'attachments': volume.attachments,
                'status': volume.status
            }
        return response

    def volume_show(self, name):
        '''
        Show one volume
        '''
        nt_ks = self.volume_conn
        volumes = self.volume_list(
            search_opts={'display_name': name},
        )
        volume = volumes[name]
#        except Exception as esc:
#            # volume doesn't exist
#            log.error(esc.strerror)
#            return {'name': name, 'status': 'deleted'}

        return volume

    def volume_create(self, name, size=100, snapshot=None, voltype=None):
        '''
        Create a block device
        '''
        nt_ks = self.volume_conn
        response = nt_ks.volumes.create(
            size=size,
            display_name=name,
            volume_type=voltype,
            snapshot_id=snapshot
        )

        return self._volume_get(response.id)

    def volume_delete(self, name):
        '''
        Delete a block device
        '''
        nt_ks = self.volume_conn
        try:
            volume = self.volume_show(name)
        except KeyError as exc:
            raise SaltCloudSystemExit('Unable to find {0} volume: {1}'.format(name, exc))
        if volume['status'] == 'deleted':
            return volume
        response = nt_ks.volumes.delete(volume['id'])
        return volume

    def volume_detach(self,
                      name,
                      timeout=300):
        '''
        Detach a block device
        '''
        try:
            volume = self.volume_show(name)
        except KeyError as exc:
            raise SaltCloudSystemExit('Unable to find {0} volume: {1}'.format(name, exc))
        if not volume['attachments']:
            return True
        response = self.compute_conn.volumes.delete_server_volume(
            volume['attachments'][0]['server_id'],
            volume['attachments'][0]['id']
        )
        trycount = 0
        start = time.time()
        while True:
            trycount += 1
            try:
                response = self._volume_get(volume['id'])
                if response['status'] == 'available':
                    return response
            except Exception as exc:
                log.debug('Volume is detaching: {0}'.format(name))
                time.sleep(1)
                if time.time() - start > timeout:
                    log.error('Timed out after {0} seconds '
                              'while waiting for data'.format(timeout))
                    return False

                log.debug(
                    'Retrying volume_show() (try {0})'.format(trycount)
                )

    def volume_attach(self,
                      name,
                      server_name,
                      device='/dev/xvdb',
                      timeout=300):
        '''
        Attach a block device
        '''
        try:
            volume = self.volume_show(name)
        except KeyError as exc:
            raise SaltCloudSystemExit('Unable to find {0} volume: {1}'.format(name, exc))
        server = self.server_by_name(server_name)
        response = self.compute_conn.volumes.create_server_volume(
            server.id,
            volume['id'],
            device=device
        )
        trycount = 0
        start = time.time()
        while True:
            trycount += 1
            try:
                response = self._volume_get(volume['id'])
                if response['status'] == 'in-use':
                    return response
            except Exception as exc:
                log.debug('Volume is attaching: {0}'.format(name))
                time.sleep(1)
                if time.time() - start > timeout:
                    log.error('Timed out after {0} seconds '
                              'while waiting for data'.format(timeout))
                    return False

                log.debug(
                    'Retrying volume_show() (try {0})'.format(trycount)
                )

    def suspend(self, instance_id):
        '''
        Suspend a server
        '''
        nt_ks = self.compute_conn
        response = nt_ks.servers.suspend(instance_id)
        return True

    def resume(self, instance_id):
        '''
        Resume a server
        '''
        nt_ks = self.compute_conn
        response = nt_ks.servers.resume(instance_id)
        return True

    def lock(self, instance_id):
        '''
        Lock an instance
        '''
        nt_ks = self.compute_conn
        response = nt_ks.servers.lock(instance_id)
        return True

    def delete(self, instance_id):
        '''
        Delete a server
        '''
        nt_ks = self.compute_conn
        response = nt_ks.servers.delete(instance_id)
        return True

    def flavor_list(self):
        '''
        Return a list of available flavors (nova flavor-list)
        '''
        nt_ks = self.compute_conn
        ret = {}
        for flavor in nt_ks.flavors.list():
            links = {}
            for link in flavor.links:
                links[link['rel']] = link['href']
            ret[flavor.name] = {
                'disk': flavor.disk,
                'id': flavor.id,
                'name': flavor.name,
                'ram': flavor.ram,
                'swap': flavor.swap,
                'vcpus': flavor.vcpus,
                'links': links,
            }
            if hasattr(flavor, 'rxtx_factor'):
                ret[flavor.name]['rxtx_factor'] = flavor.rxtx_factor
        return ret

    list_sizes = flavor_list

    def flavor_create(self,
                      name,             # pylint: disable=C0103
                      flavor_id=0,      # pylint: disable=C0103
                      ram=0,
                      disk=0,
                      vcpus=1):
        '''
        Create a flavor
        '''
        nt_ks = self.compute_conn
        nt_ks.flavors.create(
            name=name, flavorid=flavor_id, ram=ram, disk=disk, vcpus=vcpus
        )
        return {'name': name,
                'id': flavor_id,
                'ram': ram,
                'disk': disk,
                'vcpus': vcpus}

    def flavor_delete(self, flavor_id):  # pylint: disable=C0103
        '''
        Delete a flavor
        '''
        nt_ks = self.compute_conn
        nt_ks.flavors.delete(flavor_id)
        return 'Flavor deleted: {0}'.format(flavor_id)

    def keypair_list(self):
        '''
        List keypairs
        '''
        nt_ks = self.compute_conn
        ret = {}
        for keypair in nt_ks.keypairs.list():
            ret[keypair.name] = {
                'name': keypair.name,
                'fingerprint': keypair.fingerprint,
                'public_key': keypair.public_key,
            }
        return ret

    def keypair_add(self, name, pubfile=None, pubkey=None):
        '''
        Add a keypair
        '''
        nt_ks = self.compute_conn
        if pubfile:
            ifile = salt.utils.fopen(pubfile, 'r')
            pubkey = ifile.read()
        if not pubkey:
            return False
        nt_ks.keypairs.create(name, public_key=pubkey)
        ret = {'name': name, 'pubkey': pubkey}
        return ret

    def keypair_delete(self, name):
        '''
        Delete a keypair
        '''
        nt_ks = self.compute_conn
        nt_ks.keypairs.delete(name)
        return 'Keypair deleted: {0}'.format(name)

    def image_show(self, image_id):
        '''
        Show image details and metadata
        '''
        nt_ks = self.compute_conn
        image = nt_ks.images.get(image_id)
        links = {}
        for link in image.links:
            links[link['rel']] = link['href']
        ret = {
            'name': image.name,
            'id': image.id,
            'status': image.status,
            'progress': image.progress,
            'created': image.created,
            'updated': image.updated,
            'metadata': image.metadata,
            'links': links,
        }
        if hasattr(image, 'minDisk'):
            ret['minDisk'] = image.minDisk
        if hasattr(image, 'minRam'):
            ret['minRam'] = image.minRam

        return ret

    def image_list(self, name=None):
        '''
        List server images
        '''
        nt_ks = self.compute_conn
        ret = {}
        for image in nt_ks.images.list():
            links = {}
            for link in image.links:
                links[link['rel']] = link['href']
            ret[image.name] = {
                'name': image.name,
                'id': image.id,
                'status': image.status,
                'progress': image.progress,
                'created': image.created,
                'updated': image.updated,
                'metadata': image.metadata,
                'links': links,
            }
            if hasattr(image, 'minDisk'):
                ret[image.name]['minDisk'] = image.minDisk
            if hasattr(image, 'minRam'):
                ret[image.name]['minRam'] = image.minRam
        if name:
            return {name: ret[name]}
        return ret

    list_images = image_list

    def image_meta_set(self,
                       image_id=None,
                       name=None,
                       **kwargs):  # pylint: disable=C0103
        '''
        Set image metadata
        '''
        nt_ks = self.compute_conn
        if name:
            for image in nt_ks.images.list():
                if image.name == name:
                    image_id = image.id  # pylint: disable=C0103
        if not image_id:
            return {'Error': 'A valid image name or id was not specified'}
        nt_ks.images.set_meta(image_id, kwargs)
        return {image_id: kwargs}

    def image_meta_delete(self,
                          image_id=None,     # pylint: disable=C0103
                          name=None,
                          keys=None):
        '''
        Delete image metadata
        '''
        nt_ks = self.compute_conn
        if name:
            for image in nt_ks.images.list():
                if image.name == name:
                    image_id = image.id  # pylint: disable=C0103
        pairs = keys.split(',')
        if not image_id:
            return {'Error': 'A valid image name or id was not specified'}
        nt_ks.images.delete_meta(image_id, pairs)
        return {image_id: 'Deleted: {0}'.format(pairs)}

    def server_list(self):
        '''
        List servers
        '''
        nt_ks = self.compute_conn
        ret = {}
        for item in nt_ks.servers.list():
            ret[item.name] = {
                'id': item.id,
                'name': item.name,
                'status': item.status,
                'accessIPv4': item.accessIPv4,
                'accessIPv6': item.accessIPv6,
                'flavor': {'id': item.flavor['id'],
                           'links': item.flavor['links']},
                'image': {'id': item.image['id'],
                          'links': item.image['links']},
                }
        return ret

    def server_list_detailed(self):
        '''
        Detailed list of servers
        '''
        nt_ks = self.compute_conn
        ret = {}
        for item in nt_ks.servers.list():
            ret[item.name] = {
                'OS-EXT-SRV-ATTR': {},
                'OS-EXT-STS': {},
                'accessIPv4': item.accessIPv4,
                'accessIPv6': item.accessIPv6,
                'addresses': item.addresses,
                'created': item.created,
                'flavor': {'id': item.flavor['id'],
                           'links': item.flavor['links']},
                'hostId': item.hostId,
                'id': item.id,
                'image': {'id': item.image['id'],
                          'links': item.image['links']},
                'key_name': item.key_name,
                'links': item.links,
                'metadata': item.metadata,
                'name': item.name,
                'progress': item.progress,
                'status': item.status,
                'tenant_id': item.tenant_id,
                'updated': item.updated,
                'user_id': item.user_id,
            }

            if hasattr(item.__dict__, 'OS-DCF:diskConfig'):
                ret[item.name]['OS-DCF'] = {
                    'diskConfig': item.__dict__['OS-DCF:diskConfig']
                }
            if hasattr(item.__dict__, 'OS-EXT-SRV-ATTR:host'):
                ret[item.name]['OS-EXT-SRV-ATTR']['host'] = \
                    item.__dict__['OS-EXT-SRV-ATTR:host']
            if hasattr(item.__dict__, 'OS-EXT-SRV-ATTR:hypervisor_hostname'):
                ret[item.name]['OS-EXT-SRV-ATTR']['hypervisor_hostname'] = \
                    item.__dict__['OS-EXT-SRV-ATTR:hypervisor_hostname']
            if hasattr(item.__dict__, 'OS-EXT-SRV-ATTR:instance_name'):
                ret[item.name]['OS-EXT-SRV-ATTR']['instance_name'] = \
                    item.__dict__['OS-EXT-SRV-ATTR:instance_name']
            if hasattr(item.__dict__, 'OS-EXT-STS:power_state'):
                ret[item.name]['OS-EXT-STS']['power_state'] = \
                    item.__dict__['OS-EXT-STS:power_state']
            if hasattr(item.__dict__, 'OS-EXT-STS:task_state'):
                ret[item.name]['OS-EXT-STS']['task_state'] = \
                    item.__dict__['OS-EXT-STS:task_state']
            if hasattr(item.__dict__, 'OS-EXT-STS:vm_state'):
                ret[item.name]['OS-EXT-STS']['vm_state'] = \
                    item.__dict__['OS-EXT-STS:vm_state']
            if hasattr(item.__dict__, 'security_groups'):
                ret[item.name]['security_groups'] = \
                    item.__dict__['security_groups']
        return ret

    def server_show(self, server_id):
        '''
        Show details of one server
        '''
        ret = {}
        try:
            servers = self.server_list_detailed()
        except AttributeError as exc:
            raise SaltCloudSystemExit('Corrupt server in server_list_detailed. Remove corrupt servers.')
        for server_name, server in servers.iteritems():
            if str(server['id']) == server_id:
                ret[server_name] = server
        return ret

    def secgroup_create(self, name, description):
        '''
        Create a security group
        '''
        nt_ks = self.compute_conn
        nt_ks.security_groups.create(name, description)
        ret = {'name': name, 'description': description}
        return ret

    def secgroup_delete(self, name):
        '''
        Delete a security group
        '''
        nt_ks = self.compute_conn
        for item in nt_ks.security_groups.list():
            if item.name == name:
                nt_ks.security_groups.delete(item.id)
                return {name: 'Deleted security group: {0}'.format(name)}
        return 'Security group not found: {0}'.format(name)

    def secgroup_list(self):
        '''
        List security groups
        '''
        nt_ks = self.compute_conn
        ret = {}
        for item in nt_ks.security_groups.list():
            ret[item.name] = {
                'name': item.name,
                'description': item.description,
                'id': item.id,
                'tenant_id': item.tenant_id,
                'rules': item.rules,
            }
        return ret

    def _item_list(self):
        '''
        List items
        '''
        nt_ks = self.compute_conn
        ret = []
        for item in nt_ks.items.list():
            ret.append(item.__dict__)
        return ret

    def _network_show(self, name, network_lst):
        '''
        Parse the returned network list
        '''
        for net in network_lst:
            if net.label == name:
                return net.__dict__
        return False

    def network_show(self, name):
        '''
        Show network information
        '''
        nt_ks = self.compute_conn
        net_list = nt_ks.networks.list()
        return self._network_show(name, net_list)

    def network_list(self):
        '''
        List extra private networks
        '''
        nt_ks = self.compute_conn
        return [network.__dict__ for network in nt_ks.networks.list()]

    def _sanatize_network_params(self, kwargs):
        '''
        Sanatize novaclient network parameters
        '''
        params = [
            'label', 'bridge', 'bridge_interface', 'cidr', 'cidr_v6', 'dns1',
            'dns2', 'fixed_cidr', 'gateway', 'gateway_v6', 'multi_host',
            'priority', 'project_id', 'vlan_start', 'vpn_start'
        ]

        for variable in kwargs.keys():
            if variable not in params:
                del kwargs[variable]
        return kwargs

    def network_create(self, name, **kwargs):
        '''
        Create extra private network
        '''
        nt_ks = self.compute_conn
        kwargs['label'] = name
        kwargs = self._sanatize_network_params(kwargs)
        net = nt_ks.networks.create(**kwargs)
        return net.__dict__

    def _server_uuid_from_name(self, name):
        '''
        Get server uuid from name
        '''
        return self.server_list().get(name, {}).get('id', '')

    def virtual_interface_list(self, name):
        '''
        Get virtual interfaces on slice
        '''
        nt_ks = self.compute_conn
        nets = nt_ks.virtual_interfaces.list(self._server_uuid_from_name(name))
        return [network.__dict__ for network in nets]

    def virtual_interface_create(self, name, net_name):
        '''
        Add an interfaces to a slice
        '''
        nt_ks = self.compute_conn
        serverid = self._server_uuid_from_name(name)
        networkid = self.network_show(net_name).get('id', '')
        nets = nt_ks.virtual_interfaces.create(networkid, serverid)
        return nets

#The following is a list of functions that need to be incorporated in the
#nova module. This list should be updated as functions are added.
#
#absolute-limits     Print a list of absolute limits for a user
#actions             Retrieve server actions.
#add-fixed-ip        Add new IP address to network.
#add-floating-ip     Add a floating IP address to a server.
#aggregate-add-host  Add the host to the specified aggregate.
#aggregate-create    Create a new aggregate with the specified details.
#aggregate-delete    Delete the aggregate by its id.
#aggregate-details   Show details of the specified aggregate.
#aggregate-list      Print a list of all aggregates.
#aggregate-remove-host
#                    Remove the specified host from the specified aggregate.
#aggregate-set-metadata
#                    Update the metadata associated with the aggregate.
#aggregate-update    Update the aggregate's name and optionally
#                    availability zone.
#cloudpipe-create    Create a cloudpipe instance for the given project
#cloudpipe-list      Print a list of all cloudpipe instances.
#console-log         Get console log output of a server.
#credentials         Show user credentials returned from auth
#describe-resource   Show details about a resource
#diagnostics         Retrieve server diagnostics.
#dns-create          Create a DNS entry for domain, name and ip.
#dns-create-private-domain
#                    Create the specified DNS domain.
#dns-create-public-domain
#                    Create the specified DNS domain.
#dns-delete          Delete the specified DNS entry.
#dns-delete-domain   Delete the specified DNS domain.
#dns-domains         Print a list of available dns domains.
#dns-list            List current DNS entries for domain and ip or domain
#                    and name.
#endpoints           Discover endpoints that get returned from the
#                    authenticate services
#floating-ip-create  Allocate a floating IP for the current tenant.
#floating-ip-delete  De-allocate a floating IP.
#floating-ip-list    List floating ips for this tenant.
#floating-ip-pool-list
#                    List all floating ip pools.
#get-vnc-console     Get a vnc console to a server.
#host-action         Perform a power action on a host.
#host-update         Update host settings.
#image-create        Create a new image by taking a snapshot of a running
#                    server.
#image-delete        Delete an image.
#live-migration      Migrates a running instance to a new machine.
#meta                Set or Delete metadata on a server.
#migrate             Migrate a server.
#pause               Pause a server.
#rate-limits         Print a list of rate limits for a user
#reboot              Reboot a server.
#rebuild             Shutdown, re-image, and re-boot a server.
#remove-fixed-ip     Remove an IP address from a server.
#remove-floating-ip  Remove a floating IP address from a server.
#rename              Rename a server.
#rescue              Rescue a server.
#resize              Resize a server.
#resize-confirm      Confirm a previous resize.
#resize-revert       Revert a previous resize (and return to the previous
#                    VM).
#root-password       Change the root password for a server.
#secgroup-add-group-rule
#                    Add a source group rule to a security group.
#secgroup-add-rule   Add a rule to a security group.
#secgroup-delete-group-rule
#                    Delete a source group rule from a security group.
#secgroup-delete-rule
#                    Delete a rule from a security group.
#secgroup-list-rules
#                    List rules for a security group.
#ssh                 SSH into a server.
#unlock              Unlock a server.
#unpause             Unpause a server.
#unrescue            Unrescue a server.
#usage-list          List usage data for all tenants
#volume-list         List all the volumes.
#volume-snapshot-create
#                    Add a new snapshot.
#volume-snapshot-delete
#                    Remove a snapshot.
#volume-snapshot-list
#                    List all the snapshots.
#volume-snapshot-show
#                    Show details about a snapshot.
#volume-type-create  Create a new volume type.
#volume-type-delete  Delete a specific flavor
#volume-type-list    Print a list of available 'volume types'.
#x509-create-cert    Create x509 cert for a user in tenant
#x509-get-root-cert  Fetches the x509 root cert.
