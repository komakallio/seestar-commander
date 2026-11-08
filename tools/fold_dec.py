import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scope import Scope, SECONDARY
s = Scope(); t = s.t
d0 = t.Declination; print("start Dec", d0)
best = d0
try:
    t.MoveAxis(SECONDARY, -1.0)
    t0 = time.time()
    while time.time() - t0 < 15:
        time.sleep(0.4)
        d = t.Declination
        if d > best + 0.05 or d <= -89.8:
            break
        best = min(best, d)
finally:
    t.MoveAxis(SECONDARY, 0.0)
time.sleep(1)
print("end Dec", t.Declination, "alt", t.Altitude, "unwrapped HA", round(s.ha_unwrapped(), 2))
