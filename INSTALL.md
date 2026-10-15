# Installing Slurrymix

Slurrymix is one Python file. The only package manager involved is **pip**, and the
only pinned package is the test runner (`pytest`). The tool itself has **no runtime
dependencies** and never touches the network.

## Prerequisites (installed by you, not by this project)

| Requirement | Notes |
| --- | --- |
| Python 3.10 or newer (3.11 recommended) | `python3 --version`. Standard library only, no C extensions. |
| pip | `python3 -m pip --version`. Ships with CPython. |

Nothing else is needed: no database server, no compiler, no `libsqlite3-dev`, no
system service, no Docker. `install.sh` installs **only** project-level Python
packages with pip; it never runs `sudo`, `apt-get`, `brew` or `systemctl`.

## Step 1 — install and self-check

```bash
bash install.sh
```

The script:

1. upgrades `pip`, `setuptools` and `wheel` (best effort, so a machine without a
   package index still proceeds),
2. runs `python3 -m pip install -r requirements.txt` (pytest, the test runner),
3. runs `python3 -m pip install -e . --no-build-isolation` so the `slurrymix`
   console command is on `PATH`,
4. verifies the module imports, prints `slurrymix 1.0.0 ready`, and **exits**.

It is idempotent — running it twice is harmless — and it never starts the tool,
creates a state file or leaves a background process behind. If any install step
fails, the script exits non-zero.

## Step 2 — check the tool

```bash
python3 slurrymix.py --version      # slurrymix 1.0.0
python3 slurrymix.py --help         # every command, with examples
slurrymix --version                 # the same, via the installed console command
```

Both entry points work with no state file present and create none.

## Step 3 — seed some realistic data (or just look around)

There is no seed step to run: state is created by the commands themselves, so the
data is always yours. To see the whole tool working end to end on throwaway data:

```bash
bash demo.sh
```

`demo.sh` writes into a temporary directory and removes it afterwards; your working
directory is untouched. To build the same picture by hand, follow the Quick start in
[README.md](README.md) or the walkthrough in [USAGE.md](USAGE.md).

## Step 4 — run it

```bash
python3 slurrymix.py status
```

Slurrymix has no server, no daemon and no scheduled work; there is nothing to start
in the background and no port to open. When you want to work on real data, run the
commands from the directory that holds your `slurrymix.json`, or point at a specific
file:

```bash
python3 slurrymix.py --state ~/farm/2026/slurrymix.json status
```

## Step 5 — run the tests

```bash
python3 -m unittest -v      # standard library runner
python3 -m pytest -q        # the same suite under pytest (from requirements.txt)
```

Both runners execute `test_slurrymix.py`, which uses temporary directories for every
state file and never touches the repository root.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `python3: command not found` | Install Python 3.10+ and make sure it is on `PATH`. |
| `No module named pytest` | Run `python3 -m pip install -r requirements.txt`. |
| `slurrymix: command not found` | The editable install did not run: `python3 -m pip install -e . --no-build-isolation`, or just use `python3 slurrymix.py`. |
| `slurrymix.json is not valid JSON` | The state file was edited into invalid JSON. Fix it, or delete it to start fresh. |
| `state file schema version 2 is newer than this tool (1)` | The file was written by a newer Slurrymix. Upgrade the tool or edit `schema_version`. |
| Colours or box-drawing characters look wrong | Use `--no-color`, set `NO_COLOR=1`, or check that the terminal is UTF-8. |

## Uninstall

Delete `slurrymix.py` (and your `slurrymix.json` if you no longer want the data).
If you installed the console command, `python3 -m pip uninstall slurrymix` removes it.
