#!/bin/bash

cmo_dir="/home/pi/dev/cmo_1/capture_motion"
process_name="send_image.py"

cd $cmo_dir

if ps aux | grep -v grep | grep -q "python3.*$process_name"; then
    echo "$process_name is already running." >> $cmo_dir/logs/send_image.log 2>&1
else
    	echo "$process_name is not running, starting" > $cmo_dir/logs/send_image.log 2>&1
	#env >> $cmo_dir/logs/send_image.log 2>&1
	rm -Rf rm -Rf $cmo_dir/images/* >> $cmo_dir/logs/send_image.log 2>&1
	python3 send_image.py config_test.json >> $cmo_dir/logs/send_image.log 2>&1
fi
