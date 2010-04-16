'''
Created on Mar 3, 2010

@author: marat
@author: Dmytro Korsakov
'''

from scalarizr.bus import bus
from scalarizr.handlers import Handler
from scalarizr.util import configtool
from scalarizr.util import configtool
import logging
import os
import subprocess
import re

def get_handlers ():
	return [HooksHandler()]

class HooksHandler(Handler):
	_logger = None
	
	def __init__(self):
		self._logger = logging.getLogger(__name__)
		bus.on("init", self.on_init)
		
	def on_init(self):
		for event in bus.list_events():
			bus.on(event, self.create_hook(event))
			
	def create_hook(self, event):
		def hook(*args, **kwargs):
			self._logger.info("Hook on '"+event+"'" + str(args) + " " + str(kwargs))

			config = bus.config
			environ = kwargs
			environ["server_id"] = config.get(configtool.SECT_GENERAL, configtool.OPT_SERVER_ID)
			environ["behaviour"] = config.get(configtool.SECT_GENERAL, configtool.OPT_BEHAVIOUR)
			
			path = bus.base_path + "/hooks/"
			reg = re.compile(r"^\d+\-"+event+"$")
							
			if os.path.isdir(path):
				matches_list = list(fname for fname in os.listdir(path) if reg.search(fname))
				if matches_list:
					matches_list.sort()
					for fname in matches_list:
						if os.access(path + fname, os.X_OK):	
							start_command = [path + fname]
							start_command += args
							try:
								p = subprocess.Popen(
									 start_command, 
									 stdin=subprocess.PIPE, 
									 stdout=subprocess.PIPE, 
									 stderr=subprocess.PIPE,
									 env=environ)								
								stdout, stderr = p.communicate()
							
								is_start_failed = p.poll()
								
								if is_start_failed:
									self._logger.error("stderr: %s", stderr)
									
								self._logger.info("stdout: %s", stdout)	
							except OSError, e:
								self._logger.error("Error in script '%s'. %s", fname, str(e.strerror))			
		return hook