#!/bin/sh
# Inside the sandbox: a virtual screen, a desktop on it, a live view for the user, then the
# connection program that lets the bots drive it. The view (noVNC) only listens inside the
# container; sandbox.py publishes it on the Mac's 127.0.0.1.
set -eu

export DISPLAY=:1
Xvfb :1 -screen 0 1280x800x24 -nolisten tcp &

tries=0
until xdotool getdisplaygeometry >/dev/null 2>&1; do
  tries=$((tries + 1))
  if [ "$tries" -gt 100 ]; then
    echo "虚拟屏幕没有启动。" >&2
    exit 1
  fi
  sleep 0.1
done

dbus-launch --exit-with-session startxfce4 >/tmp/xfce.log 2>&1 &
x11vnc -display :1 -forever -shared -nopw -localhost -rfbport 5900 -quiet >/tmp/x11vnc.log 2>&1 &
websockify --web /usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &

# Give the desktop a moment, then open the one browser everyone (people and Playwright) uses.
sleep 3
cubes-browser about:blank &
exec /opt/agent/bin/python /opt/agent/cubes_computer.py
