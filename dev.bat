@echo off
REM ===========================================================
REM  NeutriAI dev launcher
REM
REM  Handles the two things that are easy to forget in a fresh
REM  terminal: being in backend\, and activating the venv.
REM  Runs from anywhere -- %~dp0 is this file's own folder.
REM
REM    dev api        start the API with reload
REM    dev worker     start the background worker
REM    dev smoke      end-to-end smoke test
REM    dev verify     check the Supabase setup
REM    dev url        check SUPABASE_URL and the JWT scheme
REM    dev keys       check every API key is valid
REM    dev drift      code vs schema consistency
REM    dev test       run the pytest suite
REM    dev scan       scan accuracy bench
REM    dev scandebug  stage-by-stage scan diagnostic, real tracebacks
REM    dev portion    portion estimator bench
REM    dev footprint  what the measured footprint is worth (no API key)
REM    dev shell      just drop me in an activated shell
REM ===========================================================
setlocal

cd /d "%~dp0backend"

if not exist ".venv\Scripts\activate.bat" (
  echo.
  echo   No virtualenv found at backend\.venv
  echo   Create it with:
  echo       python -m venv .venv
  echo       .venv\Scripts\activate
  echo       pip install -r requirements.txt
  echo.
  exit /b 1
)

call ".venv\Scripts\activate.bat"

if "%~1"==""          goto :usage
if "%~1"=="api"       goto :api
if "%~1"=="worker"    goto :worker
if "%~1"=="smoke"     goto :smoke
if "%~1"=="verify"    goto :verify
if "%~1"=="url"       goto :url
if "%~1"=="test"      goto :test
if "%~1"=="portion"   goto :portion
if "%~1"=="scan"      goto :scan
if "%~1"=="scandebug" goto :scandebug
if "%~1"=="migrations" goto :migrations
if "%~1"=="calibrate" goto :calibrate
if "%~1"=="coachlab"  goto :coachlab
if "%~1"=="recipelab" goto :recipelab
if "%~1"=="keys"      goto :keys
if "%~1"=="drift"     goto :drift
if "%~1"=="benchall"  goto :benchall
if "%~1"=="repeat"    goto :repeat
if "%~1"=="seg"       goto :seg
if "%~1"=="depth"     goto :depth
if "%~1"=="depthcheck" goto :depthcheck
if "%~1"=="measure"   goto :measure
if "%~1"=="mask"      goto :mask
if "%~1"=="footprint" goto :footprint
if "%~1"=="apispec"   goto :apispec
if "%~1"=="scaleaudit" goto :scaleaudit
if "%~1"=="segcheck"   goto :segcheck
if "%~1"=="platecheck" goto :platecheck
if "%~1"=="dumpmask"  goto :dumpmask
if "%~1"=="maskstability" goto :maskstability
if "%~1"=="boxreplay" goto :boxreplay
if "%~1"=="dead"       goto :dead
if "%~1"=="replay"     goto :replay
if "%~1"=="heights"    goto :heights
if "%~1"=="shell"     goto :shell
goto :usage

:api
echo Starting API on http://localhost:8000  (docs at /docs)
REM  THE SCAN'S OWN LOGS COME OUT HERE, NOT OUT OF THE BENCH.
REM
REM  `dev benchall` talks to this process over HTTP, so everything the scan
REM  path logs -- plate_box_vs_circle, plate_hint_bound, no_plate_for_box_rule
REM  -- is written to THIS window and never reaches the bench's own output.
REM  One run's box-versus-circle evidence survived only because this window
REM  happened to still be open when it was wanted. To keep it instead:
REM
REM      dev api > survey-api-log.txt 2>&1
REM
REM  AND THE MASKS, WHICH ARE NOT RECOVERABLE AFTERWARDS.
REM
REM  For a week the offline replays used masks REGENERATED from the local
REM  JPEG. Production encodes different bytes -- upload, fetch, decode,
REM  re-encode -- and the sets proved different: production's own logged box
REM  takes 7 masks on the real set and 6 on the regenerated one, which a
REM  per-mask rule makes impossible unless the masks differ. Every number
REM  measured on the cache was measuring the cache.
REM
REM  So the pipeline emits its own. This is set HERE rather than left to be
REM  remembered, because a scan that is not captured cannot be captured
REM  later -- the photograph is gone and a fresh call is a different set.
REM  A few hundred KB per scan, into a directory git ignores.
set NUTRIAI_MASK_DUMP=%~dp0mask_dumps
python -m uvicorn app.main:app --reload --port 8000
goto :eof

:seg
REM  What would measuring the food area be worth? A measuring instrument,
REM  not part of the scan pipeline -- nothing here changes an estimate.
shift
python -m scripts.segment_lab %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:measure
REM  Does MEASURING each food's footprint beat asking the model for it?
REM  Runs the detector, cuts each item out of its own box with GrabCut, and
REM  estimates it both ways against the weighed truth. An instrument, not a fix.
shift
python -m scripts.measure_lab %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:mask
REM  Can we MEASURE the food instead of asking how big it is? Segments the
REM  plate, splits it between the model's box CENTRES, and checks the answer
REM  against the kitchen scale. Add --overlay to write the mask pictures.
shift
python -m scripts.mask_lab %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:replay
REM  RUN THIS BEFORE ANY PAID BENCH.
REM
REM  Twice now a change passed the whole unit suite and was still wrong on
REM  real photographs, because every fixture in that suite draws a plate as
REM  a solid ellipse. SAM2 does not: it returns a plate with the food PUNCHED
REM  OUT of it, and a table that is a ring around the plate. Neither shape
REM  existed in any test, so neither was ever tested, and both walked through
REM  a green suite into a run that cost money.
REM
REM  This replays the real photographs through the real selection code with
REM  SAM2-shaped masks, reproduces the exact configuration that broke the
REM  bench, and fuzzes 400 random layouts for the invariants. No network, no
REM  cost, about a minute.
shift
python -m scripts.replay_check %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:heights
REM  What a measured footprint actually weighs, against the kitchen scale.
REM
REM  Once the footprint is measured, two numbers we never measured decide the
REM  weight: a height prior and a density, both tables. On the weighed carrots
REM  they say 21 mm and 0.85; the same photograph solved against the scale
REM  implies 10.7 mm. Their PRODUCT sets the answer, so their product is what
REM  this measures -- grams per mm2 of footprint, straight off the scale.
REM
REM  Measures the footprint TWICE per photograph from one call: the per-piece
REM  masks unioned, and the plate mask's own holes. They come from different
REM  parts of the same answer, so agreement means the footprint is real.
REM
REM  About 2 cents per photograph, and every mask is cached on the way past --
REM  a second run reads the cache and is free. --fresh pays again.
shift
python -m scripts.height_fit %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:dead
REM  What is defined here and reachable by nobody. This project keeps
REM  producing code that was built and never connected; dead code is the
REM  same disease one step later -- something that WAS wired, was replaced,
REM  and stayed. It prints names and deletes nothing, because whether a
REM  thing should go is a judgement. A test enforces that the list is empty.
shift
python -m scripts.dead_code %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:boxreplay
REM  IS THE FOOTPRINT'S INSTABILITY THE MODEL'S BOX? Offline, zero model calls.
REM
REM  segment_hosted.py:775 includes a mask iff its CENTROID lands in the model's
REM  box -- a binary test on a box quantised to a 0.05 grid. This replays that
REM  rule against the cached masks in mask_overlays/masks-*.npz, perturbing the
REM  box by +/-1 grid step (the result) and +/-2 (a labelled stress case), and
REM  compares it with an overlap-fraction rule swept from 0.2 to 0.8.
REM
REM  Reports DRIFT -- |area_k - area_0| / area_0, which reads straight through
REM  to grams -- and EMPTIES, counted as a first-class outcome: a perturbation
REM  that empties an item's footprint sends its weight to zero or to a prior,
REM  which is the failure being hunted.
REM
REM  Boxes are derived from each item's own mask extent and NOT enlarged. One
REM  grid step is 5% of frame and small foods cover about 5% of frame, so a box
REM  leaving its food is the finding, not a bad seed.
shift
python -m scripts.box_replay %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:dumpmask
REM  PRODUCTION'S OWN MASKS, DRAWN ON THE PHOTOGRAPH. Offline, zero model calls.
REM
REM  Reads mask_dumps/ -- written by the pipeline at the moment it chose, keyed
REM  by the digest of the bytes sent to the model -- and paints the union that
REM  set the grams solid, every refused mask outlined with the reason it was
REM  refused, the model's own box, and the plate fence the pool was bounded to.
REM  The verdicts come from HostedSegmenter._mask_verdict, the same method
REM  _union_in_box calls, so the picture cannot disagree with production.
REM
REM  This replaces `dev mask --overlay` for any question about the footprints
REM  that reach the weight. `dev mask` renders the COLOUR rule over three
REM  retired photographs from hand-typed box centres and ignores a filename --
REM  no photograph the bench scores is in its table.
REM
REM  Needs a dump. `dev api` sets NUTRIAI_MASK_DUMP, but a RELOADING server
REM  does NOT pick it up: reload re-reads code, not the environment. Stop it
REM  and start it again, then scan.
shift
python -m scripts.dump_overlay %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:maskstability
REM  IS SAM2 THE SAME TWICE? Three calls, one answer.
REM
REM  Every spread this bench has reported excludes the segmenter by accident:
REM  _auto_masks memoises on the digest of the image it re-encodes and the
REM  segmenter is a process singleton, so `--runs 3` makes ONE segmenter call.
REM  The published spreads are a FLOOR, not a total.
REM
REM  Three separate segmenter instances, identical bytes, no production change
REM  and no bench-only code path. Compares the MASK SET -- count, sorted areas,
REM  pair IoU -- never the union, which depends on the model's box.
REM
REM  Run this BEFORE any box-perturbation replay: the replay holds the mask set
REM  fixed, and if SAM2 varies that assumption is false.
REM
REM  Three model calls. Both mask sets are saved, so later comparisons are free.
shift
python -m scripts.mask_stability %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:platecheck
REM  WHICH PLATE A SCAN ACTUALLY GETS, drawn over the photograph.
REM
REM  The Hough detector was scored 30/30 through plate_surface(rgb, None).
REM  Every real scan passes a plate box, and the box branch returned before
REM  Hough was ever reached -- so that 30/30 was measured through a door
REM  production does not use. This calls plate_surface the way a scan does.
REM
REM  Free. --sam2 also asks the segmenter and costs about 2c per photograph,
REM  and is what fills in the IoU column and the threshold table.
shift
python -m scripts.plate_check %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:segcheck
REM  SEGMENTER_PROVIDER has been unset for weeks and every bench run has said
REM  "0 live" because of it. Switching it on is four .env lines, and getting
REM  any one of them wrong looks identical from the outside: the run finishes,
REM  the numbers print, and the footprint never reaches the grams. This asks
REM  the model what inputs it takes, checks those against what we send, and
REM  makes one real call on the photo the colour rule cannot do at all.
REM  Keys are never printed -- length and a four-character prefix only.
REM
REM    dev segcheck --candidates       every SAM-ish model Replicate serves,
REM                                    sorted by whether it takes point
REM                                    COORDINATES or only the automatic
REM                                    generator's grid. Schemas only, free.
REM    dev segcheck owner/model --schema-only
REM                                    read one candidate's schema without
REM                                    firing a paid call at whatever is
REM                                    currently configured.
REM
REM  points_per_side is NOT a point prompt. It is the automatic generator's
REM  sampling grid, it is what made meta/sam-2 look prompted, and both flags
REM  above exclude it by name.
shift
python -m scripts.seg_check %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:scaleaudit
REM  Every bench case declares a vessel diameter, and every gram on that photo
REM  is scaled by it -- area goes as the square, so 10% out on the diameter is
REM  21% on every weight. This measures the vessel against the credit card in
REM  the same picture and says whether the declared number holds up.
shift
python -m scripts.scale_audit %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:apispec
REM  Export the API as OpenAPI, plus a one-page index, into backend\docs.
REM  Taken from the running app rather than written by hand, so it cannot
REM  describe a route that does not exist. Contains no keys, no URLs, no
REM  environment values -- safe to send to someone outside the project.
shift
python -m scripts.api_spec %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:footprint
REM  What the measured footprint is worth, against the kitchen scale, WITHOUT
REM  an API key. Hand-annotated plate and item boxes, footprints from the
REM  pixels, grams from the geometry, compared with the weighed truth -- and
REM  the same items sized the way the bounding-box rail sizes them today.
REM  Run this after setting SEGMENTER_PROVIDER to see what SAM2 changed.
shift
python -m scripts.footprint_bench %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:depthcheck
REM  Is the depth ENDPOINT wired up, and is it returning a depth map or a
REM  colour-mapped picture of one? Run it with no arguments first: that is
REM  offline, costs nothing, and proves the decode and the geometry before
REM  a key is pasted.  (dev depthcheck  /  dev depthcheck 13 --width 254)
shift
python -m scripts.depth_probe %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:depth
REM  Does the model know how tall the food is? Compares the height it reports
REM  against a height you measured with a ruler. The one measurement standing
REM  between USE_MEASURED_HEIGHT and being switched back on.
shift
python -m scripts.depth_lab %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:worker
echo Starting background worker (7 scheduled jobs)
python -m app.workers.scheduler
goto :eof

:smoke
shift
python -m scripts.smoke_test %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:verify
shift
python -m scripts.verify_supabase %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:url
shift
python -m scripts.check_supabase_url %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:test
python -m pytest -q
goto :eof

:portion
shift
python -m scripts.portion_lab %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:scan
shift
python -m scripts.scan_bench %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:scandebug
shift
python -m scripts.scan_debug %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:benchall
shift
python -m scripts.bench_all %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:repeat
REM  The noise floor. Same photo N times, so a change can be told from the
REM  model's own spread -- measured at 8.7% on meal totals, which means a
REM  single-run comparison cannot resolve anything smaller than ~17%.
shift
python -m scripts.scan_repeat %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:drift
REM  Do the code and the migration FILES agree? Static, no database.
shift
python -m scripts.check_schema_drift %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:calibrate
REM  Register a vessel you measured with a tape. Worth about 28 photos of the
REM  same plate -- scale is the largest error left in the estimator, and a tape
REM  reading is the one input that ends the guessing for that dish outright.
shift
python -m scripts.calibrate %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:coachlab
shift
python -m scripts.coach_lab %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:recipelab
shift
python -m scripts.recipe_lab %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:migrations
REM  Are the migrations actually APPLIED to the live database? `dev drift`
REM  reads files; this asks the database. Both can agree perfectly while the
REM  schema is missing every one of them, because nobody pasted the SQL in --
REM  which produced a 500 on every scan while drift reported all clear.
REM
REM  The script existed and had no command, so the only way to run the check
REM  was to know the module path. Found by the same audit that found six other
REM  things built and not wired in.
shift
python -m scripts.check_migrations %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:keys
shift
python -m scripts.check_keys %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :eof

:shell
echo Virtualenv active. You are in backend\.
cmd /k
goto :eof

:usage
echo.
echo   NeutriAI dev launcher
echo.
echo     dev api        start the API      (http://localhost:8000/docs)
echo     dev worker     start the worker
echo     dev smoke      end-to-end smoke test
echo     dev verify     check Supabase setup
echo     dev url        check SUPABASE_URL and JWT scheme
echo     dev keys       check every API key is valid
echo     dev drift      does the code write anything the DB will refuse?
echo     dev benchall   accuracy across every weighed photo
echo     dev repeat     same photo N times   (dev repeat photos\08.jpg --n 10)
echo     dev seg        what measured food area would be worth
echo     dev depth      is the reported food height real?  (dev depth p.jpg --peaks "meat=55")
echo     dev depthcheck is the depth endpoint wired up right?  (run with no args first)
echo     dev measure    measured footprint vs the model's area claim
echo     dev test       run the test suite
echo     dev portion    portion estimator bench  (try: dev portion --ladder)
echo     dev scan       scan accuracy bench      (dev scan photo.jpg --actual "rice=180")
echo     dev scandebug  why did a scan fail      (dev scandebug photo.jpg)
echo     dev mask       what the food mask caught     (dev mask --overlay)
echo     dev footprint  what a measured footprint is worth (no API key needed)
echo     dev apispec    export openapi.json + API.md to send someone
echo     dev scaleaudit does every bench photo agree with its own card?
echo     dev maskstability  is SAM2 the same twice? (3 calls)
echo     dev dumpmask       draw production's own masks (free)
echo     dev boxreplay      does the box move the footprint? (free)
echo                        --dump DIR replays real scans from mask_dumps\
echo     dev platecheck which plate a scan gets, drawn over the photo
echo     dev segcheck   is SAM2 configured, and does it answer?
echo                    --candidates   which models take point coordinates
echo                    --schema-only  read a schema without paying
echo     dev dead       what is defined and reachable by nobody?
echo     dev replay     replay real photos through the mask rule (before paying)
echo     dev heights    what a measured footprint weighs, against the scale
echo     dev migrations are the migrations APPLIED to the live database?
echo     dev calibrate  register a vessel you measured with a tape
echo     dev coachlab   coach reply bench
echo     dev recipelab  recipe parsing bench
echo     dev shell      activated shell in backend\
echo.
goto :eof
