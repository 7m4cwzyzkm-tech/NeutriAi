# Mobile camera screen & build survey — 21 Sep 2026

Documentation only. No code, dependency, or build changes. Every claim below
names a file and line; where nothing was found, that is stated explicitly
along with what was searched.

Scope: what exists today in `mobile/` for the camera/scan flow, how a photo
and its metadata reach the backend, how the app is built and run, what
sensors are available, the state of the mobile test suite, and what the
smallest safe first steps would be to wire in `backend/app/services/ai/card_gauge.py`
(branch `camera-gauge-axis-fix`).

---

## 1. The camera/scan screen

**One screen file only**: `mobile/src/screens/ScanScreen.tsx` (394 lines). No
separate camera-wrapper or overlay component exists elsewhere in `mobile/src`
(grepped for "camera"/"overlay"/"guide" case-insensitively).

- **Camera library**: `expo-camera`, not `react-native-vision-camera` (no
  reference to the latter anywhere in `mobile/src`). Import at
  `ScanScreen.tsx:11`: `import { CameraView, useCameraPermissions } from 'expo-camera';`.
  Component usage: `:28` (`useCameraPermissions()`), `:34`
  (`cameraRef = React.useRef<CameraView>(null)`), rendered at `:321`:
  `<CameraView ref={cameraRef} style={{ flex: 1 }} facing="back" />`.
- **Capture handler** — `capture()`, `ScanScreen.tsx:43-56`:
  ```
  async function capture() {
    const [photo, measured] = await Promise.all([
      cameraRef.current?.takePictureAsync({ quality: 0.8 }),
      measureCameraGeometry(),
    ]);
    if (photo?.uri) {
      setShots((s) => [...s, photo.uri].slice(0, 3));
      setGeometry((g) => (Object.keys(g).length ? g : measured));
    }
  }
  ```
  Capture (`takePictureAsync`) runs in parallel with a geometry read via
  `measureCameraGeometry()` (`mobile/src/native/depth.ts`, imported at
  `ScanScreen.tsx:12`). A separate library-picker path, `pickFromLibrary()`
  (`:58-64`), uses `expo-image-picker`.
- **On-screen guide/overlay** — two plain RN `View`s, no SVG (no
  `react-native-svg` import in this file, despite the package being a
  dependency elsewhere in the app):
  1. An oval framing guide, `ScanScreen.tsx:325-331` (a bordered `View`,
     `borderRadius: 200`, positioned by percentage insets).
  2. **The card box, `ScanScreen.tsx:336-348`**, gated by `needCard`:
     ```
     {needCard ? (
       <View style={{
         position: 'absolute', bottom: '34%', alignSelf: 'center',
         width: 132, height: 83, borderRadius: 8,
         borderWidth: 2, borderStyle: 'dashed', borderColor: c.warn,
         ...
       }}>
         <Text ...>card here</Text>
       </View>
     ) : null}
     ```
     **Confirmed still hardcoded at 132 × 83 pt**, unchanged from earlier
     sessions' reports. It is conditional, not always shown — `needCard` is
     set by a "Retake with a card" action after a `portion_measured === false`
     result (`:181`), reset elsewhere (`:41`, `:136`, `:182`). A comment at
     `:332-335` explains the 85.6 mm bank-card rationale for the box, but the
     box's own pixel size does not derive from any device's actual field of
     view or distance — it is one fixed size for every phone.
- **Camera intrinsics / FOV / distance already present**: yes, but only via
  `mobile/src/native/depth.ts` (imported at `ScanScreen.tsx:12`, used at
  `:37`, `:49`). This module:
  - Defines `CameraGeometry` (`depth.ts:55-59`): `camera_distance_mm?`,
    `camera_fov_deg?`, `camera_aspect_ratio?`.
  - Bounds (`:62-65`): `DISTANCE_MIN_MM=80`, `DISTANCE_MAX_MM=2000`,
    `FOV_MIN_DEG=40`, `FOV_MAX_DEG=100` — matching the backend's
    `ScanRequest` field bounds (see §2).
  - Reads an optional native module `NativeModules.NeutriDepth` (`:75-76`)
    that **is not implemented anywhere in this repo** — there is no
    `ios/`/`android/` native project directory under `mobile/` at all (a
    managed Expo project, no checked-in prebuild output), and no file
    matching `*NeutriDepth*` exists. The module's own docstring (`:1-52`,
    specifically `:42-47`) describes ARKit/ARCore work that **would need to
    be written**, not something already there.
  - Practical consequence: `isDepthAvailable()` (`:79-81`) is always false
    on this codebase as it stands, and `measureCameraGeometry()`
    (`:96-123`, 400 ms timeout, never throws) always resolves to `{}` today.
    `capture()`'s `measured` is therefore always an empty object in
    practice.

**Verdict**: the screen has a UI slot and a typed data path for camera
geometry, but nothing today actually measures distance, FOV, or tilt on the
device — the native bridge is a stub with no backing implementation, and the
card box is a single fixed-size rectangle, not derived from geometry at all.

---

## 2. Photo and metadata reaching the backend

**Image upload** goes to Supabase Storage directly, not through the FastAPI
backend: `uploadImage()`, `mobile/src/api/supabase.ts:146-167` —
`supabase.storage.from(bucket).upload(path, bytes, {...})` (`:161-164`).
Called from `ScanScreen.tsx:70`:
`const paths = await Promise.all(shots.map((uri) => uploadImage('meal-photos', uri)));`.
Before upload, `prepareImage()` (`supabase.ts:88-107`) may downscale via
`expo-image-manipulator` past `MAX_EDGE_PX = 1568` (`:61`), and internally
reads width/height via `measure()` (`:70-81`) — **but this width/height is
used only locally to decide resizing and is never sent to the backend.**

**The scan-analysis call** (the one that reaches the FastAPI backend) is a
plain JSON POST, not multipart — `mobile/src/api/client.ts:122-132`:
```
nutrition: {
  scan: (payload: {
    image_paths: string[]; meal_slot?: string; calibration_id?: string;
    plate_diameter_mm?: number; camera_distance_mm?: number;
    camera_fov_deg?: number; camera_aspect_ratio?: number;
  }) => request<ScanResult>('/scans', { method: 'POST', body: payload, ... }),
```
`request()` (`client.ts:44-99`) JSON-stringifies the body (`:58`) with
`Content-Type: application/json` (`:55`). Called via `useScanMeal()`
(`mobile/src/hooks/useApi.ts:116-127`), invoked from `ScanScreen.tsx:71-75`:
```
const res = await scan.mutateAsync({
  image_paths: paths, meal_slot: slot ?? undefined, ...geometry,
});
```

**Fields actually sent today**: `image_paths` (Storage keys, not bytes),
`meal_slot`, and whatever `geometry` contains — `camera_distance_mm` /
`camera_fov_deg` / `camera_aspect_ratio`, which per §1 is `{}` in practice
today. **`plate_diameter_mm` and `calibration_id` are declared in the
client's own type signature (`client.ts:124`) but `ScanScreen.tsx` never
passes them** — the UI simply doesn't populate fields the client already
knows how to send. **No image width/height and no EXIF/orientation metadata
are sent, today, at all.**

**Backend receiver**: `POST /scans`, `backend/app/routers/scans.py:42-91`,
route decorator at `:42`, body validated as `ScanRequest`. The model,
`backend/app/models/nutrition.py:10-35`:
- `image_paths: list[str]` — `:13`
- `meal_slot: MealSlot | None` — `:14`
- `calibration_id: str | None` — `:15`
- `note: str | None` — `:16`
- `plate_diameter_mm: float | None`, bounds 60-400 — `:18`
- `camera_distance_mm: float | None`, bounds 80-2000 — `:26`
- `camera_fov_deg: float | None`, bounds 40-100 — `:27`
- `camera_aspect_ratio: float | None`, bounds 0.5-2.5 — `:28`
- `measure_footprints: bool = False` — `:35`

No `image_width_px`/`image_height_px`/orientation field exists in this model
at all. `scans.py:75-90` passes `body.camera_distance_mm`,
`.camera_fov_deg`, `.camera_aspect_ratio`, `.plate_diameter_mm`,
`.calibration_id`, `.measure_footprints` into `vision.run_scan(...)`.

**Verdict**: the backend already has a typed, bounded contract for exactly
the numbers `card_gauge.py` would produce (distance, FOV, aspect ratio) —
the wiring gap is entirely on the mobile side (nothing computes real values
to put in those fields), plus the backend model has no width/height/
orientation field to add if that were ever wanted.

---

## 3. Build and run process

**Framework**: Expo (managed workflow — no `ios/`/`android/` folders under
`mobile/`). `mobile/app.json:2-3` (`"expo": { "name": "NeutriAI", "slug":
"neutriai", ...`).

**Version discrepancy, worth flagging**: root `README.md:9` says "React
Native (Expo 52) app," but `mobile/package.json:19` pins `"expo": "^57.0.0"`
and `:36` pins `"react-native": "0.86.3"` — the README is stale against the
actual lockfile.

**Package manager**: npm (`mobile/package-lock.json` exists; no
`yarn.lock`/`pnpm-lock.yaml` at the mobile root).

**Documented commands** — quoted, not paraphrased:
- Root `README.md:66-71`:
  ```
  cd ../mobile
  npm install
  # set supabaseUrl / supabaseAnonKey / apiUrl in app.json -> expo.extra
  npx expo start
  ```
- `mobile/package.json:5-11` scripts: `"start": "expo start"`, `"ios":
  "expo run:ios"`, `"android": "expo run:android"`, `"typecheck": "tsc
  --noEmit"`, `"lint": "eslint src --ext .ts,.tsx"`.
- `docs/RUNBOOK.md:450-456` (§5.2): the same `npm install` / `npx expo
  start`, then (`:458`) "Press `i` for the iOS simulator, `a` for Android,
  or scan the QR code."
- `docs/RUNBOOK.md:460-467`: the one documented path toward a native build,
  framed around HealthKit, not general device debugging:
  ```
  For HealthKit you need a native build, not Expo Go:
  npx expo prebuild --clean
  npx expo run:ios
  ```
  "Expo Go cannot load `react-native-health`. Everything *except* Apple
  Health works in Expo Go, so start there and prebuild when you get to
  wearables." (`:467`)
- `docs/RUNBOOK.md:436-444` (§5.1) is the one place that discusses testing
  on a **physical** device specifically — networking, not building:
  "`apiUrl` is the one that trips everyone. `localhost` means *the phone*
  on a physical device... Use your machine's LAN IP" (`ipconfig getifaddr
  en0`); "The iOS simulator alone can use `localhost`. A physical device
  never can." (`:444`)

**No `mobile/README.md` exists.** No `eas.json` exists anywhere under
`mobile/` — the `--profile production` referenced in `docs/DEPLOYMENT.md:156-157`
and `docs/RUNBOOK.md:1114-1115` has no corresponding profile checked into
the repo, and neither a `development` nor an `ad hoc` EAS profile is
documented anywhere. `dev.bat` has zero mobile-related commands (grepped,
0 hits) — it is exclusively a backend testing/benchmark script.

**THE ANSWER GIL MOST NEEDS: the repo does not document a specific
procedure for installing a debug build onto a physical iPhone**, as
distinct from the simulator or Expo Go. The closest things written down are
`npx expo run:ios` (`mobile/package.json:7`) and `npx expo prebuild --clean`
(`docs/RUNBOOK.md:463`) — both documented only in the context of getting
HealthKit's native module to load, not as a general "test a change on your
phone" workflow. Neither line says how to select a connected physical
device over the simulator, or anything about Xcode device pairing. This is
reported as a gap, not filled in with generic Expo knowledge, per the task's
own instruction.

**Apple Developer account / TestFlight — not documented in the repo.** The
phrase "Apple Developer" (account) appears nowhere in the repo (checked all
of `docs/*.md`, root `README.md`, `mobile/`). "TestFlight" appears exactly
once, `docs/RUNBOOK.md:1027`, as a shell comment in the IAP-sandbox-testing
section ("Sign out of the App Store on device, build via TestFlight,
buy.") — about purchase testing, not general debug testing, and it does not
say whether a paid account is needed. "Provisioning profile" appears twice,
both about the HealthKit entitlement for **store submission**
(`docs/DEPLOYMENT.md:98-100`, `docs/RUNBOOK.md:1127`), not local debugging.
Every EAS/provisioning/TestFlight mention in the repo sits under a
production-deployment or store-submission section (`docs/DEPLOYMENT.md`
§"6. Mobile", `docs/RUNBOOK.md` §10.5/§10.6) — none of it is framed as "what
you need to test a change on your own phone." **This repo simply does not
say**, one way or the other, whether local debug testing on a physical
iPhone via `expo run:ios`/prebuild requires a paid Apple Developer Program
enrollment (Apple's own free-account on-device testing exists in general,
but nothing in this repo states or confirms that applies here) — reported
as undocumented rather than answered from outside knowledge.

---

## 4. Device sensors (tilt / motion)

**Not found.** Grepped all of `mobile/` case-insensitively for
`accelerometer`, `gyroscope`, `DeviceMotion`, `expo-sensors`,
`Magnetometer`, `Barometer`, `tilt`, and `Motion` — zero matches anywhere in
`mobile/src`. `expo-sensors` is not a `mobile/package.json` dependency.

The only motion/geometry-adjacent code is the speculative `NeutriDepth`
native-module bridge in `mobile/src/native/depth.ts` (§1), which has no
native implementation anywhere in this repo and whose own docstring
(`:41-47`) describes ARKit/ARCore work as future, not present. **There is no
accelerometer- or gyroscope-based tilt detection anywhere in this
codebase.** This matters directly for wiring in `card_gauge.gauge_state()`,
whose third parameter is `tilt_deg` (`card_gauge.py:236`) — nothing on the
mobile side produces that number today.

---

## 5. Mobile test suite

**Not found.** `mobile/package.json`'s `"scripts"` block (`:5-11`) has only
`start`, `ios`, `android`, `typecheck`, `lint` — no `"test"` script. No Jest
config exists anywhere under `mobile/` (`jest.config*`/`jest.setup*`
searched, 0 results). No `*.test.ts`/`*.test.tsx` file exists anywhere
under `mobile/` (searched, 0 results). `jest` and
`@testing-library/react-native` are absent from both dependencies and
devDependencies. It does not run under `.\dev test` (that command is
backend-only — `dev.bat` has no mobile entry at all, per §3) or separately;
there is nothing to run.

---

## 6. Smallest safe first steps to wire in `card_gauge.py`

Nothing below was built. Two sentences each.

**(a) The 12-inch distance/tilt guide overlay.** Replace `ScanScreen.tsx`'s
fixed 132×83 pt box (`:336-348`) with one sized by calling
`expected_card_px_portrait(image_width_px, image_height_px, long_fov_deg)`
(ported to TypeScript, or exposed some other way — this survey does not
choose that) using the frame's actual reported dimensions and
`portion.DEFAULT_CAMERA_FOV_DEG`'s value as the default FOV, then colour
the guide live via `gauge_state(measured_card_px, expected_px, tilt_deg)`.
The blocking gap is `tilt_deg`: nothing on the mobile side measures tilt
today (§4), so this step is really two — a static, correctly-sized box
first (needs only image dimensions, which the camera already reports), and
tilt-aware colouring second (needs a new sensor read, e.g. via a package
like `expo-sensors`' `DeviceMotion`, not present in this repo today).

**(b) Sending image width/height/orientation and the gauge's distance/tilt
reading with each upload.** Extend the `scan()` payload type in
`client.ts:122-132` and the backend's `ScanRequest`
(`backend/app/models/nutrition.py:10-35`) with new optional fields
(`image_width_px`, `image_height_px`, and whatever the gauge computes for
distance/tilt), populated in `ScanScreen.tsx`'s existing `geometry` object
next to `camera_distance_mm` (already sent when available). This is
additive on both ends — new optional fields, existing bounds pattern
(`ge`/`le` on the Pydantic model) — and does not require the native
`NeutriDepth` bridge at all, since image width/height are already available
locally from the capture/prepare step (`supabase.ts:70-81`) even though
they're currently discarded after resizing decisions.

---

## 7. Tests

Ran `pytest -q` from `backend/` (no Windows `.\dev` in this Linux cloud
container; this is the equivalent check per this repo's own tooling notes):

```
942 passed, 2 failed in 49.36s
FAILED tests/test_wiring.py::test_every_bench_case_has_a_photo_and_a_weight
FAILED tests/test_wiring.py::test_the_bench_checks_what_is_free_before_spending_anything
```

942 + 2 = 944, matching the task's stated main baseline (944 passed, 0
failed on Gil's machine) exactly, with the two known photo-dependent
failures accounted for (this container has no bench photos). This branch
is a docs-only change from `main`, so no test-count movement is expected or
seen. `camera-gauge-axis-fix`'s own baseline (967 passed, 0 failed) was not
re-run here — this survey did not touch or build against that branch.

---

## Not verified / could not check

- Whether local debug testing on a physical iPhone via `expo run:ios`/
  `prebuild` requires a paid Apple Developer Program account — genuinely
  undocumented in this repo, not merely unfound by a narrow search (see §3).
- Whether Expo's own SDK 57 general documentation (outside this repo)
  states a free-tier path for on-device debug builds — out of scope for
  this survey, which reports only what this repo itself says.
- Live behavior of `expo start` / `expo run:ios` was not exercised (hard
  rule: no build/run commands), so this is a static-code and docs survey
  only, not a confirmation that the documented commands currently work
  without error on this exact dependency set.
- `mobile/app.config.js`'s and `.env.example`'s env-var *names* were seen in
  passing (not their values) while researching `expo.extra` configuration;
  no `.env*` file content was read or quoted, per the hard rule.

---

## For Gil

The load-bearing finding: **the mobile side of the card gauge is almost
entirely unbuilt.** `mobile/src/native/depth.ts` already has the right
shape (a `CameraGeometry` type matching the backend's `ScanRequest` fields
exactly) but resolves to `{}` in practice, because `NeutriDepth` is a bridge
to a native module that was never written — there's no `ios/`/`android/`
project in this repo at all. The card box on screen is one fixed 132×83 pt
rectangle for every phone, not derived from any device's real optics. And
there is no tilt sensor anywhere, which `card_gauge.gauge_state()` needs.
None of this blocks a *static*, correctly-sized guide (step 6a's first
half) — that only needs the frame's own reported width/height, which the
camera already has — but a live, tilt-aware gauge needs a new sensor
integration this codebase doesn't have yet.

Second, separately actionable finding: the repo's own build docs have a
real gap for exactly the question "how do I test a change on my own
iPhone" — worth writing down properly once, rather than re-discovering it
each session.
