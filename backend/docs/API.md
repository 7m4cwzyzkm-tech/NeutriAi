# NeutriAI API

`/v1` prefix. 0 endpoints.

Auth is a Supabase JWT in `Authorization: Bearer <token>`. Endpoints
marked `public` take no token — everything else returns 401 without one.

Rate limit: 120 requests/minute per client.
