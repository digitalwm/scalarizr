'''
Created on Dec 5, 2009

@author: marat
'''

from scalarizr.messaging import MessageProducer, MessagingError
from scalarizr.messaging.p2p import P2pMessageStore, _P2pBase, P2pConfigOptions
from scalarizr.util import cryptotool, configtool
from urllib import splitnport
from urllib2 import urlopen, Request, URLError, HTTPError
import logging
import uuid


class P2pMessageProducer(MessageProducer, _P2pBase):
	endpoint = None
	_store = None
	_logger = None
	
	
	def __init__(self, **kwargs):
		MessageProducer.__init__(self)
		_P2pBase.__init__(self, **kwargs)
		self.endpoint = kwargs[P2pConfigOptions.PRODUCER_URL]
		self._logger = logging.getLogger(__name__)
		self._store = P2pMessageStore()
	
	def send(self, queue, message):
		self._logger.info("Sending message '%s' into queue '%s'" % (message.name, queue))
		try:
			if message.id is None:
				message.id = str(uuid.uuid4())
				
			self.fire("before_send", queue, message)				
			self._store.put_outgoing(message, queue)
			
			# Prepare POST body
			xml = message.toxml()
			#xml = xml.ljust(len(xml) + 8 - len(xml) % 8, " ")
			crypto_key = configtool.read_key(self.crypto_key_path)
			data = cryptotool.encrypt(xml, crypto_key)
			
			# Send request
			req = Request(self.endpoint + "/" + queue, data, {"X-Server-Id": self.server_id})
			urlopen(req)
			
			self._store.mark_as_delivered(message.id)
			self.fire("send", queue, message)
			
		except IOError, e:
			if isinstance(e, HTTPError) and e.code == 201:
				self._store.mark_as_delivered(message.id)
				self.fire("send", queue, message)
			else:
				self.fire("send_error", e, queue, message)
				self._logger.info("Mark message as undelivered")
				self._store.mark_as_undelivered(message.id)
				
				if isinstance(e, HTTPError):
					resp_body = e.read() if not e.fp is None else ""
					if e.code == 401:
						raise MessagingError("Cannot authenticate on message server. %s" % (resp_body))
					
					elif e.code == 400:
						raise MessagingError("Malformed request. %s" % (resp_body))
					
					else:
						raise MessagingError("Request to message server failed (code: %d). %s" % (e.code, str(e)))
				elif isinstance(e, URLError):
					host, port = splitnport(req.host, req.port)
					raise MessagingError("Cannot connect to message server on %s:%s. %s" % (host, port, str(e)))
				else:
					raise MessagingError("Cannot read crypto key. %s" % str(e))		
					

	def get_undelivered(self):
		return self._store.get_undelivered()
