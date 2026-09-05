/** Email magic-link / password auth via Supabase. */
import React, { useState } from 'react';
import { KeyboardAvoidingView, Platform, Text, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { radius, space, type, useTheme } from '../theme';
import { Body, Button, H1, Label, Row, Screen } from '../components/Primitives';
import { supabase } from '../api/supabase';

export function AuthScreen() {
  const c = useTheme();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [mode, setMode] = useState<'signin' | 'signup'>('signup');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    setBusy(true);
    setError(null);
    const fn = mode === 'signup' ? supabase.auth.signUp : supabase.auth.signInWithPassword;
    const { error: err } = await fn({ email: email.trim(), password });
    if (err) setError(err.message);
    setBusy(false);
  }

  return (
    <Screen>
      <SafeAreaView style={{ flex: 1 }}>
        <KeyboardAvoidingView
          behavior={Platform.OS === 'ios' ? 'padding' : undefined}
          style={{ flex: 1, justifyContent: 'center', padding: space.xl, gap: space.lg }}
        >
          <View>
            <Label>NutriAI</Label>
            <H1>{mode === 'signup' ? 'Create your account' : 'Welcome back'}</H1>
            <Body dim>
              {mode === 'signup'
                ? 'Fifteen days free. No card needed to start.'
                : 'Sign in to pick up where you left off.'}
            </Body>
          </View>

          {[
            { value: email, set: setEmail, placeholder: 'Email', secure: false, keyboard: 'email-address' as const },
            { value: password, set: setPassword, placeholder: 'Password', secure: true, keyboard: 'default' as const },
          ].map((f) => (
            <TextInput
              key={f.placeholder}
              value={f.value}
              onChangeText={f.set}
              placeholder={f.placeholder}
              placeholderTextColor={c.textFaint}
              secureTextEntry={f.secure}
              keyboardType={f.keyboard}
              autoCapitalize="none"
              autoCorrect={false}
              style={{
                backgroundColor: c.surface,
                borderRadius: radius.md,
                borderWidth: 1,
                borderColor: c.border,
                padding: space.lg,
                color: c.text,
                fontSize: 16,
              }}
            />
          ))}

          {error ? <Text style={[type.body, { color: c.danger }]}>{error}</Text> : null}

          <Button
            title={mode === 'signup' ? 'Start free trial' : 'Sign in'}
            loading={busy}
            disabled={!email || password.length < 6}
            onPress={submit}
          />
          <Button
            title={mode === 'signup' ? 'I already have an account' : 'Create an account'}
            variant="ghost"
            onPress={() => setMode(mode === 'signup' ? 'signin' : 'signup')}
          />
        </KeyboardAvoidingView>
      </SafeAreaView>
    </Screen>
  );
}
