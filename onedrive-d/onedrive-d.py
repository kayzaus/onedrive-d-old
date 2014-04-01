#!/usr/bin/python

# Warning: Rely heavily on system time and if the timestamp is screwed there may be unwanted file deletions.

import sys, os, gc, subprocess, signal, yaml
import threading, Queue, time
import csv,StringIO
import calendar
from dateutil import parser

# Task class models a generic task to be performed by a TaskWorker
# Tasks are put in the thread-safe Queue object
class Task():
	def __init__(self, type, p1, p2, timeStamp = None):
		self.type = type
		self.p1 = p1 # mostly used as a local path
		self.p2 = p2 # mostly used as a remote path
		if timeStamp!= None:
			self.timeStamp = timeStamp	# time, etc.
	
	def debug(self):
		return "type=" + self.type + " | localPath=" + self.p1 + " | remotePath=" + self.p2
	
# TaskWorker objects consumes the tasks in taskQueue
# sleep when in idle
class TaskWorker(threading.Thread):
	WORKER_SLEEP_INTERVAL = 5 # in seconds
	
	tasksComsumed = 0
	
	def __init__(self):
		threading.Thread.__init__(self)
		self.daemon = True
		print self.getName() + " (worker): initialied"
	
	def getArgs(self, t):
		return {
			#"recent": [],
			#"info": [],
			#"info_set": [],
			"mv": ["mv", t.p1, t.p2],
			#"link": [],
			#"ls": [],
			"mkdir": ["mkdir", t.p1],	# mkdir path NOT RECURSIVE!
			"get": ["get", t.p2, t.p1],	# get remote_file local_path
			"put": ["put", t.p1, t.p2],	# put local_file remote_dir
			"cp": ["cp", t.p1, t.p2],	# cp file folder
			"rm": ["rm", t.p1]
		}[t.type]
	
	def consume(self, t):
		args = self.getArgs(t)
		subp = subprocess.Popen(['skydrive-cli'] + args, stdout=subprocess.PIPE)
		ret = subp.communicate()
		if t.type == "get":
			old_mtime = os.stat(t.p1).st_mtime
			new_mtime = calendar.timegm(parser.parse(t.timeStamp).utctimetuple())
			os.utime(t.p1, (new_mtime, new_mtime))
			new_old_mtime = os.stat(t.p1).st_mtime
			print self.getName() + ": " + t.p1 + " Old_mtime is " + str(old_mtime) + " and new_mtime is " + str(new_mtime) + " and is changed to " + str(new_old_mtime)
		if ret[0] != None:
			print "subprocess stdout: " + ret[0]
		if ret[1] != None:
			print "subprocess stderr: " + ret[1]
		print self.getName() + ": executed the " + str(self.tasksComsumed) + " task: " + t.debug()
		
		del t
		self.tasksComsumed += 1
	
	def run(self):
		while True:
			if stopEvent.is_set():
				break
			elif taskQueue.empty():
				time.sleep(self.WORKER_SLEEP_INTERVAL)
			else:
				task = taskQueue.get()
				self.consume(task)
				taskQueue.task_done()

# DirScanner represents either a file entry or a dir entry in the OneDrive repository
# it uses a single thread to process a directory entry
class DirScanner(threading.Thread):
	_raw_log = []
	_ent_list = []
	_remotePath = ""
	_localPath = ""
	
	def __init__(self, localPath, remotePath):
		threading.Thread.__init__(self)
		self.daemon = True
		scanner_threads_lock.acquire()
		scanner_threads.append(self)
		scanner_threads_lock.release()
		self._localPath = localPath
		self._remotePath = remotePath
		print self.getName() + ": Start scanning dir " + remotePath + " (locally at \"" + localPath + "\")"
		self.ls()
	
	def ls(self):
		subp = subprocess.Popen(['skydrive-cli', 'ls', '--objects', self._remotePath], stdout=subprocess.PIPE)
		log = subp.communicate()[0]
		self._raw_log = yaml.safe_load(log)
	
	def run(self):
		self.merge()
	
	# list the current dirs and files in the local repo, and in merge() upload / delete entries accordingly
	def pre_merge(self):
		# if remote repo has a dir that does not exist locally
		# make it and start merging
		if not os.path.exists(self._localPath):
			try:
				os.mkdir(self._localPath)
			except OSError as exc: 
					if exc.errno == errno.EEXIST and os.path.isdir(self._localPath):
						pass
		else:
			# if the local path exists, record what is in the local path
			self._ent_list = os.listdir(self._localPath)
	
	# recursively merge the remote files and dirs into local repo
	def merge(self):
		self.pre_merge()
		
		if self._raw_log == None:
			return
		for entry in self._raw_log:
			if os.path.exists(self._localPath + "/" + entry["name"]):
				print self.getName() + ": Oops, " + self._localPath + "/" + entry["name"] + " exists."
				# do some merge
				self.checkout(entry, True)
				# after sync-ing
				del self._ent_list[self._ent_list.index(entry["name"])] # remove the ent from untouched list
			else:
				print self.getName() + ": Wow, " + self._localPath + "/" + entry["name"] + " does not exist."
				self.checkout(entry, False)
		
		self.post_merge()
	
	# checkout one entry, either a dir or a file, from the log
	def checkout(self, entry, isExistent = False):
		if entry["type"] == "file" or entry["type"] == "photo" or entry["type"] == "audio" or entry["type"] == "video":
			if isExistent:
				# assert for now
				assert os.path.isfile(self._localPath + "/" + entry["name"])
				local_mtime = os.stat(self._localPath + "/" + entry["name"]).st_mtime
				remote_mtime = calendar.timegm(parser.parse(entry["client_updated_time"]).utctimetuple())
				if local_mtime == remote_mtime:
					print self.getName() + ": " + self._localPath + "/" + entry["name"] + " wasn't changed. Skip it."
					return
				elif local_mtime > remote_mtime:
					print self.getName() + ": Local file \"" + self._localPath + "/" + entry["name"] + "\" is newer. Upload it..."
					taskQueue.put(Task("put", self._localPath + "/" + entry["name"], self._remotePath))
				else:
					print self.getName() + ": Local file \"" + self._localPath + "/" + entry["name"] + "\" is older. Download it..."
					taskQueue.put(Task("get", self._localPath + "/" + entry["name"], self._remotePath + "/" + entry["name"], entry["client_updated_time"]))
				
				#print self.getName() + ": adding task to sync " + self._localPath + "/" + entry["name"]
				#taskQueue.put(Task("stub", self._localPath + "/" + entry["name"], self._remotePath + "/" + entry["name"]))
			else:
				# if not existent, get the file to local repo
				taskQueue.put(Task("get", self._localPath + "/" + entry["name"], self._remotePath + "/" + entry["name"], entry["client_updated_time"]))
		else:
			print self.getName() + ": scanning dir " + self._localPath + "/" + entry["name"]
			ent = DirScanner(self._localPath + "/" + entry["name"], self._remotePath + "/" + entry["name"])
			ent.start()
	
	# process untouched files during merge
	def post_merge(self):
		# there is untouched item in current dir
		if self._ent_list != []:
			print self.getName() + ": The following items are untouched yet:\n" + str(self._ent_list)
			
			for entry in self._ent_list:
				# assume to upload all of them
				# if it is a file
				if os.path.isfile(self._localPath + "/" + entry):
					taskQueue.put(Task("put", self._localPath + "/" + entry, self._remotePath))
				else:
					# if not, then it is a dir
					print self.getName() + ": for now skip the untouched dir \"" + self._localPath + "/" + entry + "\""
		
		print self.getName() + ": done."
		# new logs should get from recent list
	
	# print the internal storage
	def debug(self):
		print "localPath: " + self._localPath + ""
		print "remotePath: " + self._remotePath + ""
		print self._raw_log
		print "\n"
		print self._ent_list
		print "\n"

# LocalMonitor runs inotifywait component and parses the log
# when an event is issued, parse it and add work to the task queue.
class LocalMonitor(threading.Thread):
	MONITOR_SLEEP_INTERVAL = 2 # in seconds
	MOVED_FROM_BUF = []
	
	event_buffer = {}
	
	def __init__(self, rootPath):
		threading.Thread.__init__(self)
		# self.daemon = True
		self.rootPath = rootPath
	
	def handle(self, logItem):
		print "received a task: "
		print logItem
		
		if "MOVED_FROM" in logItem[1] and self.MOVED_FROM_BUF == []:
			self.MOVED_FROM_BUF = logItem
			return
		elif "MOVED_TO" in logItem[1]:
			if "ISDIR" not in logItem[1]:
				taskQueue.put(Task("mv", self.MOVED_FROM_BUF[0].replace(self.rootPath, "") + self.MOVED_FROM_BUF[2], logItem[0].replace(self.rootPath, "")))
			else:
				taskQueue.put(Task("mv", self.MOVED_FROM_BUF[0].replace(self.rootPath, "") + self.MOVED_FROM_BUF[2] , logItem[0].replace(self.rootPath, "")))
			self.MOVED_FROM_BUF = []
		else:
			# remove to recycle bin, may be subject to lag
			if self.MOVED_FROM_BUF != []:
				taskQueue.put(Task("rm", self.MOVED_FROM_BUF[0].replace(self.rootPath, "") + self.MOVED_FROM_BUF[2], ""))
				self.MOVED_FROM_BUF = []
			elif "MOVED_FROM" in logItem[1]:
				taskQueue.put(Task("rm", logItem[0].replace(self.rootPath, "") + logItem[2], ""))
			elif "DELETE" in logItem[1]:
				taskQueue.put(Task("rm", logItem[0].replace(self.rootPath, "") + logItem[2], ""))
			elif "CREATE" == logItem[1]:
				# self.event_buffer[str(logItem[0]) + logItem[2]] = logItem
				pass
			elif "CLOSE_WRITE" in logItem[1]:
				# simply upload the newly written file
				#if logItem[0] + logItem[2] in self.event_buffer:
				#	if self.event_buffer[logItem[0] + logItem[2]][1] == "CREATE":
				taskQueue.put(Task("put", logItem[0] + logItem[2], logItem[0].replace(self.rootPath, "")))	# p2 is folder
				#	else:
				#		pass
	
	def run(self):
		subp = subprocess.Popen(['inotifywait', '-e', 'unmount,close_write,create,delete,delete_self,move', '-cmr', self.rootPath], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
		while True:
			# I think stdout buffer is fine for now
			if stopEvent.is_set():
				break
			line = subp.stdout.readline()
			if line == "":
				if self.MOVED_FROM_BUF != []:
					self.handle(self.MOVED_FROM_BUF)
				time.sleep(self.MONITOR_SLEEP_INTERVAL)
			elif line[0] == "/":
				line = line.rstrip()
				csv_entry = csv.reader(StringIO.StringIO(line))
				for x in csv_entry:
					self.handle(x)

# RemoteMonitor periodically fetches the most recent changes from OneDrive remote repo
# if there are unlocalized changes, generate the tasks
class RemoteMonitor(threading.Thread):
	MONITOR_SLEEP_INTERVAL = 2 # in seconds
	
	def __init__(self, rootPath):
		threading.Thread.__init__(self)
	
	def run(self):
		pass

CONF_PATH = "~/.onedrive"
NUM_OF_WORKERS = 4	

f = open(os.path.expanduser(CONF_PATH + "/user.conf"), "r")
CONF = yaml.safe_load(f)
f.close()

scanner_threads = []
scanner_threads_lock = threading.Lock()

taskQueue = Queue.Queue()

worker_threads = []
stopEvent = threading.Event()

for i in range(NUM_OF_WORKERS):
	w = TaskWorker()
	worker_threads.append(w)
	w.start()

# commented for local testing's purpose
DirScanner(CONF["rootPath"], "").start()

for t in scanner_threads:
	t.join()

taskQueue.join()

print "Main: all done."

# Main thread then should create monitor and let workers continually consume the queue

print "Main: create monitor"

local_mon = LocalMonitor(CONF["rootPath"])
local_mon.start()

def signal_handler(signal, frame):
	print 'got signal ' + str(signal) + '!'
	stopEvent.set()
	for w in worker_threads:
		w.join()
	sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

signal.pause()

# remote_mon = RemoteMonitor()
# remote_mon.start()
