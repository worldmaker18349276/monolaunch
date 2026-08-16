from enum import Enum
from typing import Any, Dict, Generator, List, Literal, Tuple, Set, Union, Optional, Type, TypeVar, Callable, IO, overload
import sys
import math
import yaml
import urllib.parse
from pathlib import Path
from dataclasses import dataclass, field
import warnings
from structmapper.yaml_utils import *


JSONWithPath = Union[None, JSONScalar, Path, List["JSONWithPath"], Dict[str, "JSONWithPath"]]
JSONWithOnlyLink = Union[None, str, Path, "Link", List["JSONWithOnlyLink"], Dict[str, "JSONWithOnlyLink"]]

def as_JSONWithPath(obj: JSON) -> JSONWithPath: return obj # type: ignore

@overload
def JSONLike_deep_iter(folded_dict: JSONWithPath) -> Generator[Tuple["FieldPath", Union[JSONScalar, Path]], None, None]: ... # type: ignore
@overload
def JSONLike_deep_iter(folded_dict: JSONWithOnlyLink) -> Generator[Tuple["FieldPath", Union[str, Path, "Link"]], None, None]: ... # type: ignore
def JSONLike_deep_iter(folded_dict: JSON) -> Generator[Tuple["FieldPath", JSONScalar], None, None]: # type: ignore
    stack = [(FieldPath(), folded_dict)]
    while stack:
        path, value = stack.pop()
        if isinstance(value, dict):
            for key in list(value.keys()):
                stack.append((path / key, value[key]))
        elif isinstance(value, list):
            for key in range(len(value)):
                stack.append((path / key, value[key]))
        elif value is None:
            # skip None
            pass
        else:
            yield path, value

# JSON + Resource/Include/Merge, specially for Source
SourcedJSON = Union[
    None,
    bool, int, float, str, "Resource",
    "Include", "Merge",
    List["SourcedJSON"], Dict[str, "SourcedJSON"],
]

# JSON, specially for Schema
SchemaJSON = Union[
    None,
    bool, int, float, str,
    List["SchemaJSON"], Dict[str, "SchemaJSON"],
]

def is_SourcedJSON(data: Any) -> bool:
    if type(data) in (type(None), bool, int, float, str, Resource):
        return True
    elif type(data) == list:
        return all(is_SourcedJSON(e) for e in data) # type: ignore
    elif type(data) == dict:
        return all(type(k) == str and is_SourcedJSON(v) for k, v in data.items()) # type: ignore
    elif type(data) == Merge:
        return all(is_SourcedJSON(e) for e in data.items)
    else:
        return False

def assert_SourcedJSON(data: Any) -> SourcedJSON:
    if not is_SourcedJSON(data):
        raise TypeError(f"not sourced json: {data}")
    return data

def as_SourcedJSON(data: JSON) -> SourcedJSON: return data # type: ignore


def SourcedJSON_deep_copy(obj: SourcedJSON) -> SourcedJSON:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return {k: SourcedJSON_deep_copy(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return list(SourcedJSON_deep_copy(e) for e in obj)
    elif isinstance(obj, Merge):
        return Merge([SourcedJSON_deep_copy(e) for e in obj.items])
    else:
        return obj

def SourcedJSON_deep_eq(a: SourcedJSON, b: SourcedJSON) -> bool:
    stack: List[Tuple[SourcedJSON, SourcedJSON]] = [(a, b)]
    while stack:
        a, b = stack.pop()
        if type(a) != type(b):
            return False

        if isinstance(a, dict):
            assert isinstance(b, dict)
            if set(a.keys()) != set(b.keys()):
                return False
            for k in a:
                stack.append((a[k], b[k]))
            continue
        
        if isinstance(a, list):
            assert isinstance(b, list)
            if len(a) != len(b):
                return False
            for i in range(len(a)):
                stack.append((a[i], b[i]))
            continue
        
        if isinstance(a, Merge):
            assert isinstance(b, Merge)
            stack.append((a.items, b.items))
            continue

        # special case: nan != nan
        if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
            continue

        if a != b:
            return False

    return True

def SourcedJSON_deep_diff(old: SourcedJSON, new: SourcedJSON) -> Dict["FieldPath", Optional[SourcedJSON]]:
    """
    diff in the scalar level, will consider tags (!resource, !include, !merge).
    keys of resulting dictionary are paths in raw sourced json, not resolved one.
    this is for updating yaml file.
    """
    updated: Dict[FieldPath, Optional[SourcedJSON]] = {}

    stack: List[Tuple[FieldPath, SourcedJSON, SourcedJSON]] = [(FieldPath(), old, new)]
    while stack:
        path, a, b = stack.pop()

        if type(a) != type(b):
            updated[path] = b
            continue

        if isinstance(a, dict):
            assert isinstance(b, dict)
            for k in a.keys():
                if k not in b:
                    updated[path / k] = None
            for k in b.keys():
                if k not in a:
                    updated[path / k] = b[k]
                else:
                    stack.append((path / k, a[k], b[k]))
            continue

        if isinstance(a, list):
            assert isinstance(b, list)
            for i in range(len(b)):
                if i < len(a):
                    stack.append((path / i, a[i], b[i]))
                else:
                    updated[path / i] = b[i]
            for i in range(len(b), len(a)):
                updated[path / i] = None
            continue

        if isinstance(a, Merge):
            assert isinstance(b, Merge)
            stack.append((path, a.items, b.items))
            continue

        if not SourcedJSON_deep_eq(a, b):
            updated[path] = b
            continue

    return updated

def SourcedJSON_deep_iter(obj: SourcedJSON) -> Generator[Tuple["FieldPath", "FieldPath", Union[bool, int, float, str, "Resource", "Include"]], None, None]:
    """
    two paths are raw field path and resolved path
    """
    stack = [(FieldPath(), FieldPath(), obj)]
    while stack:
        raw_path, path, value = stack.pop()
        if isinstance(value, dict):
            for key in list(value.keys()):
                stack.append((raw_path / key, path / key, value[key]))
        elif isinstance(value, list):
            for key in range(len(value)):
                stack.append((raw_path / key, path / key, value[key]))
        elif isinstance(value, Merge):
            for key in range(len(value.items)):
                stack.append((raw_path / key, path, value.items[key]))
        elif value is None:
            # skip None
            pass
        else:
            yield raw_path, path, value

class RosPackageNotFoundError(Exception):
    def __init__(self, package: str):
        self.package = package
    def __str__(self) -> str:
        return f"ros package not found: {self.package}"

class InvalidUriError(Exception):
    def __init__(self, uri: str):
        self.uri = uri
    def __str__(self) -> str:
        return f"unknown URI: {self.uri}"

# @raises(RosPackageNotFoundError, InvalidUriError)
def retrieve_resource(uri: str) -> Path:
    if uri.startswith("file://"):
        path = uri[len("file://"):]
        return (Path.cwd() / Path(path)).resolve()

    elif uri.startswith("package://"):
        path = uri[len("package://"):]
        package_name, path = [*path.split("/", 1), ""][:2]

        import rospkg # type: ignore
        try:
            package_path = rospkg.RosPack().get_path(package_name) # type: ignore
        except Exception as e:
            raise RosPackageNotFoundError(package_name) from e
        assert isinstance(package_path, str)
        return Path(package_path + "/" + path).resolve()

    elif uri.startswith("ros_home://"):
        path = uri[len("ros_home://"):]

        import rospkg # type: ignore
        ros_home = rospkg.get_ros_home() # type: ignore
        assert isinstance(ros_home, str)
        return Path(ros_home + "/" + path).resolve()

    else:
        raise InvalidUriError(uri)

@dataclass(frozen=True)
class Resource:
    """
    resource URI with context.
    
    supported resource URI:
    - file://{path_to_resource}                ->  $(pwd)/{path_to_resource}
    - package://{pkg_name}/{path_to_resource}  ->  $(rospack find pkg_name)/{path_to_resource}
    - ros_home://{path_to_resource}            ->  $ROS_HOME/{path_to_resource}

    file URI is the file path relative to current location (directory of the file contains this term);
    package URI refers to the workspace overlay where this item being read;
    ros_home URI refers to the ros home of current runtime when this item being used.

    resource URI should be transformed properly after switching carrier, otherwise the meanings may change.
    file URI should not be shared across machine since it is local resource;
    package URI can be shared across machine as long as they have the same overlay;
    ros_home URI is a runtime resource and should be prepared before each run on given machine.

    context can be attached by query suffix, like "?context=value".
    since it is parsed from the right side, if uri contains "?", just suffix with "?".

    context is designed for indicating that this resource is for which machine,
    so that resolver knows how to deal with local resource for remote machine.
    context is for resolver, it should be eliminated after resolving.
    """
    uri: str
    context: Tuple[Tuple[str, str], ...] = field(default_factory=lambda: ())

    @staticmethod
    def parse(uri: str) -> "Resource":
        uri, context = (*uri.rsplit("?", 1), "")[:2]
        return Resource(uri, tuple(urllib.parse.parse_qsl(context)))

    def __str__(self) -> str:
        uri = self.uri
        context = urllib.parse.urlencode(self.context)
        if context or "?" in uri:
            uri = uri + "?" + context
        return uri

    @staticmethod
    def create(path: Union[str, Path]) -> "Resource":
        return Resource(f"file://{Path(path)}", ())

@dataclass(frozen=True)
class Include:
    """
    include a file as a node using json pointer (ex. /path/to/file.yaml#/sub/field).
    context can be attached by query suffix, like "?context=value".

    the included nodes will inherit this context, and prepend to underlying resources' context.
    since it is parsed from the right side, if json pointer contains "?", just suffix with "?".

    context is designed for indicating that underlying resources are for which machine,
    so that resolver know how to deal with local resources for remote machine.  
    """
    link: Link = field(default_factory=Link)
    context: Tuple[Tuple[str, str], ...] = field(default_factory=lambda: ())

    @staticmethod
    def parse(link: str) -> "Include":
        link, context = (*link.rsplit("?", 1), "")[:2]
        return Include(Link.parse(link), tuple(urllib.parse.parse_qsl(context)))

    def __str__(self) -> str:
        link = str(self.link)
        context = urllib.parse.urlencode(self.context)
        if context or "?" in link:
            link = link + "?" + context
        return link

    @staticmethod
    def create(link: Union[str, Path, Link]) -> "Include":
        if isinstance(link, str):
            link = Link.parse(link)
        elif isinstance(link, Path):
            link = Link(link)
        return Include(link, ())

@dataclass(frozen=True)
class Merge:
    """
    accept sequence, merge all children.  

    - null <> any = any <> null = any   --  null behaves like empty slot
    - scalar <> scalar = later one
    - seq <> seq = zip longest with <>
    - map <> map = union zip with <>
    - non-null type <> another non-null type = later one  --  warning: incompatible types to merge
    """
    items: List[SourcedJSON] = field(default_factory=lambda: [])
    
    # def __post_init__(self):
    #     if not self.items:
    #         raise ValueError("merge should have one item at least")


class SourcedYAMLLoader(SimpleYAMLLoader):
    pass

def _resource_constructor(loader: SourcedYAMLLoader, node: yaml.nodes.Node) -> Resource:
    if isinstance(node, yaml.nodes.ScalarNode):
        return Resource.parse(loader.construct_scalar(node))
    else:
        raise yaml.constructor.ConstructorError(
            None, None,
            f"!resource expects a scalar, got {type(node).__name__}",
            node.start_mark,
        )

def _include_constructor(loader: SourcedYAMLLoader, node: yaml.nodes.Node) -> Include:
    if isinstance(node, yaml.nodes.ScalarNode):
        return Include.parse(loader.construct_scalar(node))
    else:
        raise yaml.constructor.ConstructorError(
            None, None,
            f"!include expects a scalar, got {type(node).__name__}",
            node.start_mark,
        )

def _merge_constructor(loader: SourcedYAMLLoader, node: yaml.nodes.Node) -> Merge:
    if isinstance(node, yaml.nodes.SequenceNode):
        return Merge(loader.construct_sequence(node))
    else:
        raise yaml.constructor.ConstructorError(
            None, None,
            f"!merge expects a sequence, got {type(node).__name__}",
            node.start_mark,
        )

SourcedYAMLLoader.add_constructor("!resource", _resource_constructor)
SourcedYAMLLoader.add_constructor("!include", _include_constructor)
SourcedYAMLLoader.add_constructor("!merge", _merge_constructor)


class SourcedYAMLDumper(SimpleYAMLDumper):
    pass

def _resource_representer(self: SourcedYAMLDumper, data: Resource):
    # doesn't work, always quoted
    return self.represent_scalar("!resource", str(data), style="") # type: ignore

def _include_representer(self: SourcedYAMLDumper, data: Include):
    return self.represent_scalar("!include", str(data), style="") # type: ignore

def _merge_representer(self: SourcedYAMLDumper, data: Merge):
    return self.represent_sequence("!merge", data.items)

SourcedYAMLDumper.add_representer(Resource, _resource_representer)
SourcedYAMLDumper.add_representer(Include, _include_representer)
SourcedYAMLDumper.add_representer(Merge, _merge_representer)


class ABSENCE(Enum):
    VALUE = "absence"
ABSENCE_VALUE = ABSENCE.VALUE


class LoadSchemaWarning(Warning):
    def __init__(self, link: Link):
        self.link = link

    def __str__(self):
        return f"fail to load schema, file: {self.link.relative_to(Path.cwd())}" + (f"\n{self.__cause__}" if self.__cause__ else "")

class SchemaRefLoopWarning(Warning):
    def __init__(self, link: Link):
        self.link = link

    def __str__(self):
        return f"schema ref form a look: {self.link.relative_to(Path.cwd())}"

class LoadSourceWarning(Warning):
    def __init__(self, path: Path):
        self.path = path

    def __str__(self):
        return f"fail to load YAML, file: {self.path.relative_to(Path.cwd())}\n" + (f"\n{self.__cause__}" if self.__cause__ else "")

class LinkAccessWarning(Warning):
    def __init__(self, link: Link):
        self.link = link
    
    def __str__(self):
        return f"fail to access {self.link.relative_to(Path.cwd())}"

class SchemaAccessWarning(Warning):
    def __init__(self, link: Link, type: str, key: Union[int, str]):
        self.link = link
        self.type = type
        self.key = key
    
    def __str__(self):
        return f"fail to access key {self.key!r} in schema {self.link.relative_to(Path.cwd())}, it is {self.type}"

class SchemaMismatchTypeWarning(Warning):
    def __init__(self, value_link: Link, value_type: str, schema_link: Link, schema_type: str):
        self.value_link = value_link
        self.value_type = value_type
        self.schema_link = schema_link
        self.schema_type = schema_type
    
    def __str__(self):
        return (
            f"field {self.value_link.relative_to(Path.cwd())} ({self.value_type})"
            f" doesn't match schema {self.schema_link.relative_to(Path.cwd())} ({self.schema_type})"
        )

class SchemaMismatchStructWarning(Warning):
    def __init__(self, value_link: Link, schema_link: Link, additional_keys: Set[str], missing_keys: Set[str]):
        self.value_link = value_link
        self.schema_link = schema_link
        self.additional_keys = additional_keys
        self.missing_keys = missing_keys
    
    def __str__(self):
        return (
            f"map keys of field {self.value_link.relative_to(Path.cwd())}"
            f" doesn't match map keys of schema {self.schema_link.relative_to(Path.cwd())}\n"
            f"  additional keys: {self.additional_keys or {}}\n"
            f"  missing keys: {self.missing_keys or {}}"
        )

class SchemaMismatchScalarWarning(Warning):
    def __init__(self, value_link: Link, value: Any, schema_link: Link, schema: SchemaJSON):
        self.value_link = value_link
        self.value = value
        self.schema_link = schema_link
        self.schema = schema
    
    def __str__(self):
        return (
            f"field {self.value_link.relative_to(Path.cwd())} ({self.value!r})"
            f" doesn't match schema {self.schema_link.relative_to(Path.cwd())} ({self.schema!r})"
        )

class IncompatibleMergeWarning(Warning):
    def __init__(self, left_link: Link, left_type: str, right_link: Link, right_type: str):
        self.left_link = left_link
        self.left_type = left_type
        self.right_link = right_link
        self.right_type = right_type
    
    def __str__(self):
        return "incompatible types to merge:\n  left: {} as {}\n  right: {} as {}".format(
            str(self.left_link.relative_to(Path.cwd())), self.left_type,
            str(self.right_link.relative_to(Path.cwd())), self.right_type,
        )

class NotScalarNodeWarning(Warning):
    def __init__(self, link: Link):
        self.link = link
    
    def __str__(self):
        return f"node {self.link.relative_to(Path.cwd())} is not scalar"

class InvalidScalarWarning(Warning):
    def __init__(self, value: Any):
        self.value = value
    
    def __str__(self):
        return f"{self.value} is not a scalar value"

class ManualSyncResourceWarning(Warning):
    def __init__(self, resource: Resource):
        self.resource = resource
    
    def __str__(self):
        return f"sync resource cannot be specified manually: {self.resource}"

class SyncResourceUnknownRuntimeError(Exception):
    def __init__(self, resource: Resource):
        self.resource = resource
    
    def __str__(self):
        return f"runtime of sync resource is unknown: {self.resource}"

class SyncResourceSourceNotAbsoluteError(Exception):
    def __init__(self, resource: Resource):
        self.resource = resource
    
    def __str__(self):
        return f"source path of sync resource is not absolute path: {self.resource}"

class InvalidIncludeWarning(Warning):
    def __init__(self, value: Any):
        self.value = value
    
    def __str__(self):
        return f"{self.value} is not a valid include path"

class EmptyMergeWarning(Warning):
    def __init__(self, link: Link):
        self.link = link
    
    def __str__(self):
        return f"!merge list cannot be empty: at {self.link.relative_to(Path.cwd())!s}"

class ParseTypeWarning(Warning):
    def __init__(self, value: Any, expected: Union[type, Tuple[type, ...]]):
        self.value = value
        self.expected = (expected,) if isinstance(expected, type) else expected
    
    def __str__(self):
        return f"fail to parse {self.value!r}, expect " + ", ".join(t.__name__ for t in self.expected)

class RootIsNotMapWarning(Warning):
    def __init__(self, path: Path):
        self.path = path
    
    def __str__(self):
        return f"root node must be a map: {self.path}"

class SyncFileNotReadyWarning(Warning):
    def __init__(self, which: str):
        self.which = which
    
    def __str__(self):
        return f"{self.which} yaml file is not ready"

class InvalidDeletionSynchronizationWarning(Warning):
    def __init__(self, fieldpath: FieldPath):
        self.fieldpath = fieldpath
    
    def __str__(self):
        return f"try to delete field {self.fieldpath} and sync to original file, but it is invalid"

_F = TypeVar("_F", bound=Callable[..., Any])
def raises(*exceptions: Type[BaseException]):
    def decorator(func: _F) -> _F:
        func.__raises__ = exceptions  # type: ignore[attr-defined]
        return func
    return decorator


@dataclass(frozen=True)
class SyncResource:
    source: Path      # /path/to/source
    destination: Path # ${ROS_HOME}/resources/sync/path/to/source
    machine: str      # machine://usr:pswd@host/path/to/env_loader.sh (empty -> local)

class SyncResourceManager:
    @raises(ManualSyncResourceWarning, SyncResourceUnknownRuntimeError, SyncResourceSourceNotAbsoluteError)
    def rewrite_for_sync(self, resource: Resource, sync_resources: List[SyncResource]) -> str:
        """
        map local file (file://) to runtime resource dir (ros_home://) on given machine,
        and collect all sync resources for syncing resource later.
        """
        if self.get_sync_target(resource.uri) is not None:
            warnings.warn(ManualSyncResourceWarning(resource))
            return resource.uri

        if not resource.uri.startswith("file://"):
            return resource.uri

        machine = self.get_machine(resource)
        if not machine:
            raise SyncResourceUnknownRuntimeError(resource)

        src_path = Path(resource.uri[len("file://"):])
        if not src_path.is_absolute():
            raise SyncResourceSourceNotAbsoluteError(resource)
        ros_home_uri = self.set_sync_source(src_path)
        dst_path = self.get_sync_target(ros_home_uri)
        assert dst_path is not None
        sync_resources.append(SyncResource(src_path, dst_path, machine))
        return ros_home_uri

    def aggregate_sync_resources(self, sync_resources: List[SyncResource]) -> Dict[str, JSON]:
        res: JSON = {}
        if sync_resources:
            res["$sync_resources"] = [
                {
                    "source": str(sync_resource.source),
                    "destination": str(sync_resource.destination),
                    "machine": sync_resource.machine,
                }
                for sync_resource in sync_resources
            ]
        return res

    def get_sync_target(self, uri: str) -> Optional[Path]:
        if uri.startswith("ros_home://resources/sync"):
            return Path(r"${ROS_HOME}/" + uri[len("ros_home://"):].replace("$", r"${DOLLARSIGN}"))
        return None

    def set_sync_source(self, local_path: Path) -> str:
        assert local_path.is_absolute()
        return "ros_home://resources/sync/" + str(local_path.relative_to("/"))

    def get_machine(self, resource: Resource) -> str:
        return dict(resource.context).get("runtime_machine", "")

    @overload
    def attach_machine(self, resource: Resource, machine: str) -> Resource: ...
    @overload
    def attach_machine(self, resource: Include, machine: str) -> Include: ...
    def attach_machine(self, resource: Union[Resource, Include], machine: str) -> Union[Resource, Include]:
        if isinstance(resource, Resource):
            if not resource.uri.startswith("file://"):
                return resource
            return Resource(resource.uri, resource.context + (("runtime_machine", machine),))
        else:
            return Include(resource.link, resource.context + (("runtime_machine", machine),))

_V = TypeVar("_V")
@raises(ParseTypeWarning)
def _SchemaJSON_checked_get(data: SchemaJSON, key: str, expected: Union[type, Tuple[type, ...]], default: _V) -> _V:
    if not isinstance(data, dict):
        warnings.warn(ParseTypeWarning(data, dict))
        return default
    if key not in data:
        return default
    value = data[key]
    if expected and not isinstance(value, expected):
        warnings.warn(ParseTypeWarning(value, expected))
        return default
    return value # type: ignore

@raises(ParseTypeWarning)
def _SchemaJSON_checked_get_JSON(data: SchemaJSON, key: str) -> JSON:
    if not isinstance(data, dict):
        warnings.warn(ParseTypeWarning(data, dict))
        return None
    if key not in data:
        return None
    value = data[key]
    if not is_JSON(value):
        warnings.warn(ParseTypeWarning(value, object))
        return None
    return value

@dataclass(frozen=True)
class SchemaMetadata:
    description: str
    choices: Tuple[JSON, ...]
    default: JSON
    range: Tuple[float, float]

    @raises(ParseTypeWarning)
    @staticmethod
    def parse(node: SchemaJSON) -> "SchemaMetadata":
        if not isinstance(node, dict):
            node = {}
        return SchemaMetadata(
            _SchemaJSON_checked_get(node, "description", str, ""),
            tuple(
                _SchemaJSON_checked_get_JSON(choice, "const")
                for choice in _SchemaJSON_checked_get(node, "oneOf", list, [])
            ),
            _SchemaJSON_checked_get_JSON(node, "default"),
            (
                float(_SchemaJSON_checked_get(node, "minimum", (int, float), -math.inf)),
                float(_SchemaJSON_checked_get(node, "maximum", (int, float), math.inf)),
            ),
        )

@dataclass(frozen=True)
class SchemaSource:
    """
    simple json schema

    <schema>  = { "anyOf": [ <schema> ] }  // wrap
              | { "$ref": <link> }         // ref
              | {}                         // any
              | {                          // struct
                  "type": "object",
                  "properties": {
                    (<key>: <schema>,)*
                  }
                }
              | {                          // dict
                  "type": "object",
                  "additionalProperties": <schema>
                }
              | {                          // array
                  "type": "array",
                  "items": <schema>
                }
              | {                          // scalar
                  "type": "boolean" | "integer" | "number" | "string"
                }
              | { "type": "null" }         // null
    """
    
    link: Link
    node: SchemaJSON

    def get_inner(self) -> Optional["SchemaSource"]:
        if isinstance(self.node, dict) and isinstance(anyOf := self.node.get("anyOf", []), list) and len(anyOf) == 1:
            return SchemaSource(self.link / "anyOf" / 0, anyOf[0])
        return None

    def get_ref(self) -> Optional[Link]:
        if isinstance(self.node, dict) and isinstance(ref := self.node.get("$ref", None), str):
            return Link.parse(ref)
        return None

    # get type (any/struct/dict/array/null/scalar/unknown)
    # and value (None/struct item types/dict value type/array value type/scalar type/None)
    def access(self) -> Tuple[
        Literal["any", "struct", "dict", "array", "null", "scalar", "unknown"],
        Union[None, Dict[str, "SchemaSource"], "SchemaSource", type]
    ]:
        if isinstance(self.node, dict) and not self.node:
            return "any", None

        if isinstance(self.node, dict):
            node_type = self.node.get("type", "")

            if (
                node_type == "object"
                and isinstance(properties := self.node.get("properties", None), dict)
            ):
                return "struct", {
                    key: SchemaSource(self.link / "properties" / key, node)
                    for key, node in properties.items()
                }

            if (
                node_type == "object"
                and isinstance(additionalProperties := self.node.get("additionalProperties", None), dict)
            ):
                return "dict", SchemaSource(self.link / "additionalProperties", additionalProperties)

            if node_type == "array" and "items" in self.node:
                return "array", SchemaSource(self.link / "items", self.node["items"])

            if node_type == "null":
                return "null", type(None)
            if node_type == "boolean":
                return "scalar", bool
            if node_type == "integer":
                return "scalar", int
            if node_type == "number":
                return "scalar", float
            if node_type == "string":
                return "scalar", str

        return "unknown", None

    def get_metadata(self) -> SchemaMetadata:
        return SchemaMetadata.parse(self.node)

    def resolve_path(self, path: Path) -> Path:
        return (self.link.filepath.parent / path).resolve()

    @raises(LoadSchemaWarning)
    @staticmethod
    def load(src: Path) -> Union[SchemaJSON, ABSENCE]:
        try:
            with open(src, "r", encoding="utf-8") as f:
                return yaml.load(f, Loader=SimpleYAMLLoader)
        except (yaml.YAMLError, OSError) as e:
            err = LoadSchemaWarning(Link(src))
            err.__cause__ = e
            warnings.warn(err)
            return ABSENCE_VALUE

@dataclass(frozen=True)
class _SchemaNode:
    schema: SchemaSource
    fieldpath: FieldPath = field(default_factory=FieldPath)

@dataclass
class Source:
    link: Link
    parent: Union[SourcedJSON, Dict[Path, Union[SourcedJSON, ABSENCE]]]
    key: Union[int, str, Path] # assert parent[key] is valid
    context: Tuple[Tuple[str, str], ...]

    def __post_init__(self):
        # ensure accessibility
        self.parent[self.key] # type: ignore

    @property
    def data(self) -> SourcedJSON:
        return self.parent[self.key] # type: ignore

    @data.setter
    def data(self, value: SourcedJSON):
        self.parent[self.key] = value # type: ignore

    def resolve_path(self, path: Path) -> Path:
        return (self.link.filepath.parent / path).resolve()

    def resolve_resource(self, resource: Resource) -> Resource:
        """resolve file://relative_path, prepend context"""
        if not resource.uri.startswith("file://"):
            return Resource(resource.uri, self.context + resource.context)
        path = self.resolve_path(Path(resource.uri[len("file://"):]))
        return Resource(f"file://{path}", self.context + resource.context)

    @raises(LoadSourceWarning)
    @staticmethod
    def load(src: Path) -> Union[SourcedJSON, ABSENCE]:
        try:
            with open(src, "r", encoding="utf-8") as f:
                return yaml.load(f, Loader=SourcedYAMLLoader)
        except (yaml.YAMLError, OSError) as e:
            err = LoadSourceWarning(src)
            err.__cause__ = e
            warnings.warn(err)
            return ABSENCE_VALUE

@dataclass
class SourcedNode:
    link: Link
    sources: List[Source] # assert len(self.sources) > 0
    # assert not isinstance(source.data, (Include, Merge))
    schema: List[SchemaSource]
    
    def print(self, stream: Optional[IO[str]] = None):
        stream = stream if stream is not None else sys.stdout
        print("---", file=stream)
        print("# !merge_all", file=stream)
        for schema in self.schema:
            print("# $schema: " + str(schema.link), file=stream)
        for source in self.sources:
            print("---", file=stream)
            print("# $id: " + str(source.link), file=stream)
            yaml.dump(source.data, stream, Dumper=SourcedYAMLDumper, sort_keys=False)

    @raises(IncompatibleMergeWarning)
    def access_sources(self) -> Tuple[Literal["null", "scalar", "map", "seq"], List[Source]]:
        first_type = "null"
        first_source = None
        res: List[Source] = []
        for i, source in enumerate(reversed(self.sources)):
            data = source.data
            if data is None: continue

            inspected_type = ""
            if isinstance(data, dict):
                inspected_type = "map"
            elif isinstance(data, list):
                inspected_type = "seq"
            else:
                assert not isinstance(data, (Include, Merge))
                inspected_type = "scalar"
            
            if first_type != "null" and first_type != inspected_type:
                assert first_source is not None
                warnings.warn(IncompatibleMergeWarning(source.link, inspected_type, first_source.link, first_type))
                break
            first_type = inspected_type
            first_source = source
            res.append(self.sources[-1-i])

        return first_type, list(reversed(res))

    @raises(IncompatibleMergeWarning)
    def access(self) -> Tuple[Literal["null", "scalar", "map", "seq"], Union[None, JSONScalar, Resource, List[str], range]]:
        """
        get type (null/scalar/map/seq) and value (None/scalar value or Resource/keys/index range)
        """
        type_, sources = self.access_sources()
        value: Union[None, JSONScalar, List[str], range] = None
        if type_ == "null":
            value = None
        elif type_ == "scalar":
            data = sources[-1].data
            assert isinstance(data, (bool, int, float, str, Resource))
            if isinstance(data, Resource):
                data = sources[-1].resolve_resource(data)
            value = data # type: ignore
        elif type_ == "map":
            value = []
            for source in sources:
                data = source.data
                assert isinstance(data, dict)
                for key in data.keys():
                    if key not in value:
                        value.append(key)
        elif type_ == "seq":
            # zip longest
            value = 0
            for source in sources:
                data = source.data
                assert isinstance(data, list)
                value = max(value, len(data))
            value = range(value)
        else:
            value = None
        return type_, value

    @staticmethod
    def _check_pure_scalar(value: JSON, schema: SchemaSource) -> bool:
        schema_type, schema_value = schema.access()

        if schema_type == "any":
            return True

        if schema_type == "null" or schema_type == "scalar":
            assert isinstance(schema_value, type)
            return isinstance(value, schema_value)

        return False

    @raises(SchemaMismatchTypeWarning, SchemaMismatchStructWarning, SchemaMismatchScalarWarning)
    def check(self):
        type_, value = self.access()
        if type_ == "null":
            return

        elif type_ == "map":
            assert isinstance(value, list)
            for schema in self.schema:
                schema_type, schema_value = schema.access()

                if schema_type == "any":
                    continue

                if schema_type == "dict":
                    continue

                if schema_type == "struct":
                    value_keys = set(value)
                    assert isinstance(schema_value, dict)
                    schema_keys = set(schema_value.keys())
                    if not (value_keys <= schema_keys):
                        warnings.warn(SchemaMismatchStructWarning(self.link, schema.link, value_keys - schema_keys, set()))
                    continue

                warnings.warn(SchemaMismatchTypeWarning(self.link, type_, schema.link, schema_type))

        elif type_ == "seq":
            for schema in self.schema:
                schema_type, schema_value = schema.access()

                if schema_type == "any":
                    continue

                if schema_type == "array":
                    continue

                warnings.warn(SchemaMismatchTypeWarning(self.link, type_, schema.link, schema_type))

        elif type_ == "scalar":
            for schema in self.schema:
                schema_type, schema_value = schema.access()

                if schema_type == "any":
                    continue

                if schema_type == "scalar":
                    if isinstance(value, Resource):
                        value = str(value)
                    else:
                        value = assert_JSON(value)
                    if not self._check_pure_scalar(value, schema):
                        warnings.warn(SchemaMismatchScalarWarning(self.link, value, schema.link, schema.node))
                    continue

                warnings.warn(SchemaMismatchTypeWarning(self.link, type_, schema.link, schema_type))

class FileAlreadyLoadedError(Exception):
    def __init__(self, filepath: Path):
        self.filepath = filepath
    def __str__(self) -> str:
        return f"file is already loaded in SourcedLoader: {self.filepath}"

@dataclass
class SourceLoader:
    all_includes: Dict[Path, Union[SourcedJSON, ABSENCE]] = field(default_factory=lambda: {})
    all_schema: Dict[Path, Union[SchemaJSON, ABSENCE]] = field(default_factory=lambda: {})
    sync_resource_manager: SyncResourceManager = field(default_factory=SyncResourceManager)

    def print(self, stream: Optional[IO[str]] = None):
        stream = stream if stream is not None else sys.stdout
        for filepath, data in self.all_includes.items():
            if data is not ABSENCE_VALUE:
                print("---", file=stream)
                print("# $id: " + str(filepath), file=stream)
                yaml.dump(data, stream, Dumper=SourcedYAMLDumper, sort_keys=False)

    @raises(LoadSourceWarning, LoadSchemaWarning)
    def _load_source(self, filepath: Path, context: Tuple[Tuple[str, str], ...]) -> Tuple[Optional[Source], Optional[SchemaSource]]:
        # lazy load yaml file
        if filepath not in self.all_includes:
            self.all_includes[filepath] = Source.load(filepath)
        if self.all_includes[filepath] is ABSENCE_VALUE:
            return None, None
        raw_source = Source(Link(filepath), self.all_includes, filepath, context)
        
        if isinstance(raw_source.data, dict):
            node = raw_source.data.get("$schema", None)
            if node is None:
                raw_schema = None
            elif not isinstance(node, dict) or not is_JSON(node):
                # TODO: support $schema: /path/to/my.shema.json
                warnings.warn(LoadSchemaWarning(Link(filepath) / "$schema"))
                raw_schema = None
            else:
                raw_schema = SchemaSource(Link(filepath) / "$schema", assert_JSON(node))
        else:
            raw_schema = None

        return raw_source, raw_schema

    @raises(FileAlreadyLoadedError)
    def _new_source(self, filepath: Path) -> Source:
        if filepath in self.all_includes:
            raise FileAlreadyLoadedError(filepath)
        self.all_includes[filepath] = None
        return Source(Link(filepath), self.all_includes, filepath, ())

    @raises(LoadSchemaWarning, LinkAccessWarning)
    def _load_schema(self, link: Link) -> Optional[SchemaSource]:
        if link.filepath not in self.all_schema:
            self.all_schema[link.filepath] = SchemaSource.load(link.filepath)
        schema = self.all_schema[link.filepath]
        if schema is ABSENCE_VALUE:
            return None
        try:
            node = link.fieldpath.walk(schema)
        except FieldAccessError as err:
            warnings.warn(LinkAccessWarning(Link(link.filepath, err.path)))
            return None
        return SchemaSource(link, node)

    @raises(LoadSchemaWarning, SchemaRefLoopWarning, LinkAccessWarning)
    def _resolve_schema_ref(self, schema: SchemaSource) -> Tuple[Optional[SchemaSource], Set[Path]]:
        depends: Set[Path] = set()
        visited: Set[Link] = set()
        while True:
            inner = schema.get_inner()
            if inner is not None:
                schema = inner
                continue

            ref = schema.get_ref()
            if ref is not None:
                if ref in visited:
                    # loop
                    warnings.warn(SchemaRefLoopWarning(ref))
                    return None, set()
                visited.add(ref)
                depends.add(schema.resolve_path(ref.filepath))
                ref = Link(schema.resolve_path(ref.filepath), ref.fieldpath)
                schema_ = self._load_schema(ref)
                if schema_ is None: return None, depends
                schema = schema_
                continue

            break
        return schema, depends

    @raises(LoadSchemaWarning, SchemaRefLoopWarning, LinkAccessWarning, SchemaAccessWarning)
    def _resolve_schema(self, schema: SchemaSource, fieldpath: FieldPath) -> Tuple[Optional[SchemaSource], Set[Path]]:
        depends: Set[Path] = set()
        for key in fieldpath.elements:
            schema_, depends_ = self._resolve_schema_ref(schema)
            depends.update(depends_)
            if schema_ is None:
                return None, depends
            schema = schema_

            type_, value = schema.access()

            if type_ == "any":
                continue

            if type_ == "struct":
                assert isinstance(value, dict)
                if key not in value:
                    warnings.warn(SchemaAccessWarning(schema.link, type_, key))
                    return None, depends
                schema = value[key]
                continue

            if type_ == "dict":
                assert isinstance(value, SchemaSource)
                if not isinstance(key, str):
                    warnings.warn(SchemaAccessWarning(schema.link, type_, key))
                    return None, depends
                schema = value
                continue

            if type_ == "array":
                assert isinstance(value, SchemaSource)
                if not isinstance(key, int):
                    warnings.warn(SchemaAccessWarning(schema.link, type_, key))
                    return None, depends
                schema = value
                continue

            warnings.warn(SchemaAccessWarning(schema.link, type_, key))
            return None, depends

        schema_, depends_ = self._resolve_schema_ref(schema)
        depends.update(depends_)
        return schema_, depends

    @raises(LoadSourceWarning, LoadSchemaWarning, EmptyMergeWarning)
    def _resolve_sources(self, source: Source, fieldpath: FieldPath) -> Tuple[List[Source], List[_SchemaNode], Set[Path]]:
        outputs: List[Source] = []
        schema_nodes: List[_SchemaNode] = []
        depends: Set[Path] = set()
        inputs = [(source, fieldpath)]
        while inputs:
            source, fieldpath = inputs.pop()
            data = source.data

            if isinstance(data, Include):
                filepath_include = source.resolve_path(data.link.filepath)
                depends.add(filepath_include)
                source_include, schema_include = self._load_source(filepath_include, source.context + data.context)
                if source_include is not None:
                    inputs.append((source_include, data.link.fieldpath / fieldpath))
                if schema_include is not None:
                    schema_nodes.append(_SchemaNode(schema_include, data.link.fieldpath / fieldpath))
                continue

            if isinstance(data, Merge):
                if len(data.items) == 0:
                    warnings.warn(EmptyMergeWarning(source.link))
                for i in range(len(data.items)):
                    source_i = Source(source.link / i, data.items, i, source.context)
                    inputs.append((source_i, fieldpath))
                continue

            if not fieldpath:
                outputs.append(source)
                continue

            key = fieldpath.elements[0]
            if isinstance(key, str):
                if not isinstance(data, dict) or key not in data:
                    continue
            else:
                if not isinstance(data, list) or key >= len(data):
                    continue

            source_key = Source(source.link / key, data, key, source.context)
            inputs.append((source_key, fieldpath[1:]))

        return list(reversed(outputs)), list(reversed(schema_nodes)), depends

    @raises(LoadSourceWarning, SchemaRefLoopWarning, EmptyMergeWarning, LoadSchemaWarning, SchemaRefLoopWarning, LinkAccessWarning,
            SchemaAccessWarning)
    def load(self, src: Link) -> Tuple[Optional[SourcedNode], Set[Path]]:
        """
        load yaml file, returns node and file dependencies of current node.
        """
        src = Link(src.filepath.resolve(), src.fieldpath)
        depends = {src.filepath}
        raw_source, raw_schema = self._load_source(src.filepath, ())
        if raw_source is None: return None, depends
        sources, schema_nodes, depends_ = self._resolve_sources(raw_source, src.fieldpath)
        depends.update(depends_)
        if not sources: return None, depends
        if raw_schema is not None:
            schema_nodes.insert(0, _SchemaNode(raw_schema, src.fieldpath))
        schema_list: List[SchemaSource] = []
        for i, schema_node in enumerate(schema_nodes):
            if schema_node not in schema_nodes[:i]:
                schema, depends_ = self._resolve_schema(schema_node.schema, schema_node.fieldpath)
                depends.update(depends_)
                if schema is not None:
                    schema_list.append(schema)
        return SourcedNode(src, sources, schema_list), depends

    @raises(FileAlreadyLoadedError)
    def new(self, src: Path) -> SourcedNode:
        """
        make an null node assigned to given path.
        this path must be new, or you should remove it from all_includes manually.
        """
        src = src.resolve()
        source = self._new_source(src)
        return SourcedNode(Link(src), [source], [])

    @raises(LoadSourceWarning, SchemaRefLoopWarning, EmptyMergeWarning, LoadSchemaWarning, SchemaRefLoopWarning, LinkAccessWarning,
            SchemaAccessWarning)
    def get_(self, node: SourcedNode, fieldpath: Union[int, str, FieldPath]) -> Tuple[Optional[SourcedNode], Set[Path]]:
        """
        resolve given node until given path. returns the node of given path and its dependencies, or None for failure.
        """
        if isinstance(fieldpath, (int, str)):
            fieldpath = FieldPath((fieldpath,))

        sources: List[Source] = []
        schema_nodes = [_SchemaNode(schema, fieldpath) for schema in node.schema]
        depends: Set[Path] = set()
        for source in node.sources:
            sources_, schema_nodes_, depends_ = self._resolve_sources(source, fieldpath)
            sources.extend(sources_)
            schema_nodes.extend(schema_nodes_)
            depends.update(depends_)
        if not sources: return None, depends

        schema_list: List[SchemaSource] = []
        for i, schema_node in enumerate(schema_nodes):
            if schema_node not in schema_nodes[:i]:
                schema, depends_ = self._resolve_schema(schema_node.schema, schema_node.fieldpath)
                if schema is not None:
                    schema_list.append(schema)
                depends.update(depends_)

        return SourcedNode(node.link / fieldpath, sources, schema_list), depends

    @raises(LoadSourceWarning, EmptyMergeWarning, LoadSchemaWarning, SchemaRefLoopWarning, LinkAccessWarning,
            SchemaAccessWarning)
    def get(self, node: SourcedNode, fieldpath: Union[int, str, FieldPath]) -> Optional[SourcedNode]:
        """
        resolve given node until given path. returns the node of given path, or None for failure.
        """
        return self.get_(node, fieldpath)[0]

    @raises(SchemaMismatchTypeWarning, SchemaMismatchStructWarning, LoadSourceWarning,
            EmptyMergeWarning, LoadSchemaWarning, SchemaRefLoopWarning, LinkAccessWarning, SchemaAccessWarning,
            IncompatibleMergeWarning)
    def resolve_all(self, node: SourcedNode, sync_resources: Optional[List[SyncResource]] = None) -> Tuple[JSON, Set[Path]]:
        """
        resolve full content of given node. returns resolved json object and its dependencies.
        the subnodes failed to resolve will be assigned to null.
        resource URI will be rewritten and collected into sync_resources unless sync_resources is None.
        """
        node.check()
        depends: Set[Path] = set()
        type_, value = node.access()
        if type_ == "map":
            assert isinstance(value, list)
            res: JSON = {}
            for key in value:
                subnode, depends_ = self.get_(node, key)
                depends.update(depends_)
                if subnode is None: continue
                res[key], depends_ = self.resolve_all(subnode, sync_resources)
                depends.update(depends_)
            return res, depends
        elif type_ == "seq":
            assert isinstance(value, range)
            res: JSON = []
            for index in value:
                subnode, depends_ = self.get_(node, index)
                depends.update(depends_)
                if subnode is None: continue
                res_, depends_ = self.resolve_all(subnode, sync_resources)
                res.append(res_)
                depends.update(depends_)
            return res, depends
        else:
            assert isinstance(value, (type(None), bool, int, float, str, Resource))
            if isinstance(value, Resource):
                if sync_resources is not None:
                    value = self.sync_resource_manager.rewrite_for_sync(value, sync_resources)
                else:
                    value = value.uri
            return value, depends

    @raises(SchemaMismatchTypeWarning, SchemaMismatchStructWarning, LoadSourceWarning,
            EmptyMergeWarning, LoadSchemaWarning, SchemaRefLoopWarning, LinkAccessWarning, SchemaAccessWarning,
            IncompatibleMergeWarning)
    def load_resolved(self, src: Link, sync_resources: Optional[List[SyncResource]]) -> Tuple[JSON, Set[Path]]:
        """
        load and resolve all.
        """
        node, depends = self.load(src)
        if node is None: return node, depends
        value, depends_ = self.resolve_all(node, sync_resources)
        depends.update(depends_)
        return value, depends

    def _can_be_ensured_along(self, node: SourcedNode, fieldpath: FieldPath) -> Optional[LinkAccessWarning]:
        """
        check if _ensure_top_along can be done.
        it is invalid for accessing map/seq as seq/map.
        accessing map/seq using absent key is valid, empty slots will be filled while ensuring.
        it is valid to access null as seq/map, since it is treated as empty slot.
        access warning will be returned for failure.
        """
        # LoadSourceWarning, EmptyMergeWarning, LoadSchemaWarning, SchemaRefLoopWarning,
        # LinkAccessWarning, SchemaAccessWarning, IncompatibleMergeWarning
        with warnings.catch_warnings():
            warnings.simplefilter("always")

            for key in fieldpath.elements:
                type_, _value = node.access()
                if isinstance(key, int):
                    if type_ == "null": break
                    if type_ != "seq":
                        return LinkAccessWarning(Link(node.link.filepath, node.link.fieldpath / key))
                    node_ = self.get(node, key)
                    if node_ is None: break
                    node = node_
                else:
                    if type_ == "null": break
                    if type_ != "map":
                        return LinkAccessWarning(Link(node.link.filepath, node.link.fieldpath / key))
                    node_ = self.get(node, key)
                    if node_ is None: break
                    node = node_

        return None

    @raises(LoadSourceWarning, EmptyMergeWarning, LoadSchemaWarning, SchemaRefLoopWarning, LinkAccessWarning,
            SchemaAccessWarning)
    def _ensure_top_along(self, node: SourcedNode, fieldpath: FieldPath, ensure_null: bool = False) -> Optional[SourcedNode]:
        """
        ensure a given path can be accessed and will be stored at the file of current top layer.
        returns the node of given path, or None if it is invalid.
        it will mutate content of related nodes.
        if ensure_null is true, ensure that the top layer of given path is null.

        <!> this may invalidate other sourced node.
        """
        # check if a path can be ensured
        warning = self._can_be_ensured_along(node, fieldpath)
        if warning is not None:
            warnings.warn(warning)
            return None

        for i, key in enumerate(fieldpath.elements):
            is_last_key = i == len(fieldpath.elements) - 1

            if isinstance(key, int):
                data = node.sources[-1].data
                if data is None:
                    data = node.sources[-1].data = []
                assert isinstance(data, list)
                if key >= len(data):
                    data.extend([None]*(key + 1 - len(data)))
            else:
                data = node.sources[-1].data
                if data is None:
                    data = node.sources[-1].data = {}
                assert isinstance(data, dict)
                if key not in data:
                    data[key] = None

            source = node.sources[-1]
            source = Source(source.link / key, source.data, key, source.context)
            if isinstance(source.data, Include):
                source.data = Merge([source.data, None])
            elif isinstance(source.data, Merge):
                while isinstance(source.data, Merge):
                    if len(source.data.items) == 0:
                        source.data = Merge([None])
                        break
                    index = len(source.data.items) - 1
                    if isinstance(source.data.items[index], Include):
                        source.data = Merge([*source.data.items, None])
                        break
                    if ensure_null and is_last_key and not isinstance(source.data.items[index], Merge):
                        source.data = Merge([*source.data.items, None])
                        break
                    source = Source(source.link / index, source.data.items, index, source.context)
            else:
                if ensure_null and is_last_key and source.data is not None:
                    source.data = Merge([source.data, None])

            node_ = self.get(node, key)
            assert node_ is not None
            node = node_

        return node

    @raises(LoadSourceWarning, EmptyMergeWarning, LoadSchemaWarning, SchemaRefLoopWarning, LinkAccessWarning,
            SchemaAccessWarning, IncompatibleMergeWarning, NotScalarNodeWarning,
            InvalidScalarWarning)
    def update(self, node: SourcedNode, folded_dict: JSONWithPath, machine: str = ""):
        """
        update sourced node by folded dictionary (keys are field paths, values are scalars).
        only the file of current top layer will be mutated.
        null will be skipped, it doesn't mean deletion.
        warning will be raised if a field is invalid to access.
        values can be paths, which will be converted to !resource, attached with machine information.

        <!> this may invalidate other sourced node.
        """
        for folded_path, value in JSONLike_deep_iter(folded_dict):
            path = FieldPath(tuple(elem for subpath in folded_path.elements for elem in FieldPath.parse(str(subpath)).elements))
            subnode = self._ensure_top_along(node, path, False)
            if subnode is None:
                continue
            if not isinstance(value, (bool, int, float, str, Path)): # type: ignore
                warnings.warn(InvalidScalarWarning(value))
                continue
            if isinstance(value, Path):
                value = Resource.create(value)
                if machine:
                    value = self.sync_resource_manager.attach_machine(value, machine)
            type_, _value = subnode.access()
            if not (type_ == "scalar" or type_ == "null"):
                warnings.warn(NotScalarNodeWarning(subnode.link))
                continue
            subnode.sources[-1].data = value

    @raises(LoadSourceWarning, EmptyMergeWarning, LoadSchemaWarning, SchemaRefLoopWarning, LinkAccessWarning,
            SchemaAccessWarning)
    def include(self, node: SourcedNode, folded_dict: JSONWithOnlyLink, machine: str = ""):
        """
        insert include sourced node by folded dictionary (keys are field paths, values are include paths).
        only the file of current top layer will be mutated.
        warning will be raised if a field is invalid to access.
        values must be paths, which will be converted to !include, attached with machine information.

        <!> this may invalidate other sourced node.
        """
        for folded_path, value in JSONLike_deep_iter(folded_dict):
            path = FieldPath(tuple(elem for subpath in folded_path.elements for elem in FieldPath.parse(str(subpath)).elements))
            subnode = self._ensure_top_along(node, path, True)
            if subnode is None:
                continue
            source = subnode.sources[-1]
            assert source.data is None
            if not isinstance(value, (str, Path, Link)): # type: ignore
                warnings.warn(InvalidIncludeWarning(value))
                continue
            else:
                include = Include.create(value)
                if machine:
                    include = self.sync_resource_manager.attach_machine(include, machine)
                source.data = include

def load(link: Union[str, Link]) -> str:
    """
    load and resolve yaml file, return resolved yaml content.
    """
    loader = SourceLoader()
    link = Link.parse(link) if isinstance(link, str) else link
    link = Link(link.filepath.resolve(), link.fieldpath)

    data = None
    node, _depends = loader.load(link)
    if node is not None:
        data, _depends = loader.resolve_all(node)

    data = deep_copy_skip_empty(data)
    return yaml.dump(data, Dumper=SimpleYAMLDumper, sort_keys=False)

def get_mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return 0

class YAMLWatcher:
    """
    watch mutation of dependent files of specific node, manage sourced node and resolved json.

    loader will cache loaded files, but will not reload to latest verison automatically.
    """
    loader: SourceLoader
    mtimes: Dict[Path, int]
    path: Path
    depends: Set[Path]
    node: Optional[SourcedNode]
    data: JSON

    def __init__(self, path: Path):
        self.loader = SourceLoader()
        self.mtimes = {}

        self.path = path.resolve()
        self.depends = {self.path}
        self.node = None
        self.data = None

    def is_changed(self) -> bool:
        return any(self.mtimes.get(depend, 0) != get_mtime(depend) for depend in self.depends)

    @raises(SchemaMismatchTypeWarning, SchemaMismatchStructWarning, LoadSourceWarning,
            EmptyMergeWarning, LoadSchemaWarning, SchemaRefLoopWarning, LinkAccessWarning, SchemaAccessWarning,
            IncompatibleMergeWarning, NotScalarNodeWarning, InvalidScalarWarning,
            RootIsNotMapWarning)
    def load(self, aggregate_sync_resources: bool):
        """
        reload yaml file and resolved it.
        if aggregate_sync_resources is true, additional fields about all sync resources will be inserted.
        this is for monoresource.
        """
        self.depends = {self.path}
        self.node = None
        self.data = None

        for depend in list(self.mtimes.keys()):
            if self.mtimes[depend] != get_mtime(depend):
                del self.mtimes[depend]
                if depend in self.loader.all_includes:
                    del self.loader.all_includes[depend]
                if depend in self.loader.all_schema:
                    del self.loader.all_schema[depend]

        depends = {self.path}
        node, depends_ = self.loader.load(Link(self.path))
        depends.update(depends_)

        sync_resources: List[SyncResource] = []
        data_ = None
        if node is not None:
            data_, depends_ = self.loader.resolve_all(node, sync_resources)
            depends.update(depends_)

        self.mtimes = {
            depend: get_mtime(depend)
            for depend in [*self.loader.all_includes.keys(), *self.loader.all_schema.keys()]
        }
        
        if node is None: return
        data = deep_copy_skip_empty(data_)
        if aggregate_sync_resources:
            if data is None:
                data = self.loader.sync_resource_manager.aggregate_sync_resources(sync_resources)
            elif isinstance(data, dict):
                data.update(self.loader.sync_resource_manager.aggregate_sync_resources(sync_resources))
            else:
                warnings.warn(RootIsNotMapWarning(self.path))
                return

        self.depends = depends
        self.node = node
        self.data = data

    def save(self, path: Path):
        """
        save corresponding sourced node to given yaml file.
        you cannot add new include paths in this way.

        mutating sourced node usually make resolved json and file out-of-sync with it.
        """
        if path in self.loader.all_includes:
            node = self.loader.all_includes[path]
        elif path in self.loader.all_schema:
            node = self.loader.all_schema[path]
        else:
            assert False
        assert node is not ABSENCE_VALUE
        with open(path, "w") as f:
            yaml.dump(node, f, Dumper=SourcedYAMLDumper, sort_keys=False)


def deep_copy_skip_empty(obj: JSON) -> Optional[JSON]:
    """
    copy json, remove null and empty map.
    since null/empty map in seq cannot be removed, empty map is filled in
    (rosparam bans null, even in seq).
    return null if obj is null.
    """
    if obj is None: return None

    if isinstance(obj, dict):
        out1: Dict[str, JSON] = {}
        for k, v in obj.items():
            v = deep_copy_skip_empty(v)
            if v is None: continue
            out1[k] = v
        if not out1: return None
        return out1

    if isinstance(obj, list):
        out2: List[JSON] = []
        for e in obj:
            e = deep_copy_skip_empty(e)
            e = e if e is not None else {}
            out2.append(e)
        return out2

    return deep_copy(obj)

def rosparam_diff(old: JSON, new: JSON) -> Dict["FieldPath", Optional[JSON]]:
    """
    diff in the scalar level, returns dict from path to updated values (None for deletion).
    treat null as empty map, seq as scalar.
    (this is for updating ros parameter, because rosparam treat seq as scalar)
    """
    updated: Dict[FieldPath, Optional[JSON]] = {}

    def walk(path: FieldPath, a: JSON, b: JSON):
        nonlocal updated
        if a is None: a = {}
        if b is None: b = {}
        a_is_map = isinstance(a, dict)
        b_is_map = isinstance(b, dict)

        if not a_is_map and b_is_map:
            updated[path] = None
            walk(path, {}, b)

        elif a_is_map and not b_is_map:
            walk(path, a, {})
            updated[path] = b

        elif not a_is_map and not b_is_map:
            if not deep_eq(a, b):
                updated[path] = b

        elif a_is_map and b_is_map:
            keys = list(a.keys())
            for k in b.keys():
                if k not in keys:
                    keys.append(k)
            for k in keys:
                av = a.get(k, {})
                bv = b.get(k, {})
                walk(path / k, av, bv)

    walk(FieldPath(), old, new)
    return updated

_ResolvedListener = Callable[[Dict[FieldPath, Optional[JSON]]], None] # [FieldPath]JSON? -> None
_BackResolvedListener = Callable[[Path, Dict[FieldPath, Optional[SourcedJSON]]], None] # (Path, [FieldPath]SourcedJSON?) -> None

class YAMLSynchronizer:
    """
    synchronize sourced yaml and resolved yaml files.
    both files must exist at initial.
    
    first spin will update resolved yaml if they are out-of-sync.
    
    mutating both sides at the same time may cause undefined behavior.
    """
    original: YAMLWatcher
    resolved: YAMLWatcher
    _resolved_listeners: List[_ResolvedListener]
    _back_resolved_listeners: List[_BackResolvedListener]
    aggregate_sync_resources: bool = True
    
    def __init__(self, original_path: Path, resolved_path: Path):
        self.original = YAMLWatcher(original_path)
        self.resolved = YAMLWatcher(resolved_path)
        self._resolved_listeners = []
        self._back_resolved_listeners = []

    def init(self):
        self.resolved.load(self.aggregate_sync_resources)

    def get_status(self):
        status = ""
        status += f"original: {self.original.path} " + ("(ready)" if self.original.node is not None else "(error)") + "\n"
        status += f"resolved: {self.resolved.path} " + ("(ready)" if self.resolved.node is not None else "(error)") + "\n"
        status += f"track: " + ", ".join(str(dep) for dep in self.original.depends) + "\n"
        return status

    @raises(SchemaMismatchTypeWarning, SchemaMismatchStructWarning, LoadSourceWarning,
            EmptyMergeWarning, LoadSchemaWarning, SchemaRefLoopWarning, LinkAccessWarning, SchemaAccessWarning,
            IncompatibleMergeWarning, NotScalarNodeWarning, InvalidScalarWarning,
            RootIsNotMapWarning, SyncFileNotReadyWarning)
    def resolve(self) -> Dict[FieldPath, JSON]:
        """
        resolve sourced yaml file and update resolved yaml file.
        returns difference for rosparam.
        skips if resolved and sourced yaml files are not loaded correctly.
        """
        # TODO: compare with $sync_resources, don't update it

        if self.resolved.node is None:
            warnings.warn(SyncFileNotReadyWarning("resolved"))
            return {}

        self.original.load(self.aggregate_sync_resources)
        if self.original.node is None:
            warnings.warn(SyncFileNotReadyWarning("original"))
            return {}

        diff = rosparam_diff(self.resolved.data, self.original.data)
        if diff:
            self.resolved.loader.all_includes[self.resolved.path] = as_SourcedJSON(self.original.data)
            self.resolved.save(self.resolved.path)
        return diff

    @raises(SchemaMismatchTypeWarning, SchemaMismatchStructWarning, LoadSourceWarning,
            EmptyMergeWarning, LoadSchemaWarning, SchemaRefLoopWarning, LinkAccessWarning, SchemaAccessWarning,
            IncompatibleMergeWarning, NotScalarNodeWarning, InvalidScalarWarning,
            RootIsNotMapWarning, InvalidDeletionSynchronizationWarning)
    def back_resolve(self) -> Dict[Path, Dict[FieldPath, SourcedJSON]]:
        """
        back resolve resolved yaml file and update sourced yaml file.
        returns difference for each included path.
        due to merge machinism, fields cannot be removed.
        skips if resolved yaml file is not loaded correctly.
        """
        old_resolved_data = deep_copy(self.resolved.data)
        if self.resolved.node is None:
            warnings.warn(SyncFileNotReadyWarning("resolved"))
            return {}

        self.resolved.load(False)
        if self.resolved.node is None: # type: ignore
            warnings.warn(SyncFileNotReadyWarning("resolved"))
            return {}

        resolved_diff = deep_diff(old_resolved_data, self.resolved.data)

        diffs: Dict[Path, Dict[FieldPath, SourcedJSON]] = {}

        if resolved_diff:
            if self.original.data is None:
                warnings.warn(SyncFileNotReadyWarning("original"))
                return {}

            old_original_sources = {
                depend: SourcedJSON_deep_copy(sourced_node)
                for depend in self.original.depends
                if (sourced_node := self.original.loader.all_includes.get(depend, ABSENCE_VALUE)) is not ABSENCE_VALUE
            }

            update: JSONWithPath = {}
            for key, value in resolved_diff.items():
                if value is None:
                    # TODO: try to delete standalone (not-merged) field
                    warnings.warn(InvalidDeletionSynchronizationWarning(key))
                else:
                    update[str(key)] = as_JSONWithPath(value)
            assert self.original.node is not None
            self.original.loader.update(self.original.node, update)

            for depend in old_original_sources.keys():
                old = old_original_sources[depend]
                new = self.original.loader.all_includes[depend]
                assert new is not ABSENCE_VALUE
                diff = SourcedJSON_deep_diff(old, new)
                if diff:
                    diffs[depend] = diff

        for depend in diffs.keys():
            self.original.save(depend)

        return diffs

    def add_resolved_listener(self, callback: _ResolvedListener):
        self._resolved_listeners.append(callback)

    def add_back_resolved_listener(self, callback: _BackResolvedListener):
        self._back_resolved_listeners.append(callback)

    def _resolved_listener(self, diff: Dict[FieldPath, Optional[JSON]]):
        for lisener in self._resolved_listeners:
            lisener(diff)

    def _back_resolved_listener(self, path: Path, diff: Dict[FieldPath, Optional[SourcedJSON]]):
        for lisener in self._back_resolved_listeners:
            lisener(path, diff)

    @raises(SchemaMismatchTypeWarning, SchemaMismatchStructWarning, LoadSourceWarning,
            EmptyMergeWarning, LoadSchemaWarning, SchemaRefLoopWarning, LinkAccessWarning, SchemaAccessWarning,
            IncompatibleMergeWarning, NotScalarNodeWarning, InvalidScalarWarning)
    def spin_once(self):
        if self.original.is_changed():
            diff = self.resolve()
            if diff:
                self._resolved_listener(diff)
        if self.resolved.is_changed():
            diffs = self.back_resolve()
            for depend, diff in diffs.items():
                self._back_resolved_listener(depend, diff)

    @raises(SchemaMismatchTypeWarning, SchemaMismatchStructWarning, LoadSourceWarning,
            EmptyMergeWarning, LoadSchemaWarning, SchemaRefLoopWarning, LinkAccessWarning, SchemaAccessWarning,
            IncompatibleMergeWarning, NotScalarNodeWarning, InvalidScalarWarning)
    def spin(self, dt: float = 0.1):
        import time
        status = ""
        while True:
            self.spin_once()
            status_ = self.get_status()
            if status != status_:
                print(status_)
            status = status_
            time.sleep(dt)

def resolve(source_path: Union[str, Path], aggregate_sync_resources: bool = True) -> str:
    """
    resolve yaml file, save as {name}.resolved.yaml.
    if aggregate_sync_resources is true, it will append field "$sync_resources" at the root.
    """
    
    source_path = Path(source_path)
    resolved_path = source_path.parent / f"{source_path.stem}.resolved.yaml"
    if resolved_path.exists():
        resolved_path.unlink()
    resolved_path.touch()
    print(f"resolve {source_path} -> {resolved_path}")
    sync = YAMLSynchronizer(source_path, resolved_path)
    sync.aggregate_sync_resources = aggregate_sync_resources
    sync.init()
    sync.resolve()
    return str(resolved_path)
