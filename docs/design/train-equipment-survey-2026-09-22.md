# Train / equipment-scan survey — 22 Sep 2026

Read-only survey, no code changes. Every claim below names the file and line it
came from. Where I could not determine something by static reading alone, it
is stated as "not found" or "needs a runtime check," not guessed.

Scope: mobile equipment-scan capture flow, the backend endpoint it calls,
plan generation, and what exists today for multi-photo / video capture.

---

## 1. Mobile: Train screen and equipment-scan flow

**One screen, one file:** `mobile/src/screens/TrainScreen.tsx` (219 lines).
No other screen or component touches equipment scanning (checked with a
repo-wide grep for `Train`, `equipment`, `scanEquipment`, `EquipmentScan` —
the only hits outside this file are the API client, the hooks file, the type
definitions, and navigation wiring for the tab itself).

**Capture** — `TrainScreen.tsx:32-51`, function `scanGym()`:

```
32  async function scanGym() {
33    const res = await ImagePicker.launchCameraAsync({ quality: 0.8 });
34    if (res.canceled || !res.assets[0]) return;
35    setScanning(true);
36    try {
37      const path = await uploadImage('equipment-photos', res.assets[0].uri);
38      const detected = await api.fitness.scanEquipment([path]);
39      setEquipment(detected);
40      if (detected.fallback_to_calisthenics) {
41        Alert.alert('No equipment spotted', ...);
42      }
43    } catch (e: any) {
44      if (!e?.needsUpgrade) Alert.alert('Scan failed', e?.message ?? 'Try again.');
45    } finally {
46      setScanning(false);
47    }
48  }
```

- Uses `expo-image-picker`'s `launchCameraAsync` (system camera UI), **not**
  the custom `CameraView` the food-scan tab uses in `ScanScreen.tsx`. This is
  a second, independent capture path with its own permission handling (or
  lack of it — see §3).
- Exactly **one photo per call.** `res.assets[0]` is read and everything
  else in `res.assets` is discarded if the picker ever returned more than
  one (it won't here, since no `allowsMultipleSelection` option is passed).
- No `mediaTypes` option is passed to `launchCameraAsync`, so it defaults to
  still-image capture only. No video option exists in this call.

**Submit** — `TrainScreen.tsx:37-38`:
1. `uploadImage('equipment-photos', uri)` — defined in
   `mobile/src/api/supabase.ts:146-167`. Uploads straight to Supabase
   Storage from the phone (not through the API), downscaling to a 1568px
   long edge JPEG first if needed (`supabase.ts:61, 88-107`). Returns the
   storage path, not a URL.
2. `api.fitness.scanEquipment([path])` — defined in
   `mobile/src/api/client.ts:175-178`:
   ```
   175  scanEquipment: (image_paths: string[], space_note?: string) =>
   176    request<EquipmentScan>('/equipment/scan', {
   177      method: 'POST', body: { image_paths, space_note }, timeoutMs: 45000, retries: 0,
   178    }),
   ```
   Exact payload: `POST /equipment/scan`, JSON body
   `{ image_paths: [<one storage path>], space_note: undefined }`
   (`space_note` is never collected anywhere in `TrainScreen.tsx` — there is
   no UI for it). 45s timeout, no retry on failure (`retries: 0`).

**Expected response** — `mobile/src/api/types.ts:138-142`:
```
138  export interface EquipmentScan {
139    id: string; equipment: string[];
140    detected: { equipment: string; detail: string; confidence: number }[];
141    confidence: number; fallback_to_calisthenics: boolean;
142  }
```

**Result display** — `TrainScreen.tsx:94-109`: chips for `equipment.equipment`
and a bullet list of `equipment.detected[].detail`, shown only after a
successful scan (i.e. the request resolved with HTTP 2xx — see §3 for what
"successful" actually covers).

---

## 2. Backend: the equipment-scan endpoint

**Route** — `backend/app/routers/fitness.py:164-177`:
```
164  @router.post("/equipment/scan", response_model=EquipmentScanOut, status_code=201)
165  async def scan_equipment(body: EquipmentScanIn, user: CurrentUserDep, _q: AiScanDep):
166      result = await coach.scan_equipment(user.id, body.image_paths, body.space_note)
167      row = one(
168          service().table("equipment_scans").insert({...}).execute()
169      )
170      return EquipmentScanOut(...)
```

**Request model** — `backend/app/models/fitness.py:51-53`:
```
51  class EquipmentScanIn(InputBase):
52      image_paths: list[str] = Field(min_length=1, max_length=4)
53      space_note: str | None = Field(None, max_length=200)
```
`max_length=4` — **the backend already accepts up to 4 image paths per
scan.** This matters for task 5 below: multi-photo support is not missing
server-side, only unused by the mobile UI.

**Response model** — `backend/app/models/fitness.py:56-61` (matches the
mobile `EquipmentScan` type field-for-field).

**What the endpoint actually does** — `backend/app/services/ai/coach.py:36-71`,
`scan_equipment()`:
```
36  async def scan_equipment(user_id, image_paths, space_note) -> dict:
37      # Equipment detection has no geometry, so only the encoded image is used.
38      images = [p.b64 for p in await fetch_images("equipment-photos", image_paths)]
39      if not images:
40          return {"equipment": ["none"], "detected": [], "confidence": 0.0,
41                  "space_note": space_note or "", "fallback": True}
42
43      call = await ask_vision(
44          pipeline="equipment_detection",
45          system=EQUIPMENT_VISION_SYSTEM,
46          user_text=...,
47          images=images,
48          max_tokens=1400,
49      )
50      await record_usage(call, user_id)
51
52      if not call.ok or not isinstance(call.payload, dict):
53          return {"equipment": ["none"], "detected": [], "confidence": 0.0,
54                  "space_note": space_note or "", "fallback": True}
55      ...
```

**Equipment recognition is implemented**, not stubbed — it calls a real
external model: **OpenAI, `gpt-4o` by default** (`settings.vision_model`,
`backend/app/config.py:31`), via `ask_vision()` ->
`_openai_vision()` in `backend/app/services/ai/client.py:118-143`, using
`AsyncOpenAI(api_key=settings.openai_api_key, ...)` (`client.py:100-103`).
I did not open `.env` or print the key; `settings.openai_api_key` defaults
to `""` in `config.py:30` if unset, per the hard rule against printing
secrets I only confirmed the setting name exists, not its value.

The prompt (`EQUIPMENT_VISION_SYSTEM`, `backend/app/services/ai/prompts.py:396-422`)
asks for an `equipment` value from the exact same 18-item set as
`VALID_EQUIPMENT` in `coach.py:25-30` — the two lists match, so a
model/backend vocabulary mismatch is not a plausible cause on its own.

---

## 3. Why "it's not scanning my picture" — best-evidenced explanation

**The core defect: every failure mode collapses into the same "no equipment
found" response, and the app cannot tell them apart.**

`coach.scan_equipment()` has exactly two exit paths, and both return HTTP
201 with `fallback: True` / `equipment: ["none"]` — never an error:

1. **`coach.py:38-41`** — if `fetch_images()` returns nothing. And
   `fetch_images()` (`backend/app/services/ai/vision.py:171-183`) silently
   drops any image it can't download:
   ```
   175  def _one(path: str) -> PreparedImage | None:
   176      try:
   177          return downscale_jpeg(sb.storage.from_(bucket).download(path))
   178      except Exception as exc:  # noqa: BLE001
   179          log.warning("image_fetch_failed", path=path, error=str(exc)[:200])
   180          return None
   ```
   A failed Storage download (wrong bucket permissions, a path that didn't
   finish uploading, a transient Supabase error) never reaches the mobile
   app as an error — it reaches it as "no equipment."

2. **`coach.py:52-54`** — if the vision call itself fails
   (`call.ok == False`) or returns something that isn't a JSON object.
   `ask_vision()` (`client.py:201-221`) catches every exception from the
   OpenAI call — auth failure, network error, malformed response, model
   refusal — logs it with `log.warning("vision_failed", ...)`
   (`client.py:219`), and returns `call.ok = False`. Nothing in
   `scan_equipment()` re-raises or distinguishes this from "the model
   looked and saw nothing." **A missing or invalid `OPENAI_API_KEY` would
   produce exactly this path** — an auth error is not one of the strings
   `_openai_vision()` treats as transient (`"rate limit"`, `"timeout"`,
   `"overloaded"`, `"503"` — `client.py:141`), so it isn't even retried; it
   fails once and is swallowed the same as everything else.

On the mobile side, `TrainScreen.tsx:39-45` treats this fallback response
as a **successful** scan (the `try` block does not throw) and shows
`Alert.alert('No equipment spotted', "That's fine — NeutriAI will build a
bodyweight programme...")`. To someone standing in a room full of
equipment, this reads as "the scan ran and decided my gym is empty," which
is functionally indistinguishable from "the scan never really worked" —
matching Gil's description.

**A second, independent defect that can also produce total silence:**
`TrainScreen.tsx:43-44`:
```
43  } catch (e: any) {
44    if (!e?.needsUpgrade) Alert.alert('Scan failed', e?.message ?? 'Try again.');
```
If the request throws an `ApiError` with `needsUpgrade` true (HTTP 402, or
`code === 'quota_exceeded'` — `mobile/src/api/types.ts:201-204`), **no
alert shows at all**, and — unlike the food-scan and plan-creation flows —
nothing opens the paywall either. `usePaywallOnError()`
(`mobile/src/hooks/useApi.ts:24-32`) is wired into `useScanMeal`
(`useApi.ts:116-127`) and `useCreatePlan` (`useApi.ts:169-177`) via
`useMutation`'s `onError`, but `scanGym()` is a plain async function, not a
`useMutation`, and never calls `usePaywallOnError` or `openPaywall`. The
quota gate itself is real and shared: `POST /equipment/scan` depends on
`AiScanDep` (`fitness.py:165`) -> `consume_ai_scan()`
(`backend/app/deps.py:103-133`), the *same* metered daily-scan allowance
used by the food-scan endpoint (`QuotaExceeded` raised at `deps.py:132-133`
maps to HTTP 402 — confirmed the mapping exists in the router error
handling, not reproduced here in full). If Gil's account had already used
its daily AI-scan allowance on food scans before trying equipment scanning,
tapping "Scan my equipment" would spin briefly and then do **nothing
visible at all** — no alert, no paywall, no result.

**What I could not determine from static reading alone — needs a runtime
check:**
- Whether Gil's OpenAI key is valid/configured at all (cannot open `.env`
  per hard rule; the setting exists and is read correctly, but its value is
  out of scope for this survey).
- Which of the two failure paths above actually fired on his phone. The
  fix for each is different: an invalid/missing key or the "silent
  paywall" gap are code/config fixes; a real Supabase Storage upload
  failure is an infra/permissions question. **The single most useful
  runtime check:** look at the backend log line `vision_failed` (or absent
  entirely, meaning `fetch_images` returned nothing) around the timestamp
  of Gil's test, and separately check the `ai_usage` table
  (`client.py:264-282`, written on every `ask_vision` call whether it
  succeeded or not) for a row with `pipeline='equipment_detection'` and
  `ok=false` near that time — its presence/absence distinguishes "the
  model was called and failed" from "the image never reached the model."
  A `quota_exceeded` response would show as an HTTP 402 in the API's own
  access log for that request, if one is being kept.
- No camera-permission check exists in `scanGym()` at all — `TrainScreen.tsx`
  never calls `ImagePicker.getCameraPermissionsAsync` /
  `requestCameraPermissionsAsync`, relying entirely on
  `launchCameraAsync`'s own implicit prompt. If permission was denied on a
  previous app install, `launchCameraAsync` would resolve in a
  platform-dependent way (not traceable by static reading); this is a
  plausible third cause, not ruled out here.

---

## 4. Is the generated workout actually derived from the scan?

**Yes — evidenced, not assumed.** `backend/app/routers/fitness.py:180-202`,
`create_plan()`:
```
182  equipment = body.equipment
183  if body.equipment_scan_id and not equipment:
184      scan = maybe_one(user.sb.table("equipment_scans").select("equipment")...)
...
190      equipment = scan["equipment"]
191  equipment = equipment or ["none"]
...
202  plan = await coach.generate_plan(user.id, body, equipment, profile, history)
```
`coach.generate_plan()` (`coach.py:202-240`) uses `equipment` in two real
ways, not cosmetically:
1. `library_for(equipment)` (`coach.py:77-96`) filters the entire exercise
   library down to only what the equipment list satisfies, and this
   filtered library — not the full one — is the only list of slugs given
   to the model (`coach.py:219-224`, "Exercise library (use these exact
   slugs only)").
2. `_validate()` (`coach.py:166-199`) runs *after* generation and strips or
   substitutes any block the model prescribed that isn't satisfiable by
   `lib`, appending a user-visible warning to `safety_notes` for each swap
   (`coach.py:189-197`).
3. The deterministic fallback template (used when the model call itself
   fails, `coach.py:99-163`, invoked at `coach.py:234-235`) also builds
   only from `library_for(equipment)` — so even total model failure still
   respects detected equipment, it doesn't fall back to something generic.

Mobile side: `TrainScreen.tsx:53-64`, `generate()` passes
`equipment_scan_id: equipment?.id` and `equipment: equipment?.equipment`
straight from the scan result into `createPlan.mutate(...)`. This is a real
pipeline, not hardcoded — **conditional on the scan itself having actually
returned real equipment**, which loops back to §3: if the scan silently
fell back to `equipment: ["none"]`, the plan generated afterward is
correctly a calisthenics plan, not a bug in plan generation — it's doing
exactly what an empty equipment list should produce.

---

## 5. Video capture and multi-photo: what exists today

**Video capture: not found anywhere in the mobile app.** Repo-wide grep for
`recordAsync`, `startRecording`, and `video`/`Video` (case-insensitive)
under `mobile/src` returned no matches. `mobile/package.json:21` has
`"expo-camera": "~57.0.4"` installed and already in use by
`ScanScreen.tsx` (food-scan tab) via its `CameraView` component — SDK 57's
`CameraView` does support a video-recording mode
(`CameraView.recordAsync()` / a `mode="video"` prop), but nothing in this
codebase calls it. `expo-image-picker` (`mobile/package.json:28`,
`~57.0.16`), which is what `TrainScreen.tsx` actually uses, also supports
requesting video via a `mediaTypes` option on `launchCameraAsync`, but
`scanGym()` never passes one.

**Backend: no video path exists.**
- `USER_BUCKETS` (`backend/app/services/account.py:34`) and the equipment
  bucket check in `scripts/verify_supabase.py:54` both name
  `equipment-photos` only — no video bucket anywhere in the repo (grep for
  a bucket-creation migration found none; buckets appear to be created
  directly in the Supabase dashboard, outside the repo, consistent with
  `verify_supabase.py` being a runtime checklist rather than
  infrastructure-as-code).
- `coach.scan_equipment()` calls `fetch_images()` ->
  `downscale_jpeg()` (`vision.py:69-...`), which decodes with **PIL**
  (`from PIL import Image, ImageOps`, `vision.py:88`) — this is an
  image-only decoder; it would simply fail (caught, logged, silently
  dropped per §3's `_one()`) on video bytes.
- No storage, transcoding, or frame-extraction code exists anywhere under
  `backend/app` for any media type other than still JPEG/PNG images
  (checked via the same grep that covered the equipment path — nothing
  video-related turned up in the whole `backend/` tree except test/doc
  mentions of the word "video" in unrelated contexts, none found here).

**Multi-photo: already supported server-side, not exposed on mobile.**
As shown in §2, `EquipmentScanIn.image_paths` already accepts up to 4
paths (`models/fitness.py:52`), and `coach.scan_equipment()` and
`fetch_images()` already loop over an arbitrary-length list
(`coach.py:38`, `vision.py:182`) and send every successfully-fetched image
to the same `gpt-4o` call (`ask_vision(..., images=images, ...)`,
`coach.py:47`). The only gap is `TrainScreen.tsx:32-38`: `scanGym()`
captures exactly one photo per invocation and calls `scanEquipment([path])`
with a single-element array every time. There is no UI to take a second
photo and add it to the same scan before submitting.

**Conclusion for sizing the work:** multi-photo is a small, mobile-only
change (loop the existing capture+upload, collect paths, submit once) since
the backend contract already accepts it. Video is a genuinely new
capability top to bottom — no capture code, no upload bucket, no
server-side handling of any non-still-image format — a moderate-to-large
addition, not a small one.

---

## 6. Smallest safe first steps (not built)

**(a) Fix the current scan failure.** Task 3 found a clear, low-risk
*symptom*-level fix (the silent-paywall gap in `TrainScreen.tsx:43-44` — one
line to call `openPaywall()`/`usePaywallOnError`-equivalent logic when
`needsUpgrade` is true, matching the pattern already used by
`useScanMeal`/`useCreatePlan`), but that only fixes one of at least two
plausible causes, and does nothing for a silently-swallowed vision-call or
image-fetch failure, which is the more likely explanation for a real gym
full of equipment reading as "none." **The runtime check in §3 (the
`ai_usage` row and backend log around Gil's test) needs to run first** to
know which failure actually happened before touching code; fixing the
wrong one leaves the real bug in place.

**(b) Add multi-photo equipment capture.** Change `scanGym()` to collect
photos into a list across repeated `launchCameraAsync` calls (or add
`allowsMultipleSelection` if switching to a library picker for this step),
upload each with the existing `uploadImage()` helper, and submit the
whole array to `api.fitness.scanEquipment(paths)` once — the endpoint and
model call already accept up to 4 images with no server change needed.

**(c) Add short video capture as an alternative mode.** Switch the capture
UI from `ImagePicker.launchCameraAsync` to expo-camera's `CameraView` (as
`ScanScreen.tsx` already does) so `recordAsync()` becomes available, add a
capped-duration recording control (~30s), and — since the backend has no
video path at all — add a new bucket, a new fetch/decode path (likely
frame-sampling to still images before the same `gpt-4o` vision call, since
that call only accepts images), and size/duration limits before any of it
reaches the model.

---

## 7. Test suite

`.\dev test` = `cd backend && python -m pytest -q` (confirmed from
`dev.bat:381-383`, called after `cd /d "%~dp0backend"` at `dev.bat:23`).
Run from this cloud container:

```
983 passed, 2 failed
```

Both failures are in `tests/test_wiring.py`
(`test_every_bench_case_has_a_photo_and_a_weight`,
`test_the_bench_checks_what_is_free_before_spending_anything`), both
asserting on bench photograph files that are not present on disk in this
container — photo-dependent, matching the task's own stated expectation of
two such failures in the cloud. I made no code changes in this task, so
this is simply the current baseline, not something introduced here.

---

## Not verified (needs Gil / runtime access)

- Which of the two failure paths in §3 actually produced "it's not
  scanning" on Gil's phone — the specific `ai_usage` row / backend log
  check described there.
- Whether `OPENAI_API_KEY` is configured and valid (out of scope to check
  directly; the setting and its wiring are confirmed correct in code).
- Whether a camera-permission denial is a contributing third cause (no
  static way to confirm from this repo alone).
- Whether Gil's account had exhausted its daily AI-scan allowance at the
  time he tested — would explain the silent-paywall path in §3 exactly.
