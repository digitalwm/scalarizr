'''
Created on Mar 1, 2010

@author: marat
'''

from scalarizr.core.handlers import Handler
from scalarizr.core import Bus, BusEntries
from scalarizr.platform.ec2 import Aws
from scalarizr.messaging import Queues, Messages
import logging
import time


def get_handlers ():
	return [EbsHandler()]

class EbsHandler(Handler):
	_logger = None
	_bus = None
	_platform = None
	_aws = None
	_msg_service = None

	def __init__(self):
		self._logger = logging.getLogger(__name__)
		bus = Bus()
		self._platform = bus[BusEntries.PLATFORM]
		self._aws = Aws()
		self._msg_service = bus[BusEntries.MESSAGE_SERVICE]
		self._bus = bus		
		
		bus.define_events(
			# Fires when EBS is attached to instance
			# @param volumeId: EBS volume id
			# @param device: device name, ex: /dev/sdf
			"block_device_attached", 
			
			# Fires when EBS is detached from instance
			# @param device: device name, ex: /dev/sdf 
			"block_device_detached"
		)

	def on_BlockDeviceUpdated(self, message):
		ec2_conn = self._aws.get_ec2_conn()
		self._logger.debug(message)

		if message.action == "add":
			# Volume was attached
			max_attempts = 5
			attempt = 1
			while attempt <= max_attempts:
				for volume in ec2_conn.get_all_volumes():
					ad = volume.attach_data
					if not ad is None and \
							ad.instance_id == self._platform.get_instance_id() and \
							ad.device == message.devname and \
							ad.status == "attached":
						
						self._logger.info("EBS was attached (volumeId: %s, device: %s)" % (volume.id, ad.device))
						try:
							# Send message to Scalr
							msg = self._msg_service.new_message(Messages.BLOCK_DEVICE_ATTACHED, body={
    							"aws.volume_id" : volume.id,
    							"aws.device" : ad.device
    						})
							producer = self._msg_service.get_producer()
							producer.send(Queues.CONTROL, msg)
							
							# Notify listeners
							self._bus.fire("block_device_attached", volume=volume.id, device=ad.device)
							
						except Exception, e:
							self._logger.error("Cannot send message. %s" % str(e))
							raise 
				
				if attempt < max_attempts:
					self._logger.debug("Attempt %d not succeed. " +
								"Sleep %d seconds before the next one", attempt, attempt)		
					time.sleep(attempt)
				else:
					self._logger.debug("Attempt %d not succeed")
				attempt = attempt + 1
			else:
				self._logger.warning("Unable to verify that EBS was attached to the server")
				
		elif message.action == "remove":
			# Volume was detached
			self._logger.info("EBS was detached (device: %s)" % message.device)
			try:
				# Send message to Scalr
				msg = self._msg_service.new_message(Messages.BLOCK_DEVICE_DETACHED, body={
    				"aws.device" : message.devname
    			})
				producer = self._msg_service.get_producer()
				producer.send(Queues.CONTROL, msg)
				
				# Notify listeners
				self._bus.fire("block_device_detached", device=message.devname)
				
			except Exception, e:
				self._logger.error("Cannot send message. %s" % str(e))
				raise 

	def accept(self, message, queue, behaviour=None, platform=None, os=None, dist=None):
		return message.name == Messages.BLOCK_DEVICE_UPDATED and platform == "ec2"
