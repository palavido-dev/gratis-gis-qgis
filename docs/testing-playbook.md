# QGIS plugin test playbook

Hands-on checks for the parts of the plugin that automated tests cannot
reach: the interactive flows. Work top to bottom, they build on each
other. Roughly 45 minutes for all of it, and any single test stands
alone if you only have ten minutes.

Everything here targets the public demo at https://gratisgis.org.

---

## Before you start

1. **Check the version.** `Plugins > Manage and Install Plugins >
   Installed`, find GratisGIS. It should read **0.4.1**.
2. **Check you are signed in.** `Plugins > GratisGIS > Manage GratisGIS
   connections...`. The row should show the portal and a signed-in
   state. If anything later fails with "Your session has expired",
   come back here and sign in again; that message is honest, the
   session really did lapse.
3. **Know where the log is.** If something fails, this file is what I
   need:

   ```
   C:\Users\matt\AppData\Roaming\QGIS\QGIS4\gratisgis\logs\plugin.log
   ```

   The last 40 lines are usually enough. A screenshot of the error box
   plus that tail is a complete bug report.

**Two things that are normal, not bugs:**

- **The demo wipes itself nightly at 04:00 UTC.** Anything you publish
  disappears at the reset. That is convenient here: no cleanup needed.
- **Menu items never refuse to open.** There is no "select a layer
  first" popup. If a dialog has nothing to work with, its dropdown says
  so, for example `(no portal-backed layers in project)`. That is the
  intended empty state.

---

## Test 1. Search panel (2 min)

This is the one I just changed, so it is worth confirming first.

1. `Plugins > GratisGIS > Open GratisGIS search...`
2. Set **Type** to `Basemap`, click **Search**.
3. **Double-click "Open Street Map".**

**Expect:** the basemap is added to the canvas and draws.
*(Before 0.2.5 this said "the QGIS plugin doesn't yet open this type".)*

4. Set **Type** to `Data layer`, click **Search**, double-click
   **"Randolph County parcels"**.

**Expect:** parcels draw.

Right-click the layer and pick **Zoom to Layer**. It should land on
Randolph County, not the whole world. *(New in 0.2.10. Tile-based
layers cannot work out their own extent, so the portal's is carried
along with the layer.)*

5. Set **Type** to `Tile layer`, search, double-click **"WV lidar
   terrain hillshade (terrain, 1m)"**.

**Expect:** the hillshade draws. `Zoom to Layer` on this one still
goes to the whole world, and that is expected for now: the portal does
not yet record an extent for raster/tile items, so there is nothing to
carry. Data layers (step 4) do. Pan to Randolph County to see it.

6. **Right-click** any result.

**Expect:** an "Item properties" window with title, type, description,
tags, access, owner, dates and the item id. Read-only, one **Close**
button. That is Test 2 done at the same time.

---

## Test 3. Publish a vector layer (10 min)

The biggest untested flow. You need a small layer to publish.

### Make a throwaway layer

1. `Layer > Create Layer > New GeoPackage Layer...`
2. File name: anywhere, call it `qgis_test`. Geometry type
   **Polygon**. CRS **EPSG:4326**.
3. Add one text field: name `label`, type `Text`. Click **OK**.
4. The new empty layer appears. Click the **pencil** (Toggle Editing),
   draw **2 or 3 polygons** anywhere, type any label for each, then
   click **Save Edits** and turn editing off.

### Publish it

5. `Plugins > GratisGIS > Publish layer to GratisGIS...`  *(or the
   toolbar, new in 0.4.0)*
6. **Layer:** pick `qgis_test`. Note the list now holds rasters too.
   **Portal:** the GratisGIS connection.
7. **Title** auto-fills from the layer name. Leave or change it.
8. Leave **Access** as `Private (only you)`.
9. Look at the **Pre-flight checks** list. For this layer it should say
   `All checks passed.`
10. Click **Publish**.

**Expect, in order:** the label cycles through "Exporting layer to
GeoPackage...", "Uploading to portal (stage)...", "Creating portal
item...", then a progress bar showing the import. Finally a
**"Published"** box:

```
Layer published successfully.

Inserted: 3
Item id: <uuid>
```

**Note:** the layer is *not* added to your canvas. That is intended.

11. **Verify it landed.** In the Browser panel, right-click
    `GratisGIS > GratisGIS > My Content` and Refresh, expand
    `Data layers`. Your new item should be there. Drag it to the canvas
    and confirm your polygons draw.
12. **Zoom to Layer** on it. It should frame your polygons, not the
    planet. This is the case the extent fix exists for: a layer you
    just made from three shapes is exactly when you want to look at
    what you published.

**Worth trying deliberately:** publish a layer with **no CRS set** and
confirm pre-flight blocks it with "Layer has no CRS defined."

---

## Test 4. Clone a layer for offline use (5 min)

Test 5 builds directly on this one, so do them together if you can.

1. First put a portal data layer on the canvas: Browser tree >
   `Org Content > Data layers`, expand **Trails**, drag its sublayer to
   the canvas. *(Trails is small, 43 features.)*
2. `Plugins > GratisGIS > Clone layer for offline use...`
3. **Layer:** pick the Trails layer. **Portal:** your connection.
   *(Any layer from the tree qualifies now. This is the fix worth
   confirming, and it took two goes: the dropdown said `(no
   portal-backed layers in project)` for anything spatial, first
   because the dialog only recognised the plain-table URI shape, and
   then because it still rejected the layer on its class before
   reading the URI at all.)*
4. Click **Choose directory...**, pick anywhere writable.
5. **File name** auto-fills. Leave it. Do not type `.gpkg`, it is added
   for you.
6. Pre-flight should be clean. Click **Clone**.

**Expect:** an indeterminate progress bar, then a **"Cloned"** box
naming the feature count and full path, and a new layer in your project
called **"Trails (offline)"** that draws.

7. **Run it a second time with the same name.** You should get an
   **"Overwrite?"** prompt, which now also tells you the old copy is
   open here and will be reloaded. Say Yes and confirm it still works,
   and that you end up with one "Trails (offline)" layer rather than
   two. *(Windows will not let an open file be replaced, so this
   failed with "Access is denied" until 0.2.11. The old copy is closed
   first now.)*

   **Also worth trying:** start editing the offline layer (pencil on,
   move something, do NOT save), then re-clone over it. It should
   refuse with "Unsaved edits" rather than discarding your work.

**Keep the "Trails (offline)" layer in your project.** Test 5 edits it.

---

## Test 5. Sync an offline clone back to the portal (10 min)

**Rewritten in 0.3.0.** The old version told you not to save your
edits, which was backwards. Now it is the opposite: **save normally**,
and only saved work is sent.

The round trip is clone (Test 4), edit the clone, sync. The clone knows
where it belongs because the plugin records the portal, item and layer
inside the GeoPackage, so a copy you moved or emailed still syncs to
the right place.

### Edit the clone

1. Do **Test 4** first. Edit the **"Trails (offline)"** layer it
   produced, not the layer you cloned FROM. (The tree layer draws as
   vector tiles, which QGIS cannot edit at all; the dialog says so.)
2. Click the **pencil**, then make a few edits: move a vertex, change
   an attribute, add a feature, delete one.
3. **Click Save Layer Edits and turn editing off.** This is the change
   worth confirming. Save as many times as you like along the way.

**Bonus, and the real point:** close QGIS entirely, reopen it, and add
the offline GeoPackage back to a project. Your pending changes should
still be there in step 5. They live in the file, not in a QGIS buffer.

### Sync

4. `Plugins > GratisGIS > Sync layer with GratisGIS...`
5. **Layer:** `Trails (offline)`. **Portal:** your connection.
6. Read the summary and the change list. It should match what you did.
7. Click **Sync**.

**Expect:** a **"Synced"** box: `N change(s) sent to the portal.`

8. **Verify:** refresh the Browser tree, re-add the layer, and confirm
   your edits are there.
9. **Open the dialog again.** It should now show nothing pending: the
   clone and the portal agree.

### Worth trying deliberately

- **Leave edits unsaved** and open the dialog. It should tell you to
  save first rather than sending half your work.
- **The conflict path.** Edit a feature in your clone and save. Then
  change the *same* feature on the portal (in the web app). Now sync.
  You should get a warning naming the conflict and offering
  **Overwrite with mine** / **Skip those, send the rest** / Cancel.
  This is the case that used to overwrite silently.
- **Open the dialog with only a vector-tile layer** in the project. The
  dropdown should read `(no editable portal layers in project)` and
  point you at the clone flow.

---

## Test 6. Publish a raster from your map (10 min)

**Rewritten in 0.4.0.** There is no separate raster menu entry any
more, and no file hunting: the raster on your canvas is in the list.

1. Get a small GeoTIFF onto your canvas. If you have nothing handy,
   make one: right-click any raster in QGIS > `Export > Save As...` >
   format `GeoTIFF`, shrink the resolution so it stays a few MB, and
   let QGIS add the result to your project.
2. `Plugins > GratisGIS > Publish layer to GratisGIS...`
3. **Layer:** your GeoTIFF should be in the list, alongside the vector
   layers. Pick it.

**This is the point of the change.** You should not have to know where
the file is, and you should not have to pick a different menu item
because it is a raster rather than a vector.

4. **Title** auto-fills. **Portal:** your connection. Leave Access as
   Private.
5. Pre-flight will show a **warning** for GeoTIFF: "This format needs
   server-side conversion to PMTiles..." Expected, does not block.
6. Click **Publish**.

**Expect:** a progress bar that moves during the upload, then a
**"Published"** box with the item id and a note that conversion is
queued.

7. **Wait a few minutes**, then refresh `My Content > Tile layers` in
   the Browser tree and drag the new item to the canvas. It should
   draw.

### Worth trying deliberately

- **A layer that cannot be published.** Add a basemap (or any XYZ / WMS
  layer) and open the dialog. It should be **listed** and marked
  `(cannot be published)`, and selecting it should explain that it
  streams from a web service and suggest exporting it first. It should
  not silently vanish from the list.
- **The same for a portal layer** you dragged from the Browser tree.
  It is read straight from the portal, so there is no local file.
- **"Choose a file instead..."** for a GeoTIFF that is NOT in your
  project. That is the old flow, kept as the escape hatch.
- **Cancel** during a large upload: expect "Publish cancelled." and no
  half-made item left on the portal.

---

## Test 7. Publish the project as a map (5 min)

1. Build a small project: add 2 or 3 **portal** layers from the Browser
   tree, plus one basemap.
2. `Plugins > GratisGIS > Publish current project as GratisGIS map...`
3. Wait for "Checking which layers are on the portal..." to finish.
4. Read both lists. Portal-backed layers appear under **"Layers
   included in the map"**; anything local appears under **"Layers not
   on the portal"** with a reason.
5. **Map title** defaults to the project title. Click **Publish**.

**Expect:** a **"Published"** box with the map item id.

6. **Verify:** open that map item in the portal web app and confirm the
   layers and the viewport match what you had in QGIS.

**Also worth testing:** add a plain local file layer (a shapefile from
disk) and confirm it lands in the skipped list with the explanation
about publishing it first, and that the per-row **"Publish as data
layer..."** button opens the publish dialog with that layer already
selected.

---

## Test 8. Sign out and back in (3 min)

This exercises the credential lifecycle, which is easy to get wrong and
invisible until it breaks.

1. `Plugins > GratisGIS > Manage GratisGIS connections...`
2. **Sign out.**
3. Refresh the Browser tree. Private and org layers should stop
   drawing. *(Expected: the read-only key is revoked at sign-out.)*
4. **Sign in** again.
5. Re-add a **private** layer and confirm it draws again.

If step 5 fails, that is the credential re-mint path and I want the
log.

---

## What to send me if something fails

1. A screenshot of the error box (the exact wording matters).
2. The last ~40 lines of:
   `C:\Users\matt\AppData\Roaming\QGIS\QGIS4\gratisgis\logs\plugin.log`
3. Which test number and step.

That is enough for me to reproduce almost anything without guessing.

---

## Known gaps (expected, not bugs)

- **Layers added from the Browser tree cannot be edited in place.**
  Spatial data draws as vector tiles, which QGIS treats as a read-only
  rendering format. Clone it (Test 4) and edit the clone (Test 5).
- **Private non-spatial tables do not draw.** Tables with no geometry
  render through a public-only surface, so a private one lists but
  stays empty. Clone it to work with it.
- **The publish dialogs do not add the layer to your canvas** on
  success. Only the clone dialog does.
- **The project-publish dialog has no Access setting.** The new map
  item takes the portal's default access.
- **Raster and tile layers still zoom to the whole world.** The portal
  does not record an extent for that item type yet, so they have none
  to carry. Data layers do. Portal-side fix, not a plugin one.
- **Two contour layers on the demo are genuinely empty** (0 features),
  so they correctly draw nothing: "Contour lines (Elkins 2018 Lidar
  elevation (2m))" and "outlines".
