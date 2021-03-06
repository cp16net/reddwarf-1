# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010-2012 OpenStack LLC.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http: //www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from novaclient import exceptions as nova_exceptions
from novaclient.v1_1.client import Client
from reddwarf.common.exception import PollTimeOut
from reddwarf.common.utils import poll_until
from reddwarf.openstack.common import log as logging
from reddwarf.tests.fakes.common import authorize
from reddwarf.tests.fakes.common import get_event_spawer

import eventlet
import uuid

LOG = logging.getLogger(__name__)
FAKE_HOSTS = ["fake_host_1", "fake_host_2"]


class FakeFlavor(object):

    def __init__(self, id, disk, name, ram):
        self.id = id
        self.disk = disk
        self.name = name
        self.ram = ram
        self.vcpus = 10

    @property
    def links(self):
        url = ("http://localhost:8774/v2/5064d71eb09c47e1956cf579822bae9a/"
               "flavors/%s") % self.id
        return [{"href": url, "rel": link_type}
                for link_type in ['self', 'bookmark']]

    @property
    def href_suffix(self):
        return "flavors/%s" % self.id

    def to_dict(self):
        return {"id": self.id, "links": self.links}


class FakeFlavors(object):

    def __init__(self):
        self.db = {}
        self._add(1, 0, "m1.tiny", 512)
        self._add(2, 10, "m1.small", 2048)
        self._add(3, 10, "m1.medium", 4096)
        self._add(4, 10, "m1.large", 8192)
        self._add(5, 10, "m1.xlarge", 16384)
        self._add(6, 0, "tinier", 506)
        self._add(7, 0, "m1.rd-tiny", 512)
        self._add(8, 0, "m1.rd-smaller", 768)

    def _add(self, *args, **kwargs):
        new_flavor = FakeFlavor(*args, **kwargs)
        self.db[new_flavor.id] = new_flavor

    def get(self, id):
        id = int(id)
        if id not in self.db:
            raise nova_exceptions.NotFound(404, "Flavor id not found %s" % id)
        return self.db[id]

    def get_by_href(self, href):
        for id in self.db:
            value = self.db[id]
            # Use inexact match since faking the exact endpoints would be
            # difficult.
            if href.endswith(value.href_suffix):
                return value
        raise nova_exceptions.NotFound(404, "Flavor href not found %s" % href)

    def list(self):
        return [self.get(id) for id in self.db]


class FakeServer(object):

    next_local_id = 0

    def __init__(self, parent, owner, id, name, image_id, flavor_ref,
                 block_device_mapping, volumes):
        self.owner = owner  # This is a context.
        self.id = id
        self.parent = parent
        self.name = name
        self.image_id = image_id
        self.flavor_ref = flavor_ref
        self.old_flavor_ref = None
        self.event_spawn = get_event_spawer()
        self._current_status = "BUILD"
        self.volumes = volumes
        # This is used by "RdServers". Its easier to compute the
        # fake value in this class's initializer.
        self._local_id = self.next_local_id
        self.next_local_id += 1
        info_vols = []
        for volume in self.volumes:
            info_vols.append({'id': volume.id})
            volume.set_attachment(id)
        self.host = FAKE_HOSTS[0]
        self.old_host = None

        self._info = {'os:volumes': info_vols}

    @property
    def addresses(self):
        return {"private": [{"addr": "123.123.123.123"}]}

    def confirm_resize(self):
        if self.status != "VERIFY_RESIZE":
            raise RuntimeError("Not in resize confirm mode.")
        self._current_status = "ACTIVE"

    def revert_resize(self):
        if self.status != "VERIFY_RESIZE":
            raise RuntimeError("Not in resize confirm mode.")
        self.host = self.old_host
        self.old_host = None
        self.flavor_ref = self.old_flavor_ref
        self.old_flavor_ref = None
        self._current_status = "ACTIVE"

    def reboot(self):
        LOG.debug("Rebooting server %s" % (self.id))

        def set_to_active():
            self._current_status = "ACTIVE"
            self.parent.schedule_simulate_running_server(self.id, 1.5)

        self._current_status = "REBOOT"
        self.event_spawn(1, set_to_active)

    def delete(self):
        self.schedule_status = []
        # TODO(pdmars): This is less than ideal, but a quick way to force it
        # into the error state before scheduling the delete.
        if (self.name.endswith("_DELETE_ERROR") and
                self._current_status != "SHUTDOWN"):
            # Fail to delete properly the first time, just set the status
            # to SHUTDOWN and break. It's important that we only fail to delete
            # once in fake mode.
            self._current_status = "SHUTDOWN"
            return
        self._current_status = "SHUTDOWN"
        self.parent.schedule_delete(self.id, 1.5)

    @property
    def flavor(self):
        return FLAVORS.get_by_href(self.flavor_ref).to_dict()

    @property
    def links(self):
        url = "https://localhost:9999/v1.0/1234/instances/%s" % self.id
        return [{"href": url, "rel": link_type}
                for link_type in ['self', 'bookmark']]

    def migrate(self):
        self.resize(None)

    def resize(self, new_flavor_id=None):
        self._current_status = "RESIZE"
        if self.name.endswith("_RESIZE_TIMEOUT"):
            raise PollTimeOut()

        def set_to_confirm_mode():
            self._current_status = "VERIFY_RESIZE"

            def set_to_active():
                self.parent.schedule_simulate_running_server(self.id, 1.5)
            self.event_spawn(1, set_to_active)

        def change_host():
            self.old_host = self.host
            self.host = [host for host in FAKE_HOSTS if host != self.host][0]

        def set_flavor():
            if self.name.endswith("_RESIZE_ERROR"):
                self._current_status = "ACTIVE"
                return
            if new_flavor_id is None:
                # Migrations are flavorless flavor resizes.
                # A resize MIGHT change the host, but a migrate
                # deliberately does.
                LOG.debug("Migrating fake instance.")
                self.event_spawn(0.75, change_host)
            else:
                LOG.debug("Resizing fake instance.")
                self.old_flavor_ref = self.flavor_ref
                flavor = self.parent.flavors.get(new_flavor_id)
                self.flavor_ref = flavor.links[0]['href']
            self.event_spawn(1, set_to_confirm_mode)

        self.event_spawn(0.8, set_flavor)

    def schedule_status(self, new_status, time_from_now):
        """Makes a new status take effect at the given time."""
        def set_status():
            self._current_status = new_status
        self.event_spawn(time_from_now, set_status)

    @property
    def status(self):
        return self._current_status

    @property
    def created(self):
        return "2012-01-25T21:55:51Z"

    @property
    def updated(self):
        return "2012-01-25T21:55:51Z"

    @property
    def tenant(self):   # This is on the RdServer extension type.
        return self.owner.tenant

    @property
    def tenant_id(self):
        return self.owner.tenant


# The global var contains the servers dictionary in use for the life of these
# tests.
FAKE_SERVERS_DB = {}


class FakeServers(object):

    def __init__(self, context, flavors):
        self.context = context
        self.db = FAKE_SERVERS_DB
        self.flavors = flavors
        self.event_spawn = get_event_spawer()

    def can_see(self, id):
        """Can this FakeServers, with its context, see some resource?"""
        server = self.db[id]
        return (self.context.is_admin or
                server.owner.tenant == self.context.tenant)

    def create(self, name, image_id, flavor_ref, files=None,
               block_device_mapping=None, volume=None):
        id = "FAKE_%s" % uuid.uuid4()
        if volume:
            volume = self.volumes.create(volume['size'], volume['name'],
                                         volume['description'])
            while volume.status == "BUILD":
                eventlet.sleep(0.1)
            if volume.status != "available":
                LOG.info("volume status = %s" % volume.status)
                raise nova_exceptions.ClientException("Volume was bad!")
            mapping = "%s::%s:%s" % (volume.id, volume.size, 1)
            block_device_mapping = {'vdb': mapping}
            volumes = [volume]
        else:
            volumes = self._get_volumes_from_bdm(block_device_mapping)
        server = FakeServer(self, self.context, id, name, image_id, flavor_ref,
                            block_device_mapping, volumes)
        self.db[id] = server
        if name.endswith('SERVER_ERROR'):
            raise nova_exceptions.ClientException("Fake server create error.")
        server.schedule_status("ACTIVE", 1)
        LOG.info("FAKE_SERVERS_DB : %s" % str(FAKE_SERVERS_DB))
        return server

    def _get_volumes_from_bdm(self, block_device_mapping):
        volumes = []
        if block_device_mapping is not None:
            # block_device_mapping is a dictionary, where the key is the
            # device name on the compute instance and the mapping info is a
            # set of fields in a string, seperated by colons.
            # For each device, find the volume, and record the mapping info
            # to another fake object and attach it to the volume
            # so that the fake API can later retrieve this.
            for device in block_device_mapping:
                mapping = block_device_mapping[device]
                (id, _type, size, delete_on_terminate) = mapping.split(":")
                volume = self.volumes.get(id)
                volume.mapping = FakeBlockDeviceMappingInfo(
                    id, device, _type, size, delete_on_terminate)
                volumes.append(volume)
        return volumes

    def get(self, id):
        if id not in self.db:
            LOG.error("Couldn't find server id %s, collection=%s" % (id,
                                                                     self.db))
            raise nova_exceptions.NotFound(404, "Not found")
        else:
            if self.can_see(id):
                return self.db[id]
            else:
                raise nova_exceptions.NotFound(404, "Bad permissions")

    def get_server_volumes(self, server_id):
        """Fake method we've added to grab servers from the volume."""
        return [volume.mapping
                for volume in self.get(server_id).volumes
                if volume.mapping is not None]

    def list(self):
        return [v for (k, v) in self.db.items() if self.can_see(v.id)]

    def schedule_delete(self, id, time_from_now):
        def delete_server():
            LOG.info("Simulated event ended, deleting server %s." % id)
            del self.db[id]
        self.event_spawn(time_from_now, delete_server)

    def schedule_simulate_running_server(self, id, time_from_now):
        from reddwarf.instance.models import DBInstance
        from reddwarf.instance.models import InstanceServiceStatus
        from reddwarf.instance.models import ServiceStatuses

        def set_server_running():
            instance = DBInstance.find_by(compute_instance_id=id)
            LOG.debug("Setting server %s to running" % instance.id)
            status = InstanceServiceStatus.find_by(instance_id=instance.id)
            status.status = ServiceStatuses.RUNNING
            status.save()
        self.event_spawn(time_from_now, set_server_running)


class FakeRdServer(object):

    def __init__(self, server):
        self.server = server
        self.deleted = False
        self.deleted_at = None  # Not sure how to simulate "True" for this.
        self.local_id = server._local_id

    def __getattr__(self, name):
        return getattr(self.server, name)


class FakeRdServers(object):

    def __init__(self, servers):
        self.servers = servers

    def get(self, id):
        return FakeRdServer(self.servers.get(id))

    def list(self):
        # Attach the extra Rd Server stuff to the normal server.
        return [FakeRdServer(server) for server in self.servers.list()]


class FakeServerVolumes(object):

    def __init__(self, context):
        self.context = context

    def get_server_volumes(self, server_id):
        class ServerVolumes(object):
            def __init__(self, block_device_mapping):
                LOG.debug("block_device_mapping = %s" % block_device_mapping)
                device = block_device_mapping['vdb']
                (self.volumeId,
                    self.type,
                    self.size,
                    self.delete_on_terminate) = device.split(":")
        fake_servers = FakeServers(self.context, FLAVORS)
        server = fake_servers.get(server_id)
        return [ServerVolumes(server.block_device_mapping)]


class FakeVolume(object):

    def __init__(self, parent, owner, id, size, display_name,
                 display_description):
        self.attachments = []
        self.parent = parent
        self.owner = owner  # This is a context.
        self.id = id
        self.size = size
        self.display_name = display_name
        self.display_description = display_description
        self.event_spawn = get_event_spawer()
        self._current_status = "BUILD"
        # For some reason we grab this thing from device then call it mount
        # point.
        self.device = "vdb"

    def __repr__(self):
        msg = ("FakeVolume(id=%s, size=%s, display_name=%s, "
               "display_description=%s, _current_status=%s)")
        params = (self.id, self.size, self.display_name,
                  self.display_description, self._current_status)
        return (msg % params)

    @property
    def availability_zone(self):
        return "fake-availability-zone"

    @property
    def created_at(self):
        return "2001-01-01-12:30:30"

    def get(self, key):
        return getattr(self, key)

    def schedule_status(self, new_status, time_from_now):
        """Makes a new status take effect at the given time."""
        def set_status():
            self._current_status = new_status
        self.event_spawn(time_from_now, set_status)

    def set_attachment(self, server_id):
        """Fake method we've added to set attachments. Idempotent."""
        for attachment in self.attachments:
            if attachment['server_id'] == server_id:
                return  # Do nothing
        self.attachments.append({'server_id': server_id,
                                 'device': self.device})

    @property
    def status(self):
        return self._current_status


class FakeBlockDeviceMappingInfo(object):

    def __init__(self, id, device, _type, size, delete_on_terminate):
        self.volumeId = id
        self.device = device
        self.type = _type
        self.size = size
        self.delete_on_terminate = delete_on_terminate


FAKE_VOLUMES_DB = {}


class FakeVolumes(object):

    def __init__(self, context):
        self.context = context
        self.db = FAKE_VOLUMES_DB
        self.event_spawn = get_event_spawer()

    def can_see(self, id):
        """Can this FakeVolumes, with its context, see some resource?"""
        server = self.db[id]
        return (self.context.is_admin or
                server.owner.tenant == self.context.tenant)

    def get(self, id):
        if id not in self.db:
            LOG.error("Couldn't find volume id %s, collection=%s" % (id,
                                                                     self.db))
            raise nova_exceptions.NotFound(404, "Not found")
        else:
            if self.can_see(id):
                return self.db[id]
            else:
                raise nova_exceptions.NotFound(404, "Bad permissions")

    def create(self, size, display_name=None, display_description=None):
        id = "FAKE_VOL_%s" % uuid.uuid4()
        volume = FakeVolume(self, self.context, id, size, display_name,
                            display_description)
        self.db[id] = volume
        if size == 9:
            volume.schedule_status("error", 2)
        elif size == 13:
            raise Exception("No volume for you!")
        else:
            volume.schedule_status("available", 2)
        LOG.info("FAKE_VOLUMES_DB : %s" % FAKE_VOLUMES_DB)
        return volume

    def list(self, detailed=True):
        return [self.db[key] for key in self.db]

    def resize(self, volume_id, new_size):
        LOG.debug("Resize volume id (%s) to size (%s)" % (volume_id, new_size))
        volume = self.get(volume_id)

        def finish_resize():
            volume._current_status = "in-use"
            volume.size = new_size
        self.event_spawn(1.0, finish_resize)


class FakeAccount(object):

    def __init__(self, id, servers):
        self.id = id
        self.servers = self._servers_to_dict(servers)

    def _servers_to_dict(self, servers):
        ret = []
        for server in servers:
            server_dict = {}
            server_dict['id'] = server.id
            server_dict['name'] = server.name
            server_dict['status'] = server.status
            server_dict['host'] = server.host
            ret.append(server_dict)
        return ret


class FakeAccounts(object):

    def __init__(self, context, servers):

        self.context = context
        self.db = FAKE_SERVERS_DB
        self.servers = servers
        self.event_spawn = get_event_spawer()

    def _belongs_to_tenant(self, tenant, id):
        server = self.db[id]
        return server.tenant == tenant

    def get_instances(self, id):
        authorize(self.context)
        servers = [v for (k, v) in self.db.items()
                   if self._belongs_to_tenant(id, v.id)]
        return FakeAccount(id, servers)


FLAVORS = FakeFlavors()


class FakeHost(object):

    def __init__(self, name, servers):
        self.name = name
        self.servers = servers
        self.instances = []
        self.percentUsed = 0
        self.totalRAM = 0
        self.usedRAM = 0

    @property
    def instanceCount(self):
        return len(self.instances)

    def recalc(self):
        """
        This fake-mode exclusive method recalculates the fake data this
        object passes back.
        """
        self.instances = []
        self.percentUsed = 0
        self.totalRAM = 2004  # 16384
        self.usedRAM = 0
        for server in self.servers.list():
            print server
            if server.host != self.name:
                print "\t...not on this host."
                continue
            self.instances.append({
                'uuid': server.id,
                'name': server.name,
                'status': server.status
            })
            try:
                flavor = FLAVORS.get(server.flavor_ref)
            except ValueError:
                # Maybe flavor_ref isn't an int?
                flavor = FLAVORS.get_by_href(server.flavor_ref)
            ram = flavor.ram
            self.usedRAM += ram
        decimal = float(self.usedRAM) / float(self.totalRAM)
        self.percentUsed = int(decimal * 100)


class FakeHosts(object):

    def __init__(self, servers):
        self.hosts = {}
        for host in FAKE_HOSTS:
            self.add_host(FakeHost(host, servers))

    def add_host(self, host):
        self.hosts[host.name] = host
        return host

    def get(self, name):
        try:
            self.hosts[name].recalc()
            return self.hosts[name]
        except KeyError:
            raise nova_exceptions.NotFound(404, "Host not found %s" % name)

    def list(self):
        for name in self.hosts:
            self.hosts[name].recalc()
        return [self.hosts[name] for name in self.hosts]


class FakeRdStorage(object):

    def __init__(self, name):
        self.name = name
        self.type = ""
        self.used = 0
        self.capacity = {}
        self.provision = {}

    def recalc(self):
        self.type = "test_type"
        self.used = 10
        self.capacity['total'] = 100
        self.capacity['available'] = 90
        self.provision['total'] = 50
        self.provision['available'] = 40
        self.provision['percent'] = 10


class FakeRdStorages(object):

    def __init__(self):
        self.storages = {}
        self.add_storage(FakeRdStorage("fake_storage"))

    def add_storage(self, storage):
        self.storages[storage.name] = storage
        return storage

    def list(self):
        for name in self.storages:
            self.storages[name].recalc()
        return [self.storages[name] for name in self.storages]


class FakeClient(object):

    def __init__(self, context):
        self.context = context
        self.flavors = FLAVORS
        self.servers = FakeServers(context, self.flavors)
        self.volumes = FakeVolumes(context)
        self.servers.volumes = self.volumes
        self.accounts = FakeAccounts(context, self.servers)
        self.rdhosts = FakeHosts(self.servers)
        self.rdstorage = FakeRdStorages()
        self.rdservers = FakeRdServers(self.servers)

    def get_server_volumes(self, server_id):
        return self.servers.get_server_volumes(server_id)

    def rescan_server_volume(self, server, volume_id):
        LOG.info("FAKE rescanning volume.")


CLIENT_DATA = {}


def get_client_data(context):
    if context not in CLIENT_DATA:
        nova_client = FakeClient(context)
        volume_client = FakeClient(context)
        nova_client.volumes = volume_client
        volume_client.servers = nova_client
        CLIENT_DATA[context] = {
            'nova': nova_client,
            'volume': volume_client
        }
    return CLIENT_DATA[context]


def fake_create_nova_client(context):
    return get_client_data(context)['nova']


def fake_create_nova_volume_client(context):
    return get_client_data(context)['volume']
