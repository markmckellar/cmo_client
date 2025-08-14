#!/usr/bin/python3
import cv2
import numpy as np
import glob
import os 
import sys
import json
import time
import shutil
import subprocess
from shutil import copyfile
from cmosys import CmoSys


class MakeVideo :

    def __init__(self,comfig_file):
        self.cmosys = CmoSys(comfig_file)


    def make_video_from_image_dir_ffmpeg(self,image_folder,image_input_pattern,json_name,video_name,output_image_dir,video_json) :
            os.makedirs(output_image_dir+"/images", exist_ok=True)

            ### 1 : make the video
            command = f"ffmpeg -y -framerate 24 -i {image_folder}/{image_input_pattern} -vf \"pad=ceil(iw/2)*2:ceil(ih/2)*2\" {video_name}"
            self.cmosys.log.info(f"running:{command}")
            os.system(command)

            ### 2 : Find he length of the video
            command = f"ffprobe {video_name} -show_format 2>&1 | sed -n 's/duration=//p'"
            self.cmosys.log.info(f"running:{command}")
            run_time =  subprocess.check_output(command, shell=True)
            self.cmosys.log.info(f"got run_time of {run_time}")
            try :
            	video_json["me_time"] = float(run_time.strip().decode())
            except :
            	self.cmosys.log.info(f"got error converting run_time of {run_time}")


            ### 3 : move over any imags
            # make the dir if it does not exist
            #os.makedirs(os.path.dirname(output_image_dir), exist_ok=True)

            rep_image = None

            all_image_files = [imageFile for imageFile in os.listdir(image_folder) if imageFile.endswith(".jpg")]
            all_image_files.sort()
            total_files = len(all_image_files)

            counter = 0
            for image_file in all_image_files :
                    # str(self.motion_event_counter).rjust(4, '0')
                    src = image_folder+"/"+image_file
                    dst = output_image_dir+"/images/"+image_file
                    self.cmosys.log.info(f"output_image_dir={output_image_dir} src={src} dest={dst}")
                    copyfile(src, dst)
                    video_json["me_image_array"].append(image_file)
                    counter += 1
                    if( (counter/total_files) >= 0.5 and rep_image is None ) : rep_image = image_file

            if(rep_image is not None) :
                src = image_folder+"/"+rep_image
                dst = output_image_dir+"/me_rep_image.jpg"
                copyfile(src, dst)
                video_json["me_rep_image"] = "me_rep_image.jpg"
            
            ### 4 : grab all of the json
            all_json_files = [jsonFile for jsonFile in os.listdir(image_folder) if jsonFile.endswith(".json")]
            all_json_files.sort()

            for json_file in all_json_files :
                    motion_event_json = {}
                    self.cmosys.log.info(f"opening json file:{image_folder}/{json_file}")
                    with open(image_folder+"/"+json_file, 'r') as f:
                        motion_event_json = json.load(f)   
                        video_json["me_delta_array"].append(motion_event_json)

            ### 5 : write out the json
            with open( json_name, 'w') as outfile:
                                outfile.write( json.dumps(video_json,indent=4) )




    def process(self) :
        
        self.cmosys.refreshConfig()
        self.config = self.cmosys.config_json["motion_event_processor"]

        # is it turned on?
        if(self.config["active"])  :

            folder = self.config["watch_dir"]
            self.cmosys.log.info(f"starting processing:folder={folder}")

            subfolders = [ f.path for f in os.scandir(folder) if f.is_dir() ]
            subfolders.sort()  

            #make_video = MakeVideo()

            # look in the folder and process each file
            for image_dir in subfolders: 
                size = None
                img_array_for_a_dir = []

                # then the files...
                image_dir_base = os.path.basename(image_dir)

                ### If the file has "not_ready" in it do not procss
                if self.cmosys.notReadyTag() not in image_dir_base :
                    # we make a video...
                    not_ready_dir = f"{image_dir_base}_{self.cmosys.notReadyTag()}"
                    video_name = f"{self.config['output_dir']}/{not_ready_dir}/me_movie.mp4"
                    # we make a master json file...
                    json_name = f"{self.config['output_dir']}/{not_ready_dir}/me_data.json"

                    # we write so images to an out dir
                    output_image_dir = f"{self.config['output_dir']}/{not_ready_dir}"

                    image_input_pattern = self.config["image_input_pattern"]
                    self.cmosys.log.info(f"processing:image_dir={image_dir} image_input_pattern={image_input_pattern} write to={video_name} image_dir_base={image_dir_base}")
                    me_name = time.strftime("%Y%m%d")


                    self.make_video_from_image_dir_ffmpeg(
                        image_dir,
                        image_input_pattern,
                        json_name,
                        video_name,
                        output_image_dir,
                        {
                            "me_group":self.cmosys.config_json["me_group"],
                            "me_event_group":me_name,
                            "me_name":image_dir_base,
                            "me_tag":image_dir_base,
                            "me_image_array":[],
                            "me_delta_array":[],
                            "me_time":-1,
                            "me_video_name":"me_movie.mp4",
                            "me_rep_image":"me_rep_image.jpg",
                            "me_json_name":"me_data.json"
                        })
                    os.rename(output_image_dir,f"{self.config['output_dir']}/{image_dir_base}")


                    try :
                        if(self.config["delete_when_done"]) : shutil.rmtree(image_dir)
                    except :
                        self.cmosys.log.info(f"had an error removing {image_dir}")

    def runForever(self) :
        while(True) :
            self.process()
            sleep_time = self.config["sleep_time"]
            self.cmosys.log.info(f"sleeping for {sleep_time}...")
            time.sleep(sleep_time)

if(len(sys.argv)!=2) :
    print("Invalid arguments : your_config_file.json : the \"output_image_dir\" is the dir which will be processed")
    print("You gave %i arguments" % (len(sys.argv)))
    quit()
config_file = sys.argv[1]

mv = MakeVideo(config_file)
mv.runForever()
