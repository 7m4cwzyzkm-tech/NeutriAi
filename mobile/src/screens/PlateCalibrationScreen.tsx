/**
 * Plate calibration — the single highest-value minute a user can spend.
 *
 * Why this screen exists
 * ---------------------
 * A photo does not contain volume. The portion estimator infers it, and how
 * well depends entirely on whether something in frame has a known real-world
 * size. Its rungs, best to worst:
 *
 *   plate_reference   a plate you measured        ~10-15% error
 *   depth_model       camera distance + lens FOV  ~15-20%
 *   vessel_reference  "that's a bowl", typical    ~18-25%
 *   pixel_area        assume a 27 cm dinner plate ~25-35%
 *   ai_prior          a serving-size guess        ~40%+
 *
 * Without this screen every home meal lands on rung 4 at best, because the
 * estimator has to *assume* a 270 mm plate. Plates run from about 220 to 300
 * mm, and since area goes as the square of diameter, guessing wrong by 30 mm
 * is roughly 25% of the answer before any food is looked at.
 *
 * Measured against weighed meals, a plate of chicken, beans and rice came out
 * +104% on the assumed-plate rung. One tape measure removes that guess for
 * every meal the user ever photographs on that plate.
 *
 * Why typing a number and not photographing a reference card
 * ----------------------------------------------------------
 * A card on the plate would be nicer to use, but it re-enters the estimate
 * through the same vision-model area reading that has been the dominant error
 * source all along -- one photo of a bowl was read two different ways minutes
 * apart. A tape measure has no such variance. The best measurement available
 * is the one the user takes by hand, once.
 *
 * Skipping is a first-class outcome. A user who skips gets exactly today's
 * behaviour, so this screen can never make anything worse.
 */
import React, { useState } from 'react';
import { Alert, KeyboardAvoidingView, Platform, ScrollView, Text, TextInput, View } from 'react-native';
import { useNavigation } from '@react-navigation/native';
import { useQueryClient } from '@tanstack/react-query';
import { SafeAreaView } from 'react-native-safe-area-context';
import { radius, space, type, useTheme } from '../theme';
import { Body, Button, Card, Chip, H1, Label, Row, Screen } from '../components/Primitives';
import { api } from '../api/client';

const MM_PER_INCH = 25.4;

/** Accepted range. Narrower than the API's 10-500 mm because this screen is
 *  specifically about plates and bowls -- a number outside it is a typo or the
 *  wrong unit, and catching it here explains the problem better than a 422. */
const MIN_MM = 140;
const MAX_MM = 400;

/** Common sizes, offered as one-tap answers for anyone without a tape measure.
 *  Real diameters, not marketing sizes. */
const PRESETS = [
  { label: 'Dinner plate', mm: 270, note: 'Standard 10.5" restaurant plate' },
  { label: 'Large dinner', mm: 300, note: 'The bigger modern plate, 11.8"' },
  { label: 'Side / salad', mm: 200, note: 'Starter or dessert plate, 8"' },
  { label: 'Soup bowl', mm: 170, note: 'Measured across the rim' },
];

export default function PlateCalibrationScreen() {
  const c = useTheme();
  const nav = useNavigation<any>();
  const qc = useQueryClient();

  const [imperial, setImperial] = useState(true);
  const [value, setValue] = useState('');
  const [label, setLabel] = useState('My dinner plate');
  const [saving, setSaving] = useState(false);

  const parsed = parseFloat(value.replace(',', '.'));
  const mm = Number.isFinite(parsed)
    ? imperial ? parsed * MM_PER_INCH : parsed * 10   // metric input is in cm
    : NaN;
  const valid = Number.isFinite(mm) && mm >= MIN_MM && mm <= MAX_MM;

  // Say what is wrong, not merely that something is. A user who typed 10.5
  // with Metric selected needs to be told about the unit, not the range.
  const hint = (() => {
    if (!value.trim()) return null;
    if (!Number.isFinite(parsed)) return 'That does not look like a number.';
    if (mm < MIN_MM) {
      return imperial
        ? `That is ${(mm / 10).toFixed(1)} cm — smaller than a saucer. Did you mean centimetres?`
        : `That is only ${(mm / 10).toFixed(1)} cm. Plates are usually 20–30 cm across.`;
    }
    if (mm > MAX_MM) {
      return imperial
        ? `${parsed.toFixed(1)} inches is bigger than a serving platter. Check the number?`
        : `${parsed.toFixed(1)} cm is bigger than a serving platter. Did you mean inches?`;
    }
    return null;
  })();

  function applyPreset(presetMm: number, presetLabel: string) {
    setValue(imperial ? (presetMm / MM_PER_INCH).toFixed(1) : (presetMm / 10).toFixed(1));
    setLabel(presetLabel === 'Soup bowl' ? 'My soup bowl' : `My ${presetLabel.toLowerCase()}`);
  }

  async function save() {
    if (!valid) return;
    setSaving(true);
    try {
      await api.nutrition.calibrations.add({
        label: label.trim() || 'My plate',
        reference_kind: 'plate',
        real_diameter_mm: Math.round(mm * 10) / 10,
        is_default: true,          // the plate used unless a scan names another
      });
      qc.invalidateQueries({ queryKey: ['calibrations'] });
      done();
    } catch (e: any) {
      Alert.alert('Could not save that', e?.message ?? 'Please try again.');
    } finally {
      setSaving(false);
    }
  }

  function done() {
    nav.navigate('Main', { screen: 'Home' });
  }

  return (
    <Screen>
      <SafeAreaView style={{ flex: 1 }}>
        <KeyboardAvoidingView
          style={{ flex: 1 }}
          behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        >
          <ScrollView
            contentContainerStyle={{ padding: space.lg, gap: space.lg }}
            keyboardShouldPersistTaps="handled"
          >
            <View>
              <Label>Optional — one minute, once</Label>
              <H1>How big is your plate?</H1>
              <Body dim>
                Photos have no sense of scale, so portion sizes are worked out by
                comparing food against the plate under it. Right now we assume a
                standard 27 cm plate. Measuring yours once makes every meal you
                photograph on it noticeably more accurate.
              </Body>
            </View>

            <Card style={{ gap: space.sm }}>
              <Label>How to measure</Label>
              <Body dim>
                Across the middle, rim to rim — the widest part, not the inner
                circle the food sits in.
              </Body>
            </Card>

            <Row gap={space.sm}>
              <Chip label="Inches" active={imperial} onPress={() => { setImperial(true); setValue(''); }} />
              <Chip label="Centimetres" active={!imperial} onPress={() => { setImperial(false); setValue(''); }} />
            </Row>

            <Card style={{ gap: space.md }}>
              <Label>Diameter</Label>
              <TextInput
                value={value}
                onChangeText={setValue}
                keyboardType="decimal-pad"
                placeholder={imperial ? 'e.g. 10.5' : 'e.g. 27'}
                placeholderTextColor={c.textFaint}
                style={{
                  backgroundColor: c.surfaceAlt,
                  borderRadius: radius.md,
                  paddingHorizontal: space.md,
                  paddingVertical: space.md,
                  color: c.text,
                  fontSize: 20,
                }}
              />
              {hint ? (
                <Text style={[type.body, { color: c.warn }]}>{hint}</Text>
              ) : valid ? (
                <Text style={[type.body, { color: c.textDim }]}>
                  {imperial
                    ? `${(mm / 10).toFixed(1)} cm — saved as ${Math.round(mm)} mm.`
                    : `${(mm / MM_PER_INCH).toFixed(1)} inches — saved as ${Math.round(mm)} mm.`}
                </Text>
              ) : null}

              <Label>Name it</Label>
              <TextInput
                value={label}
                onChangeText={setLabel}
                placeholder="My dinner plate"
                placeholderTextColor={c.textFaint}
                style={{
                  backgroundColor: c.surfaceAlt,
                  borderRadius: radius.md,
                  paddingHorizontal: space.md,
                  paddingVertical: space.sm,
                  color: c.text,
                }}
              />
              <Body dim>You can add more plates and bowls later in your profile.</Body>
            </Card>

            <View style={{ gap: space.sm }}>
              <Label>No tape measure? Pick the closest</Label>
              {PRESETS.map((p) => (
                <Card key={p.label} style={{ gap: 2 }}>
                  <Row style={{ justifyContent: 'space-between', alignItems: 'center' }}>
                    <View style={{ flex: 1 }}>
                      <Text style={[type.body, { color: c.text }]}>{p.label}</Text>
                      <Text style={[type.caption, { color: c.textDim }]}>{p.note}</Text>
                    </View>
                    <Button
                      title="Use"
                      variant="ghost"
                      onPress={() => applyPreset(p.mm, p.label)}
                    />
                  </Row>
                </Card>
              ))}
              <Body dim>
                A preset is still a guess — but a closer one than assuming every
                plate is the same. Measuring beats all of them.
              </Body>
            </View>
          </ScrollView>

          <View style={{ padding: space.lg, gap: space.sm }}>
            <Button title="Save plate" loading={saving} disabled={!valid} onPress={save} />
            <Button title="Skip for now" variant="ghost" onPress={done} />
          </View>
        </KeyboardAvoidingView>
      </SafeAreaView>
    </Screen>
  );
}
