"""
sync resources before launch, so that resources can be managed in single place.

it uses commands: ssh, sshpass, rsync
"""
from inspect import cleandoc
import sys
import re
import shlex
import subprocess
import dataclasses
from pathlib import Path
import socket

from typing import List, Sequence, Tuple
import urllib.parse
from monolaunch.yaml_utils import FieldAccessError, assert_JSON, Link, load_YAML

# TODO: typecheck user input

IP_REGEX = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")

def urlquote(s: str, unsafe: str = r"%#@/:;?") -> str:
    return re.sub(
        f"[{re.escape(unsafe)}]",
        lambda m: ''.join(f"%{b:02X}" for b in m.group(0).encode("utf-8")),
        s,
    )

class SchemeParseError(Exception):
    def __init__(self, scheme: str, url: str, format: str = ""):
        self.scheme = scheme
        self.url = url
        self.format = format
    
    def __str__(self):
        return f"invalid {self.scheme} scheme url: {self.url}" + (f"\nformat: {self.format}" if self.format else "")

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
                if IP_REGEX.match(address):
                    setenv = shlex.quote(f"ROS_IP={address}")
                else:
                    setenv = shlex.quote(f"ROS_HOSTNAME={address}")
                setup_bash = shlex.quote(env_loader[0])
                env_loader = (
                    "/usr/bin/bash",
                    "-c",
                    f'source {setup_bash} && {setenv} exec "$@"',
                    "--",
                )
        else:
            env_loader = ()

        return Machine(user=user, password=password, address=address, env_loader=env_loader)
    
    def reduce_local(self) -> "Machine":
        if self.is_local():
            return Machine(user="", password="", address="localhost", env_loader=self.env_loader)
        return self

    def get_netloc(self) -> str:
        netloc = self.address
        if self.user:
            auth = urlquote(self.user, unsafe="%@:")
            if self.password:
                auth += ":" + urlquote(self.password, unsafe="%@:")
            netloc = auth + "@" + netloc
        return netloc

    def __str__(self) -> str:
        cmd, *args = self.env_loader or ("",)
        path = urlquote(cmd, unsafe="%;#?")
        args = urllib.parse.urlencode([("arg", arg) for arg in args])
        return urllib.parse.urlunparse(("machine", self.get_netloc(), path, "", args, ""))

    def is_local(self):
        # see: https://github.com/ros/ros_comm/blob/noetic-devel/tools/roslaunch/src/roslaunch/core.py#L86
        try:
            # If Python has ipv6 disabled but machine.address can be resolved somehow to an ipv6 address, then host[4][0] will be int
            machine_ips = [host[4][0] for host in socket.getaddrinfo(self.address, 0, 0, 0, socket.SOL_TCP) if isinstance(host[4][0], str)]
        except socket.gaierror:
            raise ValueError(f"cannot resolve host address for machine [{self.address}]")
        import rosgraph.network # pyright: ignore[reportMissingImports]
        local_addresses = ['localhost'] + rosgraph.network.get_local_addresses() # type: ignore
        # check 127/8 and local addresses
        is_local = ([ip for ip in machine_ips if (ip.startswith('127.') or ip == '::1')] != [])
        is_local = is_local or (set(machine_ips) & set(local_addresses) != set()) # pyright: ignore[reportUnknownArgumentType]

        #491: override local to be ssh if machine.user != local user
        if is_local and self.user:
            import getpass
            is_local = self.user == getpass.getuser()
        return is_local

    def command(self, remote_cmd: Sequence[str], with_env_loader: bool = True) -> Tuple[str, ...]:
        if with_env_loader and self.env_loader:
            remote_cmd = (*self.env_loader, *remote_cmd)
        if self.is_local():
            return tuple(remote_cmd)
        password_args = ["sshpass", "-p", self.password] if self.password else []
        remote_args = ["ssh", f"{self.user}@{self.address}" if self.user else self.address]
        return (*password_args, *remote_args, shlex.join(remote_cmd))

# TODO: ban unset
# TODO: prevent bad path
# TODO: expandvars with nounset
def expandvars(path: str) -> str:
    import os
    os.environ['DOLLARSIGN'] = '$'
    os.environ['ROS_HOME'] = os.environ.get('ROS_HOME', os.path.expandvars('$HOME/.ros'))
    return os.path.expandvars(path)

# TODO: too slow!
def remote_expandvars(machine: Machine, path: str) -> str:
    cmd = machine.command([
        "python3", "-c",
        "; ".join([
            "import os",
            "os.environ['DOLLARSIGN'] = '$'",
            "os.environ['ROS_HOME'] = os.environ.get('ROS_HOME', os.path.expandvars('$HOME/.ros'))",
            f"print(os.path.expandvars({str(path)!r}), end='')"
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
    def load_list(params_link: Link) -> List["SyncInfo"]:
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
    - source: /path/to/source/in/local/machine (can contain ${ENVVAR})
      destination: ${ROS_HOME}/path/to/destination/in/remote/machine
      machine: machine://user:pswd@addr/path/to/env_loader.sh?arg=arg1&arg=arg2
    ...
    ```
    """
    print(f"sync resources: {params_link}")
    for info in SyncInfo.load_list(Link.parse(params_link)):
        machine = Machine.parse(info.machine)
        machine = machine.reduce_local()

        expanded_source = expandvars(info.source)
        expanded_destination = remote_expandvars(machine, info.destination)
        print(f"rsync {expanded_source} -> {expanded_destination}")
        rsync(expanded_source, expanded_destination, machine, info.check_only)

__all__ = ["sync"]

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m monolaunch.monoresource <param link>\n" + cleandoc(sync.__doc__ or ""), file=sys.stderr)
        exit(1)
    sync(sys.argv[1])
