'''
Created on 14.06.2010

@author: spike
@author: marat
'''

# Core
from scalarizr import config
from scalarizr.bus import bus
from scalarizr.storage import Storage, StorageError, Volume, transfer
from scalarizr.config import BuiltinBehaviours, Configurator, ScalarizrState
from scalarizr.service import CnfController, CnfPreset
from scalarizr.messaging import Messages
from scalarizr.handlers import HandlerError, ServiceCtlHanler

# Libs
from scalarizr.libs.metaconf import Configuration, MetaconfError, NoPathError,\
	ParseError
from scalarizr.util import system2, cryptotool, disttool,\
		 filetool, firstmatched, cached, validators, initdv2, software, wait_until
from scalarizr.util.initdv2 import ParametrizedInitScript, wait_sock, InitdError

# Stdlibs
from distutils import version
from subprocess import Popen, PIPE, STDOUT
import logging, os, re,  tarfile, tempfile
import time, signal, pwd, random, shutil
import glob
import hashlib
import ConfigParser

# Extra
import pexpect


BEHAVIOUR = SERVICE_NAME = BuiltinBehaviours.MYSQL
CNF_SECTION = BEHAVIOUR
CNF_NAME = BEHAVIOUR

_logger = logging.getLogger(__name__)

class MysqlInitScript(initdv2.ParametrizedInitScript):
	def __init__(self):
		initd_script = disttool.is_redhat_based() and "/etc/init.d/mysqld" or "/etc/init.d/mysql"
		
		if not os.path.exists(initd_script):
			raise HandlerError("Cannot find MySQL init script at %s. Make sure that MySQL is installed" % initd_script)
		
		pid_file = None
		try:
			out = system2("my_print_defaults mysqld", shell=True)
			m = re.search("--pid[-_]file=(.*)", out[0], re.MULTILINE)
			if m:
				pid_file = m.group(1)
		except:
			pass
		
		initdv2.ParametrizedInitScript.__init__(self, SERVICE_NAME, 
				initd_script, pid_file, socks=[initdv2.SockParam(3306, timeout=60)])
		'''
		timeout=60 is no chance
		when server starts after rebundle, mysql takes too long to start on the attached EBS storage.
		
		Scalarizr:
		2010-12-02 10:31:12,086 - INFO - scalarizr.handlers - Starting mysql
		
		MySQL:
		Version: '5.1.41-3ubuntu12.7-log'  socket: '/var/run/mysqld/mysqld.sock'  port: 3306  (Ubuntu)
		101202 10:31:30 [Note] Plugin 'FEDERATED' is disabled.
		101202 10:31:31  InnoDB: Started; log sequence number 0 44556
		101202 10:31:31 [Note] Event Scheduler: Loaded 0 events
		
		Over 15 seconds! OMFG!!
		XXX: Requires investigation
		'''
		
		
	def stop(self):
		if not self.running:
			return True
		initdv2.ParametrizedInitScript.stop(self)

initdv2.explore(SERVICE_NAME, MysqlInitScript)


class MysqlOptions(Configurator.Container):
	'''
	mysql behaviour
	'''
	class mysqld_path(Configurator.Option):
		'''
		MySQL daemon binary path
		'''
		name = CNF_NAME + '/mysqld_path'
		required = True

		@property
		@cached
		def default(self):
			return firstmatched(lambda p: os.access(p, os.F_OK | os.X_OK), 
					('/usr/libexec/mysqld', '/usr/sbin/mysqld'), '')
		
		@validators.validate(validators.executable)
		def _set_value(self, v):
			Configurator.Option._set_value(self, v)
		
		value = property(Configurator.Option._get_value, _set_value)
			
	class mysql_path(Configurator.Option):
		'''
		MySQL command line tool path
		'''
		name = CNF_NAME + '/mysql_path'
		default = '/usr/bin/mysql'
		required = True

		@validators.validate(validators.executable)
		def _set_value(self, v):
			Configurator.Option._set_value(self, v)
			
		value = property(Configurator.Option._get_value, _set_value)

	class mysqldump_path(Configurator.Option):
		'''
		Path to the mysqldump utility
		'''
		name = CNF_NAME + '/mysqldump_path'
		default = '/usr/bin/mysqldump'
		required = True
		
		@validators.validate(validators.executable)
		def _set_value(self, v):
			Configurator.Option._set_value(self, v)
			
		value = property(Configurator.Option._get_value, _set_value)
			
	class mycnf_path(Configurator.Option):
		'''
		MySQL configuration file path
		'''
		name = CNF_NAME + '/mycnf_path'
		required = True

		@property		
		@cached
		def default(self):
			return firstmatched(lambda p: os.access(p, os.F_OK), 
					('/etc/my.cnf', '/etc/mysql/my.cnf'), '')
			
		@validators.validate(validators.file_exists)
		def _set_value(self, v):
			Configurator.Option._set_value(self, v)
		
		value = property(Configurator.Option._get_value, _set_value)

class MysqlServiceConfigurator:
	pass

# Configuration options
# Private
OPT_ROOT_PASSWORD 		= "root_password"
OPT_REPL_PASSWORD 		= "repl_password"
OPT_STAT_PASSWORD   	= "stat_password"
OPT_REPLICATION_MASTER  = "replication_master"
OPT_LOG_FILE 			= "log_file"
OPT_LOG_POS				= "log_pos"
OPT_VOLUME_CNF			= 'volume_config'
OPT_SNAPSHOT_CNF		= 'snapshot_config'
# Public
OPT_MYSQLD_PATH 		= 'mysqld_path'
OPT_MYSQL_PATH			= 'mysql_path'
OPT_MYSQLDUMP_PATH		= 'mysqldump_path'
OPT_MYCNF_PATH 			= 'mycnf_path'
OPT_CHANGE_MASTER_TIMEOUT = 'change_master_timeout'

# System users
ROOT_USER 				= "scalr"
REPL_USER 				= "scalr_repl"
STAT_USER 				= "scalr_stat"
PMA_USER 				= "pma"

# Mysql storage constants
STORAGE_DEVNAME 		= "/dev/sdo"
STORAGE_PATH 			= "/mnt/dbstorage"
STORAGE_DATA_DIR 		= "mysql-data"
STORAGE_BINLOG 			= "mysql-misc/binlog"
STORAGE_VOLUME_CNF 		= 'mysql.json'
STORAGE_SNAPSHOT_CNF 	= 'mysql-snap.json'

BACKUP_CHUNK_SIZE = 200*1024*1024


def get_handlers ():
	return [MysqlHandler()]

class MysqlMessages:
	CREATE_DATA_BUNDLE = "Mysql_CreateDataBundle"
	CREATE_DATA_BUNDLE_RESULT = "Mysql_CreateDataBundleResult"
	
	CREATE_BACKUP = "Mysql_CreateBackup"
	
	CREATE_BACKUP_RESULT = "Mysql_CreateBackupResult"
	"""
	@ivar status: ok|error
	@ivar last_error
	@ivar backup_urls: S3 URL
	"""
	
	CREATE_PMA_USER = "Mysql_CreatePmaUser"
	"""
	@ivar pma_server_ip: User host
	@ivar farm_role_id
	@ivar root_password
	"""
	
	CREATE_PMA_USER_RESULT = "Mysql_CreatePmaUserResult"
	"""
	@ivar status: ok|error
	@ivar last_error
	@ivar pma_user
	@ivar pma_password
	@ivar farm_role_id
	"""
	
	PROMOTE_TO_MASTER	= "Mysql_PromoteToMaster"
	"""
	@ivar root_password: 'scalr' user password 
	@ivar repl_password: 'scalr_repl' user password
	@ivar stat_password: 'scalr_stat' user password
	@ivar volume_id: Master EBS volume id
	@ivar volume: Master storage configuration
	"""
	
	PROMOTE_TO_MASTER_RESULT = "Mysql_PromoteToMasterResult"
	"""
	@ivar status: ok|error
	@ivar last_error: Last error message in case of status = 'error'
	@ivar volume_id: Master EBS volume id
	"""
	
	NEW_MASTER_UP = "Mysql_NewMasterUp"
	"""
	@ivar behaviour
	@ivar local_ip
	@ivar remote_ip
	@ivar role_name		
	@ivar repl_password
	"""
	
	"""
	Also MySQL behaviour adds params to common messages:
	
	= HOST_INIT_RESPONSE =
	@ivar mysql=dict(
		replication_master: 	1|0
		root_password:			'scalr' user password  					(on slave)
		repl_password:			'scalr_repl' user password				(on slave)
		stat_password: 			'scalr_stat' user password				(on slave)
		log_file:				Binary log file							(on slave)
		log_pos:				Binary log file position				(on slave)
		volume_config			Master storage configuration			(on master)
		snapshot_config			Master storage snapshot 				(both)
	)
	
	= HOST_UP =
	@ivar mysql=dict(
		root_password: 			'scalr' user password  					(on master)
		repl_password: 			'scalr_repl' user password				(on master)
		stat_password: 			'scalr_stat' user password				(on master)
		log_file: 				Binary log file							(on master) 
		log_pos: 				Binary log file position				(on master)
		volume_config:			Current storage configuration			(both)
		snapshot_config:		Master storage snapshot					(on master)		 
	) 
	"""


class MysqlCnfController(CnfController):
	_mysql_version = None	
	
	def __init__(self):
		self._logger = logging.getLogger(__name__)
		self._init_script = initdv2.lookup(SERVICE_NAME)
		self._cnf = bus.cnf
		ini = self._cnf.rawini
		self._mycnf_path = ini.get(CNF_SECTION, OPT_MYCNF_PATH)
		self._mysqld_path = ini.get(CNF_SECTION, OPT_MYSQLD_PATH)
		CnfController.__init__(self, BEHAVIOUR, self._mycnf_path, 'mysql') #TRUE,FALSE

	def _start_service(self):
		if not hasattr(self, '_mysql_cnf_err_re'):
			self._mysql_cnf_err_re = re.compile('Unknown option|ERROR')
		stderr = system2('%s --help' % self._mysqld_path, shell=True)[1]
		if re.search(self._mysql_cnf_err_re, stderr):
			raise Exception('Error in mysql configuration detected. Output:\n%s' % stderr)
		
		self._logger.info("Starting %s" % self.behaviour)
		
		if not self._init_script.running:
			try:
				self._init_script.start()
			except:
				if not self._init_script.running:
					raise
			self._logger.debug("%s started" % self.behaviour)
	
	def current_preset(self):
		self._logger.debug('Getting current MySQL preset')
		mysql = None
		preset = CnfPreset(name='System', behaviour=BEHAVIOUR)
		self._start_service()
		try:
			mysql = self._get_connection()
			mysql.sendline('SHOW GLOBAL VARIABLES;')
			mysql.expect('mysql>')
			out = mysql.before
			raw_text = out.splitlines()
			text = raw_text[4:-3]
			vars = {}
			
			for line in text:
				splitted_line = line.split('|')					
				name = splitted_line[1].strip()
				value = splitted_line[2].strip()
				"""
				print name, value 
				try:
					remove odd 
					if hasattr(self._manifest, name):
						vars[name] = value
				except AttributeError:
					self._logger.error('No spec for %s' % name)	
					pass
				"""
				vars[name] = value
				
			for opt in self._manifest:
				if opt.name in vars:
					preset.settings[opt.name] = vars[opt.name]
			return preset
		finally:
			if mysql:
				mysql.close()
	
	def apply_preset(self, preset):
		
		CnfController.apply_preset(self, preset)
	
	def _before_apply_preset(self):
		self.sendline = ''
		
	def _after_set_option(self, option_spec, value):
		self._logger.debug('callback "_after_set_option": %s %s (Need restart: %s)' 
				% (option_spec, value, option_spec.need_restart))
		
		if value != option_spec.default_value and not option_spec.need_restart:
			self._logger.debug('Setting variable %s to %s' % (option_spec.name, value))
			self.sendline += 'SET GLOBAL %s = %s; ' % (option_spec.name, value)
			

	def _after_remove_option(self, option_spec):
		if option_spec.default_value and not option_spec.need_restart:
			self._logger.debug('Setting run-time variable %s to default [%s]' 
						% (option_spec.name,option_spec.default_value))
			self.sendline += 'SET GLOBAL %s = %s; ' % (option_spec.name, option_spec.default_value)
	
	def _after_apply_preset(self):
		mysql = self._get_connection()
		try:
			if self.sendline and mysql:
				self._logger.debug(self.sendline)
				mysql.sendline(self.sendline)
				index = mysql.expect(['mysql>', pexpect.EOF, pexpect.TIMEOUT])
				if 1==index or 2==index:
					self._logger.error('Cannot set global variables: %s' % mysql.before)
				else:
					self._logger.debug('All global variables has been set.')
			elif not self.sendline:
				self._logger.debug('No global variables changed. Nothing to set.')
			elif not mysql:
				self._logger.debug('No connection to MySQL. Skipping SETs.')
		finally:
			if mysql:
				mysql.close()
	
	def _get_version(self):
		if not self._mysql_version:
			info = software.software_info('mysql')
			self._mysql_version = info.version
		return self._mysql_version
		
		
	def _get_connection(self):
		szr_cnf = bus.cnf
		root_password = szr_cnf.rawini.get(CNF_SECTION, OPT_ROOT_PASSWORD)
		return spawn_mysql(ROOT_USER, root_password)



def _reload_mycnf(f):
	def g(self, *args):
		self._mysql_config = Configuration('mysql')
		try:
			self._mysql_config.read(self._mycnf_path)
		except (OSError, MetaconfError, ParseError), e:
			raise HandlerError('Cannot read mysql config %s : %s' % (self._mycnf_path, str(e)))
		f(self, *args)
	return g	

class MysqlHandler(ServiceCtlHanler):
	_logger = None
	
	_mysql_config = None
	
	_queryenv = None
	""" @type _queryenv: scalarizr.queryenv.QueryEnvService	"""
	
	_platform = None
	""" @type _platform: scalarizr.platform.Ec2Platform """
	
	_cnf = None
	''' @type _cnf: scalarizr.config.ScalarizrCnf '''
	
	_storage_path = _data_dir = _binlog_path = None
	""" Storage parameters """
	
	_mycnf_path = None
	_mysqld_path = None
	
	storage_vol = None
	
	def __init__(self):
		self._logger = logging.getLogger(__name__)
		self._queryenv = bus.queryenv_service
		self._platform = bus.platform
		self._cnf = bus.cnf
		ini = self._cnf.rawini
		self._role_name = ini.get(config.SECT_GENERAL, config.OPT_ROLE_NAME)
		self._mycnf_path = ini.get(CNF_SECTION, OPT_MYCNF_PATH)
		self._mysqld_path = ini.get(CNF_SECTION, OPT_MYSQLD_PATH)
		self._change_master_timeout = ini.get(CNF_SECTION, OPT_CHANGE_MASTER_TIMEOUT)

		
		self._storage_path = STORAGE_PATH
		self._data_dir = os.path.join(self._storage_path, STORAGE_DATA_DIR)
		self._binlog_base = os.path.join(self._storage_path, STORAGE_BINLOG)

		initd = initdv2.lookup(SERVICE_NAME)
		ServiceCtlHanler.__init__(self, SERVICE_NAME, initd, MysqlCnfController())
		
		self._volume_config_path  = self._cnf.private_path(os.path.join('storage', STORAGE_VOLUME_CNF))
		self._snapshot_config_path = self._cnf.private_path(os.path.join('storage', STORAGE_SNAPSHOT_CNF))
			
		bus.on("init", self.on_init)
		bus.define_events(
			'before_mysql_data_bundle',
			
			'mysql_data_bundle',
			
			# @param host: New master hostname 
			'before_mysql_change_master',
			
			# @param host: New master hostname 
			# @param log_file: log file to start from 
			# @param log_pos: log pos to start from 
			'mysql_change_master'
		)

	def on_init(self):		
		bus.on("host_init_response", self.on_host_init_response)
		bus.on("before_host_up", self.on_before_host_up)
		bus.on("start", self.on_start)
		"""
		@xxx: Storage unplug failed because scalarizr has no EC2 access keys
		bus.on("before_reboot_start", self.on_before_reboot_start)
		bus.on("before_reboot_finish", self.on_before_reboot_finish)
		"""
		
	def on_start(self):
		if self._cnf.state == ScalarizrState.RUNNING:
			
			def check_mysql_pass(mysql_pexp, user, password):
							
				def hash_mysql_password(str):
					pass1 = hashlib.sha1(str).digest()
					pass2 = hashlib.sha1(pass1).hexdigest()
					return pass2.upper()
				
				hashed_pass = hash_mysql_password(password)
				
				mysql_pexp.sendline("SELECT password FROM mysql.user WHERE User = '%s' \G" % user)
				mysql_pexp.expect('mysql>', timeout=10)
				out = mysql_pexp.before
				passwords = re.findall('password:\s+\*(\w+)', out)
				
				if not passwords or not all(map(lambda x: x == hashed_pass, passwords)):
					raise Exception("Password for user %s doesn't match." % user)
					
			self._logger.debug("Checking Scalr's MySQL system users presence.")
			root_password = self._cnf.rawini.get(CNF_SECTION, OPT_ROOT_PASSWORD)
			repl_password = self._cnf.rawini.get(CNF_SECTION, OPT_REPL_PASSWORD)
			stat_password = self._cnf.rawini.get(CNF_SECTION, OPT_STAT_PASSWORD)
			
			try:
				mysql = pexpect.spawn('/usr/bin/mysql -u ' + ROOT_USER + ' -p')
				mysql.expect('Enter password:', timeout=10)
				mysql.sendline(root_password)
				mysql.expect('mysql>', timeout=10)
				check_mysql_pass(mysql, REPL_USER, repl_password)
				check_mysql_pass(mysql, STAT_USER, stat_password)
				self._logger.debug("Scalr's MySQL system users are present. Passwords are correct.")				
			except:
				self._logger.warning("Scalr's MySQL system users were changed. Recreating.")
				self._add_mysql_users(ROOT_USER, REPL_USER, STAT_USER,
									  root_password, repl_password, stat_password)
			finally:
				mysql.close()
				
			if not self.storage_vol:
				# Creating self.storage_vol object from configuration
				storage_conf = Storage.restore_config(self._volume_config_path)
				self.storage_vol = Storage.create(storage_conf)
				
				
	def accept(self, message, queue, behaviour=None, platform=None, os=None, dist=None):
		return BEHAVIOUR in behaviour and (
					message.name == MysqlMessages.NEW_MASTER_UP
				or 	message.name == MysqlMessages.PROMOTE_TO_MASTER
				or 	message.name == MysqlMessages.CREATE_DATA_BUNDLE
				or 	message.name == MysqlMessages.CREATE_BACKUP
				or 	message.name == MysqlMessages.CREATE_PMA_USER
				or  message.name == Messages.UPDATE_SERVICE_CONFIGURATION)
		
	def on_Mysql_CreatePmaUser(self, message):
		try:
			if not int(self._cnf.rawini.get(CNF_SECTION, OPT_REPLICATION_MASTER)):
				raise HandlerError('Cannot add pma user on slave')			
			try:
				root_password = self._cnf.rawini.get(CNF_SECTION, OPT_ROOT_PASSWORD)
			except Exception, e:
				raise HandlerError('Cannot retrieve mysql password from config: %s' % (e,))
			pma_server_ip = message.pma_server_ip
			farm_role_id  = message.farm_role_id
			
			self._logger.info("Adding phpMyAdmin system user")
			
			# Connecting to mysql 
			mysql = spawn_mysql(ROOT_USER, root_password)
			mysql.sendline('SELECT VERSION();')
			mysql.expect('mysql>')
			mysql_ver_str = re.search(re.compile('\d*\.\d*\.\d*', re.MULTILINE), mysql.before)

			# Determine mysql server version 
			if mysql_ver_str:
				mysql_ver = version.StrictVersion(mysql_ver_str.group(0))
				priv_count = 28 if mysql_ver >= version.StrictVersion('5.1.6') else 26
			else:
				raise HandlerError("Cannot extract mysql version from string '%s'" % mysql.before)
			
			# Generating password for pma user
			pma_password = re.sub('[^\w]','', cryptotool.keygen(20))
			sql = "DELETE FROM mysql.user WHERE User = '"+PMA_USER+"';"
			mysql.sendline(sql)
			mysql.expect("mysql>")
			# Generating sql statement, which depends on mysql server version 
			sql = "INSERT INTO mysql.user VALUES('"+pma_server_ip+"','"+PMA_USER+"',PASSWORD('"+pma_password+"')" + ",'Y'"*priv_count + ",''"*4 +',0'*4+");"
			# Pass statement to mysql client
			mysql.sendline(sql)
			mysql.expect('mysql>')
			
			# Check for errors
			if re.search('error', mysql.before, re.M | re.I):
				raise HandlerError("Cannot add PhpMyAdmin system user '%s': '%s'" % (PMA_USER, mysql.before))
			
			mysql.sendline('FLUSH PRIVILEGES;')
			mysql.close()
			del(mysql)
			
			self._logger.info('PhpMyAdmin system user successfully added')
			
			self.send_message(MysqlMessages.CREATE_PMA_USER_RESULT, dict(
				status       = 'ok',
				pma_user	 = PMA_USER,
				pma_password = pma_password,
				farm_role_id = farm_role_id,
			))
			
		except (Exception, BaseException), e:
			self._logger.exception(e)
			self.send_message(MysqlMessages.CREATE_PMA_USER_RESULT, dict(
				status		= 'error',
				last_error	=  str(e).strip(),
				farm_role_id = farm_role_id
			))
	
	@_reload_mycnf
	def on_Mysql_CreateBackup(self, message):
		
		# Retrieve password for scalr mysql user
		tmpdir = backup_path = None
		try:
			# Do backup only on slave
			if int(self._cnf.rawini.get(CNF_SECTION, OPT_REPLICATION_MASTER)):
				raise HandlerError('Create backup is not allowed on Master')
			
			# Load root password
			try:
				root_password = self._cnf.rawini.get(CNF_SECTION, OPT_ROOT_PASSWORD)
			except Exception, e:
				raise HandlerError('Cannot retrieve mysql password from config: %s' % (e,))
			
			# Get databases list
			mysql = spawn_mysql(ROOT_USER, root_password)
			mysql.sendline('SHOW DATABASES;')
			mysql.expect('mysql>')
			
			databases = list(line.split('|')[1].strip() for line in mysql.before.splitlines()[4:-3])
			if 'information_schema' in databases:
				databases.remove('information_schema')
			
			mysql.close()
			
			
			# Defining archive name and path
			backup_filename = 'mysql-backup-'+time.strftime('%Y-%m-%d-%H:%M:%S')+'.tar.gz'
			backup_path = os.path.join('/tmp', backup_filename)
			
			# Creating archive 
			backup = tarfile.open(backup_path, 'w:gz')

			# Dump all databases
			self._logger.info("Dumping all databases")
			tmpdir = tempfile.mkdtemp()			
			for db_name in databases:
				dump_path = tmpdir + os.sep + db_name + '.sql'
				mysql = pexpect.spawn('/bin/sh -c "/usr/bin/mysqldump -u ' + ROOT_USER + ' -p --create-options' + 
									  ' --add-drop-database -q -Q --flush-privileges --databases ' + 
									  db_name + '>' + dump_path +'"', timeout=900)
				mysql.expect('Enter password:')
				mysql.sendline(root_password)
				
				status = mysql.read()
				if re.search(re.compile('error', re.M | re.I), status):
					raise HandlerError('Error while dumping database %s: %s' % (db_name, status))
				
				backup.add(dump_path, os.path.basename(dump_path))
				
				mysql.close()
				del(mysql)
			
			backup.close()
			
			# Creating list of full paths to archive chunks
			if os.path.getsize(backup_path) > BACKUP_CHUNK_SIZE:
				parts = [os.path.join(tmpdir, file) for file in filetool.split(backup_path, backup_filename, BACKUP_CHUNK_SIZE , tmpdir)]
			else:
				parts = [backup_path]
					
			self._logger.info("Uploading backup to cloud storage (%s)", self._platform.cloud_storage_path)
			trn = transfer.Transfer()
			result = trn.upload(parts, self._platform.cloud_storage_path)
			self._logger.info("Mysql backup uploaded to cloud storage under %s/%s", 
							self._platform.cloud_storage_path, backup_filename)
			
			self.send_message(MysqlMessages.CREATE_BACKUP_RESULT, dict(
				status = 'ok',
				backup_parts = result
			))
						
		except (Exception, BaseException), e:
			self._logger.exception(e)
			self.send_message(MysqlMessages.CREATE_BACKUP_RESULT, dict(
				status = 'error',
				last_error = str(e)
			))
		finally:
			if tmpdir:
				shutil.rmtree(tmpdir, ignore_errors=True)
			if backup_path and os.path.exists(backup_path):
				os.remove(backup_path)				

	def on_Mysql_CreateDataBundle(self, message):
		# Retrieve password for scalr mysql user
		try:
			bus.fire('before_mysql_data_bundle')
			try:
				root_password = self._cnf.rawini.get(CNF_SECTION, OPT_ROOT_PASSWORD)
			except Exception, e:
				raise HandlerError('Cannot retrieve mysql login and password from config: %s' % (e,))
			# Creating snapshot
			snap, log_file, log_pos = self._create_snapshot(ROOT_USER, root_password)
			
			bus.fire('mysql_data_bundle', snapshot_id=snap.id)			
			
			# Sending snapshot data to scalr
			self.send_message(MysqlMessages.CREATE_DATA_BUNDLE_RESULT, dict(
				snapshot_config=snap.config(),
				log_file=log_file,
				log_pos=log_pos,
				status='ok'
			))

		except (Exception, BaseException), e:
			self.send_message(MysqlMessages.CREATE_DATA_BUNDLE_RESULT, dict(
				status		='error',
				last_error	= str(e)
			))

	@_reload_mycnf				
	def on_Mysql_PromoteToMaster(self, message):
		"""
		Promote slave to master
		@type message: scalarizr.messaging.Message
		@param message: Mysql_PromoteToMaster
		"""
		old_conf 		= None
		new_storage_vol	= None
		
		if not int(self._cnf.rawini.get(CNF_SECTION, OPT_REPLICATION_MASTER)):
			#ec2_conn = self._platform.new_ec2_conn()
			master_storage_conf = message.config
			tx_complete = False
			
			try:
				# Stop mysql
				if self._init_script.running:
					mysql = spawn_mysql(ROOT_USER, message.root_password)
					timeout = 180
					try:
						mysql.sendline("STOP SLAVE;")
						mysql.expect("mysql>", timeout=timeout)
					except pexpect.TIMEOUT:
						raise HandlerError("Timeout (%d seconds) reached " + 
								"while waiting for slave stop" % (timeout,))
					finally:
						mysql.close()
					self._stop_service()
					
				# Unplug slave storage and plug master one
				#self._unplug_storage(slave_vol_id, self._storage_path)
				old_conf = self.storage_vol.detach(force=True) # ??????
				#master_vol = self._take_master_volume(master_vol_id)
				#self._plug_storage(master_vol.id, self._storage_path)
				new_storage_vol = self._plug_storage(self._storage_path, master_storage_conf)				
				# Continue if master storage is a valid MySQL storage 
				if self._storage_valid():
					# Patch configuration files 
					self._move_mysql_dir('mysqld/log_bin', self._binlog_base)
					self._move_mysql_dir('mysqld/datadir', self._data_dir + os.sep)
					self._replication_init()
					# Update behaviour configuration
					updates = {
						OPT_ROOT_PASSWORD : message.root_password,
						OPT_REPL_PASSWORD : message.repl_password,
						OPT_STAT_PASSWORD : message.stat_password,
						OPT_REPLICATION_MASTER 	: "1"
					}
					self._update_config(updates)
					# Send message to Scalr
					self.send_message(MysqlMessages.PROMOTE_TO_MASTER_RESULT, dict(
						status="ok",
						volume_config = new_storage_vol.config()     
					))
				else:
					raise HandlerError("%s is not a valid MySQL storage" % self._storage_path)
				self._start_service()
				tx_complete = True
			except (Exception, BaseException), e:
				self._logger.error("Promote to master failed. %s", e)
				if new_storage_vol:
					new_storage_vol.detach()
				# Get back slave storage
				if old_conf:
					self._plug_storage(self._storage_path, old_conf)

				self.send_message(MysqlMessages.PROMOTE_TO_MASTER_RESULT, dict(
					status="error",
					last_error=str(e)
				))

				# Start MySQL
				self._start_service()
			
			if tx_complete:
				# Delete slave EBS
				self.storage_vol.destroy(remove_disks=True)
				self.storage_vol = new_storage_vol
				Storage.backup_config(self.storage_vol.config(), self._storage_path)
		else:
			self._logger.warning('Cannot promote to master. Already master')

	@_reload_mycnf
	def on_Mysql_NewMasterUp(self, message):
		"""
		Switch replication to a new master server
		@type message: scalarizr.messaging.Message
		@param message:  Mysql_NewMasterUp
		"""
		is_repl_master, = self._get_ini_options(OPT_REPLICATION_MASTER)
		
		if not int(is_repl_master):
			host = message.local_ip or message.remote_ip
			self._logger.info("Switching replication to a new MySQL master %s", host)
			bus.fire('before_mysql_change_master', host=host)			
			
			mysql = spawn_mysql(ROOT_USER, message.root_password)
						
			self._logger.debug("Stopping slave i/o thread")
			mysql.sendline("STOP SLAVE IO_THREAD;")
			mysql.expect("mysql>")
			self._logger.debug("Slave i/o thread stopped")
			
			self._logger.debug("Retrieving current log_file and log_pos")
			mysql.sendline("SHOW SLAVE STATUS\\G");
			mysql.expect("mysql>")
			log_file = log_pos = None
			for line in mysql.before.split("\n"):
				pair = map(str.strip, line.split(": ", 1))
				if pair[0] == "Master_Log_File":
					log_file = pair[1]
				elif pair[0] == "Read_Master_Log_Pos":
					log_pos = pair[1]
			self._logger.debug("Retrieved log_file=%s, log_pos=%s", log_file, log_pos)

			self._change_master(
				host=host, 
				user=REPL_USER, 
				password=message.repl_password,
				log_file=log_file, 
				log_pos=log_pos, 
				mysql_user=ROOT_USER,
				mysql_password=message.root_password,
				timeout=self._change_master_timeout
			)
				
			self._logger.debug("Replication switched")
			bus.fire('mysql_change_master', host=host, log_file=log_file, log_pos=log_pos)
		else:
			self._logger.debug('Skip NewMasterUp. My replication role is master')		

	
	def on_before_reboot_start(self, *args, **kwargs):
		"""
		Stop MySQL and unplug storage
		"""
		self._stop_service()
		'''
		no need to plug/unplug storage since Scalarizr do EBS-root instances bundle 
		try:
			self._unplug_storage(self._sect.get(OPT_STORAGE_VOLUME_ID), self._storage_path)
		except ConfigParser.NoOptionError:
			self._logger.debug("Skip storage unplug. There is no configured storage.")
		'''

	def on_before_reboot_finish(self, *args, **kwargs):
		"""
		Start MySQL and plug storage
		"""
		'''
		try:
			self._plug_storage(self._sect.get(OPT_STORAGE_VOLUME_ID), self._storage_path)
		except ConfigParser.NoOptionError:
			self._logger.debug("Skip storage plug. There is no configured storage.")
		'''
		self._start_service()

	def on_host_init_response(self, message):
		"""
		Check mysql data in host init response
		@type message: scalarizr.messaging.Message
		@param message: HostInitResponse
		"""
		if not message.body.has_key("mysql"):
			raise HandlerError("HostInitResponse message for MySQL behaviour must have 'mysql' property")

		dir = os.path.dirname(self._volume_config_path)
		if not os.path.exists(dir):
			os.makedirs(dir)
		
		for key, file in ((OPT_VOLUME_CNF, self._volume_config_path), 
						(OPT_SNAPSHOT_CNF, self._snapshot_config_path)):
			if os.path.exists(file):
				os.remove(file)
			if key in message.mysql:
				Storage.backup_config(message.mysql[key], file)
			del message.mysql[key]
		
		self._logger.debug("Update mysql config with %s", message.mysql)
		self._update_config(message.mysql)

		
	@_reload_mycnf
	def on_before_host_up(self, message):
		"""
		Configure MySQL behaviour
		@type message: scalarizr.messaging.Message		
		@param message: HostUp message
		"""
		
		try:
			out = system2("my_print_defaults mysqld", shell=True)
			result = re.search("--datadir=(.*)", out[0], re.MULTILINE)
			if result:
				datadir = result.group(1)
				if os.path.isdir(datadir) and not os.path.isdir(os.path.join(datadir, 'mysql')):
					self._start_service()	
					self._stop_service()				
		except:
			pass

		repl = 'master' if int(self._cnf.rawini.get(CNF_SECTION, OPT_REPLICATION_MASTER)) else 'slave'
		if repl == 'master':
			bus.fire('before_mysql_configure', replication=repl)
			self._init_master(message)									  
		else:
			bus.fire('before_mysql_configure', replication=repl)
			self._init_slave(message)		
		
		bus.fire('service_configured', service_name=SERVICE_NAME, replication=repl)
	
	def _init_master(self, message):
		"""
		Initialize MySQL master
		@type message: scalarizr.messaging.Message 
		@param message: HostUp message
		"""
		self._logger.info("Initializing MySQL master")
		
		# Plug storage
		volume_cnf = Storage.restore_config(self._volume_config_path)
		try:
			snap_cnf = Storage.restore_config(self._snapshot_config_path)
			volume_cnf['snapshot'] = snap_cnf
		except IOError:
			pass
		self.storage_vol = self._plug_storage(mpoint=self._storage_path, vol=volume_cnf)
		Storage.backup_config(self.storage_vol.config(), self._volume_config_path)		
		
		# Stop MySQL server
		self._stop_service()
		self._flush_logs()
		
		msg_data = None
		storage_valid = self._storage_valid() # It's important to call it before _move_mysql_dir

		try:
			if not storage_valid and self._mysql_config.get('mysqld/datadir').find(self._data_dir) == 0:
				# When role was created from another mysql role it contains modified my.cnf settings 
				self._repair_original_mycnf()
			
			# Patch configuration
			self._move_mysql_dir('mysqld/datadir', self._data_dir + os.sep)
			self._move_mysql_dir('mysqld/log_bin', self._binlog_base)
			
			# Init replication
			self._replication_init(master=True)
			
			# If It's 1st init of mysql master storage
			if not storage_valid:
				self._copy_debian_cnf()
					
				# Add system users	
				root_password, repl_password, stat_password = \
						self._add_mysql_users(ROOT_USER, REPL_USER, STAT_USER)
				
				# Get binary logfile, logpos and create storage snapshot
				snap, log_file, log_pos = self._create_snapshot(ROOT_USER, root_password)
				Storage.backup_config(snap.config(), self._snapshot_config_path)
				
				# Update HostUp message 
				msg_data = dict(
					root_password=root_password,
					repl_password=repl_password,
					stat_password=stat_password,
					snapshot_config=snap.config(),
					volume_config=self.storage_vol.config(),
					log_file=log_file,
					log_pos=log_pos
				)
				
			# If volume has mysql storage directory structure (N-th init)
			else:
				# Get required configuration options
				root_password, = self._get_ini_options(OPT_REPL_PASSWORD)
				
				self._copy_debian_cnf_back()
				
				# Create snapshot
				snap, log_file, log_pos = self._create_snapshot(ROOT_USER, root_password)
				Storage.backup_config(snap.config(), self._snapshot_config_path)
				
				# Update HostUp message 
				msg_data = dict(
					snapshot_config=snap.config(),
					volume_config=self.storage_vol.config(),
					log_file=log_file, 
					log_pos=log_pos
				)
		except (BaseException, Exception):
			if not storage_valid and self._storage_path:
				# Perform cleanup
				system2('rm -rf %s' % os.path.join(self._storage_path, '*'), shell=True, raise_exc=False)
			raise
			
		if msg_data:
			message.mysql = msg_data
			del msg_data[OPT_SNAPSHOT_CNF], msg_data[OPT_VOLUME_CNF] 
			self._update_config(msg_data)
			

	def _init_slave(self, message):
		"""
		Initialize MySQL slave
		@type message: scalarizr.messaging.Message 
		@param message: HostUp message
		"""
		self._logger.info("Initializing MySQL slave")
		
		# Read required configuration options
		root_pass, repl_pass, log_file, log_pos = self._get_ini_options(
				OPT_ROOT_PASSWORD, OPT_REPL_PASSWORD, OPT_LOG_FILE, OPT_LOG_POS)
		
		if not self._storage_valid():
			self._logger.debug("Initialize slave storage")
			self.storage_vol = self._plug_storage(self._storage_path, 
					dict(snapshot=Storage.restore_config(self._snapshot_config_path)))			
			Storage.backup_config(self, self.storage_vol.config(), self._volume_config_path)
		
			
		# Stop MySQL
		self._stop_service()			
		self._flush_logs()
		
		# Change configuration files
		self._logger.info("Changing configuration files")
		self._move_mysql_dir('mysqld/datadir', self._data_dir)
		self._move_mysql_dir('mysqld/log_bin', self._binlog_base)
		self._replication_init(master=False)
		
		self._copy_debian_cnf_back()
		self._start_service()
		
		# Change replication master 
		master_host = None
		self._logger.info("Requesting master server")
		while not master_host:
			try:
				master_host = list(host 
					for host in self._queryenv.list_roles(self._role_name)[0].hosts 
					if host.replication_master)[0]
			except IndexError:
				self._logger.debug("QueryEnv respond with no mysql master. " + 
						"Waiting %d seconds before the next attempt", 5)
				time.sleep(5)
				
		self._logger.debug("Master server obtained (local_ip: %s, public_ip: %s)",
				master_host.internal_ip, master_host.external_ip)
		
		host = master_host.internal_ip or master_host.external_ip
		self._change_master( 
			host=host, 
			user=REPL_USER, 
			password=repl_pass,
			log_file=log_file, 
			log_pos=log_pos, 
			mysql_user=ROOT_USER,
			mysql_password=root_pass,
			timeout=self._change_master_timeout
		)
		
		# Update HostUp message
		message.mysql = dict(
			volume_config = self.storage_vol.config()
		)		
		
	def _plug_storage(self, mpoint, vol):
		if not isinstance(vol, Volume):
			vol = Storage.create(vol)
		try:
			vol.mount(mpoint)
		except StorageError, e:
			''' XXX: Crapy. We need to introduce error codes from fstool ''' 
			if 'you must specify the filesystem type' in str(e):
				vol.mkfs()
			else:
				raise
		return vol
	
	def _get_ini_options(self, *args):
		ret = []
		for opt in args:
			try:
				ret.append(self._cnf.rawini.get(CNF_SECTION, opt))
			except ConfigParser.Error:
				err = 'Required configuration option is missed in mysql.ini: %s' % opt
				raise HandlerError(err)
		return tuple(ret)
	
	def _copy_debian_cnf_back(self):
		debian_cnf = os.path.join(self._storage_path, 'debian.cnf')
		if disttool.is_debian_based() and os.path.exists(debian_cnf):
			self._logger.debug("Copying debian.cnf from storage to mysql configuration directory")
			shutil.copy(debian_cnf, '/etc/mysql/')
		
				
	def _copy_debian_cnf(self):
		if os.path.exists('/etc/mysql/debian.cnf'):
			self._logger.debug("Copying debian.cnf file to mysql storage")
			shutil.copy('/etc/mysql/debian.cnf', self._storage_path)		
	
	
	def _storage_valid(self, path=None):
		data_dir = os.path.join(path, STORAGE_DATA_DIR) if path else self._data_dir
		binlog_base = os.path.join(path, STORAGE_BINLOG) if path else self._binlog_base
		return os.path.exists(data_dir) and glob.glob(binlog_base + '*')


	def _create_snapshot(self, root_user, root_password, dry_run=False):
		is_repl_master, = self._get_ini_options(OPT_REPLICATION_MASTER)
		was_running = self._init_script.running
		try:
			if not was_running:
				self._start_service()
			
			# Lock tables
			sql = spawn_mysql(root_user, root_password)
			sql.sendline('FLUSH TABLES WITH READ LOCK;')
			sql.expect('mysql>')
			system2('sync', shell=True)
			if int(is_repl_master):
				sql.sendline('SHOW MASTER STATUS;')
				sql.expect('mysql>')
				
				# Retrieve log file and log position
				lines = sql.before		
				log_row = re.search(re.compile('^\|\s*([\w-]*\.\d*)\s*\|\s*(\d*)', re.M), lines)
				if log_row:
					log_file = log_row.group(1)
					log_pos = log_row.group(2)
				else:
					log_file = log_pos = None
			else:
				sql.sendline('SHOW SLAVE STATUS \G')
				sql.expect('mysql>')
				lines = sql.before
				log_row = re.search(re.compile('Relay_Master_Log_File:\s*(.*?)$.*?Exec_Master_Log_Pos:\s*(.*?)$', re.M | re.S), lines)
				if log_row:
					log_file = log_row.group(1).strip()
					log_pos = log_row.group(2).strip()
				else:
					log_file = log_pos = None

			# Creating storage snapshot
			snap = None if dry_run else self._create_storage_snapshot()
	
			sql.sendline('UNLOCK TABLES;\n')
			sql.close()
			return snap, log_file, log_pos
		
		finally:
			if not was_running:
				self._stop_service()

			
	def _create_storage_snapshot(self):
		self._logger.info("Creating storage snapshot")
		try:
			return self.storage_vol.snapshot()
		except StorageError, e:
			self._logger.error("Cannot create MySQL data snapshot. %s", e)
			raise
	
	def _repair_original_mycnf(self):
		self._mysql_config.set('mysqld/datadir', '/var/lib/mysql')
		self._mysql_config.remove('mysqld/log_bin')
	
	def _add_mysql_users(self, root_user, repl_user, stat_user, root_pass=None, repl_pass=None, stat_pass=None):
		self._stop_service()
		self._logger.info("Adding mysql system users")

		myd = self._start_mysql_skip_grant_tables()
		myclient = Popen(["/usr/bin/mysql"], stdin=PIPE, stdout=PIPE, stderr=PIPE)
		out = myclient.communicate('SELECT VERSION();')[0]
		mysql_ver_str = re.search(re.compile('\d*\.\d*\.\d*', re.MULTILINE), out)
		if mysql_ver_str:
			mysql_ver = version.StrictVersion(mysql_ver_str.group(0))
			priv_count = 28 if mysql_ver >= version.StrictVersion('5.1.6') else 26
		else:
			raise HandlerError("Cannot extract mysql version from string '%s'" % out)
	
		myclient = Popen(["/usr/bin/mysql"], stdin=PIPE, stdout=PIPE, stderr=PIPE)
		
		# Generate passwords
		root_password = root_pass if root_pass else re.sub('[^\w]','', cryptotool.keygen(20))
		repl_password = repl_pass if repl_pass else re.sub('[^\w]','', cryptotool.keygen(20))
		stat_password = stat_pass if stat_pass else re.sub('[^\w]','', cryptotool.keygen(20))
		
		#root_password, repl_password, stat_password = map(lambda x: re.sub('[^\w]','', cryptotool.keygen(20)), range(3))
		# Delete old users
		sql = "DELETE FROM mysql.user WHERE User in ('%s', '%s', '%s');" % (root_user, repl_user, stat_user)
		
		# Add users
		# scalr@localhost allow all
		sql += "INSERT INTO mysql.user VALUES('localhost','"+root_user+"',PASSWORD('"+root_password+"')" + ",'Y'"*priv_count + ",''"*4 +',0'*4+");"
		# scalr_repl@% allow Repl_slave_priv
		sql += " INSERT INTO mysql.user (Host, User, Password, Repl_slave_priv) VALUES ('%','"+repl_user+"',PASSWORD('"+repl_password+"'),'Y');"
		# scalr_stat@% allow Repl_client_priv
		sql += " INSERT INTO mysql.user (Host, User, Password, Repl_client_priv) VALUES ('%','"+stat_user+"',PASSWORD('"+stat_password+"'),'Y');"
		
		sql += " FLUSH PRIVILEGES;"
		out,err = myclient.communicate(sql)
		if err:
			raise HandlerError('Cannot add mysql users: %s', err)
		
		os.kill(myd.pid, signal.SIGTERM)
		time.sleep(3)
		self._start_service()

		self._update_config(dict(
			root_password=root_password,
			repl_password=repl_password,
			stat_password=stat_password
		))

		self._logger.debug("MySQL system users added")
		return (root_password, repl_password, stat_password)
	
	def _update_config(self, data): 
		self._cnf.update_ini(BEHAVIOUR, {CNF_SECTION: data})
		
	def _replication_init(self, master=True):
		if not os.path.exists('/etc/mysql'):
			os.makedirs('/etc/mysql')
			
		# Create replication config
		self._logger.info("Creating farm-replication config")
		repl_conf_path = '/etc/mysql/farm-replication.cnf'
		try:
			file = open(repl_conf_path, 'w')
		except IOError, e:
			self._logger.error('Cannot open %s: %s', repl_conf_path, e.strerror)
			raise
		else:
			server_id = 1 if master else int(random.random() * 100000)+1
			file.write('[mysqld]\nserver-id\t\t=\t'+ str(server_id)+'\nmaster-connect-retry\t\t=\t15\n')
			file.close()
			os.chmod(repl_conf_path, 0644)
			
		self._logger.debug("farm-replication config created")
		
		if not repl_conf_path in self._mysql_config.get_list('*/!include'):
			# Include farm-replication.cnf in my.cnf
			self._mysql_config.add('!include', repl_conf_path)
			
		# Patch networking
		for option in ['bind-address','skip-networking']:
			try:
				self._mysql_config.comment('mysqld/'+option)
			except:
				pass
		self.write_config()

		if disttool.is_debian_based():
			_add_apparmor_rules(repl_conf_path)	


	def _change_master(self, host, user, password, log_file, log_pos, 
					spawn=None, mysql_user=None, mysql_password=None, timeout=None):
		spawn = spawn or self.spawn_mysql(mysql_user, mysql_password)
		self._logger.info("Changing replication master to host %s (log_file: %s, log_pos: %s)", host, log_file, log_pos)
		
		# Changing replication master
		spawn.sendline('STOP SLAVE;')
		spawn.expect('mysql>')
		spawn.sendline('CHANGE MASTER TO MASTER_HOST="%(host)s", \
						MASTER_USER="%(user)s", \
						MASTER_PASSWORD="%(password)s", \
						MASTER_LOG_FILE="%(log_file)s", \
						MASTER_LOG_POS=%(log_pos)s;' % vars())
		spawn.expect('mysql>')
		
		# Starting slave
		spawn.sendline('START SLAVE;')
		spawn.expect('mysql>')
		status = spawn.before
		if re.search(re.compile('ERROR', re.MULTILINE), status):
			raise HandlerError('Cannot start mysql slave: %s' % status)
		
		def slave_status():
			spawn.sendline('SHOW SLAVE STATUS;')
			spawn.expect('mysql>')
			status = spawn.before.split('\r\n')[4].split('|')
			return (
				# io status
				status[11].strip(),
				# sql status
				status[12].strip(),
				# error 1
				status[17].strip(),
				# error 2
				status[18].strip()
			)

		try:
			time_until = time.time() + timeout
			status = None
			while time.time() >= time_until:
				status = slave_status()
				if status[0:2] == ('Yes', 'Yes'):
					break
				time.sleep(5)
			else:
				if status:
					if status[0] != 'Yes':
						err = 'IO Error while starting mysql slave'
					else:
						err = 'SQL Error while starting mysql slave' 
					err = str(err+': %s %s') % status[-2:]
					raise HandlerError(err)
				else:
					raise HandlerError('Cannot change replication master to %s' % (host))
		finally:
			spawn.close()
				
		self._logger.debug('Replication master is changed to host %s', host)		

	def _ping_mysql(self):
		for sock in self._init_script.socks:
			wait_sock(sock)
	
	def _start_mysql_skip_grant_tables(self):
		if os.path.exists(self._mysqld_path) and os.access(self._mysqld_path, os.X_OK):
			self._logger.debug("Starting mysql server with --skip-grant-tables")
			myd = Popen([self._mysqld_path, '--skip-grant-tables'], stdin=PIPE, stdout=PIPE, stderr=STDOUT, close_fds = True)
		else:
			self._logger.error("MySQL daemon '%s' doesn't exists", self._mysqld_path)
			return False
		self._ping_mysql()
		
		return myd
			
	def _move_mysql_dir(self, directive=None, dirname = None):

		# Retrieveing mysql user from passwd		
		mysql_user	= pwd.getpwnam("mysql")
		directory	= os.path.dirname(dirname)

		try:
			raw_value = self._mysql_config.get(directive)
			if not os.path.isdir(directory):
				os.makedirs(directory)
				src_dir = os.path.dirname(raw_value + "/") + "/"
				if os.path.isdir(src_dir):
					self._logger.debug('Copying mysql directory \'%s\' to \'%s\'', src_dir, directory)
					rsync = filetool.Rsync().archive()
					rsync.source(src_dir).dest(directory).exclude(['ib_logfile*'])
					system2(str(rsync), shell=True)
					self._mysql_config.set(directive, dirname)
				else:
					self._logger.debug('Mysql directory \'%s\' doesn\'t exist. Creating new in \'%s\'', src_dir, directory)
			else:
				self._mysql_config.set(directive, dirname)
				
		except NoPathError:
			self._logger.debug('There is no such option "%s" in mysql config.' % directive)
			if not os.path.isdir(directory):
				os.makedirs(directory)
			
			self._mysql_config.add(directive, dirname)

		self.write_config()
		# Recursively setting new directory permissions
		os.chown(directory, mysql_user.pw_uid, mysql_user.pw_gid)		
		try:
			for root, dirs, files in os.walk(directory):
				for dir in dirs:
					os.chown(os.path.join(root , dir), mysql_user.pw_uid, mysql_user.pw_gid)
				for file in files:
					os.chown(os.path.join(root, file), mysql_user.pw_uid, mysql_user.pw_gid)
		except OSError, e:
			self._logger.error('Cannot chown Mysql directory %s', directory)
			
		# Adding rules to apparmor config 
		if disttool.is_debian_based():
			_add_apparmor_rules(directory)
	
	def _flush_logs(self):
		if not os.path.exists(self._data_dir):
			return
		
		info_files = ['relay-log.info', 'master.info']
		files = os.listdir(self._data_dir)
		
		for file in files:
			if file in info_files or file.find('relay-bin') != -1:
				os.remove(os.path.join(self._data_dir, file))
				
	def write_config(self):
		self._mysql_config.write(self._mycnf_path)


_mycnf = None


def _spawn_mysqldump(args, user, password=None):
	try:
		pass
	except:
		pass

def spawn_mysql(user, password=None):
	try:
		exp = pexpect.spawn('/usr/bin/mysql -u ' + user + ' -p')
		exp.expect('Enter password:')
		exp.sendline(password or '')
		exp.expect('mysql>')
	except Exception, e:
		raise HandlerError('Cannot start mysql client tool: %s' % (e,))
	finally:
		return exp

def _add_apparmor_rules(directory):
	try:
		file = open('/etc/apparmor.d/usr.sbin.mysqld', 'r')
	except IOError, e:
		pass
	else:
		app_rules = file.read()
		file.close()
		if not re.search (directory, app_rules):
			file = open('/etc/apparmor.d/usr.sbin.mysqld', 'w')
			if os.path.isdir(directory):
				app_rules = re.sub(re.compile('(\.*)(\})', re.S), '\\1\n'+directory+'/ r,\n'+'\\2', app_rules)
				app_rules = re.sub(re.compile('(\.*)(\})', re.S), '\\1'+directory+'/** rwk,\n'+'\\2', app_rules)
			else:
				app_rules = re.sub(re.compile('(\.*)(\})', re.S), '\\1\n'+directory+' r,\n'+'\\2', app_rules)
			file.write(app_rules)
			file.close()
			apparmor_initd = ParametrizedInitScript('apparmor', '/etc/init.d/apparmor')
			try:
				apparmor_initd.reload()
			except InitdError, e:
				_logger.error('Cannot restart apparmor. %s', e)	


'''
class MysqlReplication(object):
	def __init__(self, mycnf):
		pass
	
	def setup(self, master):
		pass
	
	def change_master(self):
		pass
	
	def repair_defaults(self, writefile=True):
		pass

class MysqlStorage(object):
	SNAPSHOT_NAME = 'mysql-snap.json'
	VOLUME_NAME = 'mysql.json'
	
	def __init__(self, mycnf, volume, snapshot):
		pass
	
	def repair_defaults(self, writefile=True):
		pass
	
	def valid(self):
		pass
	
	def flush_logs(self):
		pass
	
	def plug(self):
		pass
	
	def unplug(self):
		pass
'''