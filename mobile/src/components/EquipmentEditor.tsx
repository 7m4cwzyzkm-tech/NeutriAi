/**
 * Review and correct an equipment list before it becomes a plan: tap a
 * selected item to remove it, tap one of the remaining known kinds to add
 * it. Local state only -- the caller sends the result with the plan request.
 */
import React from 'react';
import { Text, View } from 'react-native';
import { space, type, useTheme } from '../theme';
import { Chip, Label, Row } from './Primitives';
import { EQUIPMENT_KINDS, equipmentLabel } from '../lib/equipment';

export function EquipmentEditor({ value, onChange, title = 'Your equipment' }: {
  value: string[];
  onChange: (next: string[]) => void;
  title?: string;
}) {
  const c = useTheme();
  const addable = EQUIPMENT_KINDS.filter((k) => !value.includes(k));
  return (
    <View style={{ gap: space.sm }}>
      <Label>{title}</Label>
      {value.length ? (
        <Row gap={space.sm} style={{ flexWrap: 'wrap' }}>
          {value.map((e) => (
            <Chip
              key={e}
              label={`${equipmentLabel(e)}  ✕`}
              active
              onPress={() => onChange(value.filter((x) => x !== e))}
            />
          ))}
        </Row>
      ) : (
        <Text style={[type.caption, { color: c.textDim }]}>
          No equipment — you'll get a bodyweight programme.
        </Text>
      )}
      <Text style={[type.caption, { color: c.textFaint }]}>
        Tap an item to remove it. Missing something? Add it:
      </Text>
      <Row gap={space.sm} style={{ flexWrap: 'wrap' }}>
        {addable.map((k) => (
          <Chip key={k} label={`+ ${equipmentLabel(k)}`} onPress={() => onChange([...value, k])} />
        ))}
      </Row>
    </View>
  );
}
