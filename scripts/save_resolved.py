#!/usr/bin/env python3

import sys
import rospy
import rospkg
import shutil
from pathlib import Path

if __name__ == "__main__":
    run_id = rospy.get_param("/run_id")
    log_dir = rospkg.get_log_dir()
    for resolved_file in sys.argv[1:]:
        if resolved_file == "--": break
        resolved_file = Path(resolved_file)
        shutil.copyfile(resolved_file, f"{log_dir}/{run_id}/{resolved_file.name}")
