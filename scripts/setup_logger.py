#!/usr/bin/env python3


import keyword
import os
from pathlib import Path
import re
import sys
from typing import Dict, Literal, Union

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
    return rospy.get_param(str(path)) # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

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

def generate_logger_config(logger_config: LoggerConfig, config_dir: Path):
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

    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "python_logging.conf").write_text(python_logging_conf_content)
    (config_dir / "rosconsole.config").write_text(rosconsole_config_content)

def resolve_logger_config(logger_config: JSON, param_name: str) -> LoggerConfig:
    if not isinstance(logger_config, dict):
        raise TypeError(f"parameter {param_name} expect dict, got {type(logger_config).__name__}")
    for level in logger_config.values():
        if level not in ("DEBUG", "INFO", "WARN", "ERROR", "FATAL"):
            raise TypeError(f"parameter {param_name} expect 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'FATAL', got {level}")
    return logger_config # pyright: ignore[reportReturnType]

def setup_logger(fieldpath: Union[str, FieldPath]):
    if isinstance(fieldpath, str):
        fieldpath = FieldPath.parse(fieldpath)
    ros_logger_config = resolve_logger_config(get_param(fieldpath), str(fieldpath))
    config_dir = ros_home() / f"resources/ros_logger_configs{fieldpath!s}"
    generate_logger_config(ros_logger_config, config_dir)
    
    os.environ["ROSCONSOLE_CONFIG_FILE"] = str(config_dir / "rosconsole.config")
    os.environ["ROS_PYTHON_LOG_CONFIG_FILE"] = str(config_dir / "python_logging.conf")

if __name__ == "__main__":
    setup_logger(sys.argv[1])
