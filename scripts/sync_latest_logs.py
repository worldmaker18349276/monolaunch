#!/usr/bin/env python3

import sys
import monolaunch.monoresource

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise ValueError("usage: sync_latest_logs.py <launch_file.launch>")
    monolaunch.monoresource.sync_latest_logs(sys.argv[1])
