"""Grab one frame from the IP camera watching the scope (config [ipcam] rtsp_url, or an RTSP URL argument).

usage: camsnap.py [rtsp_url] [out.jpg]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from scope import camsnap

url = sys.argv[1] if len(sys.argv) > 1 else None
out = sys.argv[2] if len(sys.argv) > 2 else f"data/cam/cam_{time.strftime('%H%M%S', time.gmtime())}Z.jpg"
os.makedirs(os.path.dirname(out), exist_ok=True)
print(camsnap(out, url) or "no camera configured")
