# QGIS plugin test playbook

Hands-on checks for the parts of the plugin that automated tests cannot
reach: the interactive flows. Work top to bottom, they build on each
other. Roughly 45 minutes for all of it, and any single test stands
alone if you only have ten minutes.

Everything here targets the public demo at https://gratisgis.org.

---

## Before you start

1. **Check the version.** `Plugins > Manage and Install Plugins >
   Installed`, find GratisGIS. It should read **0.2.5**.
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

5. Set **Type** to `Tile layer`, search, double-click **"WV lidar
   terrain hillshade (terrain, 1m)"**.

**Expect:** the hillshade draws. Zoom to the layer (right-click it in
Layers, `Zoom to Layer`) since it covers a small area.

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

5. `Plugins > GratisGIS > Publish vector layer to GratisGIS...`
6. **Layer:** pick `qgis_test`. **Portal:** the GratisGIS connection.
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

**Worth trying deliberately:** publish a layer with **no CRS set** and
confirm pre-flight blocks it with "Layer has no CRS defined."

---

## Test 4. Clone a layer for offline use (5 min)

1. First put a portal data layer on the canvas: Browser tree >
   `Org Content > Data layers`, expand **Trails**, drag its sublayer to
   the canvas. *(Trails is small, 43 features.)*
2. `Plugins > GratisGIS > Clone layer for offline use...`
3. **Layer:** pick the Trails layer. **Portal:** your connection.
4. Click **Choose directory...**, pick anywhere writable.
5. **File name** auto-fills. Leave it. Do not type `.gpkg`, it is added
   for you.
6. Pre-flight should be clean. Click **Clone**.

**Expect:** an indeterminate progress bar, then a **"Cloned"** box
naming the feature count and full path, and a new layer in your project
called **"Trails (offline)"** that draws.

7. **Run it a second time with the same name.** You should get an
   **"Overwrite?"** prompt. Say Yes and confirm it still works. *(This
   path used to delete your existing file before writing the new one;
   it now writes to a temp file and swaps, so a failure cannot destroy
   the old copy.)*

---

## Test 5. Push edits (10 min, trickiest)

Read the setup carefully. The dialog only accepts a specific kind of
layer, and only while edits are **unsaved**.

### Setup that actually qualifies

1. Add a data layer **from the Browser tree or Search panel** (not from
   QGIS's own OGC API dialog). Use your `qgis_test` item from Test 3,
   or Trails.
   - It must be the **OAPIF** sublayer, the one QGIS lists as a plain
     vector layer. If you dragged a spatial sublayer you may have got a
     vector-tile layer instead, which cannot be edited. If the push
     dialog does not list your layer, this is why.
2. Click the **pencil** to toggle editing on that layer.
3. Make a few edits: move a vertex, change an attribute, add a feature,
   delete one.
4. **Do NOT click Save Edits.** The dialog reads QGIS's pending edit
   buffer; saving empties it.

### Push

5. `Plugins > GratisGIS > Push edits to GratisGIS...`
6. **Layer:** your edited layer. **Portal:** your connection.
7. Read the summary line: `N create(s), N update(s), N delete(s);
   N skipped.` and the operations list.
8. Click **Push**.

**Expect:** the summary cycles "Pushing operation 1 of N...", then a
**"Pushed"** box: `N operations pushed successfully.`

9. **Verify:** open the item in the portal (or refresh the Browser tree
   and re-add the layer) and confirm your edits are there.

**Known gap, do not report as a bug:** you cannot push edits from the
**offline clone** made in Test 4. The clone is a GeoPackage on disk, and
this dialog only accepts live portal layers, so the clone will not
appear in the Layer dropdown. Round-tripping an offline edit is a real
missing feature, not a defect.

---

## Test 6. Publish a raster / tile layer (10 min)

This one uploads a **file from disk**, not a canvas layer. The layer
picker is a file picker.

1. Find a small GeoTIFF. Anything a few MB is ideal for a first run.
   If you have nothing handy, export one: right-click any raster in
   QGIS > `Export > Save As...` > format `GeoTIFF`, and shrink the
   resolution so the file stays small.
2. `Plugins > GratisGIS > Publish raster / tile layer to GratisGIS...`
3. Click **Choose file...**, pick your GeoTIFF.
4. **Title** auto-fills from the file name. **Portal:** your
   connection. Leave Access as Private.
5. Pre-flight will show a **warning** for GeoTIFF:
   "This format needs server-side conversion to PMTiles..." That is
   expected and does not block.
6. Click **Publish**.

**Expect:** a progress bar that actually moves during the upload, then
a **"Published"** box with the item id, plus a note that conversion is
queued.

7. **Wait a few minutes**, then refresh `My Content > Tile layers` in
   the Browser tree and drag the new item to the canvas. It should
   draw. *(This exercises the whole pyramid pipeline plus the tile
   routing I just fixed.)*

**Worth trying:** click **Cancel** during a large upload and confirm
you get "Publish cancelled." and no half-made item is left behind in
the portal.

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

- **Offline clones cannot be pushed back.** See Test 5.
- **Private non-spatial tables do not draw.** Tables with no geometry
  render through a public-only surface, so a private one lists but
  stays empty. Clone it to work with it.
- **The publish dialogs do not add the layer to your canvas** on
  success. Only the clone dialog does.
- **The project-publish dialog has no Access setting.** The new map
  item takes the portal's default access.
- **Two contour layers on the demo are genuinely empty** (0 features),
  so they correctly draw nothing: "Contour lines (Elkins 2018 Lidar
  elevation (2m))" and "outlines".
