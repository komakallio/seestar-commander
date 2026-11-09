# Telephoto plate solving with ASTAP

The sequencer solves telephoto frames (centring and polar alignment) with the [ASTAP](https://www.hnsky.org/astap.htm) command-line solver and its **D05** star database.
- ASTAP is a separate program (MPL-2.0). The service runs it as a subprocess and nothing is linked or bundled.
- One solve takes about **0.15–0.4 s** on a desktop PC, including writing the frame, ASTAP's own star detection and the solve. It also solves blind (without a position hint) in about 0.2–0.4 s.
- Background and measurements: [fast-solving.md](fast-solving.md) on the `fast-solving-research` branch (PR #7).

## Install (Linux x86_64)

The CLI is a single static binary; the D05 database is 1476 small files (about 107 MB). This puts both in `/opt/astap`, which is also ASTAP's own default database location:

```bash
cd /tmp
curl -L -o astap_cli.zip \
  https://sourceforge.net/projects/astap-program/files/linux_installer/astap_command-line_version_Linux_amd64.zip/download
curl -L -o d05.zip \
  https://sourceforge.net/projects/astap-program/files/star_databases/d05_star_database.zip/download
sudo mkdir -p /opt/astap
sudo python3 -m zipfile -e astap_cli.zip /opt/astap
sudo python3 -m zipfile -e d05.zip /opt/astap
sudo chmod 755 /opt/astap/astap_cli
/opt/astap/astap_cli -h | head -1          # ASTAP astrometric solver version CLI-...
ls /opt/astap/d05_*.1476 | wc -l           # 1476
rm astap_cli.zip d05.zip
```

- Without root, use any directory instead (for example `~/astap`) and point `astap_path` and `db_dir` at it.
- **Raspberry Pi:** the same, with `astap_command-line_version_Linux_aarch64.zip` (64-bit OS) or `..._armhf.zip` (32-bit).
- The other databases (D20, D50, D80) are denser. They gave the same results as D05 for the 2.2°×4° telephoto field, so D05 is enough.

## Configure

In `config.toml`:

```toml
[solver]
kind = "astap"
astap_path = "/opt/astap/astap_cli"   # or a name on PATH
db_dir = "/opt/astap"                 # the directory with the d05_*.1476 files
database = "d05"
radius_deg = 6                        # search radius around the position hint
downsample = 2                        # ASTAP -z
timeout_s = 30
```

Restart the service. Its start-up log line and `python -m sequencer status` show `solver astap (ok)`, or the reason it is unavailable (binary or database directory not found). Without a working solver, centring falls back as the plan says (`fallback`).

## What the service does

For each solve:
1. It writes the raw frame (unsigned 16-bit, no header keywords) to a temporary FITS.
2. It runs `astap_cli -f frame.fits -fov <image height in deg> -z 2 -ra <hint, hours> -spd <hint Dec + 90> -r 6 -d <db_dir> -D d05`. Without a hint it uses `-r 180` (blind).
   - `-fov` comes from the frame height in pixels and `[telephoto] pixel_arcsec`.
   - `-z 2` downsamples the raw Bayer frame 2×2 before detection. This is the binning to use: don't bin in the camera.
3. It reads `frame.ini` (`PLTSOLVD`, `CRPIX`, `CRVAL`, `CD`).
4. The reported position is the sky position of the **frame centre** evaluated from that WCS: 0-based pixel ((W−1)/2, (H−1)/2), which is FITS pixel ((W+1)/2, (H+1)/2). ASTAP puts its reference pixel exactly there (`CRPIX1 = 1080.5`, `CRPIX2 = 1920.5` for 2160×3840), so this equals `CRVAL`. The service evaluates it anyway, so it doesn't depend on that.
5. It also returns the scale and the local pixel-to-sky matrix (`cd`) that the polar tool uses for its reticle.

The polar tool and centring share this path. Centring solve frames are 2 s by default (`solve_exposure` in the plan), which keeps about 100 stars at gain 80.

## Check by hand

Copy a saved light first, since ASTAP writes `.ini` and `.wcs` files next to the input:

```bash
cp data/sessions/<night>/<target>/lights/<frame>.fits /tmp/x.fits
/opt/astap/astap_cli -f /tmp/x.fits -fov 3.99 -z 2 -r 180 -d /opt/astap -D d05
cat /tmp/x.ini     # PLTSOLVD=T, CRVAL1/CRVAL2 = frame centre (deg)
```
