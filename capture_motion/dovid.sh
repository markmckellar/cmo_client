#!/bin/bash

rm -Rf /home/pi/dev/cmo_1/capture_motion/uploads/*

cd /home/pi/dev/cmo_1/capture_motion
python3 make_video.py config_test.json
