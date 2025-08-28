import logging
import sys
import json
import subprocess
from cmosys import CmoSys

class CmoOllama:
    def __init__(self,comfig_file):
        self.cmosys = CmoSys(comfig_file)
        
    def callOllamaWithMovie(self,model,question,movie):
        try:
            # call_ollama_movie.bash "qwen2.5vl:3b" "person_animal_question" /home/pi/dev/cmo_ollama/test_data/raccoon.mp4
            #command = ['bash', script_path] + list(args)
            self.cmosys.log.info(f"caling ollama for {movie}")

            args = (model,question,movie)
            command = ['bash', "/home/pi/bin/call_ollama_movie.bash"] + list(args)
            print(f"*** ollamma before call : model={model} quesion={question} movie={movie}")
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            print(f"*** ollama stdput: {result.stdout}")
            print(f"*** ollama stderr: {result.stderr}")
            print(f"*** ollama returned : {data}")
            return data
        except subprocess.CalledProcessError as e:
            print(f"Error executing Bash script: {e}")
            print(f"Stderr: {e.stderr}")
            return None, e.stderr
        except FileNotFoundError:
            print(f"Error: Bash script not found at {script_path}")
            return None, "File not found"
        except Exception as e:
            print(f"An unexpected error occurred calling ollama: {e}")
            return None, str(e)
        
    def getErrorJson(self):
        errorJson = {
            "error": True
        }
        data = json.loads(errorJson)
        return data

    def isDataValue(self,data,key):
        if key in data:
            return data[key]
        else:
            return False
        
    def isAlert(self,data):
        raccoon = self.isDataValue(data,"raccoon")
        animal = self.isDataValue(data,"animal")
        send = ( raccoon )
        return(send)
    
    def isSendMovie(self,data):
        person = self.isDataValue(data,"person")
        cat = self.isDataValue(data,"cat")
        dog = self.isDataValue(data,"dog")
        raccoon = self.isDataValue(data,"raccoon")
        animal = self.isDataValue(data,"animal")
        legs = self.isDataValue(data,"legs")
        send = ( person or cat or dog or raccoon or animal or legs)
        return(send)
    
    def isOnlyAnimal(self,data):
        person = self.isDataValue(data,"person")
        cat = self.isDataValue(data,"cat")
        dog = self.isDataValue(data,"dog")
        raccoon = self.isDataValue(data,"raccoon")
        animal = self.isDataValue(data,"animal")
        error = self.isDataValue(data,"error")
        only_animal = ( not(person) and not(cat) and not(dog) and not(raccoon) and animal )
        return(only_animal)

    def doTests(self):
        data = self.callOllamaWithMovie("qwen2.5vl:3b","person_animal_question","/home/pi/dev/cmo_ollama/test_data/raccoon.mp4")
        print(data)
        print("raccoon=",self.isDataValue(data,"raccoon"))
        print("bad=",self.isDataValue(data,"bad"))
        print("alert=",self.isAlert(data))
        print("send_movie=",self.isSendMovie(data))
        print("only_animal=",self.isOnlyAnimal(data))
        
#if(len(sys.argv)!=2) :
#    print("Invalid arguments : your_config_file.json : the \"output_image_dir\" is the dir which will be processed")
#    print("You gave %i arguments" % (len(sys.argv)))
#    quit()
#
#comoollama = CmoOllama(sys.argv[1])
#comoollama.doTests()



#se = SendImage(config_file)
#se.runForever()
