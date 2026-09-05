/** Shared UI atoms. Every screen composes from these so the app stays coherent. */
import React from 'react';
import {
  ActivityIndicator, Pressable, StyleSheet, Text, TextStyle, View, ViewStyle,
} from 'react-native';
import * as Haptics from 'expo-haptics';
import { radius, space, type, useTheme } from '../theme';

export function Screen({ children, style }: { children: React.ReactNode; style?: ViewStyle }) {
  const c = useTheme();
  return <View style={[{ flex: 1, backgroundColor: c.bg }, style]}>{children}</View>;
}

export function Card({
  children, style, onPress,
}: { children: React.ReactNode; style?: ViewStyle; onPress?: () => void }) {
  const c = useTheme();
  const body = (
    <View
      style={[
        {
          backgroundColor: c.surface,
          borderRadius: radius.lg,
          borderWidth: StyleSheet.hairlineWidth,
          borderColor: c.border,
          padding: space.lg,
        },
        style,
      ]}
    >
      {children}
    </View>
  );
  return onPress ? (
    <Pressable onPress={onPress} style={({ pressed }) => ({ opacity: pressed ? 0.85 : 1 })}>
      {body}
    </Pressable>
  ) : body;
}

export function H1({ children, style }: { children: React.ReactNode; style?: TextStyle }) {
  const c = useTheme();
  return <Text style={[type.h1, { color: c.text }, style]}>{children}</Text>;
}

export function H2({ children, style }: { children: React.ReactNode; style?: TextStyle }) {
  const c = useTheme();
  return <Text style={[type.h2, { color: c.text }, style]}>{children}</Text>;
}

export function Body({
  children, dim, style,
}: { children: React.ReactNode; dim?: boolean; style?: TextStyle }) {
  const c = useTheme();
  return <Text style={[type.body, { color: dim ? c.textDim : c.text }, style]}>{children}</Text>;
}

export function Label({ children, style }: { children: React.ReactNode; style?: TextStyle }) {
  const c = useTheme();
  return (
    <Text style={[type.label, { color: c.textDim, textTransform: 'uppercase' }, style]}>
      {children}
    </Text>
  );
}

export function Button({
  title, onPress, variant = 'primary', loading, disabled, style,
}: {
  title: string;
  onPress: () => void;
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
  loading?: boolean;
  disabled?: boolean;
  style?: ViewStyle;
}) {
  const c = useTheme();
  const bg = {
    primary: c.accent, secondary: c.surfaceAlt, ghost: 'transparent', danger: c.danger,
  }[variant];
  const fg = {
    primary: c.accentText, secondary: c.text, ghost: c.accent, danger: '#fff',
  }[variant];

  return (
    <Pressable
      onPress={() => {
        if (disabled || loading) return;
        Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light);
        onPress();
      }}
      style={({ pressed }) => [
        {
          backgroundColor: bg,
          paddingVertical: 15,
          paddingHorizontal: space.xl,
          borderRadius: radius.md,
          alignItems: 'center',
          justifyContent: 'center',
          opacity: disabled ? 0.45 : pressed ? 0.85 : 1,
          borderWidth: variant === 'ghost' ? StyleSheet.hairlineWidth : 0,
          borderColor: c.border,
        },
        style,
      ]}
    >
      {loading ? (
        <ActivityIndicator color={fg} />
      ) : (
        <Text style={{ color: fg, fontSize: 16, fontWeight: '600' }}>{title}</Text>
      )}
    </Pressable>
  );
}

export function Chip({
  label, active, onPress,
}: { label: string; active?: boolean; onPress?: () => void }) {
  const c = useTheme();
  return (
    <Pressable
      onPress={onPress}
      style={{
        paddingVertical: 7,
        paddingHorizontal: 14,
        borderRadius: radius.pill,
        backgroundColor: active ? c.accent : c.surfaceAlt,
        borderWidth: StyleSheet.hairlineWidth,
        borderColor: active ? c.accent : c.border,
      }}
    >
      <Text style={{ color: active ? c.accentText : c.textDim, fontSize: 13, fontWeight: '600' }}>
        {label}
      </Text>
    </Pressable>
  );
}

export function Row({
  children, gap = space.sm, style,
}: { children: React.ReactNode; gap?: number; style?: ViewStyle }) {
  return (
    <View style={[{ flexDirection: 'row', alignItems: 'center', gap }, style]}>{children}</View>
  );
}

export function Divider() {
  const c = useTheme();
  return <View style={{ height: StyleSheet.hairlineWidth, backgroundColor: c.border }} />;
}

export function Empty({ title, subtitle }: { title: string; subtitle?: string }) {
  const c = useTheme();
  return (
    <View style={{ padding: space.xxl, alignItems: 'center', gap: space.sm }}>
      <Text style={[type.h2, { color: c.textDim, textAlign: 'center' }]}>{title}</Text>
      {subtitle ? (
        <Text style={[type.body, { color: c.textFaint, textAlign: 'center' }]}>{subtitle}</Text>
      ) : null}
    </View>
  );
}

export function Loading({ label }: { label?: string }) {
  const c = useTheme();
  return (
    <View style={{ padding: space.xxl, alignItems: 'center', gap: space.md }}>
      <ActivityIndicator color={c.accent} />
      {label ? <Text style={[type.body, { color: c.textDim }]}>{label}</Text> : null}
    </View>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  const c = useTheme();
  return (
    <Card style={{ margin: space.lg, borderColor: c.danger }}>
      <Body>{message}</Body>
      {onRetry ? <Button title="Try again" variant="secondary" onPress={onRetry} style={{ marginTop: space.md }} /> : null}
    </Card>
  );
}
