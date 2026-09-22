/**
 * The camera flow. Three states in one screen: capture, analysing, results.
 *
 * The results state is where the product earns trust, so it does two things
 * most calorie apps don't: it shows the gram *range* rather than a fake-precise
 * single number, and it colours each item by how confident the estimate is.
 * Everything is editable before it counts.
 */
import React, { useState } from 'react';
import { Alert, Image, LayoutChangeEvent, ScrollView, Text, View } from 'react-native';
import { CameraView, useCameraPermissions } from 'expo-camera';
import { CameraGeometry, measureCameraGeometry } from '../native/depth';
import * as ImagePicker from 'expo-image-picker';
import { useNavigation } from '@react-navigation/native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { confidenceColor, radius, space, type, useTheme } from '../theme';
import { Body, Button, Card, Chip, H1, H2, Label, Loading, Row, Screen } from '../components/Primitives';
import { uploadImage } from '../api/supabase';
import { useScanMeal } from '../hooks/useApi';
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

// The plate-framing circle's diameter, as a fraction of the frame's SHORTER
// side. The original box used top:18%/bottom:32% (50% of height) with
// left:8%/right:8% (84% of width) -- two different fractions of two
// different dimensions with a large borderRadius applied, which is a
// squashed oval on any screen whose width and height differ (every phone),
// clipping a plate that actually fills the frame. No comment or commit
// message (checked git log/blame on this file) explains why those two
// fractions were chosen independently; 0.84 is kept here only because it
// was the more generous (less clipping) of the two original margins, now
// applied to BOTH dimensions via the shorter side, so the result is an
// actual circle that fits inside the frame on any aspect ratio.
const PLATE_CIRCLE_FRAME_FRACTION = 0.84;
// Where the circle's own centre sits, as a fraction of the frame's height.
// The original box's vertical centre was at (18% + 68%) / 2 = 43% (its own
// span was 18% to 100%-32%=68%) -- biased above the geometric middle to
// leave room below for the card guide and the bottom control panel. Kept at
// the same 43% so the overall layout does not shift now that the box is
// square instead of tall.
const PLATE_CIRCLE_VERTICAL_CENTER_FRACTION = 0.43;

export function ScanScreen() {
  const c = useTheme();
  const nav = useNavigation<any>();
  const [permission, requestPermission] = useCameraPermissions();
  const [shots, setShots] = useState<string[]>([]);
  const [slot, setSlot] = useState<(typeof SLOTS)[number] | null>(null);
  const [uploading, setUploading] = useState(false);
  const [result, setResult] = useState<ScanResult | null>(null);
  const scan = useScanMeal();
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
  // Only live while the capture view is actually showing -- see
  // useTiltReading's own doc for why this can't just be "always call the
  // hook and ignore the value" (it can, Rules of Hooks require the call
  // either way; `enabled` stops the underlying subscription's battery cost).
  const showingCapture = !result && !uploading && !scan.isPending;
  const tiltDeg = useTiltReading(showingCapture);

  async function capture() {
    // Measured alongside the capture, not before or after it, and never
    // awaited on its own -- measureCameraGeometry resolves to {} rather than
    // throwing or hanging, so a missing or slow sensor cannot block a photo.
    const [photo, measured] = await Promise.all([
      cameraRef.current?.takePictureAsync({ quality: 0.8, exif: true }),
      measureCameraGeometry(),
    ]);
    if (photo?.uri) {
      setShots((s) => [...s, photo.uri].slice(0, 3));
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

  async function pickFromLibrary() {
    const res = await ImagePicker.launchImageLibraryAsync({ quality: 0.8, mediaTypes: ['images'] });
    // A photo from the library carries no distance we can trust -- it may be
    // from another device, another day, another room. Leave geometry empty and
    // let the estimator fall back honestly.
    if (!res.canceled && res.assets[0]) setShots((s) => [...s, res.assets[0].uri].slice(0, 3));
  }

  async function analyse() {
    if (!shots.length) return;
    setUploading(true);
    try {
      const paths = await Promise.all(shots.map((uri) => uploadImage('meal-photos', uri)));
      const res = await scan.mutateAsync({
        image_paths: paths,
        meal_slot: slot ?? undefined,
        ...geometry,
        ...captureExtras,
      });
      setResult(res);
    } catch (e: any) {
      // The paywall opens itself via the shared error handler; anything else
      // deserves a plain explanation rather than a silent failure.
      if (!e?.needsUpgrade) {
        Alert.alert('Could not analyse that', e?.message ?? 'Please try again.');
      }
    } finally {
      setUploading(false);
    }
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
                <Button title="Try another photo" style={{ flex: 1 }}
                        onPress={() => { setResult(null); setShots([]); setNeedCard(false); }} />
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
                <Body style={{ marginTop: 4 }}>
                  Nothing in this photo sets a size — no plate edge, no card, no distance — so
                  these numbers are a typical serving, not your portion. They are usually light.
                </Body>
                <Row gap={space.md} style={{ marginTop: space.md }}>
                  <Button
                    title="Retake with a card"
                    style={{ flex: 1 }}
                    onPress={() => { setNeedCard(true); setResult(null); setShots([]); }}
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
          <Button title="Choose from library instead" variant="ghost" onPress={pickFromLibrary} />
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
  const guideColor = guideState === 'ok' ? c.success : c.warn;
  const guideLabel =
    guideState === 'tilted' ? 'hold the phone level' : guideState === 'ok' ? 'card here' : 'finding level…';

  // A TRUE circle, computed from this device's own frame size -- not the
  // fixed top:18%/left:8%/bottom:32%/right:8% percentages this replaces
  // (two different fractions of two different dimensions, which is a
  // squashed oval on any screen whose width and height differ, clipping a
  // plate that fills the frame; see PLATE_CIRCLE_FRAME_FRACTION's own
  // comment above for why 0.84 and no other reason was found for the
  // original split). Null, like cardBox, until a real layout arrives.
  const plateCircle =
    frameSize.width > 0 && frameSize.height > 0
      ? (() => {
          const size = Math.min(frameSize.width, frameSize.height) * PLATE_CIRCLE_FRAME_FRACTION;
          return {
            size,
            left: (frameSize.width - size) / 2,
            top: frameSize.height * PLATE_CIRCLE_VERTICAL_CENTER_FRACTION - size / 2,
          };
        })()
      : null;

  return (
    <Screen>
      <View style={{ flex: 1 }}>
        <CameraView ref={cameraRef} style={{ flex: 1 }} facing="back" onLayout={onCameraLayout} />

        {/* Framing guide: keeping the whole plate in frame is what makes the
            plate-reference estimate possible, so we ask for it visually. */}
        {plateCircle ? (
          <View
            pointerEvents="none"
            style={{
              position: 'absolute', top: plateCircle.top, left: plateCircle.left,
              width: plateCircle.size, height: plateCircle.size, borderRadius: plateCircle.size / 2,
              borderWidth: 2, borderColor: 'rgba(255,255,255,0.5)',
            }}
          />
        ) : null}
        {/* A card is 85.6 mm on its long edge, the same for every bank in the
            world, which is why it works as a ruler at all. It only works if it
            is IN the shot and lying flat beside the food -- one measured 12%
            small because it sat on the table rather than on the plate.
            Sized from this device's own field of view and the gauge's
            12-inch target (cardGauge.expectedCardBoxPoints), not a single
            fixed 132x83 box for every phone -- see docs/design/mobile-camera-
            survey-2026-09-21.md for what that box used to be.
            Shown by default, every time this screen is open -- not gated
            behind `needCard` any more. Gil's 3-week-trial design wants the
            card as the ruler on EVERY photo, not a rare fallback; see
            needCard's own comment above for why that gate came off. */}
        {cardBox ? (
          <View
            pointerEvents="none"
            style={{
              position: 'absolute', bottom: '34%', alignSelf: 'center',
              width: cardBox.widthPoints, height: cardBox.heightPoints, borderRadius: 8,
              borderWidth: 2, borderStyle: 'dashed', borderColor: guideColor,
              alignItems: 'center', justifyContent: 'center',
            }}
          >
            <Text style={[type.caption, { color: guideColor, fontWeight: '600' }]}>{guideLabel}</Text>
          </View>
        ) : null}

        {/* Both guides now show together, so this is two short lines rather
            than one message that swaps entirely -- the plate instruction is
            never lost. Plain instructions, no "if you want"/"for best
            results" language: the card stays advisory (it never blocks the
            shutter below), but during the trial it is not optional either,
            so the copy just says what to do. */}
        <View pointerEvents="none" style={{ position: 'absolute', top: '10%', width: '100%', alignItems: 'center' }}>
          <Text style={[type.caption, { color: 'rgba(255,255,255,0.85)', textAlign: 'center' }]}>
            Fit the whole plate inside the circle
          </Text>
          {cardBox ? (
            <Text style={[type.caption, { color: guideColor, fontWeight: '600', textAlign: 'center', marginTop: 2 }]}>
              Lay a bank card flat beside it
            </Text>
          ) : null}
        </View>

        <SafeAreaView edges={['bottom']} style={{ position: 'absolute', bottom: 0, width: '100%' }}>
          <View style={{ padding: space.lg, gap: space.md, backgroundColor: 'rgba(0,0,0,0.55)' }}>
            {shots.length > 0 ? (
              <Row gap={space.sm}>
                {shots.map((uri) => (
                  <Image key={uri} source={{ uri }} style={{ width: 52, height: 52, borderRadius: radius.sm }} />
                ))}
                <Text style={[type.caption, { color: '#fff', flex: 1 }]}>
                  {shots.length < 3 ? 'Add another angle for a better estimate' : 'Three angles — nice'}
                </Text>
              </Row>
            ) : null}

            <Row gap={space.sm} style={{ flexWrap: 'wrap' }}>
              {SLOTS.map((s) => (
                <Chip key={s} label={s} active={slot === s} onPress={() => setSlot(s)} />
              ))}
            </Row>

            <Row gap={space.md}>
              <Button title="Library" variant="secondary" style={{ flex: 1 }} onPress={pickFromLibrary} />
              <Button title={shots.length ? 'Another angle' : 'Capture'} style={{ flex: 1 }} onPress={capture} />
            </Row>
            {shots.length > 0 ? <Button title={`Analyse ${shots.length} photo${shots.length > 1 ? 's' : ''}`} onPress={analyse} /> : null}
          </View>
        </SafeAreaView>
      </View>
    </Screen>
  );
}
