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
  const [notice, setNotice] = useState<string | null>(null);

  async function submit() {
    setBusy(true);
    setError(null);
    setNotice(null);

    const credentials = { email: email.trim().toLowerCase(), password };

    try {
      // Call THROUGH the client, never a detached reference.
      //
      //   const fn = supabase.auth.signUp;   // <- loses `this`
      //   await fn(credentials);             // <- TypeError, every time
      //
      // Supabase's auth methods are instance methods that use `this`
      // internally, so pulling one off the object and calling it bare throws
      // before it ever reaches the network. Combined with the missing
      // try/finally below, that left this button spinning forever with no
      // error shown — it looked like a hung request and was not one.
      const { data, error: err } =
        mode === 'signup'
          ? await supabase.auth.signUp(credentials)
          : await supabase.auth.signInWithPassword(credentials);

      if (err) {
        setError(err.message);
        return;
      }

      // Sign-up succeeds with NO session when the project requires email
      // confirmation. The auth listener never fires, so without this the
      // screen simply sits there looking broken after a successful signup.
      if (mode === 'signup' && !data.session) {
        setNotice(
          data.user
            ? `Check ${credentials.email} for a confirmation link, then sign in.`
            : 'Account created. Confirm your email, then sign in.',
        );
      }
      // On success WITH a session, onAuthStateChange in App.tsx navigates.
      // Nothing to do here.
    } catch (e: any) {
      // A thrown error (network down, misconfigured client) is not the same as
      // a returned one, and previously escaped entirely.
      setError(e?.message ?? 'Could not reach NeutriAI. Check your connection.');
    } finally {
      // In `finally` on purpose: every path above must clear the spinner,
      // including the ones that return early or throw.
      setBusy(false);
    }
  }

  return (
    <Screen>
      <SafeAreaView style={{ flex: 1 }}>
        <KeyboardAvoidingView
          behavior={Platform.OS === 'ios' ? 'padding' : undefined}
          style={{ flex: 1, justifyContent: 'center', padding: space.xl, gap: space.lg }}
        >
          <View>
            <Label>NeutriAI</Label>
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
          {notice ? <Text style={[type.body, { color: c.accent }]}>{notice}</Text> : null}

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
