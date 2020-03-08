# capture motion client



### install docker
```
curl -fsSL get.docker.com -o get-docker.sh && sh get-docker.sh
sudo groupadd docker
sudo gpasswd -a $USER docker
# you may need to reboot or start a new shell for the perms to be set up
docker run hello-world
```
## docker compose
```
sudo apt-get install libffi-dev libssl-dev
sudo apt-get install -y python python-pip
sudo apt-get remove python-configparser
sudo pip install docker-compose
```
### to stream camera to web
```
cd stream_to_web
# build the docker
docker build -t cv_docker_stream_vid -f cv_docker_stream_vid  .
# run the docker
docker run -it --rm  -d -p 5000:5000 --device /dev/video0 --name cv_docker_stream_vid cv_docker_stream_vid
# navigate the browser to the local webpage : http://<host ip>:5000/
# list the dockers running
docker -ps
# stop the docker
docker stop cv_docker_stream_vid
```

### to capture motion
```
cd capture_motion
docker build -t cv_docker_captrue_motion -f cv_docker_captrue_motion  .
docker run -it -d --rm  -v `pwd`/images:/images --device /dev/video0 --name cv_docker_captrue_motion cv_docker_captrue_motion
# list the dockers running
docker -ps
# stop the docker
docker stop cv_docker_captrue_motion
```

#### if you cant see video in docker :
```sudo modprobe bcm2835-v4l2 
# to make it happe nafer very boot add 
bcm2835-v4l2
# to this file
```
