# Changelog

All notable changes to this project will be documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project tracks [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
for the client library; the QGIS plugin uses its own version line in
`metadata.txt` so that QGIS Plugin Repository semantics are honored.

## [0.16.0] - 2026-08-17

### Changed

- **Feature layers are the default.** Adding a portal layer now
  gives a real feature layer with a working attribute table wherever
  the layer is small enough to load comfortably; users expect an
  added layer to just have its attributes, and tiles were a scale
  workaround, not the product. The line is 50,000 features, judged
  by the portal's per-layer count: above it (or when the count is
  unknown, as on legacy layers) the fast tile default remains, with
  "Add with full attributes" still one right-click away, and
  feature-default layers gain the inverse "Add as fast tiles". The
  rule applies uniformly to the Browser tree, search results, drag
  and drop, and maps opened from the portal.
- **Opened maps style their feature layers.** The map's saved
  symbology (base style, unique values, class breaks) now lands on
  true vector layers as a rule-based renderer, mirroring exactly
  what the tile rendering shows.
- **Tables in maps load their rows.** A geometry-less table
  referenced by a map used to be skipped with directions to Clone;
  it now arrives as an attribute-only feature layer.

## [0.15.2] - 2026-08-17

### Added

- **"Add with full attributes" in the Layers panel.** QGIS has no
  attribute table for vector tile layers at all, so "Open Attribute
  Table" sits permanently greyed on every portal layer, including
  ones an opened map put on the canvas where there is no Browser
  item to right-click. The GratisGIS context menu on tile layers now
  offers the same escape hatch the Browser has: it resolves the
  layer's source back to its portal collection and adds the feature
  version alongside, private layers included (their tile source
  already names the signed-in credential, and the feature twin
  reuses it).

## [0.15.1] - 2026-08-17

### Fixed

- **Opening a portal map lands on its saved viewport.** Every map
  opened at world extent: QGIS zooms to the first layer added to an
  empty project through a deferred call, a vector tile layer's "full
  extent" is the whole planet, and that zoom ran after (and threw
  away) the camera the plugin had just set from the map's saved
  center and zoom. The camera set is now queued behind QGIS's own,
  so the saved view is what survives. It also now transforms into
  the canvas CRS as it is at that moment, which on a fresh project
  is only decided once that first layer arrives.

## [0.15.0] - 2026-08-17

Needs GratisGIS portal 0.9.28 or newer for the private-layer parts.

### Added

- **Private layers as real feature layers.** The portal grew a
  signed-in data feed, and the plugin now uses it everywhere it
  matters. Private and organization tables load their rows instead
  of listing with a "this will load empty" warning. And every
  spatial portal layer in the Browser gains a right-click "Add with
  full attributes" that adds it as a true feature layer: working
  attribute table, selection, everything the fast tile rendering
  cannot do. Public layers keep using the public feed so shared
  projects keep working for people who never sign in.

## [0.14.0] - 2026-08-16

### Fixed

- **Maps now actually appear.** 0.13.0 shipped map opening with maps
  filtered out of both the Browser tree and search, so there was
  nothing to double-click. They now list under a Maps group at the
  top of each folder, and search gains a Map type filter.

### Added

- **An Open GratisGIS map button on the toolbar.** Lists your
  portal's maps and opens the one you pick, for when you know you
  want a map and don't want to dig through the tree.

- **Share with specific groups.** The Sharing window now lists your
  portal groups with checkboxes alongside Only me / My organization /
  Everyone. Groups get view access; finer permissions and geographic
  limits stay in the portal.

- **Right-click a layer in the Layers panel.** A GratisGIS submenu
  with Publish (preselecting the clicked layer), Sync, and Clone for
  offline use, so the portal actions live on the thing you right-
  click, not only on the toolbar.

- **Publish several layers as one data layer item.** The new
  Processing algorithm "Publish layers as one GratisGIS data layer"
  takes any number of vector layers and publishes them as a single
  portal item with one layer each, waiting for every import. This is
  how multi-layer items (a parcels layer plus its summary table) are
  authored from QGIS.

## [0.13.0] - 2026-08-16

### Added

- **Open a portal map in QGIS.** Double-click a map in the Browser
  panel (or right-click it and choose Open map in QGIS) and the whole
  map opens: its layers in order, its groups as QGIS groups, its
  basemap underneath, and the view set to where the map was looking.
  Portal styling comes along, including one-color-per-value and
  class-break coloring, so the map arrives looking like itself.
  Anything that cannot be opened in QGIS says so afterwards, by name,
  with a reason in plain words.

- **Published maps keep your QGIS styling.** Publishing a project as
  a map now carries each layer's colors to the portal: single-symbol,
  categorized, and graduated renderers all translate. Together with
  the map opening above, styling now survives the round trip.

- **Drag a layer onto My Content to publish it.** Dropping a vector
  layer from the Layers panel onto a connection's My Content in the
  Browser opens the publish window with that layer already picked.

- **Sharing without leaving QGIS.** Right-click any portal item in
  the Browser and choose Sharing... to switch it between Only me, My
  organization, and Everyone. The portal still enforces who may
  change what; the plugin just stops that being a trip to the website.

- **A GratisGIS section in the Processing Toolbox.** Two algorithms:
  Publish vector layer to GratisGIS (waits for the portal to finish
  and returns the new item id) and Clone GratisGIS layer for offline
  use (item id in, ready GeoPackage out). Both work in batch mode and
  in Model Designer, so "publish this folder of shapefiles" is now
  one batch run.

- **Hover cards in the Browser.** Hovering a portal item now shows
  what it is, who can see it, when it last changed, and its item id,
  which is the id the clone algorithm asks for.

- **A first-run hint.** The connections window now says what to do
  when it is empty, with an example address, instead of showing a
  blank list and grey buttons.

## [0.12.0] - 2026-08-16

### Changed

- **The plugin now looks like GratisGIS.** The Plugin Manager and
  Plugins menu show the portal's own mark (the sage G) instead of the
  old placeholder pin, and the six toolbar buttons use a custom icon
  set drawn in the portal's palette: deep sage with a tan accent, one
  consistent grid and stroke across the set, with a light contour
  motif carried over from the portal's branding.

  Each toolbar icon keeps a stock QGIS icon as its fallback, so a
  broken install shows a recognizable button rather than a blank one.
  One trade-off: unlike stock icons, the set does not recolor under
  QGIS's Night Mapping theme.

## [0.11.0] - 2026-08-15

Needs GratisGIS portal 0.9.27 or newer.

### Fixed

- **Opening a project with a portal raster in it froze QGIS.** This is
  the hang that needed Task Manager, and it is fixed properly this
  time.

  Portal rasters could be added two ways: read directly from their
  file, or drawn as map tiles. The direct route is what froze it. Such
  a layer in a saved project deadlocks QGIS while it opens, every
  time, and nothing the plugin can do reaches it. Adding the same
  layer by hand always worked, which is what made this look random.

  All portal rasters now draw as map tiles. The difference from
  0.10.0, which tried this and left three rasters blank, is that the
  portal now serves tiles for every raster rather than only some, so
  there is a route to point at. That is the portal change this release
  depends on.

  Two other things went with the direct route. It needed the portal
  key handed to a part of QGIS that ignores signing out, which is why
  a private raster used to keep drawing after sign-out. And it read
  the file over the network from your machine, so a slow link showed
  up as a frozen QGIS rather than a slow map.

  One trade-off worth knowing: an elevation layer drawn this way is a
  picture of the terrain, not elevation values. Download the file from
  the portal if you need to run analysis on it.

- **Repairing a project you already have.** A project saved before
  this release still contains the old kind of layer and will still
  freeze. To fix one:

      python scripts/repair_project.py "path\to\project.qgz"

  It repoints those layers at the tile route, carries over the portal
  credential the project already uses, and keeps the original
  alongside as `.qgz.bak`. Add `--dry-run` to see what it would
  change.

- **Layers already on the canvas ignored signing in or out.** Signing
  out stopped new layers but not existing ones; signing back in fixed
  the Browser panel while the map stayed blank until QGIS restarted.
  Both are the same cause: a layer keeps the credential it worked out
  when it was added. They are now told to look again.

## [0.10.1] - 2026-08-15

### Fixed

- **Reverted 0.10.0.** Portal rasters read directly from their file
  worked again. 0.10.0 had sent them to a tile route the portal did
  not serve for that kind of layer, so three rasters drew nothing.
  Superseded by 0.11.0, which fixes the portal side instead.

## [0.10.0] - 2026-08-15

### Fixed

- **Opening a project with a portal raster in it froze QGIS.** First
  attempt at the fix above. Correct about the freeze, wrong about what
  the portal served; withdrawn in 0.10.1 and redone in 0.11.0.

## [0.9.4] - 2026-08-15

### Fixed

- **Signing out did not stop layers already on the canvas from
  drawing.** The connection showed as signed out and the Browser panel
  behaved, but every portal layer already added kept loading until QGIS
  was restarted.

  Emptying the stored credential updates QGIS's password store; it does
  not reach the copy QGIS is already holding in memory for the session.
  Nothing was telling QGIS to forget that copy, so it carried on
  sending the old key.

  This was wrong in both directions. Signing back in also reused the
  key QGIS had cached rather than the fresh one it had just been given.

  Sign-out and sign-in also now leave a line in the log. A successful
  sign-out previously recorded nothing at all, which made "I signed out
  and my layers still draw" impossible to tell apart from "sign-out
  never ran".

## [0.9.3] - 2026-08-15

### Fixed

- **Portal layers stopped being recognised as portal layers.** The
  publish-as-map dialog listed a raster sitting in your own portal tree
  as "an outside service the portal does not know about", and offered
  to add it as though it were somebody else's.

  QGIS writes a layer's address in whatever order it likes, and reorders
  it when a project is saved and reopened. The plugin was reading that
  address by position rather than by name, so a reordered one no longer
  looked like a portal layer. The layer still drew perfectly, which is
  why nothing seemed wrong until you tried to publish.

  It now reads the address by name, in any order. This also affected the
  clone and sync pickers, which would have quietly stopped offering
  layers after a project reload.

## [0.9.2] - 2026-08-15

### Fixed

- **Splitting a feature offline sent only one half to the portal.** The
  other half vanished, and every sync afterwards kept offering the same
  edit no matter how many times you ran it.

  When QGIS splits a feature it keeps the original and adds a second
  one, copying every attribute across, which includes the hidden id
  that says which portal feature the row belongs to. Both halves then
  claimed to be the same feature: one overwrote the other on the way
  out, and the leftover never looked finished.

  A half that QGIS added is now recognised as a new feature and gets an
  id of its own, so both halves reach the portal and the sync finishes
  for good. This applies to anything that copies a feature, not just
  Split: copy and paste in place had the same problem.

  **If you have already hit this**, the missing half is still in your
  offline copy. Sync again and it will be sent.

## [0.9.1] - 2026-08-15

### Fixed

- **Cloning a layer a second time failed, and took the layer with it.**
  The overwrite has to close the existing layer before it can write the
  file, and on Windows the write was then refused anyway, leaving you
  with no layer and no new data. Two things were wrong.

  Closing the layer does not actually release the file: QGIS keeps the
  data open behind the scenes once anything has read from it, which is
  true of every layer you have looked at. The clone now writes into the
  existing file rather than replacing it, which works, and puts the old
  contents back if that write fails part way.

  And if it still fails, your layer is returned to the project with its
  styling and position intact, instead of quietly disappearing. The
  message says so either way, and no longer tells you to close the file
  in another program when the program holding it was QGIS.

## [0.9.0] - 2026-08-15

### Fixed

- **Publishing a big layer no longer freezes QGIS while it starts.**
  The layer was written out to a file before anything showed progress,
  on the same thread that draws the window, so a large layer left QGIS
  unresponsive from the moment you pressed Publish until the upload
  began. It now happens in the background with the rest of the job, and
  the window says "Preparing the layer..." while it does.

- **Overwriting an offline clone no longer throws away its styling.**
  Replacing a clone had to remove the old layer to release the file,
  and what came back was a plain new layer: default symbology, dropped
  to the bottom of the layer list, outside whatever group it was in.
  The file updated, so it looked like it had worked. Styling, group and
  position are now carried across.

- **A published map opened slightly too far zoomed in.** The zoom was
  worked out using a figure that is only correct on the equator, so
  every map was off, always in the same direction, and more so the
  further from the equator you are. At Randolph County it was about a
  third of a zoom level. Now correct wherever you are, and a project in
  a polar projection no longer produces a nonsense view.

## [0.8.1] - 2026-08-14

### Changed

- **Publishing a vector layer runs as one background job instead of
  two.** No change to what you do or what you get, but the whole
  sequence after the export now lives in one place that can be tested,
  and that can be reused when publishing a project needs to publish a
  layer that is not on the portal yet.

  Worth re-running Test 3 in the playbook, including cancelling
  part-way and closing the dialog mid-publish, since the flow between
  those steps was rewritten.

## [0.8.0] - 2026-08-14

### Fixed

- **Signing out now actually signs you out of the Browser panel.** Every
  private and org layer stayed listed after sign-out, and refreshing did
  not clear them. The tree remembered how the connection looked when
  each row was first drawn, and refreshing kept the remembered rows
  rather than replacing them. Rows now report the connection as it is
  at the moment you expand them.

- **A private raster kept drawing after sign-out.** Raster tile layers
  get their credential through GDAL rather than the QGIS password
  store, so clearing that store never reached them and the key stayed
  loaded until QGIS was closed. Sign-out and deleting a connection both
  clear it now.

- **Adding a private layer while signed out gave an Authentication
  Manager error.** Sign-out used to delete the credential entry
  outright, which broke every layer and saved project that pointed at
  it, including after you signed back in. The entry is kept and emptied
  instead, so those layers fail cleanly while signed out and start
  working again the moment you sign back in, without being rebuilt.

- **A signed-out folder said "Failed to load: Not signed in" in red.**
  It now reads "Not signed in.", because that is a state you chose, not
  a failure.

- **Signing in or out refreshes the Browser panel** instead of leaving
  you to do it by hand.

## [0.7.0] - 2026-08-14

### Added

- **If QGIS ever freezes, the plugin now writes down what happened.**
  Opening a project that holds GratisGIS layers has been seen to lock
  QGIS up hard enough to need Task Manager, and the plugin's log had
  nothing at all to say about it. It does now.

  The plugin watches whether the QGIS window is still responding. If it
  stops for more than ten seconds, a stack for every running thread is
  written to a `freeze-<date>.txt` file next to `plugin.log`, while the
  window is still stuck. The log also records each layer as it loads and
  whether the QGIS password store was locked at the time, so the last
  line before a freeze narrows down which layer caused it.

  Nothing here changes how layers load; it only reports. If you hit the
  freeze, `docs/diagnosing-a-freeze.md` says which files to send and how
  to reproduce it on purpose.

  Set `GRATISGIS_NO_FREEZE_WATCHDOG=1` before launching QGIS to turn the
  watchdog off.

### Fixed

- **The repository no longer rewrites its own line endings on Windows.**
  A checkout could show eleven files as modified with no actual change
  in them, which made it easy to commit noise or lose real work while
  trying to clear it.

## [0.6.0] - 2026-08-14

### Added

- **Tick which layers go in the map.** Every portal layer in your
  project gets a checkbox, ticked to begin with. Untick one and it is
  left out. The count underneath follows along, and what gets published
  is read from the ticks at the moment you press Publish, not from
  whatever the list said when it was drawn.

  Unticking survives the list refreshing itself, which it does whenever
  the plugin re-checks the portal.

## [0.5.1] - 2026-08-14

### Fixed

- **A message described a checkbox that is not there yet.** The reason
  shown for a layer that is not on the portal told you to tick
  "Publish it too", while the row carries a button and no checkbox.
  The wording now describes the button that exists. The checkbox is
  coming, and the text will change when it does, not before.

## [0.5.0] - 2026-08-14

### Fixed

- **Publish-as-map now recognises portal rasters.** An imagery or
  hillshade layer from the portal was listed as something only on your
  computer, so a project full of portal layers could show an empty
  "included in the map" list. It is recognised whichever way the item
  is stored, which is not something you can see from QGIS.
- **An offline copy counts as its original.** A layer you cloned for
  offline use is on the portal, so the map now points at the layer it
  came from instead of offering to upload your copy back as a new item.
- **A layer you just published is remembered.** Publish a layer and
  then publish the project, and the map uses what you published rather
  than offering to publish it again.

### Changed

- The reasons a layer cannot go in a map are written for someone
  publishing a map, not for someone debugging QGIS. They said things
  like "Phase 3" and named internal machinery.
- The changelog QGIS shows under Manage and Install Plugins is built
  from this file, so it stops describing a version four releases old.

## [0.4.1] - 2026-08-14

### Fixed

- **Publishing a raster failed with "HTTP 400 (code Bad Request)".**
  The plugin was telling the portal the wrong place to find the file it
  had just uploaded: it added a prefix to the storage location that was
  already there. The portal looked for something that did not exist and
  refused. Nothing was left behind on the portal, so retrying is all
  that is needed.
- **Errors from the portal now say what the portal said.** The failure
  above showed up as a bare "HTTP 400" while the portal had been
  answering with the actual reason all along. Messages now carry it.
- **The title box follows the layer you pick.** It filled in from
  whichever layer happened to be listed first and then kept that name
  after you chose a different one, which was a quiet way to publish
  under the wrong title. It still leaves a title you typed yourself
  alone.

### Changed

- **Every toolbar button has its own icon.** They were six copies of
  the GratisGIS pin, which is not a toolbar so much as a row of
  identical buttons. They now use QGIS's own icons, so they read the
  way the rest of QGIS reads and follow your theme.

## [0.4.0] - 2026-08-14

### Changed

- **One "Publish layer..." instead of two menu entries.** You should not
  have to decide whether the thing you want to publish counts as a
  vector or a raster before you can find the right menu item. Pick the
  layer; the plugin works out what it is.
- **Rasters can be published straight from your map.** If an aerial is
  already on your canvas, it is in the list, and the plugin finds the
  file behind it. Publishing used to mean going and finding that file
  on disk yourself, which was backwards for the commonest case. A
  "Choose a file instead..." button covers the file you have not added
  to your map.
- **A raster that cannot be published says why**, rather than not
  appearing. A layer streaming from a web service, or one whose file
  has moved, is listed and marked, with a sentence on what to do about
  it.

### Added

- **A GratisGIS toolbar.** The same actions as the menu, one click away
  and grouped: connect and search, then publish, then clone and sync.
  The menu stays, since that is where people look first and it cannot
  be accidentally hidden.

## [0.3.0] - 2026-08-14

### Changed

- **"Push edits" is now "Sync layer", and it works the way you would
  expect.** Save your edits as often as you like, close QGIS, come back
  next week: your pending changes are still there, because they are
  recorded inside the offline copy rather than in QGIS's unsaved-edit
  buffer. The old flow only saw edits while they were UNSAVED, so it
  had to ask you not to save, which is a strange thing to ask during a
  long editing session.

  It also only ever sends work you have saved. That closes a real hole:
  before, you could send unsaved edits and then answer "discard" in
  QGIS, leaving the portal holding changes your own copy never had,
  with nothing anywhere aware the two had drifted apart.

### Added

- **Sync tells you when someone else changed the same features.**
  Before sending, it re-reads the portal and compares each feature
  against the version you cloned. Anything that moved on both sides is
  listed by name, and you choose: send yours over theirs, or skip those
  and send the rest. It will not overwrite someone else's work without
  saying so.
- **New features get their identity before they are sent**, so a sync
  interrupted by a dropped connection can be run again without creating
  duplicates on the portal.

### Fixed

- The portal's own id for a feature (`_global_id`) was not among the
  names the clone looked for when recording which portal feature each
  local row came from.

## [0.2.11] - 2026-08-14

### Fixed

- **Cloning over an existing file failed with "Access is denied".** The
  file being replaced was almost always the previous clone, still open
  in the project, and Windows will not let an open file be replaced.
  The clone now closes the old copy first and reloads the new one in
  its place, which is what answering yes to "Overwrite?" implied all
  along. If that copy has unsaved edits it says so and stops, rather
  than quietly throwing the edits away.
- **Every refused overwrite left a hidden folder behind** next to the
  destination, named after the file it failed to write. Cleanup only
  ran on the failures that had been anticipated, and the file being
  locked was not one of them. Existing leftovers are safe to delete;
  they are named `.<filename>.<random>`.
- A write that fails for a reason outside the plugin's control now
  explains itself instead of showing a raw system error.

## [0.2.10] - 2026-08-14

### Fixed

- **"Zoom to Layer" on a portal layer zoomed out to the whole world.**
  Portal layers draw as map tiles, and a tile pyramid covers the entire
  planet no matter how little data is in it, so that is the only extent
  QGIS could work out on its own. The portal already knows where the
  data really is, so layers now carry their extent with them and zoom
  to it. This was most obvious on a layer just published from a handful
  of features, where the whole point is to look at what you published.

  Raster and tile layers on the demo will still zoom to the world for
  now: the portal is not yet recording an extent for that kind of item,
  so there is nothing for them to carry. That is a portal-side fix.

### Changed

- Layers remember their extent when a project is saved and reopened.

## [0.2.9] - 2026-08-14

### Fixed

- **The clone dialog still saw no layers.** 0.2.8 taught the source
  parser all three URI shapes, but the picker rejected each layer on its
  CLASS before ever reading its source: it accepted only vector layers,
  and a spatial layer from the browser tree is a vector-TILE layer,
  which is a separate QGIS class rather than a kind of vector layer.
  Both the picker and the selection now accept either. Cloning reads
  features over HTTP and never touches the layer's provider, so a
  read-only rendering format is a perfectly good thing to clone from.
- **A response that was not a success leaked its network connection.**
  Every non-2xx reply left a live socket for the garbage collector to
  reclaim whenever it got around to it. Non-2xx is routine here (asking
  for an item that turns out not to exist, an expired token prompting a
  refresh), so this accumulated over a long session.

### Changed

- The tests behind the clone picker now model the QGIS layer classes
  faithfully, one stand-in per real class. A single stand-in had been
  serving for every class, which made the type check pass by
  construction: that is why the suite stayed green through both of the
  clone bugs. The check is now covered by a real-QGIS assertion too, so
  a stand-in can no longer be the only witness.

## [0.2.8] - 2026-08-14

### Fixed

- **Cloning a layer for offline use produced an empty file, and the
  dialog could not see most layers anyway.** Two separate faults. The
  layer list only recognised the plain-table shape, so ordinary spatial
  layers (which arrive as vector tiles) were never offered at all and
  the dropdown read "no portal-backed layers in project". And the write
  itself staged into a placeholder file that the GeoPackage writer
  could not create over, so even a recognised layer wrote nothing. Both
  are fixed, and the write is still safe: a failed clone cannot damage
  an existing copy.

### Added

- **Offline clones can now be pushed back to the portal.** A clone
  records which portal layer it came from, inside the GeoPackage
  itself, so it still knows where it belongs after being moved or
  emailed. Edit the offline copy, then Push edits sends the changes to
  the original. Layers added from the browser tree draw as vector
  tiles, which QGIS cannot edit, so cloning is now the supported way to
  edit portal data and the push dialog says so.

## [0.2.7] - 2026-08-14

### Fixed

- **Publishing a vector layer failed at the upload step** with
  "field 'fileName': expected a string, got NoneType". The plugin
  expected the upload response to carry a file name, a size and an
  expiry; the portal has never sent any of the three, so every upload
  failed to parse and no vector layer could be published. The response
  is now read as the portal actually sends it. The whole publish path
  (upload, inspect, create, import, read back) was then run against the
  live portal to confirm nothing else in the chain was assumed rather
  than checked.

## [0.2.6] - 2026-08-14

### Fixed

- **Item properties showed a raw ID instead of the owner's name.** The
  portal sends the owner as a small record (username, full name), but
  the dialog looked for a single flat field the portal has never sent,
  so it always fell through to the internal identifier. It now reads
  the record and shows, for example, "Site Admin (admin)", keeping the
  identifier only as a last resort for an owner whose account no longer
  exists.

## [0.2.5] - 2026-08-14

### Fixed

- **Tile layers stored as PMTiles still would not draw.** The plugin
  treated a layer as unfinished unless the portal reported its status
  as exactly "ready", but a finished tile pyramid reports
  "pmtiles-ready", so all of them were shown as still being prepared
  when the tiles were in fact ready to serve. Status is no longer
  treated as a readiness gate at all: the portal keeps serving a
  usable file through every intermediate state, so what a layer is
  stored as now decides how it is opened.
- **The search panel could not add most items.** Double-clicking a
  basemap, a connected service, or anything other than two item types
  reported that the plugin "doesn't yet open this type", even though
  the browser tree adds them. It also sent data layers and tile layers
  to the wrong addresses, so a private layer added from search drew
  nothing. The search panel now opens items exactly the way the
  browser tree does, so the two can no longer disagree.

## [0.2.4] - 2026-08-14

Requires GratisGIS portal v0.9.26 or newer for the layers below.

### Added

- **The remaining tile layers now draw.** Layers stored as PMTiles
  (hillshade, steepness, visible area, height above ground) could not
  be opened by QGIS at all: nothing in QGIS reads that container when
  it holds image tiles. The portal now serves their individual tiles,
  and the plugin points at that, so they behave like any other tile
  service. Private and organization layers sign in automatically;
  public ones stay public so a shared project keeps working for
  viewers who are not signed in.

## [0.2.3] - 2026-08-14

### Fixed

- **Tile layers added an empty layer instead of the imagery.** Tile
  layers hold raster data (elevation, hillshade, imagery), but the
  plugin was asking the portal for them as vector tiles, an address
  that does not exist for these items, so QGIS added a layer that drew
  nothing and reported no error. Tile layers stored as Cloud Optimized
  GeoTIFF now open directly over the network, reading only the parts of
  the image needed for the current view, and private and organization
  layers carry their credential so they draw for anyone signed in.

### Changed

- **Tile layers QGIS cannot open now say so instead of failing
  quietly.** Layers stored as PMTiles cannot be opened by QGIS at all
  (its raster reader does not support that container), and layers still
  being prepared on the portal are not ready yet. Both now appear in
  the browser with an explanation rather than as a layer that silently
  draws nothing. Use the Download button on the item's portal page to
  work with a PMTiles layer in QGIS for now.

## [0.2.2] - 2026-08-13

### Fixed

- **The plugin failed to load with `KeyError: 'gratisgis_qgis._vendor'`
  when QGIS reloaded it**, which is what happens when you install over
  an existing copy or toggle the plugin off and on. The first load in a
  fresh QGIS session worked, so this only appeared on the second.
  QGIS clears only the modules its own import hook recorded, and the
  bundled client library is registered by the plugin directly, so a
  stale entry survived the unload and sent the next load down a path
  that skipped part of the setup. The plugin now clears both halves of
  its own state on every load and no longer assumes the surviving
  entry is present. The packaging test now performs a second load the
  same way QGIS does, so this cannot regress unnoticed.

## [0.2.1] - 2026-08-13

### Fixed

- **Sign-in crashed immediately on QGIS 4.** Starting any background
  task raised `TypeError: QgsTask(): argument 2 has unexpected type
  'int'`, so connecting to a portal failed at the first step. The
  cause was a compatibility branch that decided which value to pass
  by asking whether the flag was an integer; on Qt6 the flag types
  are integers as well, so the branch fired on exactly the QGIS
  version it was written to support. QGIS 3 is unaffected.

### Added

- `scripts/qgis_smoke.py`, a headless check that runs against a real
  QGIS install (`python-qgis.bat scripts/qgis_smoke.py`). The unit
  tests run against a simulated QGIS, which cannot catch a value of
  the wrong type being handed to a real QGIS function, which is how
  the crash above shipped. The new check exercises the plugin against
  the genuine bindings: every module imports, every version-dependent
  enum resolves, a background task runs start to finish, the browser
  provider builds, the layer addresses parse, and the authentication
  method that private layers rely on is present.

## [0.2.0] - 2026-08-13

A foundation release: same features as 0.1.0, rebuilt so they
actually hold up in a real QGIS. Still pre-alpha.

### Fixed

- **The plugin zip now installs and runs on stock QGIS.** Previous
  zips, including the 0.1.0 release, did not: the plugin required
  Python packages (httpx, pydantic) that QGIS does not ship and the
  zip could not carry, and the build's import rewriting left the
  vendored client library unable to import itself. The client is
  now pure Python standard library with no third-party dependencies
  at all, the zip vendors it unmodified, and a packaging test
  builds the zip and imports every module of it on a bare
  interpreter so a dead-on-arrival zip cannot ship again.
- **The UI no longer freezes during network work.** Sign-in,
  vector / raster / project publishes, offline clone, and push
  edits all run as background tasks with progress reporting and a
  working Cancel. Previously each of these blocked the whole QGIS
  window until it finished, up to half an hour for a large raster
  upload.
- **Safer failure behavior across the board.** A publish that fails
  or is cancelled partway no longer leaves an orphaned half-created
  item on the portal (the plugin deletes it and says so). Offline
  clone writes to a temporary file and swaps it into place only on
  success, so a failed download can no longer destroy an existing
  GeoPackage. Push edits records the portal ids of created
  features, so retrying after a partial failure updates them
  instead of creating duplicates.
- **QGIS 4 / Qt 6 compatibility.** Enum access and file-writer
  error handling that QGIS 4 under Qt 6 removed or relocated are
  resolved at runtime with QGIS 3 fallbacks, including a case that
  broke plugin load outright. Log handlers no longer stack up
  across plugin reloads.

### Added

- **Private and org-shared layers render on the canvas.** At
  sign-in the plugin mints a read-only portal API key, stores it in
  the QGIS authentication database, and attaches it to non-public
  layer URIs so those layers actually draw. The key is revoked at
  sign-out and on connection delete. Read-only means a leaked layer
  URI cannot modify data; edits always use your signed-in session.

### Changed

- `gratisgis_client` was rewritten as a synchronous, dependency-free
  library (urllib transport, frozen dataclass models). Its public
  shape changed; it serves the plugin, and the standalone GratisGIS
  Python SDK lives in the main repo.

### Known limitations

- Private non-spatial tables do not render live; clone them to
  view and edit (the portal has no authenticated features surface
  for tables yet).
- Adding a layer from the search dock uses the public surface, so
  private layers added that way draw empty; add them from the
  Browser tree instead.
- tile_layer items are served from the public surface only, so
  private tile layers do not draw.

## [0.1.0] - 2026-05-20

First end-to-end release: every phase of the original plan is in.
Pre-alpha, expect rough edges.

### Added

- **Phase 1 - Browse + add via OGC connection.** Browser-panel
  data provider exposes a `GratisGIS` subtree per configured
  connection with buckets (Mine, Shared, Org, Public) and per-item
  entries that drag onto the canvas as OAPIF or vectortile sources.
- **Phase 2 - Search + metadata.** Docked search panel for full-
  text portal search, per-item properties dialog with sharing /
  owner / extent / tags / description.
- **Phase 3 - Publish vector layer.** End-to-end dialog: pre-flight
  validation, GeoPackage export, staged upload, v3 data_layer item
  create, async import job with progress polling. Pure-Python
  translator owns the QGIS-to-v3 type mapping + layer-id
  sanitization rules.
- **Phase 4 - Push edits to portal.** Captures the QGIS edit
  buffer (adds, geometry changes, attribute changes, deletions)
  and translates it into ordered feature-CRUD calls. Co-occurring
  geom + attr edits to the same id merge into one PATCH;
  delete-after-update drops the update. Pure-Python planner.
- **Phase 5 - Publish raster / tile.** Upload PMTiles / MBTiles /
  GeoTIFF / COG / JP2 as a portal tile_layer. Pre-flight extension
  recognizer mirrors the portal's allow-list with format-specific
  reasons for TPK / ECW / MrSID. Direct PUT to MinIO via
  presigned URL, then portal-side finalize for header read +
  PMTiles pyramid build.
- **Phase 6 - Publish project as map.** Walks the QGIS project
  layer tree, recognizes layers backed by portal items, captures
  the canvas viewport (reprojected to CRS84), and creates a
  portal map item composing those layers. Skips layers from
  unknown providers with provider-aware reasons.
- **Phase 7 - Clone for offline.** Pulls the full feature set
  for a portal-backed OAPIF layer, normalizes portal-internal
  ids into a single `_portal_id` property (so the Phase 4
  push-edits round-trip stays lossless), and writes the result
  to a local GeoPackage that auto-loads into the project.
- **Phase 8 - Packaging.** `scripts/make_zip.py` builds a
  QGIS-installable plugin zip. The `gratisgis_client` library
  is vendored under `gratisgis_qgis/_vendor/` so the plugin
  works on a stock QGIS install with no extra pip steps;
  imports are rewritten on package.

### Foundation (previously unreleased)

- `gratisgis_client` portable Python client, `gratisgis_qgis`
  plugin shell, CI, tests, dev docs.
- PKCE OIDC auth flow against Keycloak.
- Connection management (add/edit/delete portal profiles).
- QGIS auth manager bridge for encrypted token storage.
- Plugin file logging.
- Keycloak realm dependency satisfied: a dedicated `qgis-plugin`
  public client ships in the GratisGIS realm (PKCE S256, custom
  scheme `gratisgis-qgis://auth-callback`, and loopback
  `http://127.0.0.1*` redirect URI). The plugin's default
  `client_id` resolves once the realm is imported.
- Portal discovery: `gratisgis_client.discover(portal_url)` fetches
  `/api/portal-info` and returns a typed `PortalInfo` (name, version,
  api base, OIDC issuer). `portal_config_from_discovery()` splits the
  issuer into the Keycloak realm pair the existing `PortalConfig`
  expects, so future endpoints can build clients from one URL.

### Changed

- **Connection dialog is one field.** The user enters only the
  portal URL; the plugin discovers everything else
  (display name, OIDC issuer, API base URL) automatically and
  caches it on the profile. The previous four-field form
  (Portal URL, Keycloak URL, Realm, Client ID) leaked Keycloak
  terminology into the user surface. Pre-existing four-field
  profiles continue to load: legacy `keycloak_url` + `realm` keys
  are synthesized into an `oidc_issuer` on read.

### Fixed

- Keycloak rejected the loopback redirect URI as `Invalid parameter:
  redirect_uri` because the registered pattern
  `http://127.0.0.1:*/*` placed a wildcard in the port position;
  Keycloak only supports trailing wildcards. The QGIS plugin client
  now registers `http://127.0.0.1*` which correctly matches any
  port and path. Applied to both the dev realm JSON and the prod
  template in the main gratis-gis repo.
- Plugin classFactory failed with `ModuleNotFoundError: No module
  named 'gratisgis_qgis'` when QGIS loaded the plugin under any
  folder name other than `gratisgis_qgis`. Internal imports inside
  `gratisgis_qgis` are now relative, so the QGIS plugin folder name
  can be anything (`gratisgis`, `gratisgis_qgis`, whatever the
  Plugin Repository or local install chooses). Smoke tested in
  QGIS 3.44 (OSGeo4W).
