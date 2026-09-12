/**
 * How a portion was sized, in words a person reading their lunch understands.
 *
 * This lived twice — once in ScanScreen, once in MealDetailScreen — and both
 * copies were missing `vessel_reference`, which has been shipping since the
 * vessel rung was added. The fallback prints the raw value, so the same meal
 * read "Plate reference" on one screen and "vessel_reference" on the other.
 *
 * One map, imported by both. A new rung in the estimator is a one-line change
 * here and it is correct everywhere.
 */
export const METHOD_LABEL: Record<string, string> = {
  plate_reference: 'Measured plate',
  vessel_reference: 'Container size',
  reference_object: 'Object in photo',
  depth_model: 'Depth estimate',
  multi_image: 'Multi-angle',
  pixel_area: 'Pixel area',
  ai_prior: 'Typical serving',
  user_entered: 'You entered this',
  barcode: 'Barcode',
};

/** Never show a user a snake_case enum value. */
export function methodLabel(method: string | null | undefined): string {
  if (!method) return 'Estimated';
  return METHOD_LABEL[method] ?? method.replace(/_/g, ' ');
}
