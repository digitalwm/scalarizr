'''
Created on Dec 23, 2009

@author: Dmytro Korsakov
'''
import binascii
import logging
import sys
import urllib
import urllib2
import xml.dom.minidom as dom
import time

from scalarizr import util
from scalarizr.util import cryptotool
from scalarizr.util import urltool



class QueryEnvError(Exception):
	pass

class QueryEnvService(object):
	_logger = None
	
	url = None
	api_version = None
	key_path = None
	server_id = None
	
	def __init__(self, url, server_id=None, key_path=None, api_version='2010-09-23'):
		self._logger = logging.getLogger(__name__)
		self.url = url if url[-1] != "/" else url[0:-1]
		self.server_id = server_id		
		self.key_path = key_path
		self.api_version = api_version
	
	def fetch(self, command, **params):
		"""
		@return object
		"""
		# Perform HTTP request
		url = "%s/%s/%s" % (self.url, self.api_version, command)
		request_body = {}
		request_body["operation"] = command
		request_body["version"] = self.api_version
		if {} != params :
			for key, value in params.items():
				request_body[key] = value
		
		file = open(self.key_path)
		key = binascii.a2b_base64(file.read())
		file.close()

		signature, timestamp = cryptotool.sign_http_request(request_body, key)		
		
		post_data = urllib.urlencode(request_body)
		headers = {
			"Date": timestamp, 
			"X-Signature": signature, 
			"X-Server-Id": self.server_id
		}
		response = None
		max_attempts = 5
		for i in range(1, max_attempts + 1):
			try:
				try:
					self._logger.debug("QueryEnv request: %s", post_data)
					opener = urllib2.build_opener(urltool.HTTPRedirectHandler)
					req = urllib2.Request(url, post_data, headers)
					response = opener.open(req)
					break
				except urllib2.URLError, e:
					if isinstance(e, urllib2.HTTPError):
						resp_body = e.read() if e.fp is not None else ""
						raise QueryEnvError, "Request failed. %s. URL: %s. Service message: %s" % (e, self.url, resp_body), sys.exc_traceback
					else:
						host, port = urllib.splitnport(req.host, req.port or 80)
						raise QueryEnvError, "Cannot connect to QueryEnv server on %s:%s. %s" % (host, port, str(e)), sys.exc_traceback
			except:
				self._logger.debug('QueryEnv failed. %s', sys.exc_info()[1])
				if i < max_attempts:
					self._logger.debug('Waiting %d seconds before the next try', 10)
					time.sleep(10)
				else:
					raise

		resp_body = response.read()
		self._logger.debug("QueryEnv response: %s", resp_body)

		# Parse XML response
		xml = None
		try:
			xml = util.xml_strip(dom.parseString(resp_body))
		except (Exception, BaseException), e:
			raise QueryEnvError("Cannot parse XML. %s" % [str(e)])		
		return xml
	
	
	def list_roles (self, role_name=None, behaviour=None):
		"""
		@return Role[]
		"""
		parameters = {}
		if None != role_name :
			parameters["role"] = role_name
		if None != behaviour:
			parameters["behaviour"] = behaviour
			
		return self._request("list-roles", parameters, self._read_list_roles_response)
	
	def list_role_params(self, name=None):
		"""
		@return dict
		"""
		parameters = {}
		if name:
			parameters["role"] = name
			self._logger.warn('\n\nrole_name=%s\n'%name)
		return self._request("list-role-params", parameters, self._read_list_role_params_response)


	def list_scripts (self, event=None, event_id=None, asynchronous=None, name=None, 
		target_ip=None, local_ip=None):
		"""
		@return Script[]
		"""
		parameters = {}
		if None != event:
			parameters["event"] = event
		if None != event_id:
			parameters["event_id"] = event_id
		if None != asynchronous:
			parameters["asynchronous"] = asynchronous
		if None != name :
			parameters["name"] = name
		if None != target_ip:
			parameters['target_ip'] = target_ip
		if None != local_ip:
			parameters['local_ip'] = local_ip
		return self._request("list-scripts",parameters, self._read_list_scripts_response)
	
	def list_virtual_hosts (self, name=None, https=None):
		"""
		@return VirtualHost[]
		"""
		parameters = {}
		if None != name :
			parameters["name"] = name
		if None != https:
			parameters["https"] = https
		return self._request("list-virtualhosts",parameters, self._read_list_virtualhosts_response)
	
	def get_https_certificate (self):
		"""
		@return (cert, pkey, cacert)
		"""
		return self._request("get-https-certificate",{}, self._read_get_https_certificate_response)
	
	def list_ebs_mountpoints (self):
		"""
		@return Mountpoint[]
		"""
		return self._request("list-ebs-mountpoints",{}, self._read_list_ebs_mountpoints_response)
	
	def get_latest_version (self):
		"""
		@return string
		"""
		return self._request("get-latest-version",{}, self._read_get_latest_version_response)
	
	def get_service_configuration(self, behaviour):
		"""
		@return dict
		"""
		return self._request("get-service-configuration",{}, 
				self._read_get_service_configuration_response, (behaviour,))

	def get_scaling_metrics(self):
		'''
		@return: list of ScalingMetric
		'''
		return self._request('get-scaling-metrics', {}, self._read_get_scaling_metrics_response)
		pass

	def _request (self, command, params={}, response_reader=None, response_reader_args=None):
		xml = self.fetch(command, **params)
		response_reader_args = response_reader_args or ()
		return response_reader(xml, *response_reader_args)

	
	def _read_list_roles_response(self, xml):
		ret = []
		
		response = xml.documentElement
		for role_el in response.firstChild.childNodes:
			role = Role()
			role.behaviour = role_el.getAttribute("behaviour").split(',')
			if role.behaviour == ('base',) or role.behaviour == ('',):
				role.behaviour = ()
			role.name = role_el.getAttribute("name")
			for host_el in role_el.firstChild.childNodes:
				host = RoleHost()
				host.index = int(host_el.getAttribute("index"))
				if host_el.hasAttribute("replication-master"):
					host.replication_master = bool(int(host_el.getAttribute("replication-master")))
				host.internal_ip = host_el.getAttribute("internal-ip")
				host.external_ip = host_el.getAttribute("external-ip")
				role.hosts.append(host)
				
			ret.append(role)
		return ret
	
	
	def _read_list_ebs_mountpoints_response(self, xml):
		ret = []
		
		response = xml.documentElement
		for mountpoint_el in response.firstChild.childNodes:
			mountpoint = Mountpoint()
			mountpoint.name = mountpoint_el.getAttribute("name")
			mountpoint.dir = mountpoint_el.getAttribute("dir")
			mountpoint.create_fs = bool(int(mountpoint_el.getAttribute("createfs")))
			mountpoint.is_array = bool(int(mountpoint_el.getAttribute("isarray")))
			for volume_el in mountpoint_el.firstChild.childNodes:
				volume = Volume()
				volume.volume_id = volume_el.getAttribute("volume-id")
				volume.device = volume_el.getAttribute("device")
				mountpoint.volumes.append(volume)
				
			ret.append(mountpoint)

		return ret
	
	
	def _read_list_scripts_response(self, xml):
		ret = []
		
		response = xml.documentElement
		if response.firstChild:
			for script_el in response.firstChild.childNodes:
				script = Script()
				script.asynchronous = bool(int(script_el.getAttribute("asynchronous")))
				script.exec_timeout = int(script_el.getAttribute("exec-timeout"))
				script.name = script_el.getAttribute("name")
				script.body = script_el.firstChild.firstChild.nodeValue
				ret.append(script)		
		return ret
	
	def _read_list_role_params_response(self, xml):
		ret = {}
		response = xml.documentElement
		if response:
			for param_el in response.firstChild.childNodes:
				ret[param_el.getAttribute("name")] = param_el.firstChild.firstChild.nodeValue
		return ret

	def _read_get_latest_version_response(self, xml):
		response = xml.documentElement
		version = response.firstChild.firstChild.nodeValue
		return version
	
	def _read_get_https_certificate_response(self, xml):
		response = xml.documentElement
		if len(response.childNodes):
			virtualhost = response.firstChild
			ca_cert = None
			for ssl_data in virtualhost.childNodes:
				if ssl_data.nodeName == "cert":
					cert = ssl_data.firstChild.nodeValue if ssl_data.firstChild else None
				elif ssl_data.nodeName == "pkey":
					pkey = ssl_data.firstChild.nodeValue if ssl_data.firstChild else None
				elif ssl_data.nodeName == "ca_cert":
					ca_cert = ssl_data.firstChild.nodeValue if ssl_data.firstChild else None
			if not cert:
				self._logger.error("Queryenv didn`t return SSL cert")
			if not pkey:
				self._logger.error("Queryenv didn`t return SSL keys")
			return (cert, pkey, ca_cert)
		self._logger.error("Queryenv return empty SSL cert & keys")
		return (None, None, None)	

	def _read_list_virtualhosts_response(self, xml):
		ret = []
		
		response = xml.documentElement
		for vhost_el in response.firstChild.childNodes:
			vhost = VirtualHost()
			vhost.hostname = vhost_el.getAttribute("hostname")
			vhost.type = vhost_el.getAttribute("type")
			vhost.raw = vhost_el.firstChild.firstChild.nodeValue
			if vhost_el.hasAttribute("https"):
				vhost.https = bool(int(vhost_el.getAttribute("https")))
			ret.append(vhost)	
				
		return ret
	
	def _read_get_service_configuration_response(self, xml, behaviour):
		ret = {}
		name = None
		restart_service = None
		
		name_attr = 'preset-name'
		restart_service_attr = 'restart-service'
		key_attr = "key"
		
		response = xml.documentElement
		
		if not response or not response.firstChild:
			return QueryEnvError("Expected element 'settings' not found in QueryEnv response")
		else:
			for settings in response.childNodes:
				
				if settings.hasAttribute('behaviour') and settings.getAttribute('behaviour') != behaviour:
					continue
			
				if settings.hasAttribute(name_attr):
					name = settings.getAttribute(name_attr)
					
				if settings.hasAttribute(restart_service_attr):
					restart_service = settings.getAttribute(restart_service_attr)
				
				for setting in settings.childNodes:
					if setting.hasAttribute(key_attr):
						k = setting.getAttribute(key_attr)
						if k and setting.firstChild:
							ret[k] = setting.firstChild.nodeValue
					
		preset = Preset()
		preset.name = str(name)
		preset.restart_service = restart_service
		preset.settings = ret
		return preset
	
	def _read_get_scaling_metrics_response(self, xml):
		ret = []
		
		response = xml.documentElement
		for metric_el in response.firstChild.childNodes:
			m = ScalingMetric()
			m.id = metric_el.getAttribute('id')
			m.name = metric_el.getAttribute('name')
			
			tpl = metric_el.childNodes[0].firstChild
			m.path = tpl and tpl.nodeValue or None
			
			tpl = metric_el.childNodes[1].firstChild
			m.retrieve_method = tpl and tpl.nodeValue or None
			
			ret.append(m)
			
		return ret


class Preset(object):
	settings = None
	name = None
	restart_service = None
	
	def __init__(self, name = None, settings = None, restart_service = None):
		self.settings = {} if not settings else settings
		self.name = None if not name else name
		self.restart_service = None if not restart_service else restart_service
	
	def __repr__(self):
		return 'name = ' + str(self.name) \
	+ "; restart_service = " + str(self.restart_service) \
	+ "; settings = " + str(self.settings)
		
	
class Mountpoint(object):
	name = None
	dir = None
	create_fs = False
	is_array = False
	volumes  = None
	
	def __init__(self, name=None, dir=None, create_fs=False, is_array=False, volumes=None):
		self.volumes = volumes or []
		self.name = name
		self.dir = dir
		self.create_fs = create_fs
		self.is_array = is_array
	
	def __str__(self):
		opts = (self.name, self.dir, self.create_fs, len(self.volumes))
		return "qe:Mountpoint(name: %s, dir: %s, create_fs: %s, num_volumes: %d)" % opts
	
	def __repr__(self):
		return "name = " + str(self.name) \
	+ "; dir = " + str(self.dir) \
	+ "; create_fs = " + str(self.create_fs) \
	+ "; is_array = " + str(self.is_array) \
	+ "; volumes = " + str(self.volumes)
	
class Volume(object):
	volume_id  = None
	device = None
	
	def __init__(self, volume_id=None, device=None):
		self.volume_id = volume_id
		self.device = device
	
	def __str__(self):
		return "qe:Volume(volume_id: %s, device: %s)" % (self.volume_id, self.device)
	
	def __repr__(self):
		return 'volume_id = ' + str(self.volume_id) \
	+ "; device = " + str(self.device)
		
class Role(object):
	behaviour = None
	name = None
	hosts = None
	
	def __init__(self, behaviour=None, name=None, hosts=None):
		self.behaviour = behaviour
		self.name = name
		self.hosts = hosts or []
	
	def __str__(self):
		opts = (self.name, self.behaviour, len(self.hosts))
		return "qe:Role(name: %s, behaviour: %s, num_hosts: %s)" % opts
	
	def __repr__(self):
		return 'behaviour = ' + str(self.behaviour) \
	+ "; name = " + str(self.name) \
	+ "; hosts = " + str(self.hosts) + ";"

class RoleHost(object):
	index = None
	replication_master = False
	internal_ip = None
	external_ip	= None
	
	def __init__(self,index=None, replication_master=False, internal_ip=None, external_ip=None):
		self.index = index
		self.replication_master = replication_master
		self.internal_ip = internal_ip
		self.external_ip = external_ip
		
	
	def __repr__(self):
		return "index = " + str(self.index) \
	+ "; replication_master = " + str(self.replication_master) \
	+ "; internal_ip = " + str(self.internal_ip) \
	+ "; external_ip = " + str(self.external_ip)
	
class Script(object):
	asynchronous = False
	exec_timeout = None 
	name = None
	body = None
	
	def __init__(self, asynchronous=False, exec_timeout=None, name=None, body=None):
		self.asynchronous = asynchronous
		self.exec_timeout = exec_timeout
		self.name = name
		self.body = body
	
	def __repr__(self):
		return "asynchronous = " + str(self.asynchronous) \
	+ "; exec_timeout = " + str(self.exec_timeout) \
	+ "; name = " + str(self.name) \
	+ "; body = " + str(self.body)
	
class VirtualHost(object):
	hostname = None
	type = None
	raw = None
	https = False
	
	def __init__(self, hostname=None, type=None, raw=None, https=False):
		self.hostname = hostname
		self.type = type
		self.raw = raw
		self.https = https
	
	
	def __repr__(self):
		return "hostname = " + str(self.hostname) \
	+ "; type = " + str(self.type) \
	+ "; raw = " + str(self.raw) \
	+ "; https = " + str(self.https)

class ScalingMetric(object):
	class RetriveMethod:
		EXECUTE = 'execute'
		READ = 'read'
	
	id = None
	name = None
	path = None
	
	_retrieve_method = None
	def _get_retrieve_method(self):
		return self._retrieve_method
	def _set_retrieve_method(self, v):
		if v in (self.RetriveMethod.EXECUTE, self.RetriveMethod.READ):
			self._retrieve_method = v
		else:
			raise ValueError("Invalid value '%s' for ScalingMetric.retrieve_method") 
	
	retrieve_method = property(_get_retrieve_method, _set_retrieve_method)
	
	def __str__(self):
		return 'qe:ScalingMetric(%s, id: %s, path: %s:%s)' % (self.name, self.id, self.path, self.retrieve_method)