#!/bin/bash

rm -Rf rm -Rf /home/pi/dev/cmo_1/capture_motion/images/*

cd /home/pi/dev/cmo_1/capture_motion
python3 send_image.py config_test.json
