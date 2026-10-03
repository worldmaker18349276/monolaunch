"""
sync resources before launch, so that resources can be managed in single place.

it uses commands: ssh, sshpass, rsync
"""
import contextlib
from inspect import cleandoc
import os
import signal
import sys
import re
import shlex
import subprocess
import dataclasses
from pathlib import Path
import socket

from typing import Any, Generator, List, Optional, Sequence, Tuple, cast
import urllib.parse
from monolaunch.yaml_utils import FieldAccessError, assert_JSON, PathWithJPointer, load_YAML, urlquote

# TODO: typecheck user input

IP_REGEX = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")

def get_local_addresses() -> List[str]:
    import rosgraph.network
    return rosgraph.network.get_local_addresses()

def getuser() -> str:
    import getpass
    return getpass.getuser()


class SchemeParseError(Exception):
    def __init__(self, scheme: str, url: str, format: str = ""):
        self.scheme = scheme
        self.url = url
        self.format = format
    
    def __str__(self):
        return f"invalid {self.scheme} scheme url: {self.url}" + (f"\nexpected format: {self.format}" if self.format else "")

@dataclasses.dataclass(frozen=True)
class Machine:
    user: str = ""
    password: str = ""
    address: str = "localhost"
    env_loader: Tuple[str, ...] = ()

    @staticmethod
    def parse(url: str) -> "Machine":
        """
        parse machine scheme url
        format: machine://user:pswd@addr/path/to/env_loader.sh?arg=arg1&arg=arg2
        
        or: machine://user:pswd@addr/path/to/devel/setup.bash?=setup
        env_loader will be rewritten as: `/usr/bin/bash -c 'source /path/to/devel/setup.bash && ROS_IP={address} exec "$@"' --`
        """
        parse_result = urllib.parse.urlparse(url, scheme="machine")
        if parse_result.scheme != "machine":
            raise SchemeParseError("machine", url, "machine://user:pswd@addr/path/to/env_loader.sh?arg=arg1&arg=arg2")

        user = urllib.parse.unquote(parse_result.username or "")
        password = urllib.parse.unquote(parse_result.password or "")
        address = parse_result.hostname or "localhost"
        env_loader_cmd = urllib.parse.unquote(parse_result.path)
        if env_loader_cmd:
            query = urllib.parse.parse_qsl(parse_result.query)
            env_loader_args = tuple(v for k, v in query if k == "arg")
            env_loader = (env_loader_cmd, *env_loader_args)
        
            if ("", "setup") in query:
                env_loader = Machine._from_setup_script(address, env_loader[0])
        else:
            env_loader = ()

        return Machine(user=user, password=password, address=address, env_loader=env_loader)

    @staticmethod
    def _from_setup_script(address: str, setup_script: str) -> Tuple[str, ...]:
        if IP_REGEX.match(address):
            setenv = shlex.quote(f"ROS_IP={address}")
        else:
            setenv = shlex.quote(f"ROS_HOSTNAME={address}")
        setup_bash = shlex.quote(setup_script)
        return (
            "/usr/bin/bash",
            "-c",
            f'source {setup_bash} && {setenv} exec "$@"',
            "--",
        )
    
    def reduce_local(self) -> "Machine":
        if self.is_local():
            return Machine(user="", password="", address="localhost", env_loader=self.env_loader)
        return self

    def get_netloc(self) -> str:
        netloc = urlquote(self.address, unsafe="/@:")
        if self.user:
            auth = urlquote(self.user, unsafe="/:")
            if self.password:
                auth += ":" + urlquote(self.password, unsafe="/")
            netloc = auth + "@" + netloc
        return netloc

    def _get_setup_script(self) -> str:
        if not (len(self.env_loader) == 4 and self.env_loader[0] == "/usr/bin/bash" and self.env_loader[1] == "-c" and self.env_loader[3] == "--"):
            return ""

        if IP_REGEX.match(self.address):
            setenv = shlex.quote(f"ROS_IP={self.address}")
        else:
            setenv = shlex.quote(f"ROS_HOSTNAME={self.address}")
        parts = shlex.split(self.env_loader[2])
        if not (len(parts) == 6 and parts[0] == "source" and parts[2:] == ["&&", setenv, "exec", "$@"]):
            return ""
        return parts[1]
    
    def __str__(self) -> str:
        if setup_script := self._get_setup_script():
            path = urlquote(setup_script, unsafe="#?")
            args = urllib.parse.urlencode([("", "setup")])
            return urllib.parse.urlunparse(("machine", self.get_netloc(), path, "", args, ""))

        cmd, *args = self.env_loader or ("",)
        path = urlquote(cmd, unsafe="#?")
        args = urllib.parse.urlencode([("arg", arg) for arg in args])
        return urllib.parse.urlunparse(("machine", self.get_netloc(), path, "", args, ""))

    def is_local(self):
        # see: https://github.com/ros/ros_comm/blob/noetic-devel/tools/roslaunch/src/roslaunch/core.py#L86
        try:
            # If Python has ipv6 disabled but machine.address can be resolved somehow to an ipv6 address, then host[4][0] will be int
            machine_ips = [host[4][0] for host in socket.getaddrinfo(self.address, 0, 0, 0, socket.SOL_TCP) if isinstance(host[4][0], str)]
        except socket.gaierror:
            raise ValueError(f"cannot resolve host address for machine [{self.address}]")
        local_addresses = ['localhost'] + get_local_addresses()
        # check 127/8 and local addresses
        is_local = ([ip for ip in machine_ips if (ip.startswith('127.') or ip == '::1')] != [])
        is_local = is_local or (set(machine_ips) & set(local_addresses) != set())

        #491: override local to be ssh if machine.user != local user
        if is_local and self.user:
            is_local = self.user == getuser()
        return is_local

    def command(self, remote_cmd: Sequence[str], with_env_loader: bool = True, cwd: Optional[Path] = None, tt: bool = False) -> Tuple[str, ...]:
        if cwd is not None:
            remote_cmd = ["bash", "-c", shlex.join(["cd", str(cwd)]) + "; exec " + shlex.join(remote_cmd)]
        if with_env_loader and self.env_loader:
            remote_cmd = (*self.env_loader, *remote_cmd)
        if self.is_local():
            return tuple(remote_cmd)
        password_args = ["sshpass", "-p", self.password] if self.password else []
        remote_args = ["ssh", *(["-tt"] if tt else []), f"{self.user}@{self.address}" if self.user else self.address]
        return (*password_args, *remote_args, shlex.join(remote_cmd))

# unset variable is invalid
def expandvars(path: str) -> str:
    import os
    os.environ['DOLLARSIGN'] = '$'
    os.environ['ROS_HOME'] = os.environ.get('ROS_HOME', os.path.expandvars('$HOME/.ros'))
    import re
    return re.compile(r'\$\{([^}]+)\}').sub(lambda m: os.environ[m.group(1)], path)

# TODO: too slow!
def remote_expandvars(machine: Machine, path: str) -> str:
    cmd = machine.command([
        "python3", "-c",
        "; ".join([
            "import os",
            "os.environ['DOLLARSIGN'] = '$'",
            "os.environ['ROS_HOME'] = os.environ.get('ROS_HOME', os.path.expandvars('$HOME/.ros'))",
            "import re",
            f"path = {str(path)!r}",
            r"print(re.compile(r'\$\{([^}]+)\}').sub(lambda m: os.environ[m.group(1)], path), end='')",
        ])
    ])

    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=True)
    return result.stdout


def rsync(source: str, destination: str, machine: Machine, check_only: bool):
    if Path(source).exists() and Path(source).is_dir():
        source = str(Path(source)) + "/"

    dst_parent = str(Path(destination).parent)
    print(f"create parent directory {dst_parent}")

    cmd = machine.command(["mkdir", "-p", dst_parent], with_env_loader=False)
    subprocess.run(cmd, check=True)

    password_args = ["sshpass", "-p", machine.password] if machine.password else []

    is_local = machine.address == "localhost" and machine.user == ""
    destination_ = ((f"{machine.user}@" if machine.user else "") + f"{machine.address}:" if not is_local else "") + destination
    if check_only:
        print(f"check transfer {source} -> {destination_}")
        result = subprocess.run([
            *password_args,
            "rsync", "-azn", "--checksum", "--itemize-changes", "--del",
            source, destination_,
        ], check=True)
        if bool(result.stdout):
            raise ValueError(f"resource need to be transferred: {source} -> {destination_}")
    else:
        print(f"transfer {source} -> {destination_}")
        subprocess.run([
            *password_args,
            "rsync", "-avz", "--checksum",
            source, destination_,
        ], check=True)

@dataclasses.dataclass(frozen=True)
class SyncInfo:
    source: str       # /path/to/source (can contain ${ENVVAR})
    destination: str  # ${ROS_HOME}/resources/sync/path/to/source
    machine: str      # machine://usr:pswd@host/path/to/env_loader.sh (empty -> local)
    check_only: bool

    @staticmethod
    def load_list(params_link: PathWithJPointer) -> List["SyncInfo"]:
        try:
            params = assert_JSON(load_YAML(params_link))
        except FieldAccessError as e:
            print(e, file=sys.stderr)
            params = []

        res: List[SyncInfo] = []
        if not isinstance(params, list):
            raise TypeError(f"{params_link} is not seq")
        for i, resource in enumerate(params):
            curr_link = params_link.append(i)
            if not isinstance(resource, dict):
                raise TypeError(f"{curr_link} is not map")
            
            machine = resource.get("machine")
            if not isinstance(machine, str):
                raise TypeError(f"{curr_link}/machine is not str")
            
            source = resource.get("source")
            if not isinstance(source, str):
                raise TypeError(f"{curr_link}/source is not str")
            
            destination = resource.get("destination")
            if not isinstance(destination, str):
                raise TypeError(f"{curr_link}/destination is not str")

            check_only = bool(resource.get("check_only", False))
            
            res.append(SyncInfo(machine=machine, source=source, destination=destination, check_only=check_only))
        
        return res

def sync(params_link: str):
    """
    load resources to given paths under remote machines.

    params_link is a link to a yaml file in the format:
    ```
    - source: /path/to/source/in/local/machine   # can contain ${ENVVAR}
      destination: ${ROS_HOME}/path/to/destination/in/remote/machine
      machine: machine://user:pswd@addr/path/to/env_loader.sh?arg=arg1&arg=arg2
    ...
    ```
    """
    print(f"sync resources: {params_link}")
    for info in SyncInfo.load_list(PathWithJPointer.parse(params_link)):
        machine = Machine.parse(info.machine)
        machine = machine.reduce_local()

        expanded_source = expandvars(info.source)
        expanded_destination = remote_expandvars(machine, info.destination)
        print(f"rsync {expanded_source} -> {expanded_destination}")
        rsync(expanded_source, expanded_destination, machine, info.check_only)

@contextlib.contextmanager
def temp_rsync(source: Path, machine: Machine) -> Generator[Path, None, None]:
    suf = "/" if source.exists() and source.is_dir() else ""

    from uuid import uuid4
    remote_tmp_dir = Path(f"/tmp/temp_rsync_{os.getpid()}_" + str(uuid4()).replace("-", "_"))
    destination = remote_tmp_dir / source.name

    print(f"create temp directory {remote_tmp_dir}")
    cmd = machine.command(["mkdir", "-p", str(remote_tmp_dir)], with_env_loader=False)
    subprocess.run(cmd, check=True)

    password_args = ["sshpass", "-p", machine.password] if machine.password else []
    is_local = machine.address == "localhost" and machine.user == ""
    destination_ = ((f"{machine.user}@" if machine.user else "") + f"{machine.address}:" if not is_local else "") + str(destination)

    try:
        print(f"transfer {source} -> {destination_}")
        subprocess.run([
            *password_args,
            "rsync", "-avz", "--checksum",
            str(source) + suf, destination_ + suf,
        ], check=True)

        try:
            yield destination

        finally:
            print(f"transfer {destination_} -> {source}")
            subprocess.run([
                *password_args,
                "rsync", "-avz", "--checksum",
                destination_ + suf, str(source) + suf,
            ], check=True)

    finally:
        print(f"remove temp directory {remote_tmp_dir}")
        cmd = machine.command(["rm", "-r", str(remote_tmp_dir)], with_env_loader=False)
        subprocess.run(cmd, check=True)

@contextlib.contextmanager
def prun(name: str, command: Sequence[str], force_exit: bool = False, exit_timeout: float = 10, **kwargs: Any):
    """
    usage:
    with prun("my task", ["cmd", "arg1", "arg2"], stdout=subprocess.PIPE) as p_task: # spawn a process
        ... # do some works
        p_task.wait() # wait until done
    # it will try to interrupt the process (SIGINT)
    # if force_exit is True, kill it after {exit_timeout} sec
    """
    process = None
    try:
        print(f"start {name}...")
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
        print(f"stop {name}...")
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGINT)
            
            try:
                process.wait(timeout=exit_timeout)
            except subprocess.TimeoutExpired:
                if not force_exit: raise
                print(f"[with_roscore] fail to interrupt process {name} ({command}), will kill it")
                process.kill()
                process.wait()

@dataclasses.dataclass(frozen=True)
class TaskInfo:
    directory: Path
    local: List[str]
    remote: List[str]
    machine: Machine

    @staticmethod
    def load(task_link: PathWithJPointer) -> "TaskInfo":
        task = assert_JSON(load_YAML(task_link))
        if not isinstance(task, dict):
            raise TypeError(f"{task_link} is not map")
        
        directory = task.get("directory")
        if not isinstance(directory, str):
            raise TypeError(f"{task_link}/directory is not str")
        
        local = task.get("local")
        if not (isinstance(local, list) and all(isinstance(e, str) for e in local) and local):
            raise TypeError(f"{task_link}/local is not str list")

        remote = task.get("remote")
        if not (isinstance(remote, list) and all(isinstance(e, str) for e in remote) and remote):
            raise TypeError(f"{task_link}/remote is not str list")
        
        machine_str = task.get("machine")
        if not isinstance(machine_str, str):
            raise TypeError(f"{task_link}/machine is not str")
        machine = Machine.parse(machine_str)
        
        return TaskInfo(directory=Path(directory), local=cast(List[str], local), remote=cast(List[str], remote), machine=machine)

def run_remote_task(task_link: str):
    """
    run a remote task.

    task_link is a link to a yaml file in the format:
    ```
    directory: "path/to/local/work/directory"
    local: ["local_command.sh", "arg1", "arg2"]
    remote: ["remote_command.sh", "arg1", "arg2"]
    machine: machine://user:pswd@addr/path/to/env_loader.sh?arg=arg1&arg=arg2
    ```
    If machine is local, remote command is simply run on local machine (no rsync, no ssh).
    Otherwise, the full remote flow below is used:
    - Rsyncs directory into remote temp directory
    - Starts local command in the background
    - Runs remote command on the remote machine, inside the synced folder
    - Interrupts local command as soon as remote command finishes
    - rsyncs the folder back to overwrite local directory,
      and removes the temp directory on the remote machine afterwards
    """
    task = TaskInfo.load(PathWithJPointer.parse(task_link))

    directory = task.directory.resolve()
    if directory.exists() or not directory.is_dir():
        raise ValueError(f"{directory} is not a directory")

    machine = task.machine.reduce_local()
    if machine.is_local():
        print("machine is local, execute task locally")
        with prun("local", task.local, cwd=directory, force_exit=True, exit_timeout=10):
            remote_command = machine.command(task.remote, cwd=directory)
            with prun("remote", remote_command, force_exit=True, exit_timeout=10) as p_remote:
                ret = p_remote.wait()
        exit(ret)

    else:
        with temp_rsync(directory, machine) as remote_tmp_directory:
            with prun("local", task.local, cwd=directory, force_exit=True, exit_timeout=10):
                remote_command = machine.command(task.remote, cwd=remote_tmp_directory, tt=True)
                with prun("remote", remote_command, force_exit=True, exit_timeout=10) as p_remote:
                    ret = p_remote.wait()
        exit(ret)


__all__ = ["sync"]

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m monolaunch.monoresource <param link>\n" + cleandoc(sync.__doc__ or ""), file=sys.stderr)
        exit(1)
    sync(sys.argv[1])
