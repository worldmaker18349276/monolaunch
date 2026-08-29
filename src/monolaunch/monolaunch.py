"""
monolaunch.py - ROS1 Python API for generating flattened .launch files declaratively.

All namespace / remap / env context is resolved and applied directly
to each <node> or <include> tag - no nested <group> tags in the output.
Params are hoisted to the top of the generated file.

Syntax
------
run(launch_func)                    - run launch file.  it will first generate flattened
                                      params, launch file and bash script to initialize
                                      and launch the program under current working directory
with group(ns=...)                  - <group> tag
with node(name, pkg, type, ...)     - <node> tag with private scope, can contain env, remap, param
                                      if pkg is not given, type should be absolute path to the script to run
with include(file, **args)          - <include> tag, can contain env, remap, param
                                      file is absolute path to the launch file to run
set_env(dict)                       - <env> tag
remap(dict)                         - <remap> tag
set_param(dict)                     - <param> tag
load_param(dict)                    - <rosparam> tag, load parameters from file, support json pointer
get_value("file.yaml#/sub/field")   - get value from given yaml file
get_value((json_obj, "sub/field"))  - get value from object directly
get_value("file.yaml#/sub/field", fallback)
                                    - if value is missing or type doesn't match fallback,
                                      fallback value will be returned.
with machine(name, address, ...)    - just like <machine> tag, set machine as default in a scope
                                      for your convenience, you can pass in url like
                                      "machine://user:pswd@addr/path/to/env_loader.sh" directly
env(name, fallback)                 - just like $(env name) or $(optenv name fallback)
find(pkg)                           - just like $(find pkg)
anon(name)                          - just like $(anon name)
dirname()                           - just like $(dirname)
ns()                                - get current namespace, or use ns("~") for private namespace

Machine
-------
the original machanism of <machine default="true"> simply sets to default globally,
regardless of which scope/namespace/include it is located in
(see: https://github.com/ros/ros_comm/issues/1884).

in monolaunch, you can use `with machine(...)` to set default machine **in this scope**.
to specify the machine the node run on, just use it as context manager:
with remote_machine:
    with node(name="remote_node"):
        pass

Remap
-----
the original mechanism of <remap> is:
- remap tags only affect contents after the tag, limited in the scope (launch, group, node),
  and also affect nested group and the contents of include.
  
- they affect a node just like bring those tags into node scope, that is,
  ```xml
  <remap .../>
  <node ...>
  </node>
  <!-- act just like -->
  <node ...>
      <remap .../>
  </node>
  ```
  
- to resolve a name, expand names under the node, than find the matched mapping.
  for example, under a node (/ns/node_name), resolving a name (sub/field):
  first, expand sub/field -> /ns/sub/field.
  then expand remap's name, for a remap (field -> /another), it becomes (/ns/field -> /another).
  it is different from /ns/sub/field, so it doesn't change.

  full expansion rule:
  - start with "/" -> no expansion
  - first element starts with "~" -> prepand with namespace and node name
  - otherwise -> prepand with namespace
  - special cases
    - abc//efg -> abc/efg     (warning, still work)
    - /~abc/efg -> /~abc/efg  (unusable)
    - ~/abc/efg -> /abc/efg   (why???)

there are few downside:
- remap between relative paths is expanded under the place of node, not under the place of remap tag.
  for example, in <remap from="~sub/field" .../>, "~" refers to any node name in the affect region.
  a relative path remap outside the node scope may cause unexpected result.
  
- to remap a topic, you need to know which part is node namespace and which part is topic path,
  even though they are the same for connection.
  for example, a node (/ns/node_name) with topic (sub/field)
  is different from, a node (/ns/sub/node_name) with topic (field), since:
  <remap from="sub/field" .../> only works on the first case;
  <remap from="field" .../> only works on the second case;
  <remap from="/ns/sub/field" .../> works on both cases.
  
- since namespacing a include file will change the full path of topics,
  the only reliable way to make a launch file with remaps is using relative path remapping,
  and it is aware of the node, you better to put remap into each node.
  
- remaps won't apply to topics under the namespace.
  <remap from="sub" .../> don't apply to topic "sub/topic".
  to remap a series of topics, the only way is remap one by one.
  if some topics are added in the future, you need to add corresponding remaps manually.
  the only advantage of organizing topics by namespace is more pleasing.
  
  however, if you remap topic "camera/image_raw", image_transport will automatically
  remap related topics ("camera/camera_info", "camera/image_raw/compressed", etc.) for you.
  this is done by programmatically detecting remaps and dealing with them accordingly.
  in other words, this is custom magic; there is no universal way to do it.

- remaps will chain together sometimes.
  <remap from="a" to="b"/>
  <remap from="b" to="c"/>
  will make topic a -> c, order is unrelated.
  but for three steps case,
  <remap from="a" to="b"/>
  <remap from="b" to="c"/>
  <remap from="c" to="d"/>
  still map topic a to c instead of d.
  
  `rospy.resolve_name('a')` only maps once, so we got 'b'.
  loop is valid somehow
  <remap from="a" to="b"/>
  <remap from="b" to="a"/>
  but I don't know where it remaps to finally.
  for ambiguous remapping
  <remap from="a" to="b"/>
  <remap from="a" to="c"/>
  the later one wins.
  parameters can be remapped too, however, they will not chain together.
  <remap from="a" to="b"/>
  <remap from="b" to="c"/>
  will make parameter a -> b.
  
the biggest mistake is the expansion timing.
in monolaunch, remaps are always expand under the current scope,
you can confidently inspect which path will be remapped to where,
and no need to know which part is namespace and which part is topic path.
we still don't recommand you to use absolute path remapping.
we will chain the mapping to fix `rospy.resolve_name`, and will detect the looping problem.

Param
-----
the original mechanism of <param> is:
- outside the private scope of node,
  absolute names aren't expanded, and relative names are expanded by prepending current namespace:
  <group ns="/current/ns">
    <param name="sub/field" .../>
  </group>
  becomes
  <param name="/current/ns/sub/field" .../>
- inside the private scope of node,
  it always prepend with current private namespace:
  <node ns="/current/ns" name="node_name" ...>
    <param name="/sub/field" .../>
  </node>
  becomes
  <param name="/current/ns/node_name/sub/field" .../>
- if param name prefix with "~", it will apply to every nodes after this tag in current scope:
  <param name="~sub/field" .../>
  <node ns="/current/ns" name="node_name" .../>
  becomes
  <node ns="/current/ns" name="node_name" ...>
    <param name="sub/field" .../>
  </node>

even if the param name and the remap name are in the same world, they have different rules.
- param names outside the node are expanded under current scope;
  remap names outside the node are expanded under the node it applied to.
- param names prefixed with "~" will be applied to later nodes;
  remap names prefixed with "~" will be expanded under private namespace.
- param names inside the node are always expanded under private namespace;
  remap names inside the node has no different from the outside.
- who the fuck design those rules?

in monolaunch, names are always expanded under current namespace
(or current private namespace if prefixed with "~").
noting that "~/field" and "~field" are the same in monolaunch, just replace "~" with "/ns/node_name/".
there is no way to apply a param to every nodes in the affect region, does anyone actually need this?

to set/load param in monolaunch, use set_param and load_param, they can be nested structure, for example:
set_param({
    "nested": {
        "field_1": "nested",
        "field_2": "fields",
        "field_3": "are",
        "field_4": "valid",
    },
    "path/to/field": "folded path is also valid",
    "path/to": {
        "another/field": "nested folded path? of cause!",
        "": "empty path? fair enough~",
    },
    "~": {
        "field": "yes, this is equivalent to ~field",
    },
    "array/0/x": "number will be treated as indexing, so this refer to curr_param.array[0].x",
})

in load_param, its values are treated as file paths to load:
load_param({"another/path": "file/path/to/subconfig.yaml#/sub/field"})
this will load "file/path/to/subconfig.yaml", take field "/sub/field", and put into "another/path"

we actually do not set/load param via rosparam command,
but aggregate them into a single param file using !include and !merge,
them resolve it before launch.
"""

import contextlib
import inspect
import keyword
import re
import warnings
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, KeysView, List, Literal, Optional, Tuple, Sequence, Union, overload
from pathlib import Path
from dataclasses import MISSING, dataclass, field
from collections import ChainMap
import sys
import os
import socket
import ipaddress
import urllib.parse
import shlex
from uuid import uuid4
import yaml
from monolaunch.yaml_utils import JSON, FieldAccessError, FieldPath, Link
from . import monoparam
from .monoparam import JSONWithOnlyLink, JSONWithPath, JSONLike_deep_iter, LinkAccessWarning, SourceLoader, SourcedJSON_deep_iter, SourcedNode, SourcedYAMLDumper

__all__ = [
    "run",
    "group", "node", "include",
    "set_param", "load_param", "get_value",
    "remap", "set_env",
    "machine",
    "env", "find", "anon", "ns", "dirname",
    "as_bool",
]


# -- build context ------------------------------------------------------------

def _split_ns(path: str) -> Tuple[str, ...]:
    return tuple(e for e in path.split("/") if e)

def _join_ns(path: Tuple[str, ...]) -> str:
    return "/" + "/".join(path)

class DuplicatedNameError(Exception):
    pass

class NeverUseError(Exception):
    pass

class MultipleUseError(Exception):
    pass

class UseInPrivateScopeError(Exception):
    pass

class FilePathNotAbsoluteError(Exception):
    pass

class LoopRemapError(Exception):
    pass

@dataclass
class Scope:
    ns: Tuple[str, ...] = ()
    is_private: bool = False
    default_machine: Optional["Machine"] = None
    remap: Dict[str, str] = field(default_factory=lambda: {})
    env: Dict[str, str] = field(default_factory=lambda: {})

@dataclass
class Ctx:
    scopes: List[Scope] = field(default_factory=lambda: [])
    param_loader: SourceLoader = field(default_factory=SourceLoader)
    param_node: Optional[SourcedNode] = None
    # node_name -> node, include_index -> include
    nodes: Dict[Union[str, int], Union["Node", "Include"]] = field(default_factory=lambda: {})
    machines: Dict[str, "Machine"] = field(default_factory=lambda: {"local": Machine(name="local", address="localhost")})
    params_filepath: Path = field(default_factory=lambda: Path(".yaml"))

    def __post_init__(self):
        self.scopes.append(Scope(default_machine=self.local_machine))

    @property
    def pns(self) -> Tuple[str, ...]:
        return tuple(x for scope in self.scopes for x in scope.ns if x)

    @property
    def ns(self) -> Tuple[str, ...]:
        scopes = self.scopes[:-1] if self.is_private else self.scopes
        return tuple(x for scope in scopes for x in scope.ns if x)

    @property
    def local_machine(self) -> "Machine":
        return self.machines["local"]

    @property
    def default_machine(self) -> "Machine":
        return next(scope.default_machine for scope in self.scopes[::-1] if scope.default_machine)

    @default_machine.setter
    def default_machine(self, default_machine: "Machine"):
        self.scopes[-1].default_machine = default_machine

    def add_node(self, ns: Tuple[str, ...], node: "Node"):
        full_name = _join_ns((*ns, node.name))
        if full_name in self.nodes:
            raise DuplicatedNameError(f"node name {full_name!r} is already used")
        assert not any(node is node_ for node_ in self.nodes.values())
        self.nodes[full_name] = node

    def add_include(self, include: "Include"):
        assert id(include) not in self.nodes
        self.nodes[id(include)] = include

    def add_machine(self, machine: "Machine"):
        if machine.name in self.machines and self.machines[machine.name].key() != machine.key():
            raise DuplicatedNameError(f"machine name {machine.name!r} is already used")
        self.machines[machine.name] = machine
    
    def find_machine(self, machine: "Machine") -> Optional["Machine"]:
        return next((machines_ for machines_ in self.machines.values() if machines_.key() == machine.key()), None)

    def push_group(self, ns: Tuple[str, ...] = (), is_private: bool = False, default_machine: Optional["Machine"] = None):
        self.scopes.append(Scope(ns, is_private, default_machine))

    def pop_group(self):
        self.scopes.pop()

    def resolve_name(self, name: str) -> str:
        if name.startswith("~"):
            name = _join_ns(self.pns + _split_ns(name[1:]))
        elif not name.startswith("/"):
            name = _join_ns(self.ns + _split_ns(name))
        return name

    @property
    def is_private(self) -> bool:
        return self.scopes[-1].is_private

    # param
    def set_param(self, param: JSONWithPath):
        self._set_param(param, self.default_machine, False)

    def load_param(self, param: JSONWithOnlyLink):
        self._set_param(param, self.default_machine, True)

    def _set_param(self, param: Union[JSONWithPath, JSONWithOnlyLink], machine: "Machine", is_load: bool):
        if not isinstance(param, dict):
            raise TypeError("param should be a dictionary")

        param = {
            self.resolve_name(key): value
            for key, value in param.items()
        } # type: ignore

        if is_load:
            for _path, value in JSONLike_deep_iter(param):
                if isinstance(value, (str, Path)):
                    is_absolute = Path(value).is_absolute()
                elif isinstance(value, Link):
                    is_absolute = value.filepath.is_absolute()
                else:
                    is_absolute = False
                if not is_absolute:
                    raise FilePathNotAbsoluteError(f"param file path must be absolute path, got: {value}, you may want to use dirname()")

        if self.param_node is None:
            self.param_node = self.param_loader.new(self.params_filepath)
        assert self.param_node is not None
        if is_load:
            self.param_loader.include(self.param_node, param, str(machine)) # type: ignore
        else:
            self.param_loader.update(self.param_node, param, str(machine)) # type: ignore

    def get_value(self, link: Link) -> JSON:
        if not link.filepath.is_absolute():
            raise FilePathNotAbsoluteError(f"param file path must be absolute path, got: {link}, you may want to use dirname()")
        tmp_param_node, _depends = self.param_loader.load(link)
        if tmp_param_node is None:
            raise FieldAccessError(link.fieldpath, str(link.filepath))
        res, _depends = self.param_loader.resolve_all(tmp_param_node, [])
        # TODO: lock this param, since generated launch file depends on it now
        return res

    # remap
    def _get_mapping(self) -> Tuple[KeysView[str], Callable[[str], Optional[str]]]:
        merged = ChainMap(*[scope.remap for scope in self.scopes[::-1]])
        def get(t: Optional[str]) -> Optional[str]:
            return merged.get(t) if t is not None else None
        def rget(t0: str) -> Optional[str]:
            t = t0
            t2 = t0
            while True:
                t_ = get(t)
                t2_ = get(get(t2))
                if t_ is not None and t2_ == t_:
                    return None
                if t_ is None: break
                t = t_
                t2 = t2_
            return t
        return merged.keys(), rget

    def get_remap(self) -> Dict[str, str]:
        keys, value_func = self._get_mapping()
        # chain mapping
        remap: Dict[str, str] = {}
        for k in keys:
            v = value_func(k)
            if v is None:
                raise LoopRemapError(f"remap {k} causes loop")
            remap[k] = v
        return remap

    def push_remap(self, mapping: Dict[str, str]):
        remap = self.scopes[-1].remap
        for k, v in mapping.items():
            k_ = self.resolve_name(k)
            v_ = self.resolve_name(v)
            if k_ == v_:
                # remove trivial map instead of error
                continue
            remap[k_] = v_

        # check loop of mapping
        _, value_func = self._get_mapping()
        for k in remap.keys():
            v = value_func(k)
            if v is None:
                raise LoopRemapError(f"remap {k} causes loop")

    # env
    def push_env(self, envvars: Dict[str, str]):
        self.scopes[-1].env.update(envvars)

    def get_env(self) -> Dict[str, str]:
        merged: Dict[str, str] = {}
        for scope in self.scopes:
            merged.update(scope.env)
        return merged

# -- primitive value types ----------------------------------------------------

@dataclass
class Include:
    file: str
    args: Dict[str, Any]
    ns: Tuple[str, ...] = ()
    clear_params: bool = False
    machine: Optional["Machine"] = None
    env: Dict[str, str] = field(default_factory=lambda: {})
    remap: Dict[str, str] = field(default_factory=lambda: {})

    _used: bool = False

    def __del__(self):
        if not self._used:
            raise NeverUseError(f"include object is created but not used: {self.file}")

    def __enter__(self):
        if self._used:
            raise MultipleUseError(f"include object cannot be reused: {self.file}")
        self._used = True
        if ctx().is_private:
            raise UseInPrivateScopeError(f"inlcude cannot be used inside node or include: {self.file}")

        self.ns = ctx().ns
        ctx().add_include(self)
        self.machine = ctx().default_machine
        ctx().add_machine(self.machine)
        ctx().push_group((), False)
        return self

    def __exit__(self, *_):
        self.env = ctx().get_env()
        self.remap = ctx().get_remap()
        ctx().pop_group()

    def to_xml(self, machine_xml: ET.Element) -> ET.Element:
        el_ = ET.Element("group")
        el_.append(machine_xml)
        for f, t in self.remap.items():
            r = ET.SubElement(el_, "remap"); r.set("from", f); r.set("to", t)

        attrs: Dict[str, str] = {}
        attrs["file"] = self.file
        if self.clear_params:
            attrs["clear_params"] = "true"
        if self.ns:
            attrs["ns"] = _join_ns(self.ns)
        el = ET.Element("include", attrs)

        for k, v in self.env.items():
            e = ET.SubElement(el, "env"); e.set("name", k); e.set("value", str(v))
        for k, v in self.args.items():
            e = ET.SubElement(el, "arg"); e.set("name", k); e.set("value", str(v))
        el_.append(el)
        return el_

@dataclass
class Node:
    name: str
    pkg: str
    type: str
    output: Literal["log", "screen"] = "log"
    cwd: Literal["ROS_HOME", "node"] = "ROS_HOME"
    args: Tuple[Any, ...] = ()
    respawn: bool = False
    respawn_delay: float = 30.0
    clear_params: bool = False
    required: bool = False
    launch_prefix: Tuple[Any, ...] = ()
    ns: Tuple[str, ...] = ()
    env: Dict[str, str] = field(default_factory=lambda: {})
    remap: Dict[str, str] = field(default_factory=lambda: {})
    machine: Optional["Machine"] = None

    _used: bool = False

    def __del__(self):
        if not self._used:
            raise NeverUseError(f"node object is created but not used: {self.name}")

    def __enter__(self):
        if self._used:
            raise MultipleUseError(f"node object cannot be reused: {self.name}")
        self._used = True
        if ctx().is_private:
            raise UseInPrivateScopeError(f"node cannot be used inside node or include: {self.name}")

        self.ns = ctx().ns
        ctx().add_node(ctx().ns, self)
        self.machine = ctx().default_machine
        ctx().add_machine(self.machine)
        ctx().push_group((self.name,), True)
        return self

    def __exit__(self, *_):
        self.env = ctx().get_env()
        self.remap = ctx().get_remap()
        ctx().pop_group()

    def to_xml(self) -> ET.Element:
        attrs: Dict[str, str] = {}

        if self.ns:
            attrs["ns"] = _join_ns(self.ns)

        attrs["name"] = self.name
        attrs["pkg"] = self.pkg
        attrs["type"] = self.type
        if self.output != "log": attrs["output"] = self.output
        if self.cwd != "ROS_HOME": attrs["cwd"] = self.cwd
        if self.args:
            attrs["args"] = shlex.join(str(a) for a in self.args)

        if self.machine:       attrs["machine"] = self.machine.name
        if self.respawn:       attrs["respawn"] = "true"; attrs["respawn_delay"] = str(self.respawn_delay)
        if self.clear_params:  attrs["clear_params"] = "true"
        if self.required:      attrs["required"] = "true"
        if self.launch_prefix: attrs["launch-prefix"] = shlex.join(str(a) for a in self.launch_prefix)

        el = ET.Element("node", attrs)
        for k, v in self.env.items():
            e = ET.SubElement(el, "env"); e.set("name", k); e.set("value", str(v))
        for f, t in self.remap.items():
            r = ET.SubElement(el, "remap"); r.set("from", f); r.set("to", t)
        return el

def urlquote(s: str, unsafe: str = r"%#@/:;?") -> str:
    return re.sub(
        f"[{re.escape(unsafe)}]",
        lambda m: ''.join(f"%{b:02X}" for b in m.group(0).encode("utf-8")),
        s,
    )

@dataclass
class Machine:
    name: str
    address: str
    env_loader: str = ""
    user: str = ""
    password: str = ""

    def key(self):
        return (self.address, self.user, self.password, self.env_loader)

    def __enter__(self):
        ctx().push_group((), False, self)
        return self

    def __exit__(self, *_):
        ctx().pop_group()

    @staticmethod
    def parse(url: str) -> "Machine":
        """
        parse machine scheme url
        format: machine://user:pswd@addr/path/to/env_loader.sh
        """
        parse_result = urllib.parse.urlparse(url, scheme="machine")
        if parse_result.scheme != "machine":
            raise ValueError(f"invalid machine scheme url: {url}\nformat: machine://user:pswd@addr/path/to/env_loader.sh")

        user = urllib.parse.unquote(parse_result.username or "")
        password = urllib.parse.unquote(parse_result.password or "")
        address = parse_result.hostname or ""
        env_loader = urllib.parse.unquote(parse_result.path)

        return Machine(name="", user=user, password=password, address=address, env_loader=env_loader)

    def get_netloc(self) -> str:
        netloc = self.address
        if self.user:
            auth = urlquote(self.user, unsafe="%@:")
            if self.password:
                auth += ":" + urlquote(self.password, unsafe="%@:")
            netloc = auth + "@" + netloc
        return netloc

    def __str__(self) -> str:
        path = urlquote(self.env_loader, unsafe="%;#?")
        return urllib.parse.urlunparse(("machine", self.get_netloc(), path, "", "", ""))

    def is_loopback(self) -> bool:
        if not self.address:
            return True

        # Direct IP address
        try:
            return ipaddress.ip_address(self.address).is_loopback
        except ValueError:
            pass

        # Hostname: resolve all addresses
        try:
            infos = socket.getaddrinfo(self.address, None)
        except socket.gaierror:
            return False

        return any(
            ipaddress.ip_address(addr[0]).is_loopback
            for _family, _, _, _, addr in infos
        )

    def to_xml(self, default: bool = False) -> ET.Element:
        attrs: Dict[str, str] = {}
        attrs["name"] = self.name
        attrs["address"] = self.address
        if self.env_loader:  attrs["env_loader"] = self.env_loader
        if self.user:        attrs["user"] = self.user
        if self.password:    attrs["password"] = self.password
        attrs["default"] = "true" if default else "false"
        el = ET.Element("machine", attrs)
        return el

@dataclass
class Group:
    ns: Tuple[str, ...] = ()

    def __enter__(self):
        ctx().push_group(self.ns)
        return self

    def __exit__(self, *_):
        ctx().pop_group()

# -- public helpers ------------------------------------------------------------

def env(name: str, fallback: Optional[str] = None) -> str:
    value = os.environ.get(name, fallback)
    if value is None:
        raise ValueError(f"envvar {name} not found")
    return value

def find(pkg: str) -> str:
    import rospkg # type: ignore
    return rospkg.RosPack().get_path(pkg) # type: ignore

def sanitize_identifier(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if not name.isidentifier() or keyword.iskeyword(name):
        name = "_" + name
    return name

def anon(name: str) -> str:
    return name + "_" + str(uuid4()).replace("-", "_")

class LinkAccessTypeWarning(Warning):
    def __init__(self, value_link: Link, value_type: type, expected_type: type):
        self.value_link = value_link
        self.value_type = value_type
        self.expected_type = expected_type
    
    def __str__(self):
        return (
            f"field {self.value_link.relative_to(Path.cwd())} ({self.value_type.__name__})"
            f" doesn't match expected type {self.expected_type.__name__}"
        )

@overload
def get_value(field_or_path: Union[str, Path, Link, Tuple[JSON, Union[str, FieldPath]]]) -> JSON: ...
@overload
def get_value(field_or_path: Union[str, Path, Link, Tuple[JSON, Union[str, FieldPath]]], fallback: None) -> None: ...
@overload
def get_value(field_or_path: Union[str, Path, Link, Tuple[JSON, Union[str, FieldPath]]], fallback: bool) -> bool: ...
@overload
def get_value(field_or_path: Union[str, Path, Link, Tuple[JSON, Union[str, FieldPath]]], fallback: int) -> int: ...
@overload
def get_value(field_or_path: Union[str, Path, Link, Tuple[JSON, Union[str, FieldPath]]], fallback: float) -> float: ...
@overload
def get_value(field_or_path: Union[str, Path, Link, Tuple[JSON, Union[str, FieldPath]]], fallback: str) -> str: ...
@overload
def get_value(field_or_path: Union[str, Path, Link, Tuple[JSON, Union[str, FieldPath]]], fallback: List[JSON]) -> List[JSON]: ...
@overload
def get_value(field_or_path: Union[str, Path, Link, Tuple[JSON, Union[str, FieldPath]]], fallback: Dict[str, JSON]) -> Dict[str, JSON]: ...

def get_value(field_or_path: Union[str, Path, Link, Tuple[JSON, Union[str, FieldPath]]], fallback: JSON = MISSING) -> JSON: # type: ignore
    if not isinstance(field_or_path, tuple):
        if isinstance(field_or_path, Link):
            link = field_or_path
        elif isinstance(field_or_path, Path):
            link = Link(field_or_path)
        else:
            link = Link.parse(field_or_path)

        get_value_ = lambda: ctx().get_value(link)

    else:
        if isinstance(field_or_path[1], FieldPath):
            fieldpath = field_or_path[1]
        else:
            fieldpath = FieldPath.parse(field_or_path[1])
        link = Link(Path("<python object>"), fieldpath)

        get_value_ = lambda: fieldpath.walk(field_or_path[0])

    if fallback is MISSING: # type: ignore
        return get_value_()

    try:
        res = get_value_()
    except FieldAccessError as err:
        warnings.warn(LinkAccessWarning(Link(link.filepath, err.path)))
        res = None

    if not isinstance(res, type(fallback)):
        warnings.warn(LinkAccessTypeWarning(link, type(res), type(fallback))) # type: ignore
        res = fallback
    return res

def set_param(json: JSONWithPath):      ctx().set_param(json)
def load_param(json: JSONWithOnlyLink): ctx().load_param(json)
def remap(mapping: Dict[str, str]):     ctx().push_remap(mapping)
def set_env(mapping: Dict[str, str]):   ctx().push_env(mapping)

_ctx: Optional[Ctx] = None

def ctx() -> Ctx:
    global _ctx
    return _ctx # type: ignore

@contextlib.contextmanager
def _with_ctx():
    global _ctx
    try:
        _ctx = Ctx()
        yield
    finally:
        _ctx = None

def ns(n: Literal["", "~"] = "") -> str:
    if n == "~":
        return _join_ns(ctx().pns)
    else:
        return _join_ns(ctx().ns)

def dirname() -> Path:
    return Path(inspect.currentframe().f_back.f_globals["__file__"]).parent.resolve() # type: ignore

def init(script: Union[str, Path]):
    # TODO: assign an init script which will run after preparing resource, before launch
    #       ex. move resource file to proper place
    ...

def group(ns: str = "") -> Group:
    if ns.startswith("/"):
        raise ValueError(f"group ns should be relative path, got: {ns}")
    return Group(ns=_split_ns(ns))

def machine(url: str = "", *, name: str = "", address: str = "", env_loader: str = "", user: str = "", password: str = "") -> Machine:
    if url:
        machine = Machine.parse(url)
    else:
        machine = Machine(name=name, address=address, env_loader=env_loader, user=user, password=password)
    if not machine.name:
        machine_ = ctx().find_machine(machine)
        if machine_ is None:
            machine.name = anon(sanitize_identifier(machine.address))
        else:
            machine.name = machine_.name
    return machine

def node(*, name: str = "", pkg: str = "", type: Union[str, Path],
         output: Literal["log", "screen"] = "log", cwd: Literal["ROS_HOME", "node"] = "ROS_HOME",
         args: Sequence[Any] = (), respawn: bool = False, respawn_delay: float = 30.0,
         clear_params: bool = False, required: bool = False, launch_prefix: Sequence[Any] = ()) -> Node:
    if output not in ("log", "screen"):
        raise TypeError(f"output should be one of log, screen")
    if cwd not in ("ROS_HOME", "node"):
        raise TypeError(f"cwd should be one of ROS_HOME, node")
    if not isinstance(args, (tuple, list)):
        raise TypeError(f"args should be list")

    if not name:
        name = anon(sanitize_identifier(str(type)))

    if not pkg:
        # treat type as direct path to script
        if not Path(type).is_absolute():
            raise FilePathNotAbsoluteError(f"since pkg is empty, type should be absolute path: {type}, you may want to use dirname()")
        args = (type, *args)
        pkg = "monolaunch"
        type = "exec.sh"

    return Node(name=name, pkg=pkg, type=str(type), output=output, cwd=cwd,
                args=tuple(args), respawn=respawn, respawn_delay=respawn_delay,
                clear_params=clear_params, required=required, launch_prefix=tuple(launch_prefix))

def include(file: Union[str, Path], *, clear_params: bool = False, **args: Any) -> Include:
    if not Path(file).is_absolute():
        raise FilePathNotAbsoluteError(f"include path should be absolute path: {file}, you may want to use dirname()")

    return Include(file=str(file), clear_params=clear_params, args=args)

def as_bool(s: Union[str, bool]) -> bool:
    if isinstance(s, bool): return s
    if s in ("true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"):
        return True
    elif s in ("false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"):
        return False
    else:
        raise ValueError(f"{s} is not valid bool literal")


class ForeignSyncResourceWarning(Warning):
    def __init__(self, resource_name: str, runtime_machine_name: str, host_node_name: str, host_machine_name: str):
        self.resource_name = resource_name
        self.runtime_machine_name = runtime_machine_name
        self.host_node_name = host_node_name
        self.host_machine_name = host_machine_name

    def __str__(self):
        return (
            f"resource {self.resource_name} "
            f"sync to machine {self.runtime_machine_name} "
            f"but it is a param under {self.host_node_name}"
            + (f" ({self.host_machine_name})" if self.host_machine_name else "")
        )

def check_foreign_sync_resources(ctx: Ctx):
    if ctx.param_node is None: return
    for source in ctx.param_node.sources:
        for _raw_path, path, value in SourcedJSON_deep_iter(source.data):
            # strip until index element
            if isinstance(value, (monoparam.Include, monoparam.Resource)):
                runtime_machine = dict(value.context).get("runtime_machine")
                runtime_machine_key = Machine.parse(runtime_machine).key() if runtime_machine is not None else None
                host_node = next((node for node in ctx.nodes.values() if isinstance(node, Node) and FieldPath((*node.ns, node.name)).is_prefix(path)), None)
                host_machine_key = host_node.machine.key() if host_node is not None and host_node.machine is not None else None
                if host_machine_key != runtime_machine_key:
                    resource_name = f"!include {value.link}" if isinstance(value, monoparam.Include) else value.uri
                    runtime_machine_name = runtime_machine or ""
                    if host_node is None:
                        host_node_name = "public namespace"
                        host_machine_name = ""
                    else:
                        assert isinstance(host_node, Node)
                        host_machine_name = str(host_node.machine or "local machine")
                        host_node_name = f"node {_join_ns((*host_node.ns, host_node.name))}"
                    warnings.warn(ForeignSyncResourceWarning(resource_name, runtime_machine_name, host_node_name, host_machine_name))

def generate(launch_func: Any, use_param_loader: bool = True) -> Path:
    with _with_ctx():
        cwd = Path.cwd()
        name = launch_func.__name__
        ctx().params_filepath = cwd / f"{name}.yaml"

        launch_func()

        # add param loader
        if use_param_loader:
            param_loader_node = Node(
                name="param_loader", pkg="monolaunch", type="param_loader.py",
                respawn=True, respawn_delay=3.0, clear_params=True,
                machine=ctx().local_machine,
            )
            with param_loader_node:
                pass

        # save param
        # TODO: add option to embed param into launch file (how?)
        param_node = ctx().param_node
        check_foreign_sync_resources(ctx())
        if param_node is not None or use_param_loader:
            if param_node is None:
                param_node = ctx().param_loader.new(ctx().params_filepath)
                assert param_node is not None
            with open(ctx().params_filepath, "w") as f:
                yaml.dump(param_node.sources[0].data, f, Dumper=SourcedYAMLDumper, sort_keys=False)


        launch_el = ET.Element("launch")

        # add initial param resolver
        if param_node:
            launch_el.append(ET.Element("arg", dict(name="monoparam_source", default=str(ctx().params_filepath))))
            launch_el.append(ET.Element("param", dict(name="$monoparam_source", type="str", value="$(arg monoparam_source)")))

            resolved_param_expr = f"__import__('monolaunch.monoparam').monoparam.save_resolved(monoparam_source)"
            launch_el.append(ET.Element("arg", dict(name="resolved_param_expr", default=resolved_param_expr)))
            launch_el.append(ET.Element("arg", dict(name="resolved_param", default="$(eval eval(resolved_param_expr))")))

        # add <rosparam>
        if param_node is not None or use_param_loader:
            launch_el.append(ET.Element("rosparam", dict(command="load", file="$(arg resolved_param)")))

        # add resource loader
        sync_resources_expr = f"__import__('monolaunch.monoresource').monoresource.sync(resolved_param)"
        launch_el.append(ET.Element("arg", dict(name="sync_resources_expr", default=sync_resources_expr)))
        launch_el.append(ET.Element("arg", dict(name="sync_resources", default="$(eval eval(sync_resources_expr))")))
        
        # add <machine>
        for machine in ctx().machines.values():
            launch_el.append(machine.to_xml())

        # add <node> and <include>
        nodes = list(ctx().nodes.values())
        for node in nodes:
            if isinstance(node, Node):
                launch_el.append(node.to_xml())

            else: # Include
                assert node.machine is not None
                launch_el.append(node.to_xml(machine_xml=node.machine.to_xml(default=True)))

        _indent(launch_el)

        # save launch file
        launch_filepath = cwd / f"{name}.launch"
        with open(launch_filepath, "wb") as f:
            ET.ElementTree(launch_el).write(f, encoding="utf-8", xml_declaration=True)

        return launch_filepath

def run(launch_func: Any = None, *, use_param_loader: bool = True) -> Any:
    if launch_func is None:
        return lambda launch_func: run(launch_func, use_param_loader=use_param_loader) # type: ignore
    
    try:
        launch_filepath = generate(launch_func=launch_func, use_param_loader=use_param_loader)
    except Exception as e:
        print(f"\033[31m{e}\033[m")
        exit(1)

    if "--dry-run" in sys.argv:
        sys.argv.remove("--dry-run")
        print(shlex.join(["roslaunch", str(launch_filepath), *sys.argv[1:]]))
        return
    import os
    os.execvp("roslaunch", ["roslaunch", str(launch_filepath), *sys.argv[1:]])

def _indent(el: ET.Element, level: int = 0):
    indent = "\n" + "  " * level
    if len(el):
        if not el.text or not el.text.strip(): el.text = indent + "  "
        if not el.tail or not el.tail.strip(): el.tail = indent
        for child in el: _indent(child, level + 1)
        child = el[-1]
        if not child.tail or not child.tail.strip(): child.tail = indent
    else:
        if level and (not el.tail or not el.tail.strip()): el.tail = indent
    if not level: el.tail = "\n"
