/**
 * Water: today's progress, quick-log, and opt-in local reminders.
 *
 * Reminders are LOCAL notifications, not server push -- this project has no
 * EAS projectId (see native/notifications.ts's registerForPush()), so push
 * cannot reach a device at all today. See native/localReminders.ts for the
 * honest limit of that approach: this screen recomputes and reschedules
 * today's remaining reminder slots on mount, on every app foreground, and
 * whenever the reminder settings change -- it does not assume a schedule set
 * once will keep running itself in the background forever.
 */
import React, { useEffect } from 'react';
import { AppState, ScrollView, Switch, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { space, type, useTheme } from '../theme';
import { Body, Button, Card, Chip, H1, H2, Label, Loading, Row, Screen } from '../components/Primitives';
import { ProgressRing } from '../components/Rings';
import { useHydrationSettings, useLogWater, useUpdateHydrationSettings, useWater } from '../hooks/useApi';
import { refreshWaterReminders } from '../native/localReminders';
import type { HydrationSettings } from '../api/types';

const GLASS_ML = 250;
const BOTTLE_ML = 500;
const LARGE_BOTTLE_ML = 1000;

const START_PRESETS = ['06:00', '07:00', '08:00', '09:00'];
const END_PRESETS = ['18:00', '19:00', '20:00', '21:00', '22:00'];
const INTERVAL_PRESETS = [30, 60, 90, 120, 180];

const hhmm = (s?: string) => (s ?? '').slice(0, 5);

export function WaterScreen() {
  const c = useTheme();
  const { data: water, isLoading } = useWater();
  const { data: settings } = useHydrationSettings();
  const logWater = useLogWater();
  const updateSettings = useUpdateHydrationSettings();

  // Reschedule today's remaining slots whenever this screen mounts (covers
  // "just opened the app") and whenever the app comes back to the
  // foreground. Never requests permission here -- see localReminders.ts's
  // own doc for why that has to stay a separate, explicit call site.
  useEffect(() => {
    if (!settings) return;
    refreshWaterReminders(settings);
    const sub = AppState.addEventListener('change', (state) => {
      if (state === 'active') refreshWaterReminders(settings);
    });
    return () => sub.remove();
  }, [
    settings?.reminder_enabled, settings?.reminder_start,
    settings?.reminder_end, settings?.reminder_every_min,
  ]);

  function applySettings(patch: Partial<HydrationSettings>, { justEnabled = false } = {}) {
    updateSettings.mutate(patch, {
      onSuccess: () => {
        if (!settings) return;
        refreshWaterReminders({ ...settings, ...patch }, { requestPermission: justEnabled });
      },
    });
  }

  if (isLoading || !water) return <Screen><Loading /></Screen>;

  return (
    <Screen>
      <SafeAreaView style={{ flex: 1 }} edges={['top']}>
        <ScrollView contentContainerStyle={{ padding: space.lg, gap: space.lg, paddingBottom: 120 }}>
          <View>
            <Label>Hydration</Label>
            <H1>Water</H1>
          </View>

          <Card style={{ alignItems: 'center', gap: space.md }}>
            <ProgressRing size={180} stroke={16} pct={water.pct} color={c.water}>
              <View style={{ alignItems: 'center' }}>
                <Text style={[type.h1, { color: c.text }]}>{water.total_ml}</Text>
                <Text style={[type.caption, { color: c.textFaint }]}>of {water.goal_ml} ml</Text>
              </View>
            </ProgressRing>
            <Row gap={space.xl}>
              <View style={{ alignItems: 'center' }}>
                <Text style={[type.h2, { color: c.text }]}>{water.remaining_ml}</Text>
                <Text style={[type.caption, { color: c.textFaint }]}>ml to go</Text>
              </View>
              <View style={{ alignItems: 'center' }}>
                <Text style={[type.h2, { color: c.text }]}>{water.streak}</Text>
                <Text style={[type.caption, { color: c.textFaint }]}>streak</Text>
              </View>
            </Row>
            {!water.on_pace && water.remaining_ml > 0 ? (
              <Body dim>A little behind pace for today — nothing urgent, just a nudge.</Body>
            ) : null}
          </Card>

          {/* The prominent one -- this is also what a tapped reminder notification
              lands on, so it needs to work as a single confident tap with no
              modal in the way ("confirm you drank it," not a form). */}
          <Button
            title={`Log a glass (${GLASS_ML} ml)`}
            loading={logWater.isPending}
            onPress={() => logWater.mutate({ ml: GLASS_ML, container: 'glass' })}
          />
          <Row gap={space.sm}>
            <Chip
              label={`Bottle +${BOTTLE_ML}`}
              onPress={() => logWater.mutate({ ml: BOTTLE_ML, container: 'bottle' })}
            />
            <Chip
              label={`Large +${LARGE_BOTTLE_ML}`}
              onPress={() => logWater.mutate({ ml: LARGE_BOTTLE_ML, container: 'large_bottle' })}
            />
          </Row>

          <Card style={{ gap: space.md }}>
            <Row style={{ justifyContent: 'space-between' }}>
              <H2>Reminders</H2>
              <Switch
                value={!!settings?.reminder_enabled}
                onValueChange={(value) => applySettings({ reminder_enabled: value }, { justEnabled: value })}
                trackColor={{ false: c.surfaceAlt, true: c.accent }}
                thumbColor="#fff"
              />
            </Row>

            {settings?.reminder_enabled ? (
              <>
                <View style={{ gap: space.xs }}>
                  <Label>Starts</Label>
                  <Row gap={space.sm} style={{ flexWrap: 'wrap' }}>
                    {START_PRESETS.map((t) => (
                      <Chip
                        key={t}
                        label={t}
                        active={hhmm(settings.reminder_start) === t}
                        onPress={() => applySettings({ reminder_start: t })}
                      />
                    ))}
                  </Row>
                </View>
                <View style={{ gap: space.xs }}>
                  <Label>Ends</Label>
                  <Row gap={space.sm} style={{ flexWrap: 'wrap' }}>
                    {END_PRESETS.map((t) => (
                      <Chip
                        key={t}
                        label={t}
                        active={hhmm(settings.reminder_end) === t}
                        onPress={() => applySettings({ reminder_end: t })}
                      />
                    ))}
                  </Row>
                </View>
                <View style={{ gap: space.xs }}>
                  <Label>Every</Label>
                  <Row gap={space.sm} style={{ flexWrap: 'wrap' }}>
                    {INTERVAL_PRESETS.map((min) => (
                      <Chip
                        key={min}
                        label={min < 60 ? `${min} min` : `${min / 60}h`}
                        active={settings.reminder_every_min === min}
                        onPress={() => applySettings({ reminder_every_min: min })}
                      />
                    ))}
                  </Row>
                </View>
                <Body dim>
                  {hhmm(settings.reminder_start)}–{hhmm(settings.reminder_end)}, every{' '}
                  {settings.reminder_every_min} min.
                </Body>
              </>
            ) : (
              <Body dim>Off. Turn reminders on to get a nudge during the day.</Body>
            )}
          </Card>
        </ScrollView>
      </SafeAreaView>
    </Screen>
  );
}
