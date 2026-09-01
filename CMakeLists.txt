cmake_minimum_required(VERSION 3.0.2)
project(monolaunch)

find_package(catkin REQUIRED COMPONENTS
  rospy
)

catkin_python_setup()

catkin_package()

install(PROGRAMS
  scripts/param_loader.py
  scripts/launch_converter.py
  scripts/launch_prefix.py
  scripts/exec.sh
  DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)
