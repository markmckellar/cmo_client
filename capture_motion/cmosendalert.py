import logging
import sys
import json
import subprocess
import pychromecast
from cmosys import CmoSys

# http://173.255.215.223:9090/cmo_client/alerts/raccoon_alert.mp3
# http://173.255.215.223:9090/cmo_client/alerts/unknown_animal_alert.mp3
#Upstairs MAC address: FO:5C:77:3E:F5:25
#IP : 192.168.1.251
#
#Bedroom : MAC address: D8:EB:46:79:3B:E8
#IP 192.168.1.240

class CmoSendAlert:
    def __init__(self,comfig_file):
        self.cmosys = CmoSys(comfig_file)
        
   
    def doTests(self):
        chromecasts, browser = pychromecast.get_listed_chromecasts() 
        for cc in chromecasts:
            print(cc.device.friendly_name)
        cast = pychromecast.Chromecast("192.168.1.251")  # Replace with your Chromecast's IP
        cast.wait() # Wait for the Chromecast to be ready
        mp3_url = "http://173.255.215.223:9090/cmo_client/alerts/raccoon_alert.mp3"  # Replace with the actual URL
        cast.media_controller.play_media(mp3_url, "audio/mp3")

        
if(len(sys.argv)!=2) :
    print("Invalid arguments : your_config_file.json : the \"mp3-file\" ")
    print("You gave %i arguments" % (len(sys.argv)))
    quit()

cmosendalert = CmoSendAlert(sys.argv[1])
cmosendalert.doTests()



#se = SendImage(config_file)
#se.runForever()