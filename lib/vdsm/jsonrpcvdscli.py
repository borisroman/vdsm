#
# Copyright 2015 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

from functools import partial
from uuid import uuid4
import socket


import six
from yajsonrpc import stompreactor
from yajsonrpc import \
    JsonRpcRequest, \
    JsonRpcNoResponseError, \
    CALL_TIMEOUT

from api import vdsmapi
from vdsm import response
from .config import config
from . import sslutils


_COMMAND_CONVERTER = {
    'create': 'VM.create',
    'destroy': 'VM.destroy',
    'fullList': 'Host.getVMFullList',
    'getAllVmStats': 'Host.getAllVmStats',
    'getVdsCapabilities': 'Host.getCapabilities',
    'getVdsStats': 'Host.getStats',
    'getVmStats': 'VM.getStats',
    'hotplugDisk': 'VM.hotplugDisk',
    'hotplugNic': 'VM.hotplugNic',
    'hotunplugDisk': 'VM.hotunplugDisk',
    'hotunplugNic': 'VM.hotunplugNic',
    'list': 'Host.getVMList',
    'migrationCreate': 'VM.migrationCreate',
    'ping': 'Host.ping',
    'setBalloonTarget': 'VM.setBalloonTarget',
    'setCpuTunePeriod': 'VM.setCpuTunePeriod',
    'setCpuTuneQuota': 'VM.setCpuTuneQuota',
    'setMOMPolicy': 'Host.setMOMPolicy',
    'setSafeNetworkConfig': 'Host.setSafeNetworkConfig',
    'setupNetworks': 'Host.setupNetworks',
    'updateVmPolicy': 'VM.updateVmPolicy',
}


class _Server(object):

    def __init__(self, client, xml_compat):
        self._vdsmapi = vdsmapi.get_api()
        self._client = client
        self._xml_compat = xml_compat

    def _prepare_args(self, className, methodName, args, kwargs):
        sym = self._vdsmapi['commands'][className][methodName]
        allargs = (arg[1:] if arg.startswith('*') else arg for arg
                   in six.iterkeys(sym.get('data', {})))
        params = dict(zip(allargs, args))
        params.update(kwargs)
        return params

    def _callMethod(self, methodName, *args, **kwargs):
        try:
            method = _COMMAND_CONVERTER[methodName]
        except KeyError as e:
            raise Exception("Attempt to call function: %s with "
                            "arguments: %s error: %s" %
                            (methodName, args, e))

        class_name, method_name = method.split('.')
        params = self._prepare_args(class_name, method_name, args, kwargs)

        timeout = kwargs.pop('_transport_timeout', CALL_TIMEOUT)

        req = JsonRpcRequest(method, params, reqId=str(uuid4()))

        responses = self._client.call(req, timeout=timeout)
        if responses:
            resp = responses[0]
        else:
            raise JsonRpcNoResponseError(method)

        if resp.error is not None:
            return response.error_raw(resp.error["code"],
                                      resp.error["message"])

        if not self._xml_compat:
            return response.success_raw(resp.result)

        if resp.result and resp.result is not True:
            # None is translated to True inside our JSONRPC implementation
            return response.success(**resp.result)

        return response.success()

    def migrationCreate(self, params):
        return self._callMethod('migrationCreate',
                                params['vmId'],
                                params)

    def create(self, params):
        return self._callMethod('create',
                                params['vmId'],
                                params)

    def __getattr__(self, methodName):
        return partial(self._callMethod, methodName)

    def __del__(self):
        self._client.close()


def _create(requestQueue,
            host=None, port=None,
            useSSL=None,
            responseQueue=None):
    if host is None:
        host = socket.gethostname()
    if port is None:
        port = int(config.getint('addresses', 'management_port'))

    if useSSL is None:
        useSSL = config.getboolean('vars', 'ssl')

    if useSSL:
        sslctx = sslutils.create_ssl_context()
    else:
        sslctx = None

    return stompreactor.StandAloneRpcClient(
        host, port, requestQueue, str(uuid4()), sslctx,
        lazy_start=False)


def connect(requestQueue, stompClient=None,
            host=None, port=None,
            useSSL=None,
            responseQueue=None, xml_compat=True):
    if not stompClient:
        client = _create(requestQueue,
                         host, port, useSSL,
                         responseQueue)
    else:
        client = stompreactor.StompRpcClient(
            stompClient,
            requestQueue,
            str(uuid4())
        )

    return _Server(client, xml_compat)
