# Predecessor evidence: Container Build and Container Scan

Verbatim excerpt from SpectruMonetGram's `PIPELINE-GATES.md` (supplied by the owner, 10 November 2026), recording its first real platform pipeline on 3 November 2026. It is kept here because Light Bee's Dockerfile shape (fail-closed `apt-get upgrade`, pip removed, flattened `FROM scratch`) is the predecessor's, and two decisions rest on it.

What it establishes:

* The fail-closed `apt-get update && apt-get -y upgrade` built on the platform's Container Build stage (the image was built and then scanned).
* The same shape produced 58 Debian base-image findings at its first Container Scan, separate from 33 Python-package findings. Light Bee removes the Python half by carrying no Pillow, urllib3, certifi or matplotlib.

What it does not establish: how, or whether, version 2.0.3 cleared Container Scan. That is TBC, re-verify with the owner. If the base-image findings were cleared by a platform exception rather than a rebuild, Light Bee's pruned final copy (only the Python runtime, the venv and their linked libraries) moves from the risk table into the Phase 5 plan.

## Excerpt

**FACT. The Container Scan is strict and it counts the base image.** The first
scan returned 6 critical and 85 high. Of those 91:

```
 33   pillow, urllib3 and certifi      ours, fixed by raising the pins
 58   Debian 13 base packages          libc6, util-linux, ncurses, zlib1g,
                                       perl-base, libsqlite3-0, gcc runtime
```

pillow 12.3.0 alone carried about thirty advisories. Raising it to 13.0.0
drags matplotlib and streamlit forward with it, which is why 35 of the 45
runtime pins moved for three security floors.

**The 58 base-image findings are not fixable by pinning.** The Dockerfile
already runs `apt-get update && apt-get -y upgrade` fail-closed, so the image
carries the newest Debian has published. Three routes, in order of preference:

● **Rebuild and re-scan.** The base tag floats on purpose, so a build today
  picks up anything Debian has released since. Free, and it is the first thing
  to try.
● **Prune the runtime image.** The final stage is `FROM scratch` with the whole
  prep filesystem copied in, so the image carries perl, tar, coreutils,
  util-linux, sqlite and ncurses that a Python web application never touches.
  Copying only the Python runtime, the venv and the libraries they actually
  link would remove most of the findings. It needs Docker and several
  iterations to get right, and the shell-form `CMD` would have to go with it.
● **Request exceptions** for base-OS CVEs with no upstream fix.
