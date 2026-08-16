#!/usr/bin/env python3
"""
Convert a ROS1 .launch XML file into a monolaunch python script.

Usage:
    python3 launch_converter.py input.launch [output.py]

If output.py is omitted, output is written next to the input file with
a .py extension, and the generated function has the same name as the
input file.
"""

import sys
import re
from typing import Dict, List, Optional, Sequence, Tuple, Union
from pathlib import Path
import xml.etree.ElementTree as ET
import warnings


def as_bool(s: Union[str, bool]) -> bool:
    if isinstance(s, bool): return s
    if s in ("true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"):
        return True
    elif s in ("false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"):
        return False
    else:
        raise ValueError(f"{s} is not valid bool literal")

class ConvertWarning(Warning):
    pass

def warn(msg: str):
    warnings.warn(ConvertWarning(msg))


def sanitize_identifier(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[^0-9a-zA-Z_]", "_", name)
    if not name:
        name = "_"
    if re.match(r"^[0-9]", name):
        name = "_" + name
    return name

def indent_lines(lines: List[str], level: int) -> List[str]:
    pad = "    " * level
    out: List[str] = []
    for ln in lines:
        if ln == "":
            out.append("")
        else:
            out.append(pad + ln)
    return out

def strip_tag(tag: str) -> str:
    # in case of namespaces (unlikely in ros launch files)
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag

def element_to_comment_lines(elem: Optional[ET.Element]) -> List[str]:
    """Turn a element into '# ...' lines,
    one per non-blank source line. Returns [] for None/whitespace-only text."""
    if elem is None:
        return []
    lines: List[str] = []
    text = ET.tostring(elem, 'utf-8').decode('utf-8')
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped:
            lines.append(f"# {stripped}")
    return lines

def text_to_comment_lines(text: Optional[str]) -> List[str]:
    """Turn a blob of plain XML text (or comment body) into '# ...' lines,
    one per non-blank source line. Returns [] for None/whitespace-only text."""
    if not text or not text.strip():
        return []
    return list(text.splitlines())

def check_unknown_attrs(elem: ET.Element, known: List[str]) -> List[str]:
    """Return '# ...' comment line (and emit warnings) for any attribute on
    elem that isn't in the known set of attribute names for that tag."""
    tag_name = strip_tag(elem.tag)
    attrs: List[str] = []
    for k, v in elem.attrib.items():
        if k not in known:
            warn(f"unknown attribute {k!r} on <{tag_name}>; skipped")
            attrs.append(f"{k!s}={v!r}")
    if attrs:
        return [f"# <{tag_name} ... " + " ".join(attrs) + "/>"]
    else:
        return []

def find_dollar_parens(s: str) -> List["slice[int, int, None]"]:
    """Find all top-level $(...) groups with balanced parens.
    Returns list of slices where s[slice] == '$(...)'
    """
    results: List["slice[int, int, None]"] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i:i+2] == "$(":
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                if s[j] == "(":
                    depth += 1
                elif s[j] == ")":
                    depth -= 1
                j += 1
            results.append(slice(i, j, None))
            i = j
        else:
            i += 1
    return results

_ARG_CALL_RE = re.compile(r"""arg\s*\(\s*(['"])([^'"]*)\1\s*\)""")

def convert_subst_expr(inner: str) -> str:
    inner = inner.strip()
    if not inner:
        warn("empty substitution '$()' encountered")
        return "''"
    cmd, rest = (inner.split(None, 1) + [""])[:2]

    if cmd == "env":
        return f"env({rest!r})"
    elif cmd == "optenv":
        sub = rest.split(None, 1)
        var = sub[0] if sub else ""
        fallback = sub[1] if len(sub) > 1 else ""
        return f"env({var!r}, {fallback!r})"
    elif cmd == "arg":
        return sanitize_identifier(rest)
    elif cmd == "find":
        return f"find({rest!r})"
    elif cmd == "anon":
        return f"anon({rest!r})"
    elif cmd == "eval":
        return _ARG_CALL_RE.sub(lambda m: sanitize_identifier(m.group(2)), rest.strip())
    else:
        warn(f"unknown substitution '$({inner})', leaving as literal string")
        return repr(f"$({inner})")

def convert_value_expr(value: str) -> str:
    """Convert a raw XML attribute-value string into a python source
    expression string. Handles $(eval ...) as full value, a value that is
    entirely one substitution, a value that mixes literal text with
    substitutions (-> f-string), and plain literal text (-> str literal).
    """
    parts = find_dollar_parens(value)
    if not parts:
        return repr(value)
    elif parts == [slice(0, len(value), None)]:
        return convert_subst_expr(value[2:-1])

    exprs = [convert_subst_expr(value[s.start+2:s.stop-1]) for s in parts]

    body = ""
    last = 0
    for i, s in enumerate(parts):
        body += value[last:s.start].replace("{", "{{").replace("}", "}}")
        body += "{" + str(i) + "}"
        last = s.stop
    body += value[last:].replace("{", "{{").replace("}", "}}")

    return repr(body) + ".format(%s)" % ", ".join(exprs)

def convert_value_expr_as_type(value: str, type_attr: str) -> str:
    if type_attr == "int":
        try:
            return str(int(value.strip()))
        except ValueError:
            pass
        return f"int({convert_value_expr(value)})"

    if type_attr in ("double", "float"):
        try:
            return repr(float(value.strip()))
        except ValueError:
            pass
        return f"float({convert_value_expr(value)})"

    if type_attr == "bool":
        try:
            return repr(as_bool(value.strip()))
        except ValueError:
            pass
        return f"as_bool({convert_value_expr(value)})"

    if type_attr in ("str", "string"):
        return convert_value_expr(value)

    if type_attr in ("auto", "yaml"):
        warn(f"param type {type_attr!r} treated as plain string value: {value!r}")
        return convert_value_expr(value)

    warn(f"unknown param type {type_attr!r}, treating as string")
    return convert_value_expr(value)


def param_key(key: str, inside_node: bool) -> str:
    if inside_node and not key.startswith("~"):
        key = "~" + key
    return key

def convert_param_elem(elem: ET.Element, inside_node: bool, known: Sequence[str]) -> Tuple[List[Tuple[str, str]], List[str]]:
    known = list(known)

    known.append("name")
    name = elem.get("name")
    if name is None:
        warn("<param> without 'name' attribute skipped")
        return [], element_to_comment_lines(elem)

    known.append("value")
    value = elem.get("value")
    if value is None:
        # could be textfile/binfile/command style param, or nested content
        warn(f"<param name={name!r}> has no 'value' attribute"
             " (textfile/binfile/command or nested content not supported); skipped")
        return [], element_to_comment_lines(elem)

    known.append("type")
    val_type = (elem.get("type") or "auto").strip().lower()
    val_code = convert_value_expr_as_type(value, val_type)
    key = param_key(name, inside_node)
    return [(key, val_code)], check_unknown_attrs(elem, known)

def convert_remap_elem(elem: ET.Element, inside_node: bool, known: Sequence[str]) -> Tuple[List[Tuple[str, str]], List[str]]:
    known = list(known)

    known.append("from")
    known.append("to")
    frm = elem.get("from")
    to = elem.get("to")
    if frm is None or to is None:
        warn("<remap> missing 'from' or 'to' attribute; skipped")
        return [], element_to_comment_lines(elem)

    return [(frm, convert_value_expr(to))], check_unknown_attrs(elem, known)

def convert_env_elem(elem: ET.Element, inside_node: bool, known: Sequence[str]) -> Tuple[List[Tuple[str, str]], List[str]]:
    known = list(known)

    known.append("name")
    known.append("value")
    name = elem.get("name")
    value = elem.get("value")
    if name is None or value is None:
        warn("<env> missing 'name' or 'value' attribute; skipped")
        return [], element_to_comment_lines(elem)

    return [(name, convert_value_expr(value))], check_unknown_attrs(elem, known)

def convert_rosparam_elem(elem: ET.Element, inside_node: bool, known: Sequence[str]) -> Tuple[List[Tuple[str, str]], List[str]]:
    known = list(known)

    known.append("command")
    command = elem.get("command", "load")
    if command != "load":
        warn(f"<rosparam command={command!r}> not supported (only 'load' is); skipped")
        return [], element_to_comment_lines(elem)

    known.append("subst_value")
    if elem.get("subst_value"):
        warn("<rosparam subst_value=...> not supported; skipped")
        return [], element_to_comment_lines(elem)

    known.append("file")
    file_attr = elem.get("file")
    if file_attr is None:
        warn('<rosparam command="load"> without \'file\' attribute not supported '
             "(inline yaml content not supported); skipped")
        return [], element_to_comment_lines(elem)

    known.append("ns")
    key = param_key(elem.get("ns", ""), inside_node)
    path_expr = f"dirname() / ({convert_value_expr(file_attr)})"
    return [(key, path_expr)], check_unknown_attrs(elem, known)


def wrap_condition(lines: List[str], elem: ET.Element) -> List[str]:
    """Wrap `lines` (already-final, unindented-at-0 list of source lines)
    in an `if` block according to the element's if/unless attributes.
    Returns a new list of lines (still unindented at 0, i.e. caller must
    indent further as needed)."""
    if_attr = elem.get("if")
    unless_attr = elem.get("unless")
    if if_attr is not None and unless_attr is not None:
        warn("element has both 'if' and 'unless'; using 'if' only")
        unless_attr = None

    if if_attr is not None:
        cond = convert_value_expr_as_type(if_attr, "bool")
        header = f"if {cond}:"
        return [header] + indent_lines(lines or ["pass"], 1)
    if unless_attr is not None:
        cond = convert_value_expr_as_type(unless_attr, "bool")
        header = f"if not ({cond}):"
        return [header] + indent_lines(lines or ["pass"], 1)
    return lines

def wrap_ns(lines: List[str], elem: ET.Element) -> List[str]:
    ns_attr = elem.get("ns")
    if ns_attr is not None:
        ns = convert_value_expr(ns_attr)
        header = f"with group(ns={ns}):"
        return [header] + indent_lines(lines or ["pass"], 1)
    return lines


_INCLUDE_ATTR_TYPES = {
    "if": "skip",
    "unless": "skip",
    "ns": "skip",
    "file": "str",
}
_GROUP_ATTR_TYPES = {
    "if": "skip",
    "unless": "skip",
    "ns": "str",
}
_MACHINE_ATTR_TYPES = {
    "default": "skip",
    "name": "str",
    "address": "str",
    "env_loader": "str",
    "user": "str",
    "password": "str",
    "default": "skip",
}
_NODE_ATTR_TYPES = {
    "if": "skip",
    "unless": "skip",
    "ns": "skip",
    "machine": "skip",
    "args": "list",
    "launch_prefix": "list",
    "respawn": "bool",
    "clear_params": "bool",
    "required": "bool",
    "respawn_delay": "float",
    "name": "str",
    "pkg": "str",
    "type": "str",
    "output": "str",
    "cwd": "str",
}

def kwargs_from_attrs(elem: ET.Element, types: Dict[str, str]) -> Tuple[List[Tuple[str, str]], List[str]]:
    known: List[str] = []
    kwargs: List[Tuple[str, str]] = []
    for k in elem.attrib:
        kw = sanitize_identifier(k)
        if kw not in types:
            continue
        known.append(k)
        type_ = types[kw]
        if type_ == "skip":
            continue
        raw = elem.get(k) or "None"
        if type_ == "list":
            v = convert_value_expr(raw) + ".split()"
        elif type_ == "str":
            v = convert_value_expr(raw)
        else:
            v = convert_value_expr_as_type(raw, type_)
        kwargs.append((kw, v))
    return kwargs, check_unknown_attrs(elem, known)


def get_machine_var_name(machine_attr: str) -> str:
    var_name = None
    parts = find_dollar_parens(machine_attr)
    if not parts:
        var_name = "var_" + sanitize_identifier(machine_attr)
    if parts == [slice(0, len(machine_attr), None)]:
        sub_parts = machine_attr[2:-1].strip().split(None, 1)
        if sub_parts and sub_parts[0] == "arg" and len(sub_parts) > 1:
            var_name = "var_" + sanitize_identifier(sub_parts[1])

    if var_name is None:
        warn(f"attribute {machine_attr!r} is not a plain $(arg ...) or plain string; forcely sanitize it as identifier")
        var_name = "var_" + sanitize_identifier(machine_attr)
    return var_name

def convert_machine_elem(elem: ET.Element) -> List[str]:
    default = elem.get("default")
    if default is not None and default.strip().lower() not in ("false", "0"):
        warn(
            f"<machine default={default!r}> is not supported"
            ' (only default="false" or omitted is supported)'
        )
        return element_to_comment_lines(elem)

    name_attr = elem.get("name")
    if name_attr is not None:
        var_name = get_machine_var_name(name_attr)
    else:
        warn("<machine> without 'name' attribute; use _ as variable name")
        var_name = "_"

    kwargs, unknown = kwargs_from_attrs(elem, _MACHINE_ATTR_TYPES)
    lines = unknown
    lines.append(f"{var_name} = machine(")
    for kw, v in kwargs:
        lines.append(f"    {kw}={v},")
    lines.append(")")
    return lines

def convert_node_elem(elem: ET.Element) -> List[str]:
    kwargs, unknown = kwargs_from_attrs(elem, _NODE_ATTR_TYPES)
    body = convert_children(list(elem), inside_node=True, leading_text=elem.text)

    header = "with node(%s):" % ", ".join(f"{kw}={v}" for kw, v in kwargs)
    lines = unknown + [header] + indent_lines(body or ["pass"], 1)

    machine_attr = elem.get("machine")
    if machine_attr is not None:
        var_name = get_machine_var_name(machine_attr)
        lines = [f"with {var_name}:"] + indent_lines(lines or ["pass"], 1)

    return lines

def convert_include_elem(elem: ET.Element) -> List[str]:
    if elem.get("file") is None:
        warn("<include> without 'file' attribute skipped")
        return element_to_comment_lines(elem)
    kwargs, unknown = kwargs_from_attrs(elem, _INCLUDE_ATTR_TYPES)

    rest_children: List[ET.Element] = []
    unknown_children: List[ET.Element] = []
    for child in elem:
        if child.tag is ET.Comment:
            rest_children.append(child)
            continue
        if strip_tag(child.tag) != "arg":
            rest_children.append(child)
            continue
        name = child.get("name")
        value = child.get("value")
        if name is None or value is None:
            warn("<include><arg> missing 'name' or 'value'; skipped")
            unknown_children.append(child)
            continue
        v = convert_value_expr(value)
        kwargs.append((name, v))
    body = convert_children(rest_children, True, leading_text=elem.text)
    for unknown_child in unknown_children:
        body.extend(element_to_comment_lines(unknown_child))

    lines = unknown
    lines.append("with include(")
    for kw, v in kwargs:
        if sanitize_identifier(kw) != kw:
            lines.append(f"    **{{{kw!r}: {v}}},")
        else:
            lines.append(f"    {kw}={v},")
    lines.append("):")
    lines += indent_lines(body or ["pass"], 1)
    return lines

def convert_group_elem(elem: ET.Element, inside_node: bool) -> List[str]:
    kwargs, unknown = kwargs_from_attrs(elem, _GROUP_ATTR_TYPES)
    body = convert_children(list(elem), inside_node=inside_node, leading_text=elem.text)
    if not kwargs:
        lines = unknown + body
    else:
        header = "with group(%s):" % ", ".join(f"{kw}={v}" for kw, v in kwargs)
        lines = unknown + [header] + indent_lines(body or ["pass"], 1)
    return lines

def convert_arg_elem_as_statement(elem: ET.Element) -> List[str]:
    """Used for <arg> tags that fall outside the leading top-level run."""
    known: List[str] = []
    
    known.append("name")
    name = elem.get("name")
    if name is None:
        warn("<arg> without 'name' attribute skipped")
        return element_to_comment_lines(elem)
    ident = sanitize_identifier(name)

    known.append("default")
    known.append("value")
    default = elem.get("default")
    value = elem.get("value")
    if value is not None:
        code = convert_value_expr(value)
    elif default is not None:
        code = convert_value_expr(default)
    else:
        warn(f"<arg name={name!r}> has neither 'default' nor 'value'"
             " and appears outside the leading arg block; emitting as None")
        code = "None"
    return check_unknown_attrs(elem, known) + [f"{ident} = {code}"]

def convert_children(children: List[ET.Element], inside_node: bool, leading_text: Optional[str] = None) -> List[str]:
    lines: List[str] = []
    lines.extend(text_to_comment_lines(leading_text))

    mapping_bufs: Dict[str, Dict[str, str]] = {
        "set_param": {},
        "load_param": {},
        "remap": {},
        "set_env": {},
    }

    def flush_all_except(func_name: str = "") -> List[str]:
        nonlocal mapping_bufs
        for func_name_, buf in mapping_bufs.items():
            if func_name_ != func_name and buf:
                # there is only one buf is nonempty
                res: List[str] = []
                res.append(func_name_ + "({")
                for key, val_code in buf.items():
                    res.append(f"    {key!r}: {val_code!s},")
                res.append("})")
                buf.clear()
                return res
        else:
            return []

    def set_mapping(func_name: str, key: str, value_code: str) -> List[str]:
        nonlocal mapping_bufs
        if key in mapping_bufs[func_name]:
            # duplicated key, flush first
            res = flush_all_except()
            mapping_bufs[func_name][key] = value_code
            return res

        else:
            # flush others
            res = flush_all_except(func_name)
            mapping_bufs[func_name][key] = value_code
            return res

    mapping_funcs = {
        "param": ("set_param", convert_param_elem),
        "rosparam": ("load_param", convert_rosparam_elem),
        "remap": ("remap", convert_remap_elem),
        "env": ("set_env", convert_env_elem),
    }

    for child in children:
        if child.tag is ET.Comment:
            lines.extend(flush_all_except())
            lines.extend(element_to_comment_lines(child))
            lines.extend(text_to_comment_lines(child.tail))
            continue

        tag = strip_tag(child.tag)
        if tag in mapping_funcs:
            func_name, convert = mapping_funcs[tag]

            known = ["if", "unless"]
            if child.get("if") is not None or child.get("unless") is not None:
                lines.extend(flush_all_except())
                pairs, extra_comments = convert(child, inside_node, known)
                lines.extend(extra_comments)
                for key, value_code in pairs:
                    lines.extend(set_mapping(func_name, key, value_code))
                lines.extend(wrap_condition(flush_all_except(), child))
                lines.extend(text_to_comment_lines(child.tail))
                continue
            else:
                pairs, extra_comments = convert(child, inside_node, known)
                lines.extend(extra_comments)
                for key, value_code in pairs:
                    lines.extend(set_mapping(func_name, key, value_code))
                lines.extend(text_to_comment_lines(child.tail))
                continue

        # any other tag: flush buffers first, then handle
        lines.extend(flush_all_except())

        if tag == "machine":
            lines.extend(convert_machine_elem(child))
            lines.extend(text_to_comment_lines(child.tail))
            continue

        if tag == "node":
            lines.extend(wrap_condition(wrap_ns(convert_node_elem(child), child), child))
            lines.extend(text_to_comment_lines(child.tail))
            continue

        if tag == "include":
            lines.extend(wrap_condition(wrap_ns(convert_include_elem(child), child), child))
            lines.extend(text_to_comment_lines(child.tail))
            continue

        if tag == "group":
            lines.extend(wrap_condition(convert_group_elem(child, inside_node), child))
            lines.extend(text_to_comment_lines(child.tail))
            continue

        if tag == "arg":
            lines.extend(wrap_condition(convert_arg_elem_as_statement(child), child))
            lines.extend(text_to_comment_lines(child.tail))
            continue

        warn(f"unsupported tag <{tag}>; skipped")
        lines.extend(element_to_comment_lines(child))
        lines.extend(text_to_comment_lines(child.tail))

    lines.extend(flush_all_except())
    return lines


def collect_top_args(root: ET.Element) -> Tuple[List[Tuple[str, Optional[str]]], int]:
    """Return (params, body_start_index).
    params is a list of tuple: (name, default_code(or None if is required))
    """
    children = list(root)
    params: List[Tuple[str, Optional[str]]] = []
    idx = len(children)

    for i, child in enumerate(children):
        if child.tag is ET.Comment:
            idx = i
            break
        if strip_tag(child.tag) != "arg":
            idx = i
            break
        if child.get("if") is not None or child.get("unless") is not None:
            idx = i
            break
        name = child.get("name")
        if name is None:
            warn("<arg> without 'name' attribute; stopping top-level arg block")
            idx = i
            break

        value = child.get("value")
        if value is not None:
            idx = i
            break

        default = child.get("default")
        if default is None:
            params.append((name, None))
            continue

        if bool(re.match(r"^\$\(\s*(eval|arg)\b", default.strip())):
            idx = i
            break

        default_code = convert_value_expr(default)
        params.append((name, default_code))
    else:
        idx = len(children)

    return params, idx

def build_signature(params: List[Tuple[str, Optional[str]]]):
    required = [p for p in params if p[1] is None]
    defaulted = [p for p in params if p[1] is not None]

    parts: List[str] = []
    for p in required:
        parts.append(sanitize_identifier(p[0]))
    for p in defaulted:
        parts.append(f"{sanitize_identifier(p[0])}={p[1]}")
    return parts

def convert_launch_file(input_path: Union[str, Path]):
    input_path = Path(input_path)
    func_name = sanitize_identifier(input_path.stem)

    tree = ET.parse(str(input_path), parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    root = tree.getroot()
    if strip_tag(root.tag) != "launch":
        warn(f"root element is <{strip_tag(root.tag)}>, expected <launch>")

    params, idx = collect_top_args(root)
    sig_parts = build_signature(params)

    children = list(root)
    body_children = children[idx:]
    if idx == 0:
        leading_text = root.text
    else:
        leading_text = children[idx - 1].tail
    body_lines = convert_children(body_children, inside_node=False, leading_text=leading_text)
    if not body_lines:
        body_lines = ["pass"]

    out: List[str] = []
    out.append("from pathlib import Path")
    out.append("from monolaunch.monolaunch import *")
    out.append("")
    out.append("")
    if sig_parts:
        out.append(f"def {func_name}(")
        out.append("    *,")
        for sig_part in sig_parts:
            out.append(f"    {sig_part},")
        out.append("):")
    else:
        out.append(f"def {func_name}():")
    out.extend(indent_lines(body_lines, 1))
    out.append("")

    return "\n".join(out)

def main():
    if len(sys.argv) < 2:
        sys.stderr.write(__doc__ or "")
        sys.exit(1)

    input_path = sys.argv[1]
    if len(sys.argv) >= 3:
        output_path = sys.argv[2]
    else:
        output_path = str(Path(input_path).parent / (Path(input_path).stem + ".py"))

    source = convert_launch_file(input_path)

    with open(output_path, "w") as f:
        f.write(source)

    sys.stderr.write("wrote %s\n" % output_path)


if __name__ == "__main__":
    main()
