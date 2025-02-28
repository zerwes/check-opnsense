#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# vim: set fileencoding=utf-8:noet

##  Copyright 2022 Bashclub
##  BSD-2-Clause
##
##  Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:
##
##  1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
##
##  2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
##
## THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
## THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS
## BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE
## GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
## LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

## OPNsense CheckMK Agent
## to install
## copy to /usr/local/etc/rc.syshook.d/start/99-checkmk_agent and chmod +x
##

__VERSION__ = "0.99.2"

import sys
import os
import shlex
import glob
import re
import time
import json
import socket
import signal
import struct
import subprocess
import pwd
import threading
import ipaddress
import base64
import traceback
import syslog
import requests
from urllib3.connection import HTTPConnection
from urllib3.connectionpool import HTTPConnectionPool
from requests.adapters import HTTPAdapter
from cryptography import x509
from cryptography.hazmat.backends import default_backend as crypto_default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from xml.etree import cElementTree as ELementTree
from collections import Counter,defaultdict
from pprint import pprint
from socketserver import TCPServer,StreamRequestHandler

SCRIPTPATH = os.path.abspath(os.path.basename(__file__))
if os.path.islink(SCRIPTPATH):
    SCRIPTPATH = os.path.realpath(os.readlink(SCRIPTPATH))
BASEDIR = "/usr/local"
MK_CONFDIR = os.path.join(BASEDIR,"etc")
CHECKMK_CONFIG = os.path.join(MK_CONFDIR,"checkmk.conf")
LOCALDIR = os.path.join(BASEDIR,"local")
SPOOLDIR = os.path.join(BASEDIR,"spool")

class object_dict(defaultdict):
    def __getattr__(self,name):
        return self[name] if name in self else ""

def etree_to_dict(t):
    d = {t.tag: {} if t.attrib else None}
    children = list(t)
    if children:
        dd = object_dict(list)
        for dc in map(etree_to_dict, children):
            for k, v in dc.items():
                dd[k].append(v)
        d = {t.tag: {k:v[0] if len(v) == 1 else v for k, v in dd.items()}}
    if t.attrib:
        d[t.tag].update(('@' + k, v) for k, v in t.attrib.items())
    if t.text:
        text = t.text.strip()
        if children or t.attrib:
            if text:
              d[t.tag]['#text'] = text
        else:
            d[t.tag] = text
    return d

def log(message,prio="notice"):
    priority = { 
        "crit"      :syslog.LOG_CRIT,
        "err"       :syslog.LOG_ERR,
        "warning"   :syslog.LOG_WARNING,
        "notice"    :syslog.LOG_NOTICE, 
        "info"      :syslog.LOG_INFO, 
    }.get(str(prio).lower(),syslog.LOG_DEBUG)
    syslog.openlog(ident="checkmk_agent",logoption=syslog.LOG_PID | syslog.LOG_NDELAY,facility=syslog.LOG_DAEMON)
    syslog.syslog(priority,message)


def pad_pkcs7(message,size=16):
    _pad = size - (len(message) % size)
    if type(message) == str:
        return message + chr(_pad) * _pad
    else:
        return message + bytes([_pad]) * _pad

class NginxConnection(HTTPConnection):
    def __init__(self):
        super().__init__("localhost")
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect("/var/run/nginx_status.sock")

class NginxConnectionPool(HTTPConnectionPool):
    def __init__(self):
        super().__init__("localhost")
    def _new_conn(self):
        return NginxConnection()

class NginxAdapter(HTTPAdapter):
    def get_connection(self, url, proxies=None):
        return NginxConnectionPool()


def check_pid(pid):
    try:
        os.kill(pid,0)
        return True
    except OSError: ## no permission check currently root
        return False

class checkmk_handler(StreamRequestHandler):
    def handle(self):
        with self.server._mutex:
            try:
                _strmsg = self.server.do_checks(remote_ip=self.client_address[0])
            except Exception as e:
                raise
                _strmsg = str(e).encode("utf-8")
            try:
                self.wfile.write(_strmsg)
            except:
                pass

class checkmk_checker(object):
    _available_sysctl_list = []
    _available_sysctl_temperature_list = []
    _certificate_timestamp = 0
    _check_cache = {}
    _datastore_mutex = threading.RLock()
    _datastore = object_dict()

    def encrypt(self,message,password='secretpassword'):
        SALT_LENGTH = 8
        KEY_LENGTH = 32
        IV_LENGTH = 16
        PBKDF2_CYCLES = 10_000
        SALT = b"Salted__"
        _backend = crypto_default_backend()
        _kdf_key =  PBKDF2HMAC(
            algorithm = hashes.SHA256,
            length = KEY_LENGTH + IV_LENGTH,
            salt = SALT,
            iterations = PBKDF2_CYCLES,
            backend = _backend
        ).derive(password.encode("utf-8"))
        _key, _iv = _kdf_key[:KEY_LENGTH],_kdf_key[KEY_LENGTH:]
        _encryptor = Cipher(
            algorithms.AES(_key),
            modes.CBC(_iv),
            backend = _backend
        ).encryptor()
        message = pad_pkcs7(message)
        message = message.encode("utf-8")
        _encrypted_message = _encryptor.update(message) + _encryptor.finalize()
        return pad_pkcs7(b"03",10) + SALT + _encrypted_message

    def _encrypt(self,message): ## openssl ## todo ## remove
        _cmd = shlex.split('openssl enc -aes-256-cbc -md sha256 -iter 10000 -k "secretpassword"',posix=True)
        _proc = subprocess.Popen(_cmd,stderr=subprocess.DEVNULL,stdout=subprocess.PIPE,stdin=subprocess.PIPE)
        _out,_err = _proc.communicate(input=message.encode("utf-8"))
        return b"03" + _out

    def do_checks(self,debug=False,remote_ip=None,**kwargs):
        self._getosinfo()
        _errors = []
        _failed_sections = []
        _lines = ["<<<check_mk>>>"]
        _lines.append("AgentOS: {os}".format(**self._info))
        _lines.append(f"Version: {__VERSION__}")
        _lines.append("Hostname: {hostname}".format(**self._info))
        if self.onlyfrom:
            _lines.append("OnlyFrom: {0}".format(",".join(self.onlyfrom)))

        _lines.append(f"LocalDirectory: {LOCALDIR}")
        _lines.append(f"AgentDirectory: {MK_CONFDIR}")
        _lines.append(f"SpoolDirectory: {SPOOLDIR}")

        for _check in dir(self):
            if _check.startswith("check_"):
                _name = _check.split("_",1)[1]
                if _name in self.skipcheck:
                    continue
                try:
                    _lines += getattr(self,_check)()
                except:
                    _failed_sections.append(_name)
                    _errors.append(traceback.format_exc())

        _lines.append("<<<local:sep(0)>>>")
        for _check in dir(self):
            if _check.startswith("checklocal_"):
                _name = _check.split("_",1)[1]
                if _name in self.skipcheck:
                    continue
                try:
                    _lines += getattr(self,_check)()
                except:
                    _failed_sections.append(_name)
                    _errors.append(traceback.format_exc())

        if os.path.isdir(LOCALDIR):
            for _local_file in glob.glob(f"{LOCALDIR}/**",recursive=True):
                if os.path.isfile(_local_file) and os.access(_local_file,os.X_OK):
                    try:
                        _cachetime = int(_local_file.split(os.path.sep)[-2])
                    except:
                        _cachetime = 0
                    try:
                        _lines.append(self._run_cache_prog(_local_file,_cachetime))
                    except:
                        _errors.append(traceback.format_exc())

        if os.path.isdir(SPOOLDIR):
            _now = time.time()
            for _filename in glob.glob(f"{SPOOLDIR}/*"):
                _maxage = re.search("^\d+",_filename)

                if _maxage:
                    _maxage = int(_maxage.group())
                    _mtime = os.stat(_filename).st_mtime
                    if _now - _mtime > _maxage:
                        continue
                with open(_filename) as _f:
                    _lines.append(_f.read())

        _lines.append("")
        if debug:
            sys.stdout.write("\n".join(_errors))
            sys.stdout.flush()
        if _failed_sections:
            _lines.append("<<<check_mk>>>")
            _lines.append("FailedPythonPlugins: {0}".format(",".join(_failed_sections)))

        if self.encryptionkey:
            return self.encrypt("\n".join(_lines),password=self.encryptionkey)
        return "\n".join(_lines).encode("utf-8")

    def _get_storedata(self,section,key):
        with self._datastore_mutex:
            return self._datastore.get(section,{}).get(key)
    def _set_storedata(self,section,key,value):
        with self._datastore_mutex:
            if section not in self._datastore:
                self._datastore[section] = object_dict()
            self._datastore[section][key] = value

    def _getosinfo(self):
        _info = json.load(open("/usr/local/opnsense/version/core","r"))
        _changelog = json.load(open("/usr/local/opnsense/changelog/index.json","r"))
        _config_modified = os.stat("/conf/config.xml").st_mtime
        try:
            _latest_firmware = list(filter(lambda x: x.get("series") == _info.get("product_series"),_changelog))[-1]
            _current_firmware = list(filter(lambda x: x.get("version") == _info.get("product_version").split("_")[0],_changelog))[0].copy() ## not same
            _current_firmware["age"] = int(time.time() - time.mktime(time.strptime(_current_firmware.get("date"),"%B %d, %Y")))
            _current_firmware["version"] = _info.get("product_version")
        except:
            raise
            _lastest_firmware = {}
            _current_firmware = {}
        try:
            _upgrade_json = json.load(open("/tmp/pkg_upgrade.json","r"))
            _upgrade_packages = dict(map(lambda x: (x.get("name"),x),_upgrade_json.get("upgrade_packages")))
            _current_firmware["version"] = _upgrade_packages.get("opnsense").get("current_version")
            _latest_firmware["version"] = _upgrade_packages.get("opnsense").get("new_version")
        except:
            _current_firmware["version"] = _current_firmware["version"].split("_")[0]
            _latest_firmware["version"] = _current_firmware["version"] ## fixme ## no upgradepckg error on opnsense ... no new version
        self._info = {
            "os"                : _info.get("product_name"),
            "os_version"        : _current_firmware.get("version"),
            "version_age"       : _current_firmware.get("age",0),
            "config_age"        : int(time.time() - _config_modified) ,
            "last_configchange" : time.strftime("%H:%M %d.%m.%Y",time.localtime(_config_modified)),
            "product_series"    : _info.get("product_series"),
            "latest_version"    : _latest_firmware.get("version"),
            "latest_date"       : _latest_firmware.get("date"),
            "hostname"          : self._run_prog("hostname").strip(" \n")
        }

    @staticmethod
    def ip2int(ipaddr):
        return struct.unpack("!I",socket.inet_aton(ipaddr))[0]

    @staticmethod
    def int2ip(intaddr):
        return socket.inet_ntoa(struct.pack("!I",intaddr))

    def pidof(self,prog,default=None):
        _allprogs = re.findall("(\w+)\s+(\d+)",self._run_prog("ps ax -c -o command,pid"))
        return int(dict(_allprogs).get(prog,default))

    def _config_reader(self,config=""):
        _config = ELementTree.parse("/conf/config.xml")
        _root = _config.getroot()
        return etree_to_dict(_root).get("opnsense",{})

    @staticmethod
    def get_common_name(certrdn):
        try:
            return next(filter(lambda x: x.oid == x509.oid.NameOID.COMMON_NAME,certrdn)).value.strip()
        except:
            return str(certrdn)

    def _certificate_parser(self):
        self._certificate_timestamp = time.time()
        self._certificate_store = {}
        for _cert in self._config_reader().get("cert"):
            try:
                _certpem = base64.b64decode(_cert.get("crt"))
                _x509cert = x509.load_pem_x509_certificate(_certpem,crypto_default_backend())
                _cert["not_valid_before"]   = _x509cert.not_valid_before.timestamp()
                _cert["not_valid_after"]    = _x509cert.not_valid_after.timestamp()
                _cert["serial"]             = _x509cert.serial_number
                _cert["common_name"]        = self.get_common_name(_x509cert.subject)
                _cert["issuer"]             = self.get_common_name(_x509cert.issuer)
            except:
                pass
            self._certificate_store[_cert.get("refid")] = _cert
            
    def _get_certificate(self,refid):
        if time.time() - self._certificate_timestamp > 3600:
            self._certificate_parser()
        return self._certificate_store.get(refid)

    def _get_certificate_by_cn(self,cn,caref=None):
        if time.time() - self._certificate_timestamp > 3600:
            self._certificate_parser()
        if caref:
            _ret = filter(lambda x: x.get("common_name") == cn and x.get("caref") == caref,self._certificate_store.values())
        else:
            _ret = filter(lambda x: x.get("common_name") == cn,self._certificate_store.values())
        try:
            return next(_ret)
        except StopIteration:
            return {}

    def get_opnsense_ipaddr(self):
        try:
            _ret = {}
            for _if,_ip,_mask in re.findall("^([\w_]+):\sflags=(?:8943|8051|8043|8863).*?inet\s([\d.]+)\snetmask\s0x([a-f0-9]+)",self._run_prog("ifconfig"),re.DOTALL | re.M):
                _ret[_if] = "{0}/{1}".format(_ip,str(bin(int(_mask,16))).count("1"))
            return _ret
        except:
            return {}

    def _get_opnsense_ipaddr(self):
        RE_IPDATA = re.compile("(?P<inet>inet6?)\s(?P<ip>[\da-f:.]+)\/(?P<cidr>\d+).*?(?:vhid\s(?P<vhid>\d+)|$)|carp:\s(?P<carp_status>MASTER|BACKUP)\svhid\s(?P<carp_vhid>\d+)\sadvbase\s(?P<carp_base>\d+)\sadvskew\s(?P<carp_skew>\d)|(vlan):\s(?P<vlan>\d*)",re.DOTALL | re.M)
        try:
            _ret = {}
            for _if,_data in re.findall("([\w_]+):\s(.*?)\n(?=(?:\w|$))",self._run_prog("ifconfig -f inet:cidr,inet6:cidr"),re.DOTALL | re.M):
                _ret[_if] = RE_IPDATA.search(_data).groups()
            return _ret
        except:
            return {}


    def get_opnsense_interfaces(self):
        _ifs = {}
        #pprint(self._config_reader().get("interfaces"))
        #sys.exit(0)
        for _name,_interface in self._config_reader().get("interfaces",{}).items():
            if _interface.get("enable") != "1":
                continue
            _desc = _interface.get("descr")
            _ifs[_interface.get("if","_")] = _desc if _desc else _name.upper()

        try: 
            _wgserver = self._config_reader().get("OPNsense").get("wireguard").get("server").get("servers").get("server")
            if type(_wgserver) == dict:
                _wgserver = [_wgserver]
            _ifs.update(
                dict(
                    map(
                        lambda x: ("wg{}".format(x.get("instance")),"Wireguard_{}".format(x.get("name").strip().replace(" ","_"))),
                        _wgserver
                    )
                )
            )
        except:
            pass
        return _ifs

    def checklocal_firmware(self):
        if self._info.get("os_version") != self._info.get("latest_version"):
            return ["1 Firmware update_available=1|last_updated={version_age:.0f}|apply_finish_time={config_age:.0f} Version {os_version} ({latest_version} available {latest_date}) Config changed: {last_configchange}".format(**self._info)]
        return ["0 Firmware update_available=0|last_updated={version_age:.0f}|apply_finish_time={config_age:.0f} Version {os_version}  Config changed: {last_configchange}".format(**self._info)]

    def check_label(self):
        _ret = ["<<<labels:sep(0)>>>"]
        _dmsg = self._run_prog("dmesg",timeout=10)
        if _dmsg.lower().find("hypervisor:") > -1:
            _ret.append('{"cmk/device_type":"vm"}')
        return _ret

    def check_net(self):
        _now = int(time.time())
        _opnsense_ifs = self.get_opnsense_interfaces()
        _ret = ["<<<statgrab_net>>>"]
        _interface_data = []
        _interface_data = self._run_prog("/usr/bin/netstat -i -b -d -n -W -f link").split("\n")
        _header = _interface_data[0].lower()
        _header = _header.replace("pkts","packets").replace("coll","collisions").replace("errs","error").replace("ibytes","rx").replace("obytes","tx")
        _header = _header.split()
        _interface_stats = dict(
            map(
                lambda x: (x.get("name"),x),
                [
                    dict(zip(_header,_ifdata.split()))
                    for _ifdata in _interface_data[1:] if _ifdata
                ]
            )
        )

        _ifconfig_out = self._run_prog("ifconfig -m -v -f inet:cidr,inet6:cidr")
        _ifconfig_out += "END" ## fix regex
        self._all_interfaces = object_dict()
        self._carp_interfaces = object_dict()
        for _interface, _data in re.findall("^(?P<iface>[\w.]+):\s(?P<data>.*?(?=^\w))",_ifconfig_out,re.DOTALL | re.MULTILINE):
            _interface_dict = object_dict()
            _interface_dict.update(_interface_stats.get(_interface,{}))
            _interface_dict["interface_name"] = _opnsense_ifs.get(_interface,_interface)
            _interface_dict["up"] = "false"
            #if _interface.startswith("vmx"): ## vmware fix 10GBe (as OS Support)
            #    _interface_dict["speed"] = "10000"
            _interface_dict["systime"] = _now
            for _key, _val in re.findall("^\s*(\w+)[:\s=]+(.*?)$",_data,re.MULTILINE):
                if _key == "description":
                   _interface_dict["interface_name"] = re.sub("_\((lan|wan|opt\d)\)","",_val.strip().replace(" ","_"))
                if _key == "groups":
                    _interface_dict["groups"] = _val.strip().split()
                if _key == "ether":
                    _interface_dict["phys_address"] = _val.strip()
                if _key == "status" and _val.strip() == "active":
                    _interface_dict["up"] = "true"
                if _interface.startswith("wg") and _interface_dict.get("flags",0) & 0x01:
                    _interface_dict["up"] = "true"
                if _key == "flags":
                    _interface_dict["flags"] = int(re.findall("^[a-f\d]+",_val)[0],16)
                    ## hack pppoe no status active or pppd pid
                    if _interface.lower().startswith("pppoe") and _interface_dict["flags"] & 0x10 and _interface_dict["flags"] & 0x1: 
                        _interface_dict["up"] = "true"
                    ## http://web.mit.edu/freebsd/head/sys/net/if.h
                    ## 0x1 UP
                    ## 0x2 BROADCAST
                    ## 0x8 LOOPBACK
                    ## 0x10 POINTTOPOINT
                    ## 0x40 RUNNING
                    ## 0x100 PROMISC
                    ## 0x800 SIMPLEX
                    ## 0x8000 MULTICAST
                if _key == "media":
                    _match = re.search("\((?P<speed>\d+G?)base(?:.*?<(?P<duplex>.*?)>)?",_val)
                    if _match:
                        _interface_dict["speed"] = _match.group("speed").replace("G","000")
                        _interface_dict["duplex"] = _match.group("duplex")
                if _key == "inet":
                    _match = re.search("^(?P<ipaddr>[\d.]+)\/(?P<cidr>\d+).*?(?:vhid\s(?P<vhid>\d+)|$)",_val,re.M)
                    if _match:
                        _cidr = _match.group("cidr")
                        _ipaddr = _match.group("ipaddr")
                        _vhid = _match.group("vhid")
                        if not _vhid:
                            _interface_dict["cidr"] = _cidr ## cidr wenn kein vhid
                        ## fixme ipaddr dict / vhid dict
                if _key == "inet6":
                    _match = re.search("^(?P<ipaddr>[0-9a-f:]+)\/(?P<prefix>\d+).*?(?:vhid\s(?P<vhid>\d+)|$)",_val,re.M)
                    if _match:
                        _ipaddr = _match.group("ipaddr")
                        _prefix = _match.group("prefix")
                        _vhid = _match.group("vhid")
                        if not _vhid:
                            _interface_dict["prefix"] = _prefix
                        ## fixme ipaddr dict / vhid dict
                if _key == "carp":
                    _match = re.search("(?P<status>MASTER|BACKUP)\svhid\s(?P<vhid>\d+)\sadvbase\s(?P<base>\d+)\sadvskew\s(?P<skew>\d+)",_val,re.M)
                    if _match:
                        _carpstatus = _match.group("status")
                        _vhid = _match.group("vhid")
                        self._carp_interfaces[_vhid] = (_interface,_carpstatus)
                        _advbase = _match.group("base")
                        _advskew = _match.group("skew")
                        ## fixme vhid dict
                if _key == "id":
                    _match = re.search("priority\s(\d+)",_val)
                    if _match:
                        _interface_dict["bridge_prio"] = _match.group(1)
                if _key == "member":
                    _member = _interface_dict.get("member",[])
                    _member.append(_val.split()[0])
                    _interface_dict["member"] = _member
                if _key == "Opened":
                    try:
                        _pid = int(_val.split(" ")[-1])
                        if check_pid(_pid):
                            _interface_dict["up"] = "true"
                    except ValueError:
                        pass

            if _interface_dict["flags"] & 0x2 or _interface_dict["flags"] & 0x10 or _interface_dict["flags"] & 0x80: ## nur broadcast oder ptp
                self._all_interfaces[_interface] = _interface_dict
            else:
                continue
            #if re.search("^[*]?(pflog|pfsync|lo)\d?",_interface):
            #    continue
            if not _opnsense_ifs.get(_interface):
                continue
            for _key,_val in _interface_dict.items():
                if _key in ("mtu","ipackets","ierror","idrop","rx","opackets","oerror","tx","collisions","drop","interface_name","up","systime","phys_address","speed","duplex"):
                    if type(_val) in (str,int,float):
                        _ret.append(f"{_interface}.{_key} {_val}")

        return _ret

    def checklocal_services(self):
        _phpcode = '<?php require_once("config.inc");require_once("system.inc"); require_once("plugins.inc"); require_once("util.inc"); foreach(plugins_services() as $_service) { printf("%s;%s;%s\n",$_service["name"],$_service["description"],service_status($_service));} ?>'
        _proc = subprocess.Popen(["php"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,encoding="utf-8")
        _data,_ = _proc.communicate(input=_phpcode,timeout=15)
        _services = []
        for _service in _data.strip().split("\n"):
            _services.append(_service.split(";"))
        _num_services = len(_services)
        _stopped_services = list(filter(lambda x: x[2] != '1',_services))
        _num_stopped = len(_stopped_services)
        _num_running = _num_services - _num_stopped
        _stopped_services = ", ".join(map(lambda x: x[1],_stopped_services))
        if _num_stopped > 0:
            return [f"2 Services running_services={_num_running:.0f}|stopped_service={_num_stopped:.0f} Services: {_stopped_services} not running"]
        return [f"0 Services running_services={_num_running:.0f}|stopped_service={_num_stopped:.0f} All Services running"]

    def checklocal_carpstatus(self):
        _ret = []
        _virtual = self._config_reader().get("virtualip")
        if not _virtual:
            return []
        _virtual = _virtual.get("vip")
        if not _virtual:
            return []
        if type(_virtual) != list:
            _virtual = [_virtual]
        for _vip in _virtual:
            if _vip.get("mode") != "carp":
                continue
            _vhid = _vip.get("vhid")
            _ipaddr = _vip.get("subnet")
            _interface, _carpstatus = self._carp_interfaces.get(_vhid,(None,None))
            _carpstatus_num = 1 if _carpstatus == "MASTER" else 0
            _interface_name = self._all_interfaces.get(_interface,{}).get("interface_name",_interface)
            if int(_vip.get("advskew")) < 50:
                _status = 0 if _carpstatus == "MASTER" else 1
            else:
                _status = 0 if _carpstatus == "BACKUP" else 1
            if not _interface:
                continue
            _ret.append(f"{_status} \"CARP: {_interface_name}@{_vhid}\" master={_carpstatus_num} {_carpstatus} {_ipaddr} ({_interface})")
        return _ret

    def check_dhcp(self):
        if not os.path.exists("/var/dhcpd/var/db/dhcpd.leases"):
            return []
        _ret = ["<<<isc_dhcpd>>>"]
        _ret.append("[general]\nPID: {0}".format(self.pidof("dhcpd",-1)))
        
        _dhcpleases = open("/var/dhcpd/var/db/dhcpd.leases","r").read()
        ## FIXME 
        #_dhcpleases_dict = dict(map(lambda x: (self.ip2int(x[0]),x[1]),re.findall(r"lease\s(?P<ipaddr>[0-9.]+)\s\{.*?.\n\s+binding state\s(?P<state>\w+).*?\}",_dhcpleases,re.DOTALL)))
        _dhcpleases_dict = dict(re.findall(r"lease\s(?P<ipaddr>[0-9.]+)\s\{.*?.\n\s+binding state\s(?P<state>active).*?\}",_dhcpleases,re.DOTALL))
        _dhcpconf = open("/var/dhcpd/etc/dhcpd.conf","r").read()
        _ret.append("[pools]")
        for _subnet in re.finditer(r"subnet\s(?P<subnet>[0-9.]+)\snetmask\s(?P<netmask>[0-9.]+)\s\{.*?(?:pool\s\{.*?\}.*?)*}",_dhcpconf,re.DOTALL):
            #_cidr = bin(self.ip2int(_subnet.group(2))).count("1")
            #_available = 0
            for _pool in re.finditer("pool\s\{.*?range\s(?P<start>[0-9.]+)\s(?P<end>[0-9.]+).*?\}",_subnet.group(0),re.DOTALL):
                #_start,_end = self.ip2int(_pool.group(1)), self.ip2int(_pool.group(2))
                #_ips_in_pool = filter(lambda x: _start < x[0] < _end,_dhcpleases_dict.items())
                #pprint(_dhcpleases_dict)
                #pprint(sorted(list(map(lambda x: (self._int2ip(x[0]),x[1]),_ips_in_pool))))
                #_available += (_end - _start)
                _ret.append("{0}\t{1}".format(_pool.group(1),_pool.group(2)))
            
            #_ret.append("DHCP_{0}/{1} {2}".format(_subnet.group(1),_cidr,_available))
        
        _ret.append("[leases]")
        for _ip in sorted(_dhcpleases_dict.keys()):
            _ret.append(_ip)
        return _ret

    def checklocal_pkgaudit(self):
        try:
            _data = json.loads(self._run_cache_prog("pkg audit -F --raw=json-compact -q",cachetime=360,ignore_error=True))
            _vulns = _data.get("pkg_count",0)
            if _vulns > 0:
                _packages = ", ".join(_data.get("packages",{}).keys())
                return [f"1 Audit issues={_vulns} Pkg: {_packages} vulnerable"]
            raise
        except:
            pass
        return ["0 Audit issues=0 OK"]


    @staticmethod
    def _read_from_openvpnsocket(vpnsocket,cmd):
        _sock = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        try:
            _sock.connect(vpnsocket)
            assert (_sock.recv(4096).decode("utf-8")).startswith(">INFO")
            cmd = cmd.strip() + "\n"
            _sock.send(cmd.encode("utf-8"))
            _data = ""
            while True:
                _socket_data = _sock.recv(4096).decode("utf-8")
                _data += _socket_data
                if _data.strip().endswith("END") or _data.strip().startswith("SUCCESS:") or _data.strip().startswith("ERROR:"):
                    break
            return _data
        finally:
            if _sock:
                _sock.send("quit\n".encode("utf-8"))
            _sock.close()
            _sock = None
        return ""

    def _get_traffic(self,modul,interface,totalbytesin,totalbytesout):
        _hist_data = self._get_storedata(modul,interface)
        _slot = int(time.time())
        _slot -= _slot%60
        _hist_slot = 0
        _traffic_in = _traffic_out = 0
        if _hist_data:
            _hist_slot,_hist_bytesin, _hist_bytesout = _hist_data
            _traffic_in = int(totalbytesin -_hist_bytesin) / max(1,_slot - _hist_slot)
            _traffic_out = int(totalbytesout - _hist_bytesout) /  max(1,_slot - _hist_slot)
        if _hist_slot != _slot:
            self._set_storedata(modul,interface,(_slot,totalbytesin,totalbytesout))
        return _traffic_in,_traffic_out

    @staticmethod
    def _get_dpinger_gateway(gateway):
        _path = "/var/run/dpinger_{0}.sock".format(gateway)
        if os.path.exists(_path):
            _sock = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            try:
                _sock.connect(_path)
                _data = _sock.recv(1024).decode("utf-8").strip()
                _name, _rtt, _rttsd, _loss = re.findall("(\w+)\s(\d+)\s(\d+)\s(\d+)$",_data)[0]
                assert _name.strip() == gateway
                return int(_rtt)/1_000_000.0,int(_rttsd)/1_000_000.0, int(_loss)
            except:
                raise
        return -1,-1,-1

    def checklocal_gateway(self):
        _ret = []
        _gateways = self._config_reader().get("gateways")
        if not _gateways:
            return []
        _gateway_items = _gateways.get("gateway_item",[])
        if type(_gateway_items) != list:
            _gateway_items = [_gateway_items] if _gateway_items else []
        _interfaces = self._config_reader().get("interfaces",{})
        _ipaddresses = self.get_opnsense_ipaddr()
        for _gateway in _gateway_items:
            if type(_gateway.get("descr")) != str:
                _gateway["descr"] = _gateway.get("name")
            if _gateway.get("monitor_disable") == "1" or _gateway.get("disabled") == "1":
                continue
            _interface = _interfaces.get(_gateway.get("interface"),{})
            _gateway["realinterface"] = _interface.get("if")
            if _gateway.get("ipprotocol") == "inet":
                _gateway["ipaddr"] = _ipaddresses.get(_interface.get("if"))
            else:
                _gateway["ipaddr"] = ""
            _gateway["rtt"], _gateway["rttsd"], _gateway["loss"] = self._get_dpinger_gateway(_gateway.get("name"))
            _gateway["status"] = 0
            if _gateway.get("loss") > 0 or _gateway.get("rtt") > 100:
                _gateway["status"] = 1
            if _gateway.get("loss") > 90 or _gateway.get("loss") == -1:
                _gateway["status"] = 2

            _ret.append("{status} \"Gateway {descr}\" rtt={rtt}|rttsd={rttsd}|loss={loss} Gateway on Interface: {realinterface} {gateway}".format(**_gateway))
        return _ret

    def checklocal_openvpn(self):
        _ret = []
        _cfr = self._config_reader().get("openvpn")
        if type(_cfr) != dict:
            return _ret

        _cso = _cfr.get("openvpn-csc")
        _monitored_clients = {}
        if type(_cso) == dict:
            _cso = [_cso]
        if type(_cso) == list:
            _monitored_clients = dict(map(lambda x: (x.get("common_name").upper(),dict(x,current=[])),_cso))
            
        _now = time.time()
        _vpnclient = _cfr.get("openvpn-client",[])
        _vpnserver = _cfr.get("openvpn-server",[])
        if type(_vpnserver) != list:
            _vpnserver = [_vpnserver] if _vpnserver else []
        if type(_vpnclient) != list:
            _vpnclient = [_vpnclient] if _vpnclient else []
        for _server in _vpnserver + _vpnclient:
            ## server_tls, p2p_shared_key p2p_tls
            _server["name"] = _server.get("description").strip() if _server.get("description") else "OpenVPN_{protocoll}_{local_port}".format(**_server)

            _caref = _server.get("caref")
            _server_cert = self._get_certificate(_server.get("certref"))
            _server["status"] = 3
            _server["expiredays"] = 0
            _server["expiredate"] = "no certificate found"
            if _server_cert:
                _notvalidafter = _server_cert.get("not_valid_after",0)
                _server["expiredays"] = int((_notvalidafter - _now) / 86400)
                _server["expiredate"] = time.strftime("Cert Expire: %d.%m.%Y",time.localtime(_notvalidafter))
                if _server["expiredays"] < 61:
                    _server["status"] = 2 if _server["expiredays"] < 31 else 1
                else:
                    _server["expiredate"] = "\\n" + _server["expiredate"]

            _server["type"] = "server" if _server.get("local_port") else "client"
            if _server.get("mode") in ("p2p_shared_key","p2p_tls"):
                _unix = "/var/etc/openvpn/{type}{vpnid}.sock".format(**_server)
                try:
                    
                    _server["bytesin"], _server["bytesout"] = self._get_traffic("openvpn",
                        "SRV_{name}".format(**_server),
                        *(map(lambda x: int(x),re.findall("bytes\w+=(\d+)",self._read_from_openvpnsocket(_unix,"load-stats"))))
                    )
                    _laststate = self._read_from_openvpnsocket(_unix,"state 1").strip().split("\r\n")[-2]
                    _timestamp, _server["connstate"], _data = _laststate.split(",",2)
                    if _server["connstate"] == "CONNECTED":
                        _data = _data.split(",")
                        _server["vpn_ipaddr"] = _data[1]
                        _server["remote_ipaddr"] = _data[2]
                        _server["remote_port"] = _data[3]
                        _server["source_addr"] = _data[4]
                        _server["status"] = 0 if _server["status"] == 3 else _server["status"]
                        _ret.append('{status} "OpenVPN Connection: {name}" connections_ssl_vpn=1;;|if_in_octets={bytesin}|if_out_octets={bytesout}|expiredays={expiredays} Connected {remote_ipaddr}:{remote_port} {vpn_ipaddr} {expiredate}\Source IP: {source_addr}'.format(**_server))
                    else:
                        if _server["type"] == "client":
                            _server["status"] = 2
                            _ret.append('{status} "OpenVPN Connection: {name}" connections_ssl_vpn=0;;|if_in_octets={bytesin}|if_out_octets={bytesout}|expiredays={expiredays} {connstate} {expiredate}'.format(**_server))
                        else:
                            _server["status"] = 1 if _server["status"] != 2 else 2
                            _ret.append('{status} "OpenVPN Connection: {name}" connections_ssl_vpn=0;;|if_in_octets={bytesin}|if_out_octets={bytesout}|expiredays={expiredays} waiting on Port {local_port}/{protocol} {expiredate}'.format(**_server))
                except:
                    _ret.append('2 "OpenVPN Connection: {name}" connections_ssl_vpn=0;;|expiredays={expiredays}|if_in_octets=0|if_out_octets=0 Server down Port:/{protocol} {expiredate}'.format(**_server))
                    continue
            else:
                if not _server.get("maxclients"):
                    _max_clients = ipaddress.IPv4Network(_server.get("tunnel_network")).num_addresses -2
                    if _server.get("topology_subnet") != "yes":
                        _max_clients = max(1,int(_max_clients/4)) ## p2p
                    _server["maxclients"] = _max_clients
                try:
                    _unix = "/var/etc/openvpn/{type}{vpnid}.sock".format(**_server)
                    try:
                        
                        _server["bytesin"], _server["bytesout"] = self._get_traffic("openvpn",
                            "SRV_{name}".format(**_server),
                            *(map(lambda x: int(x),re.findall("bytes\w+=(\d+)",self._read_from_openvpnsocket(_unix,"load-stats"))))
                        )
                        _server["status"] = 0 if _server["status"] == 3 else _server["status"]
                    except:
                        _server["bytesin"], _server["bytesout"] = 0,0
                        raise
                    
                    _number_of_clients = 0
                    _now = int(time.time())
                    _response = self._read_from_openvpnsocket(_unix,"status 2")
                    for _client_match in re.finditer("^CLIENT_LIST,(.*?)$",_response,re.M):
                        _number_of_clients += 1
                        _client_raw = list(map(lambda x: x.strip(),_client_match.group(1).split(",")))
                        _client = {
                            "server"         : _server.get("name"),
                            "common_name"    : _client_raw[0],
                            "remote_ip"      : _client_raw[1].rsplit(":",1)[0], ## ipv6
                            "vpn_ip"         : _client_raw[2],
                            "vpn_ipv6"       : _client_raw[3],
                            "bytes_received" : int(_client_raw[4]),
                            "bytes_sent"     : int(_client_raw[5]),
                            "uptime"         : _now - int(_client_raw[7]),
                            "username"       : _client_raw[8] if _client_raw[8] != "UNDEF" else _client_raw[0],
                            "clientid"       : int(_client_raw[9]),
                            "cipher"         : _client_raw[11].strip("\r\n")
                        }
                        if _client["username"].upper() in _monitored_clients:
                            _monitored_clients[_client["username"].upper()]["current"].append(_client)

                    _server["clientcount"] = _number_of_clients
                    _ret.append('{status} "OpenVPN Server: {name}" connections_ssl_vpn={clientcount};;{maxclients}|if_in_octets={bytesin}|if_out_octets={bytesout}|expiredays={expiredays} {clientcount}/{maxclients} Connections Port:{local_port}/{protocol} {expiredate}'.format(**_server))
                except:
                    _ret.append('2 "OpenVPN Server: {name}" connections_ssl_vpn=0;;{maxclients}|expiredays={expiredays}|if_in_octets=0|if_out_octets=0| Server down Port:{local_port}/{protocol} {expiredate}'.format(**_server))

        for _client in _monitored_clients.values():
            _current_conn = _client.get("current",[])
            if _client.get("disable") == 1:
                continue
            if not _client.get("description"):
                _client["description"] = _client.get("common_name")
            _client["description"] = _client["description"].strip(" \r\n")
            _client["expiredays"] = 0
            _client["expiredate"] = "no certificate found"
            _client["status"] = 3
            _cert = self._get_certificate_by_cn(_client.get("common_name"))
            if _cert:
                _notvalidafter = _cert.get("not_valid_after")
                _client["expiredays"] = int((_notvalidafter - _now) / 86400)
                _client["expiredate"] = time.strftime("Cert Expire: %d.%m.%Y",time.localtime(_notvalidafter))
                if _client["expiredays"] < 61:
                    _client["status"] = 2 if _client["expiredays"] < 31 else 1
                else:
                    _client["expiredate"] = "\\n" + _client["expiredate"]

            if _current_conn:
                _client["uptime"] = max(map(lambda x: x.get("uptime"),_current_conn))
                _client["count"] = len(_current_conn)
                _client["bytes_received"], _client["bytes_sent"] = self._get_traffic("openvpn",
                    "CL_{description}".format(**_client),
                    sum(map(lambda x: x.get("bytes_received"),_current_conn)),
                    sum(map(lambda x: x.get("bytes_sent"),_current_conn))
                )
                _client["status"] = 0 if _client["status"] == 3 else _client["status"]
                _client["longdescr"] = ""
                for _conn in _current_conn:
                    _client["longdescr"] += "Server:{server} {remote_ip}:{vpn_ip} {cipher} ".format(**_conn)
                _ret.append('{status} "OpenVPN Client: {description}" connectiontime={uptime}|connections_ssl_vpn={count}|if_in_octets={bytes_received}|if_out_octets={bytes_sent}|expiredays={expiredays} {longdescr} {expiredate}'.format(**_client))
            else:
                _ret.append('{status} "OpenVPN Client: {description}" connectiontime=0|connections_ssl_vpn=0|if_in_octets=0|if_out_octets=0|expiredays={expiredays} Nicht verbunden {expiredate}'.format(**_client))
        return _ret

    def checklocal_ipsec(self):
        _ret = []
        _ipsec_config = self._config_reader().get("ipsec")
        if type(_ipsec_config) != dict:
            return []
        _phase1config = _ipsec_config.get("phase1")
        if type(_phase1config) != list:
            _phase1config = [_phase1config]
        _json_data = self._run_prog("/usr/local/opnsense/scripts/ipsec/list_status.py")
        if len(_json_data.strip()) < 20:
            return []
        for _conid,_con in json.loads(_json_data).items():
            _conid = _conid[3:]
            try:
                _config = next(filter(lambda x: x.get("ikeid") == _conid,_phase1config))
            except StopIteration:
                continue
            _childsas = None
            _con["status"] = 2
            _con["bytes_received"] = 0
            _con["bytes_sent"] = 0
            _con["remote-host"] = "unknown"
            for _sas in _con.get("sas",[]):
                _con["state"] = _sas.get("state","unknown")
                if not _con["local-id"]:
                    _con["status"] = 1
                    _con["state"] = "ABANDOMED"
                    _con["local-id"] = _sas.get("local-id")
                if not _con["remote-id"]:
                    _con["status"] = 1
                    _con["remote-id"] = _sas.get("remote-id")
                    _con["state"] = "ABANDOMED"

                _childsas = filter(lambda x: x.get("state") == "INSTALLED",_sas.get("child-sas").values())
                _con["remote-name"] = _config.get("descr",_con.get("remote-id"))

                try:
                    _childsas = next(_childsas)
                    _con["remote-host"] = _sas.get("remote-host")
                    _connecttime = max(1,int(_childsas.get("install-time",0)))
                    _con["bytes_received"] = int(int(_childsas.get("bytes-in",0)) /_connecttime)
                    _con["bytes_sent"] = int(int(_childsas.get("bytes-out",0)) / _connecttime)
                    _con["life-time"] = int(_childsas.get("life-time",0))

                    _con["status"] = 0 if _con["status"] == 2 else 1
                    break
                except StopIteration:
                    pass
            try:
                if _childsas:
                    _ret.append("{status} \"IPsec Tunnel: {remote-name}\" if_in_octets={bytes_received}|if_out_octets={bytes_sent}|lifetime={life-time} {state} {local-id} - {remote-id}({remote-host})".format(**_con))
                else:
                    _ret.append("{status} \"IPsec Tunnel: {remote-name}\" if_in_octets=0|if_out_octets=0|lifetime=0 not connected {local-id} - {remote-id}({remote-host})".format(**_con))
            except KeyError: ##fixme error melden
                continue
        return _ret

    def checklocal_wireguard(self):
        _ret = []
        try:
            _clients = self._config_reader().get("OPNsense").get("wireguard").get("client").get("clients").get("client")
            if type(_clients) != list:
                _clients = [_clients] if _clients else []
            _clients = dict(map(lambda x: (x.get("pubkey"),x),_clients))
        except:
            return []

        _now = time.time()
        for _client in _clients.values(): ## fill defaults
            _client["interface"] = ""
            _client["endpoint"]  = ""
            _client["last_handshake"]  = 0
            _client["bytes_received"]  = 0
            _client["bytes_sent"] = 0
            _client["status"] = 2

        _dump = self._run_prog(["wg","show","all","dump"]).strip()
        for _line in _dump.split("\n"):
            _values = _line.split("\t")
            if len(_values) != 9:
                continue
            _client = _clients.get(_values[1].strip())
            if not _client:
                continue
            _client["interface"] = _values[0].strip()
            _client["endpoint"]  = _values[3].strip().rsplit(":",1)[0]
            _client["last_handshake"]  = int(_values[5].strip())
            _client["bytes_received"], _client["bytes_sent"]  = self._get_traffic("wireguard","",int(_values[6].strip()),int(_values[7].strip()))
            _client["status"] = 2 if _now - _client["last_handshake"] > 300 else 0  ## 5min timeout

        for _client in _clients.values():
            if _client.get("status") == 2 and _client.get("endpoint") != "":
                _client["endpoint"] = "last IP:" + _client["endpoint"]
            _ret.append('{status} "WireGuard Client: {name}" if_in_octets={bytes_received}|if_out_octets={bytes_sent} {interface}: {endpoint} - {tunneladdress}'.format(**_client))

        return _ret

    def checklocal_unbound(self):
        _ret = []
        try:
            _output = self._run_prog(["/usr/local/sbin/unbound-control", "-c", "/var/unbound/unbound.conf", "stats_noreset"])
            _unbound_stat = dict(
                map(
                    lambda x: (x[0].replace(".","_"),float(x[1])),
                        re.findall("total\.([\w.]+)=([\d.]+)",_output)
                )
            )
            _ret.append("0 \"Unbound DNS\" dns_successes={num_queries:.0f}|dns_recursion={num_recursivereplies:.0f}|dns_cachehits={num_cachehits:.0f}|dns_cachemiss={num_cachemiss:.0f}|avg_response_time={recursion_time_avg} Unbound running".format(**_unbound_stat))
        except:
            _ret.append("2 \"Unbound DNS\" dns_successes=0|dns_recursion=0|dns_cachehits=0|dns_cachemiss=0|avg_response_time=0 Unbound not running")
        return _ret

    def checklocal_acmeclient(self):
        _ret = []
        _now = time.time()
        try:
            _acmecerts = self._config_reader().get("OPNsense").get("AcmeClient").get("certificates").get("certificate")
            if type(_acmecerts) == dict:
                _acmecerts = [_acmecerts]
        except:
            _acmecerts = []
        for _cert_info in _acmecerts:
            if _cert_info.get("enabled") != "1":
                continue
            if not _cert_info.get("description"):
                _cert_info["description"] = _cert_info.get("name","unknown")
            _certificate = self._get_certificate(_cert_info.get("certRefId"))
            _cert_info["status"] = 1
            if _certificate:
                if type(_certificate) != dict:
                    _certificate = {}
                _expiredays = _certificate.get("not_valid_after",_now) - _now
                _not_valid_before = _certificate.get("not_valid_before",_cert_info.get("lastUpdate"))
                _certificate_age = _now - int(_not_valid_before if _not_valid_before else _now)
                _cert_info["age"] = int(_certificate_age)
                if _cert_info.get("statusCode") == "200":
                    if _certificate_age > float(_cert_info.get("renewInterval","inf")):
                        _cert_info["status"] = 0
                if _expiredays < 10:
                    _cert_info["status"] = 2
                _cert_info["issuer"] = _certificate.get("issuer")
                _cert_info["lastupdatedate"] = time.strftime("%d.%m.%Y",time.localtime(int(_cert_info.get("lastUpdate",0))))
                _cert_info["expiredate"] = time.strftime("%d.%m.%Y",time.localtime(_certificate.get("not_valid_after",0)))
                _ret.append("{status} \"ACME Cert: {description}\" age={age} Last Update: {lastupdatedate} Status: {statusCode} Cert expire: {expiredate}".format(**_cert_info))
            else:
                if _cert_info.get("statusCode") == "100":
                    _ret.append("1 \"ACME Cert: {description}\" age=0 Status: pending".format(**_cert_info))
                else:
                    _ret.append("2 \"ACME Cert: {description}\" age=0 Error Status: {statusCode}".format(**_cert_info))
        return _ret

    def _read_nginx_socket(self):
        session = requests.Session()
        session.mount("http://nginx/", NginxAdapter())
        response = session.get("http://nginx/vts")
        return response.json()

    def checklocal_nginx(self):
        _ret = []
        _config = self._config_reader().get("OPNsense").get("Nginx")
        if type(_config) != dict:
            return []
        _upstream_config = _config.get("upstream")
        if type(_upstream_config) != list:
            _upstream_config = [_upstream_config]

        try:        
            _data = self._read_nginx_socket()
        except (requests.exceptions.ConnectionError,FileNotFoundError):
            return [] ## no socket
        for _serverzone,_serverzone_data in _data.get("serverZones",{}).items():
            if _serverzone == "*":
                continue
            _serverzone_data["serverzone"] = _serverzone
            _ret.append("0 \"Nginx Zone: {serverzone}\" bytesin={inBytes}|bytesout={outBytes} OK".format(**_serverzone_data))
        for _upstream,_upstream_data in _data.get("upstreamZones",{}).items():
            if _upstream.startswith("upstream"):
                #_upstream_config_data["status"] = _upstream_data.get("")
                _upstream_config_data = next(filter(lambda x: x.get("@uuid","").replace("-","") == _upstream[8:],_upstream_config))
                #_upstream_data["upstream"] = _upstream_config_data.get("description",_upstream)
                upstream=_upstream_config_data.get("description",_upstream)
                _ret.append(f"0 \"Nginx Upstream: {upstream}\" - OK") ## fixme

        return _ret

    def check_haproxy(self):
        _ret = ["<<<haproxy:sep(44)>>>"]
        _path = "/var/run/haproxy.socket"
        try:
            _haproxy_servers = dict(map(lambda x: (x.get("@uuid"),x),self._config_reader().get("OPNsense").get("HAProxy").get("servers").get("server")))
            _healthcheck_servers = []
            for _backend in self._config_reader().get("OPNsense").get("HAProxy").get("backends").get("backend"):
                if _backend.get("healthCheckEnabled") == "1" and _backend.get("healthCheck") != None:
                    for _server_id in _backend.get("linkedServers","").split(","):
                        _server = _haproxy_servers.get(_server_id)
                        _healthcheck_servers.append("{0},{1}".format(_backend.get("name",""),_server.get("name","")))
        except:
            return []
        if os.path.exists(_path):
            _sock = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            _sock.connect(_path)
            _sock.send("show stat\n".encode("utf-8"))
            _data = ""
            while True:
                _sockdata = _sock.recv(4096)
                if not _sockdata:
                    break
                _data += _sockdata.decode("utf-8")
            
            for _line in _data.split("\n"):
                _linedata = _line.split(",")
                if len(_linedata) < 33:
                    continue
                #pprint(list(enumerate(_linedata)))
                if _linedata[32] == "2":
                    if "{0},{1}".format(*_linedata) not in _healthcheck_servers:
                        continue ## ignore backends check disabled
                _ret.append(_line)
        return _ret

    def check_smartinfo(self):
        if not os.path.exists("/usr/local/sbin/smartctl"):
            return []
        REGEX_DISCPATH = re.compile("(sd[a-z]+|da[0-9]+|nvme[0-9]+|ada[0-9]+)$")
        _ret = ["<<<disk_smart_info:sep(124)>>>"]
        for _dev in filter(lambda x: REGEX_DISCPATH.match(x),os.listdir("/dev/")):
            try:
                _ret.append(str(smart_disc(_dev)))
            except:
                pass
        return _ret

    def check_ipmi(self):
        if not os.path.exists("/usr/local/bin/ipmitool"):
            return []
        _ret = ["<<<ipmi:sep(124)>>>"]
        _out = self._run_prog("/usr/local/bin/ipmitool sensor list")
        _ret += re.findall("^(?!.*\sna\s.*$).*",_out,re.M)
        return _ret

    def check_df(self):
        _ret = ["<<<df>>>"]
        _ret += self._run_prog("df -kTP -t ufs").split("\n")[1:]
        return _ret

    def check_kernel(self):
        _ret = ["<<<kernel>>>"]
        _out = self._run_prog("sysctl vm.stats",timeout=10)
        _kernel = dict([_v.split(": ") for _v in _out.split("\n") if len(_v.split(": ")) == 2])
        _ret.append("{0:.0f}".format(time.time()))
        _ret.append("cpu {0} {1} {2} {4} {3}".format(*(self._run_prog("sysctl -n kern.cp_time","").split(" "))))
        _ret.append("ctxt {0}".format(_kernel.get("vm.stats.sys.v_swtch")))
        _sum = sum(map(lambda x: int(x[1]),(filter(lambda x: x[0] in ("vm.stats.vm.v_forks","vm.stats.vm.v_vforks","vm.stats.vm.v_rforks","vm.stats.vm.v_kthreads"),_kernel.items()))))
        _ret.append("processes {0}".format(_sum))
        return _ret

    def check_temperature(self):
        _ret = ["<<<lnx_thermal:sep(124)>>>"]
        _out = self._run_prog("sysctl dev.cpu",timeout=10)
        _cpus = dict([_v.split(": ") for _v in _out.split("\n") if len(_v.split(": ")) == 2])
        _cpu_temperatures = list(map(
            lambda x: float(x[1].replace("C","")),
            filter(
                lambda x: x[0].endswith("temperature"),
                _cpus.items()
            )
        ))
        if _cpu_temperatures:
            _cpu_temperature = int(max(_cpu_temperatures) * 1000)
            _ret.append(f"CPU|enabled|unknown|{_cpu_temperature}")
        
        _count = 0
        for _tempsensor in self._available_sysctl_temperature_list:
            _out = self._run_prog(f"sysctl -n {_tempsensor}",timeout=10)
            if _out:
                try:
                    _zone_temp = int(float(_out.replace("C","")) * 1000)
                except ValueError:
                    _zone_temp = None
                if _zone_temp:
                    if _tempsensor.find(".pchtherm.") > -1:
                        _ret.append(f"thermal_zone{_count}|enabled|unknown|{_zone_temp}|111000|critical|108000|passive")
                    else:
                        _ret.append(f"thermal_zone{_count}|enabled|unknown|{_zone_temp}")
                    _count += 1
        if len(_ret) < 2:
           return []
        return _ret

    def check_mem(self):
        _ret = ["<<<statgrab_mem>>>"]
        _pagesize = int(self._run_prog("sysctl -n hw.pagesize"))
        _out = self._run_prog("sysctl vm.stats",timeout=10)
        _mem = dict(map(lambda x: (x[0],int(x[1])) ,[_v.split(": ") for _v in _out.split("\n") if len(_v.split(": ")) == 2]))
        _mem_cache = _mem.get("vm.stats.vm.v_cache_count") * _pagesize
        _mem_free = _mem.get("vm.stats.vm.v_free_count") * _pagesize
        _mem_inactive = _mem.get("vm.stats.vm.v_inactive_count") * _pagesize
        _mem_total = _mem.get("vm.stats.vm.v_page_count") * _pagesize
        _mem_avail = _mem_inactive + _mem_cache + _mem_free
        _mem_used = _mem_total - _mem_avail # fixme mem.hw
        _ret.append("mem.cache {0}".format(_mem_cache))
        _ret.append("mem.free {0}".format(_mem_free))
        _ret.append("mem.total {0}".format(_mem_total))
        _ret.append("mem.used {0}".format(_mem_used))
        _ret.append("swap.free 0")
        _ret.append("swap.total 0")
        _ret.append("swap.used 0")
        
        return _ret

    def check_zpool(self):
        _ret = ["<<<zpool_status>>>"]
        try:
            for _line in self._run_prog("zpool status -x").split("\n"):
                if _line.find("errors: No known data errors") == -1:
                    _ret.append(_line)
        except:
            return []
        return _ret

    def check_zfs(self):
        _ret = ["<<<zfsget>>>"]
        _ret.append(self._run_prog("zfs get -t filesystem,volume -Hp name,quota,used,avail,mountpoint,type"))
        _ret.append("[df]")
        _ret.append(self._run_prog("df -kP -t zfs"))
        _ret.append("<<<zfs_arc_cache>>>")
        _ret.append(self._run_prog("sysctl -q kstat.zfs.misc.arcstats").replace("kstat.zfs.misc.arcstats.","").replace(": "," = ").strip())
        return _ret

    def check_mounts(self):
        _ret = ["<<<mounts>>>"]
        _ret.append(self._run_prog("mount -p -t ufs").strip())
        return _ret

    def check_cpu(self):
        _ret = ["<<<cpu>>>"]
        _loadavg = self._run_prog("sysctl -n vm.loadavg").strip("{} \n")
        _proc = self._run_prog("top -b -n 1").split("\n")[1].split(" ")
        _proc = "{0}/{1}".format(_proc[3],_proc[0])
        _lastpid = self._run_prog("sysctl -n kern.lastpid").strip(" \n")
        _ncpu = self._run_prog("sysctl -n hw.ncpu").strip(" \n")
        _ret.append(f"{_loadavg} {_proc} {_lastpid} {_ncpu}")
        return _ret

    def check_netctr(self):
        _ret = ["<<<netctr>>>"]
        _out = self._run_prog("netstat -inb")
        for _line in re.finditer("^(?!Name|lo|plip)(?P<iface>\w+)\s+(?P<mtu>\d+).*?Link.*?\s+.*?\s+(?P<inpkts>\d+)\s+(?P<inerr>\d+)\s+(?P<indrop>\d+)\s+(?P<inbytes>\d+)\s+(?P<outpkts>\d+)\s+(?P<outerr>\d+)\s+(?P<outbytes>\d+)\s+(?P<coll>\d+)$",_out,re.M):
            _ret.append("{iface} {inbytes} {inpkts} {inerr} {indrop} 0 0 0 0 {outbytes} {outpkts} {outerr} 0 0 0 0 0".format(**_line.groupdict()))
        return _ret

    def check_ntp(self):
        _ret = ["<<<ntp>>>"]
        for _line in self._run_prog("ntpq -np",timeout=30).split("\n")[2:]:
            if _line.strip():
                _ret.append("{0} {1}".format(_line[0],_line[1:]))
        return _ret
        

    def check_tcp(self):
        _ret = ["<<<tcp_conn_stats>>>"]
        _out = self._run_prog("netstat -na")
        counts = Counter(re.findall("ESTABLISHED|LISTEN",_out))
        for _key,_val in counts.items():
            _ret.append(f"{_key} {_val}")
        return _ret

    def check_ps(self):
        _ret = ["<<<ps>>>"]
        _out = self._run_prog("ps ax -o state,user,vsz,rss,pcpu,command")
        for _line in re.finditer("^(?P<stat>\w+)\s+(?P<user>\w+)\s+(?P<vsz>\d+)\s+(?P<rss>\d+)\s+(?P<cpu>[\d.]+)\s+(?P<command>.*)$",_out,re.M):
            _ret.append("({user},{vsz},{rss},{cpu}) {command}".format(**_line.groupdict()))
        return _ret
        

    def check_uptime(self):
        _ret = ["<<<uptime>>>"]
        _uptime_sec = time.time() - int(self._run_prog("sysctl -n kern.boottime").split(" ")[3].strip(" ,"))
        _idle_sec = re.findall("(\d+):[\d.]+\s+\[idle\]",self._run_prog("ps axw"))[0]
        _ret.append(f"{_uptime_sec} {_idle_sec}")
        return _ret

    def _run_prog(self,cmdline="",*args,shell=False,timeout=60,ignore_error=False):
        if type(cmdline) == str:
            _process = shlex.split(cmdline,posix=True)
        else:
            _process = cmdline
        try:
            return subprocess.check_output(_process,encoding="utf-8",shell=shell,stderr=subprocess.DEVNULL,timeout=timeout)
        except subprocess.CalledProcessError as e:
            if ignore_error:
                return e.stdout
            return ""
        except subprocess.TimeoutExpired:
            return ""

    def _run_cache_prog(self,cmdline="",cachetime=10,*args,shell=False,ignore_error=False):
        if type(cmdline) == str:
            _process = shlex.split(cmdline,posix=True)
        else:
            _process = cmdline
        _process_id = "".join(_process)
        _runner = self._check_cache.get(_process_id)
        if _runner == None:
            _runner = checkmk_cached_process(_process,shell=shell,ignore_error=ignore_error)
            self._check_cache[_process_id] = _runner
        return _runner.get(cachetime)

class checkmk_cached_process(object):
    def __init__(self,process,shell=False,ignore_error=False):
        self._processs = process
        self._islocal = os.path.dirname(process[0]).startswith(LOCALDIR)
        self._shell = shell
        self._ignore_error = ignore_error
        self._mutex = threading.Lock()
        with self._mutex:
            self._data = (0,"")
            self._thread = None

    def _runner(self,timeout):
        try:
            _data = subprocess.check_output(self._processs,shell=self._shell,encoding="utf-8",stderr=subprocess.DEVNULL,timeout=timeout)
        except subprocess.CalledProcessError as e:
            if self._ignore_error:
                _data = e.stdout
            else:
                _data = ""
        except subprocess.TimeoutExpired:
            _data = ""
        with self._mutex:
            self._data = (int(time.time()),_data)
            self._thread = None

    def get(self,cachetime):
        with self._mutex:
            _now = time.time()
            _mtime = self._data[0]
        if _now - _mtime > cachetime or cachetime == 0:
            if not self._thread:
                if cachetime > 0:
                    _timeout = cachetime*2-1
                else:
                    _timeout = None
                with self._mutex:
                    self._thread = threading.Thread(target=self._runner,args=[_timeout])
                self._thread.start()

            self._thread.join(30) ## waitmax
        with self._mutex:
            _mtime, _data = self._data
        if not _data.strip():
            return ""
        if self._islocal:
            _data = "".join([f"cached({_mtime},{cachetime}) {_line}" for _line in _data.splitlines(True) if len(_line.strip()) > 0])
        else:
            _data = re.sub("\B[<]{3}(.*?)[>]{3}\B",f"<<<\\1:cached({_mtime},{cachetime})>>>",_data)
        return _data

class checkmk_server(TCPServer,checkmk_checker):
    def __init__(self,port,pidfile,user,onlyfrom=None,encryptionkey=None,skipcheck=None,**kwargs):
        self.pidfile = pidfile
        self.onlyfrom = onlyfrom.split(",") if onlyfrom else None
        self.skipcheck = skipcheck.split(",") if skipcheck else []
        self._available_sysctl_list = self._run_prog("sysctl -aN").split()
        self._available_sysctl_temperature_list = list(filter(lambda x: x.lower().find("temperature") > -1 and x.lower().find("cpu") == -1,self._available_sysctl_list))
        self.encryptionkey = encryptionkey
        self._mutex = threading.Lock()
        self.user = pwd.getpwnam(user)
        self.allow_reuse_address = True
        TCPServer.__init__(self,("",port),checkmk_handler,bind_and_activate=False)

    def _change_user(self):
        _, _, _uid, _gid, _, _, _ = self.user
        if os.getuid() != _uid:
            os.setgid(_gid)
            os.setuid(_uid)

    def verify_request(self, request, client_address):
        if self.onlyfrom and client_address[0] not in self.onlyfrom:
            return False
        return True

    def server_start(self):
        log("starting checkmk_agent\n")
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGHUP, self._signal_handler)
        self._change_user()
        try:
            self.server_bind()
            self.server_activate()
        except:
            self.server_close()
            raise
        try:
            self.serve_forever()
        except KeyboardInterrupt:
            sys.stdout.flush()
            sys.stdout.write("\n")
            pass

    def _signal_handler(self,signum,*args):
        if signum in (signal.SIGTERM,signal.SIGINT):
            log("stopping checkmk_agent")
            threading.Thread(target=self.shutdown,name='shutdown').start()
            sys.exit(0)
        log("checkmk_agent running")

    def daemonize(self):
        try:
            pid = os.fork()
            if pid > 0:
                ## first parent
                sys.exit(0)
        except OSError as e:
            log("err","Fork failed")
            sys.exit(1)
        os.chdir("/")
        os.setsid()
        os.umask(0)
        try:
            pid = os.fork()
            if pid > 0:
                ## second
                sys.exit(0)
        except OSError as e:
            log("err","Fork 2 failed")
            sys.exit(1)
        sys.stdout.flush()
        sys.stderr.flush()
        self._redirect_stream(sys.stdin,None)
        self._redirect_stream(sys.stdout,None)
        self._redirect_stream(sys.stderr,None)
        with open(self.pidfile,"wt") as _pidfile:
            _pidfile.write(str(os.getpid()))
        os.chown(self.pidfile,self.user[2],self.user[3])
        try:
            self.server_start()
        finally:
            try:
                os.remove(self.pidfile)
            except:
                pass
        
    @staticmethod
    def _redirect_stream(system_stream,target_stream):
        if target_stream is None:
            target_fd = os.open(os.devnull, os.O_RDWR)
        else:
            target_fd = target_stream.fileno()
        os.dup2(target_fd, system_stream.fileno())

    def __del__(self):
        pass ## todo


REGEX_SMART_VENDOR = re.compile(r"^\s*(?P<num>\d+)\s(?P<name>[-\w]+).*\s{2,}(?P<value>[\w\/() ]+)$",re.M)
REGEX_SMART_DICT = re.compile(r"^(.*?):\s*(.*?)$",re.M)
class smart_disc(object):
    def __init__(self,device):
        self.device = device
        MAPPING = {
            "Model Family"      : ("model_family"       ,lambda x: x),
            "Model Number"      : ("model_family"       ,lambda x: x),
            "Product"           : ("model_family"       ,lambda x: x),
            "Vendor"            : ("vendor"             ,lambda x: x),
            "Revision"          : ("revision"           ,lambda x: x),
            "Device Model"      : ("model_type"         ,lambda x: x),
            "Serial Number"     : ("serial_number"      ,lambda x: x),
            "Serial number"     : ("serial_number"      ,lambda x: x),
            "Firmware Version"  : ("firmware_version"   ,lambda x: x),
            "User Capacity"     : ("capacity"           ,lambda x: x.split(" ")[0].replace(",","")),
            "Total NVM Capacity": ("capacity"           ,lambda x: x.split(" ")[0].replace(",","")),
            "Rotation Rate"     : ("rpm"                ,lambda x: x.replace(" rpm","")),
            "Form Factor"       : ("formfactor"         ,lambda x: x),
            "SATA Version is"   : ("transport"          ,lambda x: x.split(",")[0]),
            "Transport protocol": ("transport"          ,lambda x: x),
            "SMART support is"  : ("smart"              ,lambda x: int(x.lower() == "enabled")),
            "Critical Warning"  : ("critical"           ,lambda x: self._saveint(x,base=16)),
            "Temperature"       : ("temperature"        ,lambda x: x.split(" ")[0]),
            "Data Units Read"   : ("data_read_bytes"    ,lambda x: x.split(" ")[0].replace(",","")),
            "Data Units Written": ("data_write_bytes"   ,lambda x: x.split(" ")[0].replace(",","")),
            "Power On Hours"    : ("poweronhours"       ,lambda x: x.replace(",","")),
            "Power Cycles"      : ("powercycles"        ,lambda x: x.replace(",","")),
            "NVMe Version"      : ("transport"          ,lambda x: f"NVMe {x}"),
            "Raw_Read_Error_Rate"   : ("error_rate"     ,lambda x: x.replace(",","")),
            "Reallocated_Sector_Ct" : ("reallocate"     ,lambda x: x.replace(",","")),
            "Seek_Error_Rate"       : ("seek_error_rate",lambda x: x.replace(",","")),
            "Power_Cycle_Count"     : ("powercycles"        ,lambda x: x.replace(",","")),
            "Temperature_Celsius"   : ("temperature"        ,lambda x: x.split(" ")[0]),
            "UDMA_CRC_Error_Count"  : ("udma_error"         ,lambda x: x.replace(",","")),
            "Offline_Uncorrectable" : ("uncorrectable"      ,lambda x: x.replace(",","")),
            "Power_On_Hours"        : ("poweronhours"       ,lambda x: x.replace(",","")),
            "Spin_Retry_Count"      : ("spinretry"          ,lambda x: x.replace(",","")),
            "Current_Pending_Sector": ("pendingsector"      ,lambda x: x.replace(",","")),
            "Current Drive Temperature"         : ("temperature"        ,lambda x: x.split(" ")[0]),
            "Reallocated_Event_Count"           : ("reallocate_ev"      ,lambda x: x.split(" ")[0]),
            "Warning  Comp. Temp. Threshold"    : ("temperature_warn"   ,lambda x: x.split(" ")[0]),
            "Critical Comp. Temp. Threshold"    : ("temperature_crit"   ,lambda x: x.split(" ")[0]),
            "Media and Data Integrity Errors"   : ("media_errors"       ,lambda x: x),
            "Airflow_Temperature_Cel"           : ("temperature"        ,lambda x: x),
            "SMART overall-health self-assessment test result" : ("smart_status" ,lambda x: int(x.lower() == "passed")),
            "SMART Health Status"   : ("smart_status" ,lambda x: int(x.lower() == "ok")),
        }
        self._get_data()
        for _key, _value in REGEX_SMART_DICT.findall(self._smartctl_output):
            if _key in MAPPING.keys():
                _map = MAPPING[_key]
                setattr(self,_map[0],_map[1](_value))

        for _vendor_num,_vendor_text,_value in REGEX_SMART_VENDOR.findall(self._smartctl_output):
            if _vendor_text in MAPPING.keys():
                _map = MAPPING[_vendor_text]
                setattr(self,_map[0],_map[1](_value))

    def _saveint(self,val,base=10):
        try:
            return int(val,base)
        except (TypeError,ValueError):
            return 0

    def _get_data(self):
        try:
            self._smartctl_output = subprocess.check_output(["smartctl","-a","-n","standby", f"/dev/{self.device}"],encoding=sys.stdout.encoding,timeout=10)
        except subprocess.CalledProcessError as e:
            if e.returncode & 0x1:
                raise
            _status = ""
            self._smartctl_output = e.output
            if e.returncode & 0x2:
                _status = "SMART Health Status:  CRC Error"
            if e.returncode & 0x4:
                _status = "SMART Health Status:  PREFAIL"
            if e.returncode & 0x3:
                _status = "SMART Health Status:  DISK FAILING"
                
            self._smartctl_output += f"\n{_status}\n"
        except subprocess.TimeoutExpired:
            self._smartctl_output += "\nSMART smartctl Timeout\n"

    def __str__(self):
        _ret = []
        if not getattr(self,"model_type",None):
            self.model_type = getattr(self,"model_family","unknown")
        for _k,_v in self.__dict__.items():
            if _k.startswith("_") or _k in ("device"): 
                continue
            _ret.append(f"{self.device}|{_k}|{_v}")
        return "\n".join(_ret)

if __name__ == "__main__":
    import argparse
    class SmartFormatter(argparse.HelpFormatter):

        def _split_lines(self, text, width):
            if text.startswith('R|'):
                return text[2:].splitlines()  
            # this is the RawTextHelpFormatter._split_lines
            return argparse.HelpFormatter._split_lines(self, text, width)
    _checks_available = sorted(list(map(lambda x: x.split("_")[1],filter(lambda x: x.startswith("check_") or x.startswith("checklocal_"),dir(checkmk_checker)))))
    _ = lambda x: x
    _parser = argparse.ArgumentParser(f"checkmk_agent for opnsense\nVersion: {__VERSION__}\n##########################################\n", formatter_class=SmartFormatter)
    _parser.add_argument("--port",type=int,default=6556,
        help=_("Port checkmk_agent listen"))
    _parser.add_argument("--start",action="store_true",
        help=_("start the daemon"))
    _parser.add_argument("--stop",action="store_true",
        help=_("stop the daemon"))
    _parser.add_argument("--nodaemon",action="store_true",
        help=_("run in foreground"))
    _parser.add_argument("--status",action="store_true",
        help=_("show status if running"))
    _parser.add_argument("--config",type=str,dest="configfile",default=CHECKMK_CONFIG,
        help=_("path to config file"))
    _parser.add_argument("--user",type=str,default="root",
        help=_(""))
    _parser.add_argument("--encrypt",type=str,dest="encryptionkey",
        help=_("Encryption password (do not use from cmdline)"))
    _parser.add_argument("--pidfile",type=str,default="/var/run/checkmk_agent.pid",
        help=_(""))
    _parser.add_argument("--onlyfrom",type=str,
        help=_("comma seperated ip addresses to allow"))
    _parser.add_argument("--skipcheck",type=str,
        help=_("R|comma seperated checks that will be skipped \n{0}".format("\n".join([", ".join(_checks_available[i:i+10]) for i in range(0,len(_checks_available),10)]))))
    _parser.add_argument("--debug",action="store_true",
        help=_("debug Ausgabe"))
    args = _parser.parse_args()
    if args.configfile and os.path.exists(args.configfile):
        for _k,_v in re.findall(f"^(\w+):\s*(.*?)(?:\s+#|$)",open(args.configfile,"rt").read(),re.M):
            if _k == "port":
                args.port = int(_v)
            if _k == "encrypt":
                args.encryptionkey = _v
            if _k == "onlyfrom":
                args.onlyfrom = _v
            if _k == "skipcheck":
                args.skipcheck = _v
            if _k.lower() == "localdir":
                LOCALDIR = _v
            if _k.lower() == "spooldir":
                SPOOLDIR = _v

    _server = checkmk_server(**args.__dict__)
    _pid = None
    try:
        with open(args.pidfile,"rt") as _pidfile:
            _pid = int(_pidfile.read())
    except (FileNotFoundError,IOError):
        _out = subprocess.check_output(["sockstat", "-l", "-p", str(args.port),"-P", "tcp"],encoding=sys.stdout.encoding)
        try:
            _pid = int(re.findall("\s(\d+)\s",_out.split("\n")[1])[0])
        except (IndexError,ValueError):
            pass
    if args.start:
        if _pid:
            try:
                os.kill(_pid,0)
            except OSError:
                pass
            else:
                log("err",f"allready running with pid {_pid}")
                sys.exit(1)
        _server.daemonize()

    elif args.status:
        if not _pid:
            log("Not running\n")
        else:
            os.kill(int(_pid),signal.SIGHUP)
    elif args.stop:
        if not _pid:
            log("Not running\n")
            sys.exit(1)
        os.kill(int(_pid),signal.SIGTERM)

    elif args.debug:
        sys.stdout.write(_server.do_checks(debug=True).decode(sys.stdout.encoding))
        sys.stdout.flush()
    elif args.nodaemon:
        _server.server_start()
    else:
#        _server.server_start()
## default start daemon
        if _pid:
            try:
                os.kill(_pid,0)
            except OSError:
                pass
            else:
                log(f"allready running with pid {_pid}")
                sys.exit(1)
        _server.daemonize()
