# ClipFlow Railway FFmpeg Worker

Deploy this directory as a new Railway service from a GitHub repository (Dockerfile detected).

Set these Railway variables:
- `CLIPFLOW_WORKER_SECRET`: long random string, exactly matching Floot's secret.
- `PUBLIC_BASE_URL`: `https://YOUR-WORKER.up.railway.app`
- `FLOOT_APP_ORIGIN`: exact public HTTPS origin of your Floot app (no trailing slash). It must match the origin where the user starts processing.

Then connect the same `CLIPFLOW_WORKER_SECRET` and `RAILWAY_WORKER_URL` (`PUBLIC_BASE_URL`) in Floot's secure credential flow.

The service needs persistent local disk or a single instance during preparation/rendering. `/tmp` is ephemeral and may be lost on restart. This is an MVP worker, not an autoscaling production queue.

Test `GET /health`. Start with a short MP4 under 100 MB. Floot queueTask may require Floot Pro. Clips are center-cropped with burned-in grouped captions; speaker tracking is not yet implemented.

Security: never expose the secret. The worker restricts callback origin and checks private-network source hosts. For production hardening, pin source hosts, add resource quotas, and use a durable object store for extracted audio.
