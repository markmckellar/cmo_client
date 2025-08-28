#!/usr/bin/python3

import sys 
import time
import requests
import os
import socket       
import logging
import json
from cmosys import CmoSys
import shutil


class SendImage :

	def __init__(self,comfig_file):
		self.cmosys = CmoSys(comfig_file)

	def process(self) :
		self.cmosys.refreshConfig()
		for one_config in cmosys.config_json["upload_servers"] :

			self.config = one_config
			sleep_time = self.config["sleep_time"]
			#server_url = self.config["server"]
			if(self.config["active"])  :
				watch_dir = self.config["watch_dir"] 

				subfolders = [ f.path for f in os.scandir(watch_dir) if f.is_dir() ]
				subfolders.sort()  

				for me_event_dir in subfolders:

					if self.cmosys.notReadyTag() not in me_event_dir :

						###full_me_event_dir = watch_dir + "/"+ me_event_dir
						#print(full_file_name)


						me_data = self.cmosys.readInJsonFile(f"{me_event_dir}/{self.cmosys.config_json['me_data_file_name']}")

						me_group = me_data['me_group']
						me_event_group = me_data['me_event_group']
						me_name = me_data['me_name']

				
						self.sendFile(me_event_dir,me_group,me_event_group,me_name,'',me_data['me_video_name'])
						self.sendFile(me_event_dir,me_group,me_event_group,me_name,'',me_data['me_rep_image'])
						self.sendFile(me_event_dir,me_group,me_event_group,me_name,'',me_data['me_json_name'])
						if(self.config["send_images"]) :
							for image_file in me_data['me_image_array'] :
								self.sendFile(me_event_dir+"/images",me_group,me_event_group,me_name,'images',image_file)

						if(self.config["delete_when_done"]) : 
							shutil.rmtree(me_event_dir) 


	def sendFile(self,me_event_dir,me_group,me_event_group,me_name,image_dir,file_name) :	
		server_url = self.config["server"]
		sleep_time = self.config["sleep_time"]
		watch_dir = self.config["watch_dir"]

		server_url_and_api = f"{self.config['server']}/{self.config['endpoint']}/{me_group}/{me_event_group}/{me_name}?image_dir={image_dir}"


		full_file_name = f"{me_event_dir}/{file_name}"

		self.cmosys.log.info(f"  working on {me_event_dir} to {server_url_and_api} file is {full_file_name}")

		# me_data = self.cmosys.readInJsonFile(f"{me_event_dir}/{self.cmosys.config_json['me_data_file_name']}")
		# self.cmosys.log.info(f"     {me_event_dir} : me_video_name : {me_data['me_video_name']}")
		# self.cmosys.log.info(f"     {me_event_dir} : me_rep_image : {me_data['me_rep_image']}")
		# self.cmosys.log.info(f"     {me_event_dir} : me_json_name : {me_data['me_json_name']}")


		try :
			with open(full_file_name, 'rb') as f:
				r = requests.post(server_url_and_api, files={'file': f})
				self.cmosys.log.info("transimited : " + full_file_name)

		except :
			self.cmosys.log.info("Unexpected error sending file (will sleep):", sys.exc_info()[0])
			self.cmosys.log.info(f"sleeping for {sleep_time}...")
			time.sleep(sleep_time)
	

	def runForever(self) :
		
		while(True) :
			self.process()
			sleep_time = self.config["sleep_time"]
			self.cmosys.log.info(f"sleeping for {sleep_time}...")
			time.sleep(sleep_time)



# get the config file 

if(len(sys.argv)!=2) :
	print("Invalid arguments : your_config_file.json : the \"output_image_dir\" is the dir which will be processed")
	print("You gave %i arguments" % (len(sys.argv)))
	quit()

cmosys = CmoSys(sys.argv[1])
config_file = sys.argv[1]

se = SendImage(config_file)
se.runForever()
