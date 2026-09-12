-- ============================================================
-- NeutriAI :: 0016  record the camera geometry a scan was taken with
--
-- The portion estimator has always had a depth rung -- given the distance from
-- camera to food and the lens field of view, the real-world size of the frame
-- is trigonometry rather than a prior:
--
--     frame_width = 2 * distance * tan(fov / 2)
--
-- It was never reachable. Nothing set depth_mm: the API had no field for it,
-- so every scan fell through to a vessel prior or a plate assumption. This
-- adds the columns so the values are stored alongside the result, which is
-- what makes them auditable later -- when an estimate is wrong we can ask
-- whether the distance was wrong rather than guessing at the geometry.
--
-- Both are nullable and stay null on devices that cannot measure them. The
-- estimator already falls back cleanly; the point is that phones which CAN
-- report this get a materially better rung (~15-20% error against ~25-35%),
-- and it is the only rung that works with no plate in the photo at all --
-- food on paper, a cutting board, a restaurant table.
-- ============================================================

alter table food_scans
  add column if not exists camera_distance_mm numeric(7,1)
    check (camera_distance_mm is null or camera_distance_mm between 80 and 2000),
  add column if not exists camera_fov_deg numeric(5,2)
    check (camera_fov_deg is null or camera_fov_deg between 40 and 100);

comment on column food_scans.camera_distance_mm is
  'Camera-to-subject distance in mm, from ARKit/ARCore depth. Null when the device cannot measure it.';
comment on column food_scans.camera_fov_deg is
  'Horizontal field of view of the capturing lens, in degrees. Null when unreported.';
