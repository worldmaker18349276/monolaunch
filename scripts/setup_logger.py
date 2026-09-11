#!/usr/bin/env python3
"""
launch-prefix of a node to setup logger for it.
it will generate logger config from parameter ~$ros_logger_config for roscpp and rospy,
and setup environmental variable.

usage: `rosrun --prefix {this script} {your_pkg} {your_node}`
or in launch file:
```
<node ... launch-prefix="rosrun monolaunch setup_logger.py">
```
"""

import keyword
import os
from pathlib import Path
import re
import sys
from typing import Dict, Literal, Optional, Tuple

from monolaunch.yaml_utils import JSON, FieldPath


def sanitize_identifier(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if not name.isidentifier() or keyword.iskeyword(name):
        name = "_" + name
    return name

def ros_home():
    import rospkg # pyright: ignore[reportMissingImports]
    return Path(rospkg.get_ros_home()) # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

def get_param(path: FieldPath) -> JSON:
    import rospy # pyright: ignore[reportMissingImports]
    return rospy.get_param(str(path)) # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType, reportReturnType]

LoggerConfig = Dict[str, Literal["DEBUG", "INFO", "WARN", "ERROR", "FATAL"]]

_ROS_TO_PYTHON_LOGGING_LEVEL = {
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "WARN": "WARNING",
    "ERROR": "ERROR",
    "FATAL": "CRITICAL",
}

_PYTHON_LOGGING_CONF_LOGGER_TEMPLATE = """
[logger_{id}]
level={level}
handlers=
propagate=1
qualname={name}
"""

_PYTHON_LOGGING_CONF_TEMPLATE = """
[loggers]
keys=root, rosout{logger_ids}

[handlers]
keys=fileHandler,streamHandler

[formatters]
keys=defaultFormatter

[logger_root]
level=INFO
handlers=fileHandler

[logger_rosout]
level=INFO
handlers=streamHandler
propagate=1
qualname=rosout

{logger_settings}

[handler_fileHandler]
class=handlers.RotatingFileHandler
level=DEBUG
formatter=defaultFormatter
# log filename, mode, maxBytes, backupCount
args=(os.environ['ROS_LOG_FILENAME'],'a', 50000000, 4)

[handler_streamHandler]
class=rosgraph.roslogging.RosStreamHandler
level=DEBUG
formatter=defaultFormatter
# colorize output flag
args=(True,)

[formatter_defaultFormatter]
format=[%(name)s][%(levelname)s] %(asctime)s: %(message)s
"""

def generate_logger_config(logger_config: LoggerConfig) -> Tuple[str, str]:
    """
    generate simple logger config files for roscpp and rospy.
    """
    rosconsole_config_content = "\n".join(
        f"log4j.logger.{name}={level}"
        for name, level in logger_config.items()
    )
    python_logging_conf_content = _PYTHON_LOGGING_CONF_TEMPLATE.format(
        logger_ids="".join(", " + sanitize_identifier(name) for name in logger_config.keys()),
        logger_settings="\n".join(
            _PYTHON_LOGGING_CONF_LOGGER_TEMPLATE.format(
                id=sanitize_identifier(name),
                name=name,
                level=_ROS_TO_PYTHON_LOGGING_LEVEL[level],
            )
            for name, level in logger_config.items()
        ),
    )
    return python_logging_conf_content, rosconsole_config_content

def get_logger_config(fieldpath: FieldPath) -> LoggerConfig:
    try:
        logger_config = get_param(fieldpath)
    except KeyError:
        logger_config = {}
    if not isinstance(logger_config, dict):
        raise TypeError(f"[setup_logger] parameter {fieldpath} expect dict, got {type(logger_config).__name__}")
    for key, level in logger_config.items():
        if level not in ("DEBUG", "INFO", "WARN", "ERROR", "FATAL"):
            raise TypeError(f"[setup_logger] parameter {fieldpath}/{key} expect 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'FATAL', got {level}")
    return logger_config # pyright: ignore[reportReturnType]

ROS_LOGGER_CONFIG_FIELDNAME = "$ros_logger_config"

def determine_ros_logger_config_fieldpath() -> Optional[FieldPath]:
    ros_namespace = os.environ.get("ROS_NAMESPACE")
    name = next((arg[len("__name:="):] for arg in sys.argv[::-1] if arg.startswith("__name:=")), None)
    ns = next((arg[len("__ns:="):] for arg in sys.argv[::-1] if arg.startswith("__ns:=")), None)
    ns = ns or ros_namespace
    if name is None or ns is None:
        return None
    return FieldPath.parse(ns).append(name).append(ROS_LOGGER_CONFIG_FIELDNAME)

def setup_logger(fieldpath: FieldPath):
    ros_logger_config = get_logger_config(fieldpath)
    if ros_logger_config:
        python_logging_conf_content, rosconsole_config_content = generate_logger_config(ros_logger_config)
        config_dir = ros_home() / f"resources/ros_logger_configs{fieldpath!s}"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "python_logging.conf").write_text(python_logging_conf_content)
        (config_dir / "rosconsole.config").write_text(rosconsole_config_content)
        os.environ["ROS_PYTHON_LOG_CONFIG_FILE"] = str(config_dir / "python_logging.conf")
        os.environ["ROSCONSOLE_CONFIG_FILE"] = str(config_dir / "rosconsole.config")

def main():
    fieldpath = determine_ros_logger_config_fieldpath()
    if fieldpath is None:
        raise RuntimeError("[setup_logger] fail to setup logger: cannot determine private namespace of this node")
    print(f"[setup_logger] generate ros logger config from parameter {fieldpath}")
    setup_logger(fieldpath)
    sys.argv = sys.argv[1:]
    os.execv(sys.argv[0], sys.argv)

if __name__ == "__main__":
    main()
