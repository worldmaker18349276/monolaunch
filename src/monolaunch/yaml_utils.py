"""
a YAML utility tools for simple YAML.

it is based on pyyaml but focus on simple YAML.

YAML supports more flexible structure than JSON, such as:
- shared nodes (circular referencing is also valid)
- mapping with non-string keys
- custom objects

that are not what we want, we just want to serialize object in YAML format.
we only needs to consider:
- None
- distinguishable simple scalar type: bool, int, float (including nan and inf), string
- simple container type: list and dict with string keys

and we prefer the syle:
- None -> `null`, bool -> `false`, `true`
- single-line string always double quoted (except keys of map)
- multi-line string use `|` style if possible
- float always contains `.`, including `.nan` and `.inf`
- flow-style for simple list and dict (they have same scalar value type, and dict only contains single-letter keys)
- block seq style always indent, so that it can be folded in editor

we provide some tools for dealing with JSON object in deep.
we also provide a simple resolver for !include and !merge tags:

- `!include` imports another YAML file.

  base.yaml:
  ```
  a: 1
  b: 2
  ```
  
  config.yaml:
  ```
  base: !include base.yaml
  ```
  
  resolve to:
  ```
  base:
    a: 1
    b: 2
  ```

- `!merge` merges a list of mappings.

  ```
  config: !merge
    - timeout: 10
      retries: 3
    - timeout: 30
  ```
  
  resolve to:
  ```
  config:
    timeout: 30
    retries: 3
  ```

  rules:
  - null <> any = any <> null = any   --  null behave like empty slot
  - scalar <> scalar = later one
  - seq <> seq = zip longest with <>
  - map <> map = union zip with <>
  - non-null type <> another non-null type = later one

ExYAMLLoader also keeps unknown tags and construct them as TaggedScalar, TaggedList, TaggedDict,
and ExYAMLDumper is able to re-export those tagged objects.
you can use `python -m monolaunch.yaml_utils <yaml file>` directly to resolve YAML with !include and !merge.
"""
from inspect import cleandoc
import math
import re
from typing import Any, Dict, Generator, List, Sequence, Set, Tuple, Union, Optional, cast
from pathlib import Path
import urllib.parse
from dataclasses import dataclass, field
import yaml

__all__ = [
    "JSONScalar", "JSON",
    "is_JSON", "assert_JSON",
    "deep_update", "deep_merge", "deep_copy", "deep_eq", "deep_diff", "deep_iter",
    "FieldAccessError", "JsonPath", "JPointer", "PathWithJPointer", "TypedPathWithJPointer",
    "SimpleYAMLLoader", "load_YAML", "SimpleYAMLDumper", "save_YAML",
    "TaggedScalar", "TaggedDict", "TaggedList", "TaggedJSON",
    "ExYAMLLoader", "load_ExYAML", "ExYAMLDumper", "save_ExYAML",
]


JSONScalar = Union[bool, int, float, str] # int, float are different, nan, inf are allowed
JSON = Union[None, JSONScalar, List["JSON"], Dict[str, "JSON"]]

@dataclass(frozen=True)
class TaggedScalar:
    data: JSONScalar
    tag: str = ""

class TaggedDict(Dict[str, "TaggedJSON"]):
    tag: str = ""

class TaggedList(List["TaggedJSON"]):
    tag: str = ""

TaggedJSON = Union[None, JSONScalar, List["TaggedJSON"], Dict[str, "TaggedJSON"], TaggedScalar, TaggedList, TaggedDict]


def is_JSON(data: Any) -> bool:
    """
    check if an object is json, the type must match exactly, not just a subtype.
    """
    if type(data) in (type(None), bool, int, float, str):
        return True
    elif type(data) == list:
        return all(is_JSON(e) for e in cast(List[Any], data))
    elif type(data) == dict:
        return all(type(k) == str and is_JSON(v) for k, v in cast(Dict[Any, Any], data).items())
    else:
        return False

def assert_JSON(data: Any) -> JSON:
    """
    assert if an object is json, the type must match exactly, not just a subtype.
    """
    if not is_JSON(data):
        raise TypeError(f"not json: {data}")
    return cast(JSON, data)


def deep_copy(obj: JSON) -> JSON:
    """
    deep copy the whole json.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return {k: deep_copy(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [deep_copy(e) for e in obj]
    return obj

def deep_update(base: JSON, update: JSON) -> JSON:
    """
    update base by copying update, returns updated base.
    update dict and list, treating null as an empty slot.
    """
    # skip null
    if base is not None and update is None:
        return base

    if type(base) != type(update):
        return deep_copy(update)

    if isinstance(base, dict):
        assert isinstance(update, dict)
        for k, v in update.items():
            base[k] = deep_update(base.get(k), v)
        return base

    if isinstance(base, list):
        assert isinstance(update, list)
        # zip longest
        if len(base) < len(update):
            base.extend([None]*(len(update) - len(base)))
        elif len(base) > len(update):
            update = [*update, *[None]*(len(base) - len(update))]
        for i in range(len(base)):
            base[i] = deep_update(base[i], update[i])
        return base

    return deep_copy(update)

def _deep_merge(path: "JPointer", base: JSON, update: JSON) -> Tuple[JSON, List["JPointer"]]:
    # skip null
    if base is not None and update is None:
        return base, []

    if base is None and update is not None:
        return deep_copy(update), []

    if type(base) != type(update):
        return base, [path]

    if isinstance(base, dict):
        assert isinstance(update, dict)
        inconsistencies: List[JPointer] = []
        for k, v in update.items():
            v, a = _deep_merge(path.append(k), base.get(k), v)
            base[k] = v
            inconsistencies.extend(a)
        return base, inconsistencies

    if isinstance(base, list):
        assert isinstance(update, list)
        inconsistencies: List[JPointer] = []
        # zip longest
        if len(base) < len(update):
            base.extend([None]*(len(update) - len(base)))
        elif len(base) > len(update):
            update = [*update, *[None]*(len(base) - len(update))]
        for i in range(len(base)):
            v, a = _deep_merge(path.append(i), base[i], update[i])
            base[i] = v
            inconsistencies.extend(a)
        return base, inconsistencies

    if base == update:
        return base, []

    return base, [path]

def deep_merge(base: JSON, update: JSON) -> Tuple[JSON, List["JPointer"]]:
    """
    merge base by copying update, returns merged base and inconsistent paths.
    unlike deep_update, different values at the same field will not be overrided, and warnings will be raised.
    """
    return _deep_merge(JPointer(), base, update)

def deep_eq(lhs: JSON, rhs: JSON) -> bool:
    """
    deep compare two jsons.
    noting that nan equal to nan, True and 1 and 1.0 are different.
    """
    stack: List[Tuple[JSON, JSON]] = [(lhs, rhs)]
    while stack:
        lhs, rhs = stack.pop()
        if type(lhs) != type(rhs):
            return False

        if isinstance(lhs, dict):
            assert isinstance(rhs, dict)
            if set(lhs.keys()) != set(rhs.keys()):
                return False
            for k in lhs:
                stack.append((lhs[k], rhs[k]))
            continue
        
        if isinstance(lhs, list):
            assert isinstance(rhs, list)
            if len(lhs) != len(rhs):
                return False
            for i in range(len(lhs)):
                stack.append((lhs[i], rhs[i]))
            continue

        # special case: nan != nan
        if isinstance(lhs, float) and isinstance(rhs, float) and math.isnan(lhs) and math.isnan(rhs):
            continue

        if lhs != rhs:
            return False

    return True

def deep_diff(old: JSON, new: JSON) -> Dict["JPointer", Optional[JSON]]:
    """
    diff in the scalar level.
    null is treated as empty slot.
    returns map from paths to: scalars for updating values, or maps/seqs for changing types, or None for deletion.
    """
    updated: Dict[JPointer, Optional[JSON]] = {}

    stack: List[Tuple[JPointer, JSON, JSON]] = [(JPointer(), old, new)]
    while stack:
        path, a, b = stack.pop()

        if type(a) != type(b):
            updated[path] = b
            continue

        if isinstance(a, dict):
            assert isinstance(b, dict)
            for k in a.keys():
                if k not in b:
                    updated[path.append(k)] = None
            for k in b.keys():
                if k not in a:
                    updated[path.append(k)] = b[k]
                else:
                    stack.append((path.append(k), a[k], b[k]))
            continue

        if isinstance(a, list):
            assert isinstance(b, list)
            for i in range(len(b)):
                if i < len(a):
                    stack.append((path.append(i), a[i], b[i]))
                else:
                    updated[path.append(i)] = b[i]
            for i in range(len(b), len(a)):
                updated[path.append(i)] = None
            continue

        # scalar
        if not deep_eq(a, b):
            updated[path] = b
            continue

    return updated

def deep_iter(obj: JSON) -> Generator[Tuple["JPointer", JSONScalar], None, None]:
    """
    traverse into dict/list until scalar, skip null, yield path and scalar.
    """
    stack = [(JPointer(), obj)]
    while stack:
        path, value = stack.pop()
        if isinstance(value, dict):
            for key in list(value.keys()):
                stack.append((path.append(key), value[key]))
        elif isinstance(value, list):
            for key in range(len(value)):
                stack.append((path.append(key), value[key]))
        elif value is None:
            # skip None
            pass
        else:
            yield path, value


class FieldAccessError(Exception):
    def __init__(self, path: "JPointer", obj: str):
        self.obj = obj
        self.path = path
    
    def __str__(self):
        return f"fail to access {self.path} from {self.obj}"

class InvalidJPointerFormat(Exception):
    def __init__(self, fieldpath: str):
        self.fieldpath = fieldpath

    def __str__(self):
        return f"invalid JSON pointer syntax: {self.fieldpath}"

INDEX_REGEX = re.compile("^(0|[1-9][0-9]*)$")

def urlquote(s: str, unsafe: str = r"#@/:;?") -> str:
    # minimal %-encode
    s = re.sub(r"%(?=[0-9a-fA-F][0-9a-fA-F])", "%25", s)
    return re.sub(
        f"[{re.escape(unsafe)}]",
        lambda m: ''.join(f"%{b:02X}" for b in m.group(0).encode("utf-8")),
        s,
    )

JsonPath = Sequence[Union[int, str]]

@dataclass(frozen=True)
class JPointer:
    """
    JSON pointer, a path for traversing nested map and seq.
    example: /a/b/c indicates the subfield obj["a"]["b"]["c"] of a json object obj.
    "-" for seq is banned.
    see: https://datatracker.ietf.org/doc/html/rfc6901
    """
    elements: Tuple[str, ...] = field(default_factory=lambda: ())

    @staticmethod
    def parse(fieldpath: str) -> "JPointer":
        """
        parse JSON pointer.
        """
        if not fieldpath:
            return JPointer(())
        if not fieldpath.startswith("/"):
            raise InvalidJPointerFormat(fieldpath)
        return JPointer(tuple(e.replace("~1", "/").replace("~0", "~") for e in fieldpath[1:].split("/")))

    @staticmethod
    def from_list(fieldpath: JsonPath) -> "JPointer":
        return JPointer(tuple(str(e) for e in fieldpath))

    def __str__(self) -> str:
        # minimal escape
        return "".join("/" + e.replace("~0", "~00").replace("~1", "~01").replace("/", "~1") for e in self.elements)

    def __repr__(self) -> str:
        return f"JPointer.parse({str(self)!r})"

    def __truediv__(self, key_or_subpath: Union[int, str, "JPointer"]) -> "JPointer":
        if isinstance(key_or_subpath, JPointer):
            return self.extend(key_or_subpath)
        else:
            if isinstance(key_or_subpath, int):
                key_or_subpath = str(key_or_subpath)
            # JPointer.parse("/a/b") / "c/d"  ==  JPointer.parse("/a/b/c/d")
            if key_or_subpath and not key_or_subpath.startswith("/"):
                key_or_subpath = "/" + key_or_subpath
            return self.extend(JPointer.parse(key_or_subpath))
    
    def append(self, key: Union[int, str]) -> "JPointer":
        if isinstance(key, int):
            key = str(key)
        return JPointer(self.elements + (key,))
    
    def extend(self, subpath: "JPointer") -> "JPointer":
        return JPointer(self.elements + subpath.elements)
    
    def __bool__(self) -> bool:
        return bool(self.elements)
    
    def __getitem__(self, index: slice) -> "JPointer":
        assert isinstance(index, slice)
        return JPointer(self.elements[index])

    def is_prefix(self, longer: "JPointer") -> bool:
        return longer.elements[:len(self.elements)] == self.elements

    @staticmethod
    def is_index(key: str) -> bool:
        return bool(INDEX_REGEX.match(key))

    # @raises(FieldAccessError)
    def walk(self, root: JSON) -> JSON:
        """
        traverse into nested map and seq according to this path, return the final value.
        raises FieldAccessError if the path is invalid for the given node.
        """
        node = root
        for i, key in enumerate(self.elements):
            if isinstance(node, dict):
                if key not in node:
                    raise FieldAccessError(self[:i+1], f"{type(root).__name__} object")
                node = node[key]
            elif isinstance(node, list):
                if not self.is_index(key):
                    raise FieldAccessError(self[:i+1], f"{type(root).__name__} object")
                if int(key) not in range(len(node)):
                    raise FieldAccessError(self[:i+1], f"{type(root).__name__} object")
                node = node[int(key)]
            else:
                raise FieldAccessError(self[:i+1], f"{type(root).__name__} object")
        return node

@dataclass(frozen=True)
class PathWithJPointer:
    """
    a json file path with json pointer, represents part of a json object.  
    format: /path/to/file.yaml#/sub/field  
    where "#/sub/field" indicates the subfield of this json object.
    json pointer should be %-encoded to escape '%' and '#'.
    since it is parsed from the right side, if file path contains "#", just suffix with "#".
    note that this is different from standard url.
    """
    filepath: Path = field(default_factory=Path)
    fieldpath: JPointer = field(default_factory=JPointer)
    
    @classmethod
    def parse(cls, file_field_path: str) -> "PathWithJPointer":
        filepath, fieldpath = (*file_field_path.rsplit("#", 1), "")[:2]
        fieldpath = urllib.parse.unquote(fieldpath)
        return cls(Path(filepath), JPointer.parse(fieldpath))

    def with_schema(self, schema: Union[None, str, Path, JSON] = None) -> "TypedPathWithJPointer":
        if schema is None:
            # any type
            schema = {}
        if isinstance(schema, (str, Path)):
            schema = cast(JSON, {"$ref": str(schema)})
        return TypedPathWithJPointer(self.filepath, self.fieldpath, schema, len(self.fieldpath.elements))

    @classmethod
    def create(cls, link: Union[str, Path, "PathWithJPointer"]) -> "PathWithJPointer":
        if isinstance(link, str):
            link = cls.parse(link)
        elif isinstance(link, Path):
            link = cls(link)
        return link

    def __truediv__(self, key: Union[int, str, JPointer]) -> "PathWithJPointer":
        """right concat"""
        return PathWithJPointer(self.filepath, self.fieldpath / key)

    def append(self, key: Union[int, str]) -> "PathWithJPointer":
        return PathWithJPointer(self.filepath, self.fieldpath.append(key))

    def extend(self, subfieldpath: JPointer) -> "PathWithJPointer":
        return PathWithJPointer(self.filepath, self.fieldpath.extend(subfieldpath))

    def resolve(self, base_path: Optional[Path] = None) -> "PathWithJPointer":
        return PathWithJPointer(((base_path or Path()) / self.filepath).resolve(), self.fieldpath)

    def __str__(self) -> str:
        filepath = str(self.filepath)
        fieldpath = urlquote(str(self.fieldpath), "#")
        if fieldpath or "#" in filepath:
            return f"{filepath}#{fieldpath}"
        else:
            return filepath

    def __repr__(self) -> str:
        return f"PathWithJPointer.parse({str(self)!r})"

@dataclass(frozen=True)
class TypedPathWithJPointer(PathWithJPointer):
    """
    a json file path with json pointer, attached with a json schema.  
    schema describes the type of node `fieldpath[:schema_root]`,
    and all types along rest path must match.
    """
    schema: JSON = field(default_factory=lambda: {})
    schema_root: int = 0

    @classmethod
    def create(cls, link: Union[str, Path, "PathWithJPointer", "TypedPathWithJPointer"]) -> "TypedPathWithJPointer":
        if isinstance(link, str):
            link = cls.parse(link)
        elif isinstance(link, Path):
            link = cls(link)
        link = cls(link.filepath, link.fieldpath)
        return link

    def __truediv__(self, key: Union[int, str, JPointer]) -> "TypedPathWithJPointer":
        link = super().__truediv__(key)
        return TypedPathWithJPointer(link.filepath, link.fieldpath, self.schema, self.schema_root)

    def append(self, key: Union[int, str]) -> "TypedPathWithJPointer":
        link = super().append(key)
        return TypedPathWithJPointer(link.filepath, link.fieldpath, self.schema, self.schema_root)

    def extend(self, subfieldpath: JPointer) -> "TypedPathWithJPointer":
        link = super().extend(subfieldpath)
        return TypedPathWithJPointer(link.filepath, link.fieldpath, self.schema, self.schema_root)

    def resolve(self, base_path: Optional[Path] = None) -> "TypedPathWithJPointer":
        link = super().resolve(base_path)
        return TypedPathWithJPointer(link.filepath, link.fieldpath, self.schema, self.schema_root)


class SimpleYAMLLoader(yaml.SafeLoader):
    """
    loader for yaml format as json object: no complex key for maps, no alias.
    """
    def compose_node(self, parent: Optional[yaml.nodes.Node], index: int):
        if self.check_event(yaml.AliasEvent): # pyright: ignore[reportUnknownMemberType]
            raise yaml.YAMLError("Aliases are not allowed")

        event = self.peek_event() # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        if getattr(event, "anchor", None) is not None: # pyright: ignore[reportUnknownArgumentType]
            raise yaml.YAMLError("Anchors are not allowed")

        return super().compose_node(parent, index)

def _dict_constructor(loader: SimpleYAMLLoader, node: yaml.nodes.Node) -> Dict[str, Any]:
    assert isinstance(node, yaml.nodes.MappingNode)
    res = loader.construct_mapping(node, deep=True)
    wrong_key_type = next((type(key).__name__ for key in res.keys() if type(key) != str), None)
    if wrong_key_type is not None:
        raise yaml.constructor.ConstructorError(f"key of map must be str, got: {wrong_key_type}")
    return cast(Dict[str, Any], res)

SimpleYAMLLoader.add_constructor("tag:yaml.org,2002:map", _dict_constructor)

# @raises(FieldAccessError)
def load_YAML(link: PathWithJPointer) -> JSON:
    """
    load yaml format as json object: no complex key for maps, no alias.
    """
    with open(link.filepath, 'r') as f:
        data = cast(JSON, yaml.load(f, Loader=SimpleYAMLLoader))
    try:
        return link.fieldpath.walk(data)
    except FieldAccessError as e:
        e.obj = str(link.filepath)
        raise e


class ExYAMLLoader(SimpleYAMLLoader):
    """
    loader for yaml with !include and !merge, and keep other tags.
    """
    def set_filepath(self, filepath: Path):
        self.filepath = filepath

    def set_raw(self, raw: bool):
        self.raw = raw

def _include_constructor(loader: ExYAMLLoader, node: yaml.nodes.Node) -> TaggedJSON:
    if loader.raw:
        return _unknown_tag_constructor(loader, "include", node)
    if not isinstance(node, yaml.nodes.ScalarNode):
        raise yaml.constructor.ConstructorError(
            None, None,
            f"!include expects a str scalar, got {type(node).__name__}",
            node.start_mark,
        )

    link = loader.construct_scalar(node)
    if not isinstance(link, str): # pyright: ignore[reportUnnecessaryIsInstance]
        raise yaml.constructor.ConstructorError(
            None, None,
            f"!include expects a str scalar, got {type(node).__name__}",
            node.start_mark,
        )

    link = PathWithJPointer.parse(link)

    subfilepath = loader.filepath.parent / link.filepath
    with open(subfilepath, 'r') as f:
        subloader = type(loader)(f)
        subloader.set_filepath(subfilepath)
        try:
            data = cast(TaggedJSON, subloader.get_single_data())
        finally:
            subloader.dispose() # pyright: ignore[reportUnknownMemberType]
    return cast(TaggedJSON, link.fieldpath.walk(cast(JSON, data)))

def _merge_constructor(loader: ExYAMLLoader, node: yaml.nodes.Node) -> TaggedJSON:
    if loader.raw:
        return _unknown_tag_constructor(loader, "merge", node)
    if not isinstance(node, yaml.nodes.SequenceNode):
        raise yaml.constructor.ConstructorError(
            None, None,
            f"!merge expects a sequence, got {type(node).__name__}",
            node.start_mark,
        )

    objs = loader.construct_sequence(node, deep=True)
    if not objs:
        return None
    obj = cast(TaggedJSON, objs[0])
    for obj_ in objs[1:]:
        obj = cast(TaggedJSON, deep_update(cast(JSON, obj), obj_))
    return obj

def _unknown_tag_constructor(loader: ExYAMLLoader, tag_suffix: str, node: yaml.nodes.Node) -> TaggedJSON:
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    elif isinstance(node, yaml.MappingNode):
        value = _dict_constructor(loader, node)
    else:
        assert False

    if isinstance(value, list):
        value = TaggedList(value)
        value.tag = tag_suffix
        return value
    if isinstance(value, dict):
        value = TaggedDict(value)
        value.tag = tag_suffix
        return value
    return TaggedScalar(value, tag_suffix)

ExYAMLLoader.add_constructor("!include", _include_constructor)
ExYAMLLoader.add_constructor("!merge", _merge_constructor)
ExYAMLLoader.add_multi_constructor("!", _unknown_tag_constructor) # pyright: ignore[reportUnknownMemberType]

# @raises(FieldAccessError)
def load_ExYAML(link: PathWithJPointer, raw: bool = False) -> TaggedJSON:
    """
    load yaml with !include and !merge, and keep other tags.
    if raw is true, don't resolve !include and !merge.
    """
    filepath = link.filepath.resolve()
    with open(filepath, 'r') as f:
        loader = ExYAMLLoader(f)
        loader.set_filepath(filepath)
        loader.set_raw(raw)
        try:
            data = cast(JSON, loader.get_single_data())
        finally:
            loader.dispose() # pyright: ignore[reportUnknownMemberType]
    # walk works for TaggedList and TaggedDict
    return cast(TaggedJSON, link.fieldpath.walk(data))


class SimpleYAMLDumper(yaml.SafeDumper):
    """
    dumper for json object as yaml format without alias.
    str scalars always quote.
    block style seqs always indent.
    vector-like seqs/maps use flow style.
    """
    def increase_indent(self, flow: bool = False, indentless: bool = False):
        # for indent block style seqs.
        # prefer
        # ```
        # seq:
        #   - item1
        #   - item2
        # ```
        # instead of
        # ```
        # seq:
        # - item1
        # - item2
        # ```
        return super().increase_indent(flow, False)

    def ignore_aliases(self, data: Any):
        return True

def is_vec_like(data: Union[JSON, TaggedJSON]) -> bool:
    DTYPES: List[Set[type]] = [{bool}, {int}, {float}, {str}]
    if isinstance(data, dict) and all(len(k) == 1 for k in data.keys()) and set(type(v) for v in data.values()) in DTYPES:
        return True
    if isinstance(data, list) and set(type(v) for v in data) in DTYPES:
        return True
    return False

def _list_representer(self: SimpleYAMLDumper, data: List[JSON]):
    return self.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=is_vec_like(data))

def _dict_representer(self: SimpleYAMLDumper, data: Dict[str, JSON]):
    content = [
        (yaml.SafeDumper.represent_str(self, k), self.represent_data(v)) # pyright: ignore[reportUnknownMemberType]
        for k, v in data.items()
    ]
    return yaml.nodes.MappingNode('tag:yaml.org,2002:map', content, flow_style=is_vec_like(data))

def _dstr_representer(self: SimpleYAMLDumper, data: str):
    if "\n" in data:
        return self.represent_scalar('tag:yaml.org,2002:str', data, style='|') # pyright: ignore[reportUnknownMemberType]
    else:
        return self.represent_scalar('tag:yaml.org,2002:str', data, style='"') # pyright: ignore[reportUnknownMemberType]

SimpleYAMLDumper.add_representer(list, _list_representer)
SimpleYAMLDumper.add_representer(dict, _dict_representer)
SimpleYAMLDumper.add_representer(str, _dstr_representer)

def save_YAML(data: JSON, path: Path):
    """
    dump json object as yaml format without alias.
    str scalars always quote.
    block style seqs always indent.
    vector-like seqs/maps use flow style.
    """
    with open(path, 'w') as f:
        yaml.dump(data, f, Dumper=SimpleYAMLDumper, sort_keys=False)


class ExYAMLDumper(SimpleYAMLDumper):
    """
    dumper for tagged json object.
    """
    pass

def _tagged_scalar_representer(self: ExYAMLDumper, data: TaggedScalar):
    node = self.represent_data(data.data) # pyright: ignore[reportUnknownMemberType]
    return self.represent_scalar(f"!{data.tag}", node.value) # pyright: ignore[reportUnknownMemberType]

def _tagged_list_representer(self: ExYAMLDumper, data: TaggedList):
    return self.represent_sequence(f"!{data.tag}", data, flow_style=is_vec_like(data))

def _tagged_dict_representer(self: ExYAMLDumper, data: TaggedDict):
    content = [
        (yaml.SafeDumper.represent_str(self, k), self.represent_data(v)) # pyright: ignore[reportUnknownMemberType]
        for k, v in data.items()
    ]
    return yaml.nodes.MappingNode(f"!{data.tag}", content, flow_style=is_vec_like(data))

ExYAMLDumper.add_representer(TaggedScalar, _tagged_scalar_representer)
ExYAMLDumper.add_representer(TaggedList, _tagged_list_representer)
ExYAMLDumper.add_representer(TaggedDict, _tagged_dict_representer)

def save_ExYAML(data: TaggedJSON, path: Path):
    """
    dump tagged json object, same as save_YAML.
    """
    with open(path, 'w') as f:
        yaml.dump(data, f, Dumper=ExYAMLDumper, sort_keys=False)


def _resolve_yaml(link: str, raw: bool = False):
    """
    load and dumps resolved yaml (!include, !merge are resolved, other tags are kept)
    """

    import warnings
    def formatwarning(message: str, category, filename, lineno, line=None): # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        return "".join(
            f"# {'     ' if i else 'WARN:'} {line}\n"
            for i, line in enumerate(str(message).splitlines())
        )
    warnings.formatwarning = formatwarning

    data = load_ExYAML(PathWithJPointer.parse(link), raw)
    data_str = yaml.dump(data, Dumper=ExYAMLDumper, sort_keys=False)

    sys.stderr.flush()
    print(data_str)

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("python -m monolaunch.yaml_utils [--raw] <yaml file>\n" + cleandoc(_resolve_yaml.__doc__ or ""), file=sys.stderr)
        exit(1)
    raw = False
    if "--raw" in sys.argv:
        raw = True
        sys.argv.remove("--raw")
    _resolve_yaml(sys.argv[1], raw)
