# Development setup

This doc covers two distinct setups: developing the **client library**
(pure Python, no QGIS) and developing the **QGIS plugin** (loaded into
a running QGIS).

## 1. Client library

This is the simplest setup. You only need Python 3.10+.

```bash
git clone https://github.com/palavido-dev/gratis-gis-qgis
cd gratis-gis-qgis
python -m venv .venv
.\.venv\Scripts\activate     # PowerShell
# or: source .venv/bin/activate   # macOS/Linux
pip install -e ".[dev]"
```

Run the client unit tests:

```bash
pytest tests/client
```

Run lint and type-check:

```bash
ruff check src tests
mypy
```

## 2. QGIS plugin

The plugin loads from QGIS's plugin directory. You can develop against
a live QGIS instance by symlinking (or copying) the plugin folder into
your QGIS user plugins directory.

### Find your plugin directory

- **Windows:** `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins`
- **macOS:** `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins`
- **Linux:** `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins`

### Symlink the plugin (development workflow)

From the repo root:

```powershell
# Windows PowerShell, run as admin so the symlink works
$dst = "$env:APPDATA\QGIS\QGIS3\profiles\default\python\plugins\gratisgis"
New-Item -ItemType SymbolicLink -Path $dst -Target "$PWD\src\gratisgis_qgis"
```

```bash
# macOS/Linux
ln -s "$PWD/src/gratisgis_qgis" \
  ~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/gratisgis
```

Then launch QGIS, open `Plugins > Manage and Install Plugins`, switch
to the `Installed` tab, and enable `GratisGIS`.

### Reload during development

The QGIS Plugin Reloader plugin (also installable from the official
QGIS Plugin Repository) lets you reload the GratisGIS plugin without
restarting QGIS. Highly recommended during development.

### Targeting a local portal

The plugin reads connection details from QSettings. For local
development, point it at:

```
Portal URL: http://localhost:3000
Keycloak URL: http://localhost:8081
Realm: gratis-gis
Client ID: field-app   # or qgis-plugin once that's added to the realm
```

The first time you sign in, a browser window opens to handle the PKCE
flow. After that, tokens live in the QGIS auth manager and refresh
silently.

## 3. Running tests inside QGIS

(Coming with Phase 1.) Tests that require QGIS APIs (providers,
Browser panel) run via `pytest-qgis`. See `tests/qgis/` for the
fixtures setup once that lands.
