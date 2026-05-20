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

Open `Plugins > GratisGIS > Manage GratisGIS connections...` and
click `New`. Enter the portal URL of your dev portal
(`http://localhost:3000` for a default local stack) and uncheck
`Verify TLS certificates` if you're hitting a self-signed dev
host. Click `Save & Sign in`.

The plugin fetches `<portal>/api/portal-info` to learn the OIDC
issuer, opens your default browser for the PKCE handshake, then
stores the tokens in the QGIS auth manager. You should not need
to enter a Keycloak URL, realm name, or client ID anywhere; the
discovery endpoint serves them. On subsequent saves, the plugin
re-fetches discovery so portal-side reconfig propagates without a
manual edit.

## 3. Running tests inside QGIS

(Coming with Phase 1.) Tests that require QGIS APIs (providers,
Browser panel) run via `pytest-qgis`. See `tests/qgis/` for the
fixtures setup once that lands.
