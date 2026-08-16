#!/usr/bin/env python3

from pathlib import Path
import warnings
from monolaunch.monoparam import *
import rospy
from std_msgs.msg import String

def main(source: Path, update_rate: float):
    def formatwarning(message, category, filename, lineno, line=None): # type: ignore
        return f"\033[33m[{category.__name__}] {message}\n\033[m" # type: ignore

    warnings.formatwarning = formatwarning

    pub = rospy.Publisher("param_update", String, queue_size=10)

    source = source.resolve()
    source_resolved = source.parent / f"{source.stem}.resolved.yaml"
    sync = YAMLSynchronizer(source, source_resolved)
    sync.init()

    def update(diff: Dict[FieldPath, Optional[JSON]]):
        nonlocal pub
        for path, value in diff.items():
            if value is None:
                if rospy.has_param(str(path)):
                    rospy.delete_param(str(path))
            else:
                rospy.set_param(str(path), value)
        pub.publish("\n".join(str(path) for path in diff.keys()))
    sync.add_resolved_listener(update)
    
    rate = rospy.Rate(update_rate)
    while not rospy.is_shutdown():
        sync.spin_once()
        rate.sleep()

if __name__ == "__main__":
    rospy.init_node("param_loader")
    param_source = Path(rospy.get_param("/$monoparam_source", ""))
    update_rate = 10.0
    main(param_source, update_rate)
