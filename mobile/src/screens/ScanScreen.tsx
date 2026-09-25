/**
 * The camera flow. Five states in one screen: info, live-capture, review,
 * analysing, results.
 *
 * The results state is where the product earns trust, so it does two things
 * most calorie apps don't: it shows the gram *range* rather than a fake-precise
 * single number, and it colours each item by how confident the estimate is.
 * Everything is editable before it counts.
 *
 * The info state exists so the live-capture view itself can show nothing but
 * the camera and its guides -- meal slot and a food/plate description (both
 * required -- the description is real data for perception/depth work, not
 * just a nicety) are collected here, first, and submitted before the camera
 * ever opens.
 *
 * Review is its own state, not a panel drawn on top of the still-live camera
 * feed: once a photo is taken the camera unmounts and the thumbnails / add-
 * another-angle / analyse controls get a plain screen of their own. "Add
 * another angle" sends the user back to live-capture to take the next shot;
 * live-capture returns to review the moment that shot lands.
 *
 * No library/photo-picker path exists here any more (Gil's call: a photo
 * with no reliable distance/scale is bad data, not a convenience) --
 * expo-image-picker is still a real dependency of the app, used by
 * TrainScreen.tsx for equipment photos, just not by this file.
 */
import React, { useState } from 'react';
import { Alert, Image, LayoutChangeEvent, ScrollView, Text, TextInput, View } from 'react-native';
import { CameraView, useCameraPermissions } from 'expo-camera';
import { CameraGeometry, measureCameraGeometry } from '../native/depth';
import { useNavigation } from '@react-navigation/native';
import { useQueryClient } from '@tanstack/react-query';
import { SafeAreaView } from 'react-native-safe-area-context';
import { confidenceColor, radius, space, type, useTheme } from '../theme';
import { Body, Button, Card, Chip, H1, H2, Label, Loading, Row, Screen } from '../components/Primitives';
import { api } from '../api/client';
import { uploadImage } from '../api/supabase';
import { keys, useScanMeal } from '../hooks/useApi';
import { useTiltReading } from '../hooks/useTiltReading';
import type { ScanResult } from '../api/types';
import { methodLabel } from '../lib/method';
import {
  DEFAULT_CAMERA_FOV_DEG,
  distanceErrorPct,
  expectedCardBoxPoints,
  gaugeState,
  TARGET_DISTANCE_MM,
} from '../lib/cardGauge';

const SLOTS = ['breakfast', 'lunch', 'dinner', 'snack'] as const;

export function ScanScreen() {
  const c = useTheme();
  const nav = useNavigation<any>();
  const [permission, requestPermission] = useCameraPermissions();
  const [shots, setShots] = useState<string[]>([]);
  // Whether the review screen (thumbnails / add another angle / analyse) is
  // showing instead of the live camera. Distinct from `shots.length > 0`:
  // "Add another angle" sends the user back to live-capture with shots
  // already non-empty, so shots.length alone cannot tell the two apart.
  const [reviewing, setReviewing] = useState(false);
  const [slot, setSlot] = useState<(typeof SLOTS)[number] | null>(null);
  // Collected on the info state, before the camera opens -- see the top
  // comment. Neither is reset by a retake (below): the meal being logged
  // and its description do not change just because the photo was retaken.
  const [note, setNote] = useState('');
  const [infoSubmitted, setInfoSubmitted] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [result, setResult] = useState<ScanResult | null>(null);
  const scan = useScanMeal();
  const qc = useQueryClient();
  const cameraRef = React.useRef<CameraView>(null);
  // Geometry belongs to the moment the shutter fired -- by the time the
  // user taps Analyse the phone has moved and any distance is stale.
  const [geometry, setGeometry] = useState<CameraGeometry>({});
  // Extra capture-time bookkeeping the backend's ScanRequest also accepts,
  // separate from `geometry` above because it is sourced differently: image
  // dimensions come from the photo itself, not the (still-unbuilt, see
  // native/depth.ts) NeutriDepth sensor bridge, and distance_error_pct/tilt
  // come from THIS screen's on-device gauge, not from `measureCameraGeometry`.
  const [captureExtras, setCaptureExtras] = useState<{
    image_width_px?: number;
    image_height_px?: number;
    image_orientation?: number;
    distance_error_pct?: number;
    tilt_deg_at_capture?: number;
  }>({});
  // Whether this retake was specifically prompted by a failed/unmeasured
  // scan (see the "Retake with a card" button below). The card guide itself
  // no longer gates on this -- Gil's 3-week-trial design (docs/HANDOFF.md)
  // wants the card guide showing on EVERY photo, not only after a failure,
  // and the flag that used to gate it was never set on the ordinary capture
  // path anyway (the comment above used to claim a banner set it; no such
  // banner exists in this file or anywhere else in mobile/src -- confirmed
  // by grep). Kept, not deleted, as the one signal this screen has for "the
  // user was specifically told their last photo needed a card" -- a cheap
  // extension point for a future escalation (e.g. more insistent copy after
  // a repeat failure), never a way to block the shutter.
  const [needCard, setNeedCard] = useState(false);
  // The live camera view's own on-screen size, in points -- captured via
  // onLayout since RN gives no other way to read a flex:1 View's rendered
  // size. Needed to size the card guide for THIS device's screen, not one
  // hardcoded number for every phone. Starts at {0,0}; the guide simply
  // does not render until a real layout arrives (see the render below).
  const [frameSize, setFrameSize] = useState({ width: 0, height: 0 });
  function onCameraLayout(e: LayoutChangeEvent) {
    const { width, height } = e.nativeEvent.layout;
    setFrameSize((prev) => (prev.width === width && prev.height === height ? prev : { width, height }));
  }
  // Only live while the LIVE camera view is actually showing -- not during
  // review, which has no camera mounted. See useTiltReading's own doc for
  // why this can't just be "always call the hook and ignore the value" (it
  // can, Rules of Hooks require the call either way; `enabled` stops the
  // underlying subscription's battery cost).
  const showingCapture = !result && !uploading && !scan.isPending && !reviewing;
  const tiltDeg = useTiltReading(showingCapture);

  // Two shutter gates on top of the level check, both from expo-camera
  // 57.0.4's own native code (ios/Current/CameraView.swift and
  // CameraPhotoCapture.swift):
  //
  // - cameraReady: the preview session has started (`onCameraReady`). Before
  //   it, takePictureAsync throws "Wait for 'onCameraReady' callback".
  // - capturing: a picture is already being taken. A second takePictureAsync
  //   while the first is pending throws "Camera is not ready yet"
  //   (CameraPhotoCapture.swift:66) -- the crash Gil hit on 25 Sep 2026, a
  //   double tap, not a cold camera. The ref catches taps that land in the
  //   same frame, before the state below has re-rendered the button.
  //
  // The CameraView only exists in the live-capture branch, so it unmounts
  // on every trip to review/analysing/results and mounts fresh on return.
  // Readiness belongs to one mount, so it is cleared whenever the capture
  // view goes away and re-set only by the new mount's own onCameraReady.
  const [cameraReady, setCameraReady] = useState(false);
  const [capturing, setCapturing] = useState(false);
  const capturingRef = React.useRef(false);
  React.useEffect(() => {
    if (!showingCapture) setCameraReady(false);
  }, [showingCapture]);

  async function capture() {
    if (capturingRef.current) return;
    capturingRef.current = true;
    setCapturing(true);
    // Measured alongside the capture, not before or after it, and never
    // awaited on its own -- measureCameraGeometry resolves to {} rather than
    // throwing or hanging, so a missing or slow sensor cannot block a photo.
    let photo: Awaited<ReturnType<CameraView['takePictureAsync']>> | undefined;
    let measured: CameraGeometry;
    try {
      [photo, measured] = await Promise.all([
        cameraRef.current?.takePictureAsync({ quality: 0.8, exif: true }),
        measureCameraGeometry(),
      ]);
    } catch (e: any) {
      // The shot did not happen. Leave the screen exactly as it was -- no
      // shot added, still on the live camera -- so another tap just works.
      console.warn('[camera] capture failed', e?.message ?? e);
      return;
    } finally {
      capturingRef.current = false;
      setCapturing(false);
    }
    if (photo?.uri) {
      setShots((s) => [...s, photo.uri].slice(0, 3));
      setReviewing(true);
      // The first shot is the one the estimator scales from.
      setGeometry((g) => (Object.keys(g).length ? g : measured));
      setCaptureExtras((prev) => {
        if (Object.keys(prev).length) return prev; // first shot only, same as geometry above
        const next: typeof prev = {
          image_width_px: photo.width,
          image_height_px: photo.height,
        };
        // EXIF orientation, when the platform/library provides it. Not
        // verified on device -- see this task's report on what could not
        // be run. Absent rather than guessed if the field isn't there.
        const orientation = (photo as { exif?: { Orientation?: number } }).exif?.Orientation;
        if (typeof orientation === 'number') next.image_orientation = orientation;
        // distance_error_pct needs a REAL measured distance -- only ever
        // present today if a future NeutriDepth native module ships (see
        // native/depth.ts); omitted, not faked, until then.
        if (typeof measured.camera_distance_mm === 'number') {
          next.distance_error_pct = distanceErrorPct(measured.camera_distance_mm, TARGET_DISTANCE_MM);
        }
        if (typeof tiltDeg === 'number') next.tilt_deg_at_capture = Math.round(tiltDeg * 10) / 10;
        return next;
      });
    }
  }

  async function analyse() {
    if (!shots.length) return;
    setUploading(true);
    let paths: string[];
    try {
      paths = await Promise.all(shots.map((uri) => uploadImage('meal-photos', uri)));
    } catch (e: any) {
      // The upload is not the scan mutation, so its error handler never sees
      // this -- it needs its own alert.
      Alert.alert('Could not upload that photo', e?.message ?? 'Please try again.');
      setUploading(false);
      return;
    }
    try {
      const res = await scan.mutateAsync({
        image_paths: paths,
        meal_slot: slot ?? undefined,
        note: note.trim() || undefined,
        ...geometry,
        ...captureExtras,
      });
      setResult(res);
    } catch {
      // useScanMeal's onError already opened the paywall or showed the
      // alert; alerting here too would show it twice.
    } finally {
      setUploading(false);
    }
  }

  /**
   * Every "retake" goes through here: the failed scan's "Try another photo",
   * the unmeasured scan's "Retake with a card", and the ordinary result's
   * "Retake photo". The accept paths ("Looks right", "Keep the estimate")
   * never do.
   *
   * The backend writes the meal the moment the scan finishes, before this
   * screen has shown it (vision.py, "persist meal"), so a retake that only
   * reset local state left that meal in the food log and in the day's totals
   * -- once per retake, without the user ever saying "log this". A retake is
   * a discard, so the meal it discards is deleted.
   *
   * Best-effort: the delete is started first but never awaited, so a slow or
   * failed request cannot hold the user on this screen. A failure is logged,
   * not alerted -- there is nothing the user can do about it here. A scan
   * that failed before anything was persisted has no meal_id; nothing is
   * deleted then.
   *
   * Geometry and capture extras are cleared too: capture() keeps them from
   * the FIRST shot only, so without this the new photo was sent with the
   * discarded photo's size and tilt.
   */
  function discardAndRetake(opts: { needCard?: boolean } = {}) {
    const mealId = result?.meal_id;
    if (mealId) {
      api.nutrition.deleteMeal(mealId)
        .then(() => {
          qc.invalidateQueries({ queryKey: keys.dashboard() });
          qc.invalidateQueries({ queryKey: keys.meals() });
        })
        .catch((e: any) => {
          console.warn('Could not delete the discarded scan meal', mealId, e?.message ?? e);
        });
    }
    setNeedCard(!!opts.needCard);
    setResult(null);
    setShots([]);
    setReviewing(false);
    setGeometry({});
    setCaptureExtras({});
  }

  // -------------------------------------------------------------------- info
  // Shown once per scan, before the camera opens. `infoSubmitted` is never
  // reset by a retake (see the failed-scan and not-measured buttons below),
  // so this does not reappear mid-flow -- only at the start of a new scan.
  if (!infoSubmitted) {
    return (
      <Screen>
        <SafeAreaView style={{ flex: 1 }} edges={['top']}>
          <View style={{ flex: 1, padding: space.lg, gap: space.lg, justifyContent: 'center' }}>
            <View>
              <Label>Before you scan</Label>
              <H1>What are you eating?</H1>
            </View>
            <Card style={{ gap: space.md }}>
              <Label>Meal</Label>
              <Row gap={space.sm} style={{ flexWrap: 'wrap' }}>
                {SLOTS.map((s) => (
                  <Chip key={s} label={s} active={slot === s} onPress={() => setSlot(s)} />
                ))}
              </Row>
              <Label>What is it?</Label>
              <TextInput
                value={note}
                onChangeText={setNote}
                placeholder="e.g. chicken salad, leftovers from Tuesday"
                placeholderTextColor={c.textFaint}
                style={{
                  backgroundColor: c.surfaceAlt, borderRadius: radius.md,
                  padding: space.lg, color: c.text, fontSize: 16,
                }}
              />
            </Card>
            <Button title="Continue to camera" disabled={!slot || !note.trim()} onPress={() => setInfoSubmitted(true)} />
          </View>
        </SafeAreaView>
      </Screen>
    );
  }

  // ---------------------------------------------------------------- results
  if (result) {
    const a = result.assessment;

    /**
     * A scan that found nothing is a FAILURE, and must look like one.
     *
     * This screen used to render every result the same way, so a failed scan
     * announced "Scan complete — 0 kcal", showed a vague "worth a second look"
     * card, and put the actual reason in 12pt faint grey at the very bottom
     * under "How this was calculated". The real error was on screen the whole
     * time, styled as a footnote. Someone reading that has no idea what went
     * wrong or whether to retry.
     */
    const failed = result.items.length === 0;

    if (failed) {
      return (
        <Screen>
          <SafeAreaView style={{ flex: 1 }}>
            <ScrollView contentContainerStyle={{ padding: space.lg, gap: space.lg }}>
              <View>
                <Label>Scan failed</Label>
                <H1>Nothing was logged</H1>
              </View>

              <Card style={{ borderColor: c.danger }}>
                <Label>What went wrong</Label>
                <View style={{ gap: 8, marginTop: space.sm }}>
                  {(result.notes.length ? result.notes : ['No reason was returned.']).map((n, i) => (
                    <Body key={i}>{n}</Body>
                  ))}
                </View>
              </Card>

              <Card>
                <Label>Details</Label>
                <View style={{ gap: 4, marginTop: space.sm }}>
                  <Text style={[type.caption, { color: c.textFaint }]}>status: {result.status}</Text>
                  <Text style={[type.caption, { color: c.textFaint }]}>
                    took {((result.latency_ms ?? 0) / 1000).toFixed(1)}s
                  </Text>
                  <Text style={[type.caption, { color: c.textFaint }]}>scan {result.scan_id}</Text>
                </View>
              </Card>

              <Row gap={space.md}>
                <Button title="Try another photo" style={{ flex: 1 }} onPress={() => discardAndRetake()} />
              </Row>
            </ScrollView>
          </SafeAreaView>
        </Screen>
      );
    }

    return (
      <Screen>
        <SafeAreaView style={{ flex: 1 }}>
          <ScrollView contentContainerStyle={{ padding: space.lg, gap: space.lg, paddingBottom: 120 }}>
            <View>
              <Label>Scan complete</Label>
              <H1>{Math.round(result.totals.kcal)} kcal</H1>
              <Row gap={space.sm} style={{ marginTop: space.sm }}>
                <View
                  style={{
                    paddingVertical: 4, paddingHorizontal: 10, borderRadius: radius.pill,
                    backgroundColor: confidenceColor(c, result.overall_confidence) + '22',
                  }}
                >
                  <Text style={{ color: confidenceColor(c, result.overall_confidence), fontSize: 12, fontWeight: '600' }}>
                    {result.confidence_band} confidence
                  </Text>
                </View>
                {result.latency_ms ? (
                  <Text style={[type.caption, { color: c.textFaint }]}>
                    {(result.latency_ms / 1000).toFixed(1)}s
                  </Text>
                ) : null}
              </Row>
            </View>

            {result.portion_measured === false ? (
              <Card style={{ borderColor: c.warn, borderWidth: 2 }}>
                <Label>Not measured</Label>
                {/* Honest about two things the old copy got wrong (Gil, 24 Sep
                    2026): it said "nothing in this photo sets a size" even
                    when a card was plainly in the shot, and it never said
                    that the green framing guide is not this check. The guide
                    only reads the phone's tilt (see guideState above); the
                    measurement is a separate check on the uploaded photo
                    (reference_cv.find_reference), and it can say no to a
                    card it cannot see clearly. The response carries no reason
                    for the "no", so this copy covers both "nothing found"
                    and "found but not confidently" without guessing which. */}
                <Body style={{ marginTop: 4 }}>
                  We couldn't find a card or plate edge in this photo that we could measure with
                  confidence — even if one was in the shot. So these numbers are a typical
                  serving, not your portion. They are usually light.
                </Body>
                <Body dim style={{ marginTop: space.sm }}>
                  The green guide while you framed the shot only checks that your phone is level.
                  Measuring happens afterwards, on the photo itself, and it can still say no. A
                  retake with the whole card lying flat and fully visible, all four corners in
                  view, gives it the best chance.
                </Body>
                <Row gap={space.md} style={{ marginTop: space.md }}>
                  <Button
                    title="Retake with a card"
                    style={{ flex: 1 }}
                    onPress={() => discardAndRetake({ needCard: true })}
                  />
                  <Button
                    title="Keep the estimate"
                    variant="secondary"
                    style={{ flex: 1 }}
                    onPress={() => nav.navigate('Home')}
                  />
                </Row>
              </Card>
            ) : null}

            {result.needs_review ? (
              <Card style={{ borderColor: c.warn }}>
                <Body>
                  This one is worth a second look before it counts — the photo made a few items
                  hard to size accurately.
                </Body>
              </Card>
            ) : null}

            {result.items.map((item, i) => (
              <Card key={i}>
                <Row style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
                  <View style={{ flex: 1, gap: 4 }}>
                    <Text style={[type.body, { color: c.text, fontWeight: '600', textTransform: 'capitalize' }]}>
                      {item.name}
                    </Text>
                    <Text style={[type.caption, { color: c.textDim }]}>
                      {Math.round(item.grams)} g
                      {item.grams_low && item.grams_high
                        ? `  (${Math.round(item.grams_low)}–${Math.round(item.grams_high)})`
                        : ''}
                    </Text>
                    <Row gap={space.xs}>
                      <View
                        style={{
                          width: 6, height: 6, borderRadius: 3,
                          backgroundColor: confidenceColor(c, item.confidence),
                        }}
                      />
                      <Text style={[type.caption, { color: c.textFaint }]}>
                        {methodLabel(item.estimation_method)}
                      </Text>
                    </Row>
                  </View>
                  <View style={{ alignItems: 'flex-end' }}>
                    <Text style={[type.h2, { color: c.text }]}>{Math.round(item.macros.kcal)}</Text>
                    <Text style={[type.caption, { color: c.textFaint }]}>
                      P{Math.round(item.macros.protein_g)} C{Math.round(item.macros.carbs_g)} F{Math.round(item.macros.fat_g)}
                    </Text>
                  </View>
                </Row>
              </Card>
            ))}

            {a ? (
              <Card style={{ borderColor: a.severity === 'none' ? c.border : c.warn, gap: space.sm }}>
                <Label>{a.severity === 'none' ? 'On track' : 'Heads up'}</Label>
                <H2>{a.headline}</H2>
                <Body dim>{a.detail}</Body>
                {a.portion_advice.length > 0 ? (
                  <View style={{ gap: 6, marginTop: space.sm }}>
                    {a.portion_advice.map((p, i) => (
                      <Text key={i} style={[type.body, { color: c.textDim }]}>• {p}</Text>
                    ))}
                  </View>
                ) : null}
                {a.next_meal.suggestions.length > 0 ? (
                  <View style={{ marginTop: space.md, gap: 6 }}>
                    <Label>Next meal — {a.next_meal.target_kcal} kcal, {a.next_meal.emphasis}</Label>
                    {a.next_meal.suggestions.map((s, i) => (
                      <Text key={i} style={[type.body, { color: c.textDim }]}>• {s}</Text>
                    ))}
                  </View>
                ) : null}
              </Card>
            ) : null}

            {result.notes.length > 0 ? (
              <Card>
                <Label>How this was calculated</Label>
                <View style={{ gap: 6, marginTop: space.sm }}>
                  {result.notes.map((n, i) => (
                    <Text key={i} style={[type.caption, { color: c.textFaint }]}>• {n}</Text>
                  ))}
                </View>
              </Card>
            ) : null}

            <Row gap={space.md}>
              <Button
                title="Edit items"
                variant="secondary"
                style={{ flex: 1 }}
                onPress={() => nav.navigate('MealDetail', { mealId: result.meal_id, edit: true })}
              />
              <Button title="Looks right" style={{ flex: 1 }} onPress={() => nav.navigate('Home')} />
            </Row>
            {/* The way out of a result that looks fine but is wrong -- the
                wrong foods, or one missed. Below the two accept-side actions
                and quieter than them, so it is there when needed and not the
                first thing a thumb lands on. Discards this meal (see
                discardAndRetake) and goes straight back to the camera; the
                meal slot and description are kept. */}
            <Button title="Retake photo" variant="ghost" onPress={() => discardAndRetake()} />
          </ScrollView>
        </SafeAreaView>
      </Screen>
    );
  }

  // -------------------------------------------------------------- analysing
  if (uploading || scan.isPending) {
    return (
      <Screen>
        <SafeAreaView style={{ flex: 1, justifyContent: 'center' }}>
          <Loading label={uploading ? 'Uploading your photo…' : 'Identifying the food and sizing portions…'} />
          <Text style={[type.caption, { color: c.textFaint, textAlign: 'center', paddingHorizontal: space.xxl }]}>
            This usually takes about five seconds. Multi-angle shots take a little longer but are
            noticeably more accurate.
          </Text>
        </SafeAreaView>
      </Screen>
    );
  }

  // ---------------------------------------------------------------- capture
  if (!permission?.granted) {
    return (
      <Screen>
        <SafeAreaView style={{ flex: 1, justifyContent: 'center', padding: space.xl, gap: space.lg }}>
          <H1>Camera access</H1>
          <Body dim>
            NeutriAI needs the camera to identify what's on your plate. Photos are uploaded to your
            own private folder and are never public.
          </Body>
          <Button title="Allow camera" onPress={requestPermission} />
        </SafeAreaView>
      </Screen>
    );
  }

  // ----------------------------------------------------------------- review
  // No CameraView mounted here at all -- this used to be a panel drawn on
  // top of the still-live camera feed; Gil wants the live feed showing
  // nothing but itself and its guides once a photo exists. "Add another
  // angle" is a plain state change back to live-capture, not a capture
  // itself -- the next shot is taken from there.
  if (reviewing) {
    return (
      <Screen>
        <SafeAreaView style={{ flex: 1 }}>
          <View style={{ flex: 1, padding: space.lg, gap: space.lg, justifyContent: 'center' }}>
            <View>
              <Label>Review</Label>
              <H1>{shots.length} photo{shots.length > 1 ? 's' : ''} captured</H1>
            </View>
            <Row gap={space.sm}>
              {shots.map((uri) => (
                <Image key={uri} source={{ uri }} style={{ width: 72, height: 72, borderRadius: radius.sm }} />
              ))}
            </Row>
            <Body dim>
              {shots.length < 3 ? 'Add another angle for a better estimate' : 'Three angles — nice'}
            </Body>
            <Button
              title="Add another angle"
              variant="secondary"
              onPress={() => setReviewing(false)}
            />
            <Button
              title={`Analyse ${shots.length} photo${shots.length > 1 ? 's' : ''}`}
              onPress={analyse}
            />
          </View>
        </SafeAreaView>
      </Screen>
    );
  }

  // The guide box's size, in points, for THIS device's own on-screen camera
  // view -- see cardGauge.expectedCardBoxPoints for why points (not raw
  // camera pixels) is the right unit here. DEFAULT_CAMERA_FOV_DEG is the
  // same long-axis default the backend falls back to (portion.py:47) when a
  // phone does not report its own FOV -- which is always, today, since this
  // app has no native module that reads a real one (native/depth.ts).
  // Using the documented default rather than a fabricated "real" FOV is
  // deliberate; the gap between them is a code comment, not user-facing text.
  const cardBox =
    frameSize.width > 0 && frameSize.height > 0
      ? expectedCardBoxPoints(frameSize.width, frameSize.height, DEFAULT_CAMERA_FOV_DEG, TARGET_DISTANCE_MM)
      : null;
  // measuredCardPx === expectedPx (ratio exactly 1) because this build has
  // no live card-width measurement -- no frame-processing library was added
  // (out of scope; see this task's hard rules and report). So this can only
  // ever resolve to "ok" or "tilted," never "too_far"/"too_close" -- an
  // honest limit, not a bug: the ONE thing actually measured live here is
  // tilt, from the real accelerometer, and gaugeState's own tilt-first
  // precedence is exactly what makes reporting just that safe to do through
  // the real function rather than a hand-rolled shortcut.
  const guideState = cardBox ? gaugeState(cardBox.widthPoints, cardBox.widthPoints, tiltDeg ?? 0) : null;
  // Camera first: until it is running, "card here" in green would invite a
  // tap the disabled shutter then ignores.
  const guideColor = cameraReady && guideState === 'ok' ? c.success : c.warn;
  const guideLabel = !cameraReady
    ? 'starting camera…'
    : guideState === 'tilted' ? 'hold the phone level' : guideState === 'ok' ? 'card here' : 'finding level…';

  return (
    <Screen>
      <View style={{ flex: 1 }}>
        <CameraView
          ref={cameraRef}
          style={{ flex: 1 }}
          facing="back"
          onLayout={onCameraLayout}
          onCameraReady={() => setCameraReady(true)}
          onMountError={(e) => console.warn('[camera] preview could not start', e.message)}
        />

        {/* The card guide, moved to the TOP of the screen (Gil, 22 Sep
            2026): positioned just below the plate circle it still read as
            too close, which visually crowded the two guides together even
            after fix-card-box-position separated them. (The plate circle
            was later removed entirely -- Gil, 24 Sep 2026 -- to make
            capture faster. It was only a client-side centering aid: the
            backend finds the plate in the full uploaded frame with its own
            detection, food_seg.py's Hough/colour outline, which never used
            the overlay. This guide's position never depended on it.)
            Sitting inside a
            top-edge SafeAreaView rather than a frameSize-derived pixel
            offset, so it clears a notch/Dynamic Island on any device
            without a hardcoded guess -- the same pattern the capture
            button already uses at the bottom. No settings gear icon exists
            on this screen (checked navigation and this file) to clear;
            the only real constraint here is the safe area itself, which
            this pattern already handles.
            A card is 85.6 mm on its long edge, the same for every bank in
            the world, which is why it works as a ruler at all. It only
            works if it is IN the shot and lying flat beside the food --
            one measured 12% small because it sat on the table rather than
            on the plate. Sized from this device's own field of view and
            the gauge's 12-inch target (cardGauge.expectedCardBoxPoints),
            not a single fixed 132x83 box for every phone -- see
            docs/design/mobile-camera-survey-2026-09-21.md for what that
            box used to be. Shown by default, every time this screen is
            open -- not gated behind `needCard` any more. Gil's 3-week-
            trial design wants the card as the ruler on EVERY photo, not a
            rare fallback; see needCard's own comment above for why that
            gate came off. */}
        {cardBox ? (
          <SafeAreaView edges={['top']} style={{ position: 'absolute', top: 0, width: '100%', alignItems: 'center' }}>
            <View style={{ paddingTop: space.sm, alignItems: 'center' }}>
              <View
                pointerEvents="none"
                style={{
                  width: cardBox.widthPoints, height: cardBox.heightPoints, borderRadius: 8,
                  borderWidth: 2, borderStyle: 'dashed', borderColor: guideColor,
                  alignItems: 'center', justifyContent: 'center',
                }}
              >
                <Text style={[type.caption, { color: guideColor, fontWeight: '600' }]}>{guideLabel}</Text>
              </View>
              <Text style={[type.caption, { color: guideColor, fontWeight: '600', textAlign: 'center', marginTop: 4 }]}>
                Lay a bank card flat beside it
              </Text>
            </View>
          </SafeAreaView>
        ) : null}

        {/* The one control this screen keeps: a small shutter button,
            pinned to the very bottom. Gated on guideState === 'ok' (Gil,
            22 Sep 2026): there is no live card-detection on this build --
            gaugeState only ever resolves 'ok' or 'tilted' because
            measuredCardPx is hardcoded equal to expectedPx above (no
            frame-processing library exists to measure a card live; out of
            scope, confirmed with Gil). So 'ok' here means the phone is
            being held level, nothing about a card actually being in
            frame. Gil has explicitly accepted gating on this real,
            measured signal (steadiness) rather than waiting on a card-
            detection feature that does not exist -- disabled also covers
            the initial null state before the first layout/tilt reading
            arrives, so the button cannot be tapped before guideState has
            a real value. Also gated on cameraReady, and busy while a
            capture is in flight (see cameraReady/capturing above) -- level
            says nothing about whether the camera can take a picture.
            Hardware-volume-button capture is Gil's eventual
            preference but needs a native module and a custom dev build
            outside Expo Go -- explicitly deferred, not part of this task;
            a small on-screen button stands in for it. Text over an icon:
            no icon library is used anywhere else in this app, and adding
            one only for this button is exactly the kind of new dependency
            that would be out of scope here. */}
        <SafeAreaView edges={['bottom']} style={{ position: 'absolute', bottom: 0, width: '100%', alignItems: 'center' }}>
          <View style={{ paddingBottom: space.xl }}>
            <Button
              title="Capture"
              disabled={guideState !== 'ok' || !cameraReady}
              loading={capturing}
              style={{ paddingHorizontal: space.xxl }}
              onPress={capture}
            />
          </View>
        </SafeAreaView>
      </View>
    </Screen>
  );
}
