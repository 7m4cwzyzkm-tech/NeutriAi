/**
 * The equipment kinds the backend understands -- a mirror of
 * backend/app/services/ai/coach.py's VALID_EQUIPMENT, minus "none" (which is
 * the ABSENCE of equipment, not something to pick). The plan prompt and the
 * exercise-library filter both key off these exact strings, so the app only
 * ever offers these, never free text. Keep in step with the backend set.
 */
export const EQUIPMENT_KINDS = [
  'dumbbell', 'kettlebell', 'barbell', 'resistance_band', 'bench',
  'squat_rack', 'pull_up_bar', 'cable_machine', 'smith_machine', 'treadmill',
  'bike', 'rower', 'medicine_ball', 'trx', 'plate', 'jump_rope', 'box',
  'machine_generic',
] as const;

export function equipmentLabel(kind: string): string {
  return kind.replace(/_/g, ' ');
}

/** A saved/scanned list as the user edits it: "none" is not an item. */
export function editableEquipment(list: readonly string[] | undefined | null): string[] {
  return (list ?? []).filter((e) => e !== 'none');
}

/**
 * The edited list as PlanRequest.equipment. An empty list MUST go out as
 * ["none"]: create_plan treats [] as "not given" and falls back to the
 * scan's stored list -- which would put back exactly what the user removed.
 */
export function equipmentForRequest(list: readonly string[]): string[] {
  return list.length ? [...list] : ['none'];
}
