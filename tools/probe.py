"""Read-only probe of the SeeStar's Alpaca devices. Connects but never moves anything."""
import os
import sys
from alpaca.telescope import Telescope
from alpaca.camera import Camera
from alpaca.focuser import Focuser

sys.path.insert(0, os.path.dirname(__file__))
import config
HOST = sys.argv[1] if len(sys.argv) > 1 else config.SEESTAR


def dump(dev, names):
    for n in names:
        try:
            v = getattr(dev, n)
            if callable(v):
                continue
        except Exception as e:  # property not implemented etc.
            v = f"<{type(e).__name__}: {e}>"
        print(f"  {n}: {v}")


t = Telescope(HOST, 0)
c = Camera(HOST, 1)
f = Focuser(HOST, 1)
for d in (t, c, f):
    d.Connected = True

print("Telescope 0")
dump(t, ["Name", "DriverVersion", "InterfaceVersion", "AlignmentMode", "AtPark", "AtHome",
         "Tracking", "Slewing", "Altitude", "Azimuth", "RightAscension", "Declination",
         "SiteLatitude", "SiteLongitude", "SiteElevation", "UTCDate",
         "CanSlewAltAz", "CanSlewAltAzAsync", "CanMoveAxis", "CanPark", "CanUnpark",
         "CanSetTracking", "CanSync", "CanSyncAltAz", "CanFindHome", "SupportedActions"])
for ax in (0, 1):
    try:
        print(f"  CanMoveAxis({ax}): {t.CanMoveAxis(ax)}  AxisRates({ax}): "
              f"{[(r.Minimum, r.Maximum) for r in t.AxisRates(ax)]}")
    except Exception as e:
        print(f"  axis {ax}: <{e}>")

print("Camera 1")
dump(c, ["Name", "DriverVersion", "InterfaceVersion", "CameraState", "CameraXSize", "CameraYSize",
         "PixelSizeX", "PixelSizeY", "BinX", "MaxBinX", "SensorType", "BayerOffsetX", "BayerOffsetY",
         "ExposureMin", "ExposureMax", "Gain", "GainMin", "GainMax", "Offset", "MaxADU",
         "CanAbortExposure", "CanStopExposure", "CCDTemperature", "ImageReady", "SupportedActions"])

print("Focuser 1")
dump(f, ["Name", "Absolute", "Position", "MaxStep", "MaxIncrement", "IsMoving",
         "TempCompAvailable", "Temperature", "StepSize"])
