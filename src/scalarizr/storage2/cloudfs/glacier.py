
import hashlib

from urlparse import urlparse
from scalarizr.node import __node__
from boto.glacier.writer import chunk_hashes, tree_hash, bytes_to_hex 
from scalarizr.storage2.cloudfs import CloudFileSystem 


class GlacierFilesystem(CloudFileSystem):

	def __init__(self):
		self._conn = None
		self._vault_name = None
		self._part_size = None
		self._total_size = 0
		self._tree_hashes = []
		
	def _connect_glacier(self):
		'''
		Returns Boto.Glacier.Layer1 object
		'''
		#return __node__['ec2']['connect_glacier']()
		from boto.glacier.layer1 import Layer1
		return Layer1('AKIAJO6DOVEREBMYUERQ', 'LBEvgTXt+o7X3NsUr0c5paD4Uf9EWZsyrWMOixeD', 615271354814)


	def multipart_init(self, path, part_size):
		'''
		Returns upload_id
		'''
		self._conn = self._connect_glacier()
		self._part_size = part_size
		self._vault_name = urlparse(path).netloc
		
		response = self._conn.initiate_multipart_upload(self._vault_name, part_size, None)

		return response['UploadId']

	def multipart_put(self, upload_id, part_num, part):
		start_byte = part_num * self._part_size
		content_range = (start_byte, start_byte + len(part) - 1) 
		linear_hash = hashlib.sha256(part).hexdigest()
		part_tree_hash = tree_hash(chunk_hashes(part))
		self._tree_hashes.append(part_tree_hash)
		hex_part_tree_hash = bytes_to_hex(part_tree_hash)

		self._conn.upload_part(
			self._vault_name,
			upload_id,
			linear_hash,
			hex_part_tree_hash,
			content_range,
			part
		)

		self._total_size += len(part)

	def multipart_complete(self, upload_id):
		'''
		Returns glacier://Vault_1/?avail_zone=us-east-1&archive_id=NkbByEejwEggmBz2fTHgJrg0XBoDfjP4q6iu87-TjhqG6eGoOY9Z8i1_AUyUsuhPAdTqLHy8pTl5nfCFJmDl2yEZONi5L26Omw12vcs01MNGntHEQL8MBfGlqrEXAMPLEArchiveId
		'''
		hex_tree_hash = bytes_to_hex(tree_hash(self._tree_hashes))

		response = self._conn.complete_multipart_upload(
			self._vault_name,
			upload_id,
			hex_tree_hash,
			self._total_size
		)

		return response['ArchiveId']

	def multipart_abort(self, upload_id):
		self._conn.abort_multipart_upload(self._vault_name, upload_id)
