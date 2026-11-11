#!/usr/bin/env python3

import sys
import monolaunch.monoresource

if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        raise ValueError("usage: sync_latest_logs.py <launch_file.launch> [<last_nth_zero_based> | <uuid>]")
    uuid = "latest"
    if len(sys.argv) >= 3:
        try:
            last_nth_zero_based = int(sys.argv[2])
        except:
            uuid = sys.argv[2]
        else:
            uuid = monolaunch.monoresource.find_log_dir(sys.argv[1], last_nth_zero_based).name
    monolaunch.monoresource.sync_logs(sys.argv[1], uuid)
