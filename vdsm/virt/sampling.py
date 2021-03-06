#
# Copyright 2008-2015 Red Hat, Inc.
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

"""
Support for VM and host statistics sampling.
"""

from collections import defaultdict, deque, namedtuple
import errno
import logging
import os
import re
import threading
import time

from vdsm.constants import P_VDSM_RUN, P_VDSM_CLIENT_LOG
from vdsm import ipwrapper
from vdsm import netinfo
from vdsm import utils
from vdsm.config import config

from . import hoststats
from . import virdomain
from .utils import ExpiringCache

import caps

_THP_STATE_PATH = '/sys/kernel/mm/transparent_hugepage/enabled'
if not os.path.exists(_THP_STATE_PATH):
    _THP_STATE_PATH = '/sys/kernel/mm/redhat_transparent_hugepage/enabled'


class InterfaceSample(object):
    """
    A network interface sample.

    The sample is set at the time of initialization and can't be updated.
    """
    def readIfaceStat(self, ifid, stat):
        """
        Get and interface's stat.

        .. note::
            Really ugly implementation; from time to time, Linux returns an
            empty line. TODO: understand why this happens and fix it!

        :param ifid: The ID of the interface you want to query.
        :param stat: The type of statistic you want get.

        :returns: The value of the statistic you asked for.
        :type: int
        """
        f = '/sys/class/net/%s/statistics/%s' % (ifid, stat)
        tries = 5
        while tries:
            tries -= 1
            try:
                with open(f) as fi:
                    s = fi.read()
            except IOError as e:
                # silently ignore missing wifi stats
                if e.errno != errno.ENOENT:
                    logging.debug("Could not read %s", f, exc_info=True)
                return 0
            try:
                return int(s)
            except:
                if s != '':
                    logging.warning("Could not parse statistics (%s) from %s",
                                    f, s, exc_info=True)
                logging.debug('bad %s: (%s)', f, s)
                if not tries:
                    raise

    def __init__(self, link):
        ifid = link.name
        self.rx = self.readIfaceStat(ifid, 'rx_bytes')
        self.tx = self.readIfaceStat(ifid, 'tx_bytes')
        self.rxDropped = self.readIfaceStat(ifid, 'rx_dropped')
        self.txDropped = self.readIfaceStat(ifid, 'tx_dropped')
        self.rxErrors = self.readIfaceStat(ifid, 'rx_errors')
        self.txErrors = self.readIfaceStat(ifid, 'tx_errors')
        self.operstate = 'up' if link.oper_up else 'down'
        self.speed = _getLinkSpeed(link)
        self.duplex = _getDuplex(ifid)

    _LOGGED_ATTRS = ('operstate', 'speed', 'duplex')

    def _log_attrs(self, attrs):
        return ' '.join(
            '%s:%s' % (attr, getattr(self, attr)) for attr in attrs)

    def to_connlog(self):
        return self._log_attrs(self._LOGGED_ATTRS)

    def connlog_diff(self, other):
        """Return a textual description of the interesting stuff new to self
        and missing in 'other'."""

        return self._log_attrs(
            attr for attr in self._LOGGED_ATTRS
            if getattr(self, attr) != getattr(other, attr))


class TotalCpuSample(object):
    """
    A sample of total CPU consumption.

    The sample is taken at initialization time and can't be updated.
    """
    def __init__(self):
        with open('/proc/stat') as f:
            self.user, userNice, self.sys, self.idle = \
                map(int, f.readline().split()[1:5])
        self.user += userNice


class CpuCoreSample(object):
    """
    A sample of the CPU consumption of each core

    The sample is taken at initialization time and can't be updated.
    """
    CPU_CORE_STATS_PATTERN = re.compile(r'cpu(\d+)\s+(.*)')

    def __init__(self):
        self.coresSample = {}
        with open('/proc/stat') as src:
            for line in src:
                match = self.CPU_CORE_STATS_PATTERN.match(line)
                if match:
                    coreSample = {}
                    user, userNice, sys, idle = \
                        map(int, match.group(2).split()[0:4])
                    coreSample['user'] = user
                    coreSample['userNice'] = userNice
                    coreSample['sys'] = sys
                    coreSample['idle'] = idle
                    self.coresSample[match.group(1)] = coreSample

    def getCoreSample(self, coreId):
        strCoreId = str(coreId)
        if strCoreId in self.coresSample:
            return self.coresSample[strCoreId]


class NumaNodeMemorySample(object):
    """
    A sample of the memory stats of each numa node

    The sample is taken at initialization time and can't be updated.
    """
    def __init__(self):
        self.nodesMemSample = {}
        numaTopology = caps.getNumaTopology()
        for nodeIndex in numaTopology:
            nodeMemSample = {}
            if len(numaTopology) < 2:
                memInfo = caps.getUMAHostMemoryStats()
            else:
                memInfo = caps.getMemoryStatsByNumaCell(int(nodeIndex))
            nodeMemSample['memFree'] = memInfo['free']
            # in case the numa node has zero memory assigned, report the whole
            # memory as used
            nodeMemSample['memPercent'] = 100
            if int(memInfo['total']) != 0:
                nodeMemSample['memPercent'] = 100 - \
                    int(100.0 * int(memInfo['free']) / int(memInfo['total']))
            self.nodesMemSample[nodeIndex] = nodeMemSample


class PidCpuSample(object):
    """
    A sample of the CPU consumption of a process.

    The sample is taken at initialization time and can't be updated.
    """
    def __init__(self, pid):
        with open('/proc/%s/stat' % pid) as stat:
            self.user, self.sys = \
                map(int, stat.read().split()[13:15])


class TimedSample(object):
    def __init__(self):
        self.timestamp = time.time()


def _get_interfaces_and_samples():
    links_and_samples = {}
    for link in ipwrapper.getLinks():
        try:
            links_and_samples[link.name] = InterfaceSample(link)
        except IOError as e:
            # this handles a race condition where the device is now no
            # longer exists and netlink fails to fetch it
            if e.errno == errno.ENODEV:
                continue
            raise
    return links_and_samples


class HostSample(TimedSample):
    """
    A sample of host-related statistics.

    Contains the sate of the host in the time of initialization.
    """
    MONITORED_PATHS = ['/tmp', '/var/log', '/var/log/core', P_VDSM_RUN]

    def _getDiskStats(self):
        d = {}
        for p in self.MONITORED_PATHS:
            free = 0
            try:
                stat = os.statvfs(p)
                free = stat.f_bavail * stat.f_bsize / (2 ** 20)
            except:
                pass
            d[p] = {'free': str(free)}
        return d

    def __init__(self, pid):
        """
        Initialize a HostSample.

        :param pid: The PID of this vdsm host.
        :type pid: int
        """
        super(HostSample, self).__init__()
        self.interfaces = _get_interfaces_and_samples()
        self.pidcpu = PidCpuSample(pid)
        self.ncpus = os.sysconf('SC_NPROCESSORS_ONLN')
        self.totcpu = TotalCpuSample()
        meminfo = utils.readMemInfo()
        freeOrCached = (meminfo['MemFree'] +
                        meminfo['Cached'] + meminfo['Buffers'])
        self.memUsed = 100 - int(100.0 * (freeOrCached) / meminfo['MemTotal'])
        self.anonHugePages = meminfo.get('AnonHugePages', 0) / 1024
        try:
            with open('/proc/loadavg') as loadavg:
                self.cpuLoad = loadavg.read().split()[1]
        except:
            self.cpuLoad = '0.0'
        self.diskStats = self._getDiskStats()
        try:
            with open(_THP_STATE_PATH) as f:
                s = f.read()
                self.thpState = s[s.index('[') + 1:s.index(']')]
        except:
            self.thpState = 'never'
        self.cpuCores = CpuCoreSample()
        self.numaNodeMem = NumaNodeMemorySample()
        ENGINE_DEFAULT_POLL_INTERVAL = 15
        try:
            self.recentClient = (
                self.timestamp - os.stat(P_VDSM_CLIENT_LOG).st_mtime <
                2 * ENGINE_DEFAULT_POLL_INTERVAL)
        except OSError as e:
            if e.errno == errno.ENOENT:
                self.recentClient = False
            else:
                raise

    def to_connlog(self):
        text = ', '.join(
            ('%s:(%s)' % (ifid, ifacesample.to_connlog()))
            for (ifid, ifacesample) in self.interfaces.iteritems())
        return ('recent_client:%s, ' % self.recentClient) + text

    def connlog_diff(self, other):
        text = ''
        for ifid, sample in self.interfaces.iteritems():
            if ifid in other.interfaces:
                diff = sample.connlog_diff(other.interfaces[ifid])
                if diff:
                    text += '%s:(%s) ' % (ifid, diff)
            else:
                text += 'new %s:(%s) ' % (ifid, sample.to_connlog())

        for ifid, sample in other.interfaces.iteritems():
            if ifid not in self.interfaces:
                text += 'dropped %s:(%s) ' % (ifid, sample.to_connlog())

        if self.recentClient != other.recentClient:
            text += 'recent_client:%s' % self.recentClient

        return text


_MINIMUM_SAMPLES = 1


class SampleWindow(object):
    """Keep sliding window of samples."""

    def __init__(self, size, timefn=time.time):
        if size < _MINIMUM_SAMPLES:
            raise ValueError("window size must be not less than %i" %
                             _MINIMUM_SAMPLES)

        self._samples = deque(maxlen=size)
        self._timefn = timefn

    def append(self, value):
        """
        Record the current time and append new sample, removing the oldest
        sample if needed.
        """
        timestamp = self._timefn()
        self._samples.append((timestamp, value))

    def stats(self):
        """
        Return a tuple in the format: (first, last, difftime), containing
        the first and the last samples in the defined 'window' and the
        time difference between them.
        """
        if len(self._samples) < 2:
            return None, None, None

        first_timestamp, first_sample = self._samples[0]
        last_timestamp, last_sample = self._samples[-1]

        elapsed_time = last_timestamp - first_timestamp
        return first_sample, last_sample, elapsed_time

    def last(self, nth=1):
        """
        Return the nth-last collected sample.
        """
        if len(self._samples) < nth:
            return None
        _, sample = self._samples[-nth]
        return sample


StatsSample = namedtuple('StatsSample',
                         ['first_value', 'last_value',
                          'interval', 'stats_age'])


EMPTY_SAMPLE = StatsSample(None, None, None, None)


class StatsCache(object):
    """
    Cache for bulk stats samples.
    Provide facilities to retrieve per-vm samples, and the glue code to deal
    with disappearing per-vm samples.

    Rationale for the 'clock()' method and for the odd API of the 'put()'
    method with explicit 'monotonic_ts' argument:

    QEMU processes can go rogue and block on a sampling operation, most
    likely, but not only, because storage becomes unavailable.
    In turn, that means that libvirt API that VDSM uses can get stuck,
    but eventually those calls can unblock.

    VDSM has countermeasures for those cases. Stuck threads are replaced,
    thanks to Executor. But still, before to be destroyed, a replaced
    thread can mistakenly try to add a sample to a StatsCache.

    Because of worker thread replacement, that sample from stuck thread
    can  be stale.
    So, under the assumption that at stable state stats collection has
    a time cost negligible with respect the collection interval, we need
    to take the sample timestamp BEFORE to start the possibly-blocking call.
    If we take the timestamp after the call, we have no means to distinguish
    between a well behaving call and an unblocked stuck call.
    """

    _log = logging.getLogger("virt.sampling.StatsCache")

    def __init__(self, clock=utils.monotonic_time):
        self._clock = clock
        self._lock = threading.Lock()
        self._samples = SampleWindow(size=2, timefn=self._clock)
        self._last_sample_time = 0
        self._vm_last_timestamp = defaultdict(int)

    def add(self, vmid):
        """
        Warm up the cache for the given VM.
        This is to avoid races during the first sampling and the first
        reporting, which may result in a VM wrongly reported as unresponsive.
        """
        with self._lock:
            self._vm_last_timestamp[vmid] = self._clock()

    def remove(self, vmid):
        """
        Remove any data from the cache related to the given VM.
        """
        with self._lock:
            del self._vm_last_timestamp[vmid]

    def get(self, vmid):
        """
        Return the available StatSample for the given VM.
        """
        with self._lock:
            first_batch, last_batch, interval = self._samples.stats()

            if first_batch is None:
                return EMPTY_SAMPLE

            first_sample = first_batch.get(vmid)
            last_sample = last_batch.get(vmid)

            if first_sample is None or last_sample is None:
                return EMPTY_SAMPLE

            stats_age = self._clock() - self._vm_last_timestamp[vmid]

            return StatsSample(first_sample, last_sample,
                               interval, stats_age)

    def clock(self):
        """
        Provide timestamp compatible with what put() expects
        """
        return self._clock()

    def put(self, bulk_stats, monotonic_ts):
        """
        Add a new bulk sample to the collection.
        `monotonic_ts' is the sample time which must be associated with
        the sample.
        Discard silently out of order samples, which are assumed to be
        returned by unblocked stuck calls, to avoid overwrite fresh data
        with stale one.
        """
        with self._lock:
            last_sample_time = self._last_sample_time
            if monotonic_ts >= last_sample_time:
                self._samples.append(bulk_stats)
                self._last_sample_time = monotonic_ts

                self._update_ts(bulk_stats, monotonic_ts)
            else:
                self._log.warning(
                    'dropped stale old sample: sampled %f stored %f',
                    monotonic_ts, last_sample_time)

    def _update_ts(self, bulk_stats, monotonic_ts):
        # FIXME: this is expected to be costly performance-wise.
        for vmid in bulk_stats:
            self._vm_last_timestamp[vmid] = monotonic_ts


stats_cache = StatsCache()


# this value can be tricky to tune.
# we should avoid as much as we can to trigger
# false positive fast flows (getAllDomainStats call).
# to do so, we should avoid this value to be
# a multiple of known timeout and period:
# - vm sampling period is 15s (we control that)
# - libvirt (default) qemu monitor timeout is 30s (we DON'T contol this)
_TTL = 40.0


class VMBulkSampler(object):
    def __init__(self, conn, get_vms, stats_cache,
                 stats_flags=0, ttl=_TTL):
        self._conn = conn
        self._get_vms = get_vms
        self._stats_cache = stats_cache
        self._stats_flags = stats_flags
        self._skip_doms = ExpiringCache(ttl)
        self._sampling = threading.Semaphore()  # used as glorified counter
        self._log = logging.getLogger("virt.sampling.VMBulkSampler")

    def __call__(self):
        timestamp = self._stats_cache.clock()
        acquired = self._sampling.acquire(blocking=False)
        # we are deep in the hot path. bool(ExpiringCache)
        # *is* costly so we should avoid it if we can.
        fast_path = acquired and not self._skip_doms
        try:
            if fast_path:
                # This is expected to be the common case.
                # If everything's ok, we can skip all the costly checks.
                bulk_stats = self._conn.getAllDomainStats(self._stats_flags)
            else:
                # A previous call got stuck, or not every domain
                # has properly recovered. Thus we must whitelist domains.
                doms = self._get_responsive_doms()
                self._log.debug('sampling %d domains', len(doms))
                if doms:
                    bulk_stats = self._conn.domainListGetStats(
                        doms, self._stats_flags)
                else:
                    bulk_stats = []
        except Exception:
            self._log.exception("vm sampling failed")
        else:
            self._stats_cache.put(_translate(bulk_stats), timestamp)
        finally:
            if acquired:
                self._sampling.release()

    def _get_responsive_doms(self):
        vms = self._get_vms()
        doms = []
        for vm_id, vm_obj in vms.iteritems():
            to_skip = self._skip_doms.get(vm_id, False)
            if to_skip:
                continue
            elif not vm_obj.isDomainReadyForCommands():
                self._skip_doms[vm_id] = True
            else:
                # TODO: This racy check may fail if the underlying libvirt
                # domain has died just after checking isDomainReadyForCommands
                # succeeded.
                doms.append(vm_obj._dom._dom)
        return doms

    def __str__(self):
        return "<VMBulkSampler at 0x%x>" % id(self)


HOST_STATS_AVERAGING_WINDOW = 5


host_samples = SampleWindow(size=HOST_STATS_AVERAGING_WINDOW)


class HostStatsThread(object):
    """
    A thread that periodically samples host statistics.
    """
    _CONNLOG = logging.getLogger('connectivity')

    _log = logging.getLogger('vds.sampling.HostStats')

    def __init__(self, samples=host_samples, clock=utils.monotonic_time):
        self._samples = samples
        hoststats.start(clock)
        self._pid = os.getpid()

        self._stopEvent = threading.Event()

        self._thread = threading.Thread(target=self._run, name='hoststats')
        self._thread.daemon = True

    def start(self):
        self._thread.start()

    def stop(self):
        self._stopEvent.set()

    def wait(self):
        self._thread.join()

    def _run(self):

        sample_interval = config.getint(
            'vars', 'host_sample_stats_interval')

        try:
            # wait a bit before starting to sample
            time.sleep(sample_interval)
            while not self._stopEvent.isSet():
                try:
                    sample = HostSample(self._pid)
                    self._samples.append(sample)
                    second_last = self._samples.last(nth=2)
                    if second_last is None:
                        self._CONNLOG.debug('%s', sample.to_connlog())
                    else:
                        diff = sample.connlog_diff(second_last)
                        if diff:
                            self._CONNLOG.debug('%s', diff)
                except virdomain.TimeoutError:
                    self._log.exception("Timeout while sampling stats")
                self._stopEvent.wait(sample_interval)
        except:
            if not self._stopEvent.isSet():
                self._log.exception("Error while sampling stats")


def _getLinkSpeed(dev):
    if dev.isNIC():
        speed = netinfo.nicSpeed(dev.name)
    elif dev.isBOND():
        speed = netinfo.bondSpeed(dev.name)
    elif dev.isVLAN():
        speed = netinfo.vlanSpeed(dev.name)
    else:
        speed = 0
    return speed


def _getDuplex(ifid):
    """Return whether a device is connected in full-duplex. Return 'unknown' if
    duplex state is not known"""
    try:
        with open('/sys/class/net/%s/duplex' % ifid) as src:
            return src.read().strip()
    except IOError:
        return 'unknown'


def _translate(bulk_stats):
    return dict((dom.UUIDString(), stats)
                for dom, stats in bulk_stats)
