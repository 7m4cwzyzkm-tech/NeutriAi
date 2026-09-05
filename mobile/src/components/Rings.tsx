/**
 * Macro and progress rings.
 *
 * Drawn with react-native-svg rather than an animated library because the value
 * changes at most a few times a minute — a spring animation here would cost
 * battery for no perceived benefit. The one place we animate is the fasting
 * ring, which genuinely moves continuously.
 */
import React from 'react';
import { Text, View } from 'react-native';
import Svg, { Circle, G } from 'react-native-svg';
import { space, type, useTheme } from '../theme';

export function ProgressRing({
  size = 120, stroke = 12, pct, color, track, children,
}: {
  size?: number; stroke?: number; pct: number; color: string;
  track?: string; children?: React.ReactNode;
}) {
  const c = useTheme();
  const r = (size - stroke) / 2;
  const circumference = 2 * Math.PI * r;
  // Clamp the visual at 100% but let the number underneath tell the truth.
  const clamped = Math.max(0, Math.min(100, pct));
  const dash = (clamped / 100) * circumference;

  return (
    <View style={{ width: size, height: size, alignItems: 'center', justifyContent: 'center' }}>
      <Svg width={size} height={size} style={{ position: 'absolute' }}>
        <G rotation={-90} originX={size / 2} originY={size / 2}>
          <Circle
            cx={size / 2} cy={size / 2} r={r}
            stroke={track ?? c.surfaceAlt} strokeWidth={stroke} fill="none"
          />
          <Circle
            cx={size / 2} cy={size / 2} r={r}
            stroke={color} strokeWidth={stroke} fill="none"
            strokeDasharray={`${dash} ${circumference}`}
            strokeLinecap="round"
          />
        </G>
      </Svg>
      {children}
    </View>
  );
}

export function CalorieRing({
  consumed, target, burned = 0,
}: { consumed: number; target: number; burned?: number }) {
  const c = useTheme();
  const adjusted = target + Math.round(burned * 0.6);
  const remaining = adjusted - consumed;
  const pct = (consumed / Math.max(adjusted, 1)) * 100;
  // Over target turns the ring amber, well over turns it red. The colour is the
  // fastest signal; the number is the detail.
  const color = pct > 115 ? c.danger : pct > 100 ? c.warn : c.accent;

  return (
    <ProgressRing size={168} stroke={16} pct={pct} color={color}>
      <View style={{ alignItems: 'center' }}>
        <Text style={[type.hero, { color: c.text }]}>{Math.abs(Math.round(remaining))}</Text>
        <Text style={[type.caption, { color: c.textDim }]}>
          {remaining >= 0 ? 'kcal left' : 'kcal over'}
        </Text>
        <Text style={[type.caption, { color: c.textFaint, marginTop: 2 }]}>
          {Math.round(consumed)} / {adjusted}
        </Text>
      </View>
    </ProgressRing>
  );
}

export function MacroBar({
  label, value, target, color,
}: { label: string; value: number; target: number; color: string }) {
  const c = useTheme();
  const pct = Math.max(0, Math.min(100, (value / Math.max(target, 1)) * 100));
  return (
    <View style={{ flex: 1, gap: 6 }}>
      <View style={{ flexDirection: 'row', justifyContent: 'space-between' }}>
        <Text style={[type.caption, { color: c.textDim }]}>{label}</Text>
        <Text style={[type.caption, { color: c.text, fontVariant: ['tabular-nums'] }]}>
          {Math.round(value)}/{Math.round(target)}g
        </Text>
      </View>
      <View style={{ height: 6, borderRadius: 3, backgroundColor: c.surfaceAlt, overflow: 'hidden' }}>
        <View style={{ width: `${pct}%`, height: '100%', backgroundColor: color, borderRadius: 3 }} />
      </View>
    </View>
  );
}

export function MacroRow({
  protein, carbs, fat, targets,
}: {
  protein: number; carbs: number; fat: number;
  targets: { protein_g: number; carbs_g: number; fat_g: number };
}) {
  const c = useTheme();
  return (
    <View style={{ flexDirection: 'row', gap: space.md }}>
      <MacroBar label="Protein" value={protein} target={targets.protein_g} color={c.protein} />
      <MacroBar label="Carbs" value={carbs} target={targets.carbs_g} color={c.carbs} />
      <MacroBar label="Fat" value={fat} target={targets.fat_g} color={c.fat} />
    </View>
  );
}
