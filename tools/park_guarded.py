import sys, os, time, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scope import Scope, PRIMARY, SECONDARY, camsnap
import safety
s = Scope(); t = s.t
u0 = s.ha_unwrapped(); p = s.position()
print(f"before: unwrapped HA {u0:.2f} Dec {p['dec']:.2f} AtPark {t.AtPark}", flush=True)
camsnap(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'poc', 'cam_park_before.jpg'))
err = []
def do_park():
    try:
        t.Park()
    except Exception as e:
        err.append(e)
th = threading.Thread(target=do_park, daemon=True); th.start()
t0 = time.time(); aborted = False; maxdev = 0
while time.time() - t0 < 120:
    time.sleep(0.3)
    u = s.ha_unwrapped(); dec = t.Declination
    maxdev = max(maxdev, abs(u - u0))
    if abs(u - u0) > 10:
        for f in (lambda: t.AbortSlew(), lambda: t.MoveAxis(PRIMARY, 0), lambda: t.MoveAxis(SECONDARY, 0)):
            try: f()
            except Exception as e: print("stop:", e)
        aborted = True; print(f"ABORTED: RA moved {u - u0:+.1f} deg", flush=True); break
    if not th.is_alive() and not t.Slewing:
        break
time.sleep(1)
p = s.position()
print(f"after: unwrapped HA {s.ha_unwrapped():.2f} Dec {p['dec']:.2f} alt {p['alt']:.1f} AtPark {t.AtPark} "
      f"max RA deviation {maxdev:.2f} aborted {aborted} err {err}", flush=True)
camsnap(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'poc', 'cam_park_after.jpg'))
if t.AtPark and not aborted:
    print("park hook:", safety.parked(), flush=True)
