# Diagnosing a QGIS freeze

For the case where QGIS stops responding and has to be killed from
Task Manager. The known instance is opening a project that contains
GratisGIS layers while signed out, tracked as issue #24.

## What the plugin does on its own

From 0.7.0 the plugin watches for this without being asked.

A timer on the GUI thread records a heartbeat once a second. A
background thread watches that heartbeat. If it goes more than ten
seconds stale, the GUI thread has stopped running events, and the
background thread writes a stack dump for every Python thread to:

```
%APPDATA%\QGIS\QGIS3\profiles\<profile>\gratisgis\logs\freeze-<date>-<time>.txt
```

The same directory holds `plugin.log`. Both belong on any bug report.

The dump opens with a table of thread ids and names, because
`faulthandler` labels stacks with a hex id and nothing else. Find the
line marked `<- GUI thread`, take its id, and read that stack first.
Its last frame is the call that did not return.

`plugin.log` should now carry a trail up to the moment of the freeze:

```
project read starting; auth database is locked (QGIS will prompt if a layer needs it)
layer added: 'Roads' (portal, authcfg=ab12cd3) type=xyz&url=https://...
layer added: 'Parcels' (portal, authcfg=ab12cd3) type=xyz&url=https://...
```

A layer that hangs while being built never reaches that log line, so
the culprit is not the last layer listed, it is **the next one in the
project's layer order**.

To switch the watchdog off, set `GRATISGIS_NO_FREEZE_WATCHDOG=1`
before launching QGIS.

## Getting a native stack

The Python dump shows the last Python frame before a call into C++. If
the deadlock is entirely inside Qt, GDAL, or QGIS core, that frame
names the operation but not the lock, and the leading theory for #24
is a mutex inside `QgsAuthManager`. For that you need a native stack,
taken from outside the frozen process.

`py-spy` is the least invasive way, and does not need QGIS restarted
or rebuilt:

```
pip install py-spy
py-spy dump --pid <qgis pid> --native
```

Find the pid in Task Manager under `qgis-bin.exe`. Run the terminal as
Administrator or py-spy cannot attach.

Take the dump **while QGIS is still hung**. Once it is killed the
evidence is gone, and a freeze that has been reproduced once is not
guaranteed to reproduce twice.

## Reproducing issue #24

The conditions believed necessary, all at once:

1. A QGIS profile with a **master password set** on the auth database,
   not saved to the OS keyring or password manager.
2. **Signed out** of the GratisGIS connection. Sign-out deletes the
   layer authcfg from the auth database, while every saved project
   keeps pointing at it by id, so the reference is guaranteed dangling.
3. A project holding **several private or org-shared portal layers**,
   saved while signed in.
4. QGIS **restarted**, so the auth database is locked again for the
   session. This is the step most easily missed: entering the master
   password once unlocks it until QGIS exits, and the freeze does not
   reproduce for the rest of that run.

Then open the project from the Recent Projects list.

## Why those conditions

A private portal layer's URI carries `authcfg=<id>`. Resolving one
sends QGIS into `QgsAuthManager`, which needs the auth database
unlocked, which raises a **modal** master-password prompt. During a
project load that prompt can be raised once per layer, and QGIS has a
history of deadlocking on exactly this path:

- [qgis/QGIS#35993](https://github.com/qgis/QGIS/issues/35993): the
  credential dialog calls `verifyMasterPassword()`, which reaches
  `authDatabaseConnection()` and blocks on a mutex.
- [qgis/QGIS#51317](https://github.com/qgis/QGIS/issues/51317): the
  auth manager hangs when reached without a loaded master key.

The plugin makes this more likely than a typical consumer of the auth
system, in two ways worth stating plainly. It puts `authcfg=` on every
non-public layer URI, so a project can reference the auth database a
dozen times in one load. And signing out **removes** the authcfg
entirely rather than emptying it, so the ids in a saved project resolve
to nothing at all.

None of this is confirmed as the cause. It is where to look first, and
the instrumentation above exists to replace it with evidence.

## What a fix has to satisfy

Whatever the root cause turns out to be: **opening a project must never
be able to block the GUI.** A portal layer that cannot authenticate
should fail fast, draw nothing, and say so. Losing a layer is
recoverable; losing unsaved work to a Task Manager kill is not.

Moving the auth work earlier, to plugin startup, is a plausible fix for
the dangling-reference half and a plausible way to make the modal-prompt
half worse, by relocating the prompt to launch. Reproduce before
choosing.
