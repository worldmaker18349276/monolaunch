#!/usr/bin/env python3
"""
uage:
with_roscore.py roslaunch <launch_file.launch> ...
with_roscore.py --filename <launch_file.launch> roslaunch ...
"""

import contextlib
from pathlib import Path
import shlex
import subprocess
import signal
import sys
import os
from typing import Tuple
from monolaunch.monoresource import Machine
import xml.etree.ElementTree as ET


@contextlib.contextmanager
def prun(command, force_exit=False, exit_timeout=10, **kwargs):
    """
    usage:
    with prun(["cmd", "arg1", "arg2"], stdout=subprocess.PIPE) as p_task: # spawn a process
        ... # do some works
        p_task.wait() # wait until done
    # it will try to interrupt the process (SIGINT)
    # if force_exit is True, kill it after {exit_timeout} sec
    """
    process = None
    try:
        kwargs = {
            "stdin": subprocess.PIPE,
            "stdout": None,
            "stderr": subprocess.STDOUT,
            "text": True,
            **kwargs,
        }
        process = subprocess.Popen(command, **kwargs)
        yield process
    finally:
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGINT)
            
            try:
                process.wait(timeout=exit_timeout)
            except subprocess.TimeoutExpired:
                if not force_exit: raise
                print(f"[with_roscore] fail to interrupt process {command}, will kill it")
                process.kill()
                process.wait()

def _get_local_and_master_machine(launch_file: Path) -> Tuple[Machine, Machine]:
    root = ET.parse(launch_file).getroot()

    machines = {
        machine.get("name"): machine.attrib
        for machine in root.findall("machine")
    }

    def get_machine(machine_name: str) -> Machine:
        machine_tag = machines.get(machine_name)
        if machine_tag is None:
            raise RuntimeError(f"[with_roscore] cannot find machine tag with name {machine_name!r}")
        user = machine_tag.get("user", "")
        password = machine_tag.get("password", "")
        address = machine_tag.get("address", "")
        env_loader = machine_tag.get("env-loader", "")
        return Machine(user=user, password=password, address=address, env_loader=tuple(shlex.split(env_loader)))

    local = get_machine("local")
    if not local.is_local():
        raise RuntimeError("[with_roscore] local machine is not local")

    master = None
    for node in root.findall("master"):
        machine_name = node.get("machine")
        if machine_name is None:
            raise RuntimeError("[with_roscore] cannot find machine attr in the master tag")
        master = get_machine(machine_name)
        break

    if master is None:
        raise RuntimeError("[with_roscore] cannot find master tag")

    return local, master

def main():
    if len(sys.argv) <= 1:
        raise ValueError("[with_roscore] usage: with_roscore.py roslaunch <launch_file.launch> ...")

    if sys.argv[1] != "--filename":
        print("[with_roscore] --filename is not provided, will determine it automatically")
        if len(sys.argv) < 3 or not sys.argv[1].endswith("/roslaunch") or not sys.argv[2].endswith(".launch"):
            raise ValueError("[with_roscore] `with_roscore.py` must be prefixed before `roslaunch <launch_file.launch> ...`")
        filename = Path(sys.argv[2]).resolve()
        sys.argv[1:1] = ["--filename", str(filename)]

    if len(sys.argv) < 4 or not sys.argv[2].endswith(".launch") or not sys.argv[3].endswith("/roslaunch"):
        raise ValueError("[with_roscore] `with_roscore.py --filename <filename>` must be prefixed before `roslaunch ...`")
    if not Path(sys.argv[2]).exists():
        raise ValueError(f"[with_roscore] given filename {sys.argv[2]} doesn't exist")

    filename = Path(sys.argv[2])
    command = sys.argv[3:]
    command[1:1] = ["--wait"] # force to wait my roscore

    local, master = _get_local_and_master_machine(filename)

    ros_master_uri = f"http://{master.address}:11311"
    os.environ["ROS_MASTER_URI"] = ros_master_uri

    roscore = master.command(["roscore"])
    command = local.command(command)

    print("[with_roscore] run roscore:\n" + shlex.join(roscore))
    #                  _________________ to prevent SIGINT propagates into subprocess
    with prun(roscore, start_new_session=True):
        print("[with_roscore] run roslaunch:\n" + shlex.join(command))
        with prun(command, force_exit=True, exit_timeout=30) as roslaunch_proc:
            result_returncode = roslaunch_proc.wait()
    sys.exit(result_returncode)

if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
