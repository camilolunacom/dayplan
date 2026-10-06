# Repository Operations

## Production deployment

- Every production change must exist in a Git commit before syncing source, building an image, or recreating the managed app.
- Run the applicable tests, create the commit, verify `git status --short` is empty, and record `git rev-parse HEAD` before deployment.
- Never deploy directly from uncommitted working-tree files. If production already differs from Git, reconcile it into a reviewed commit before making another production change.
- Production source is the plain copy at `/DATA/src/dayplan`; never run `git pull` there.
- Use only `deploy/zimaos-app.yml` for the ZimaOS-managed app. The root `docker-compose.yml` is for local development.
- Back up the live SQLite database transactionally before deployment. Keep TSDProxy Funnel disabled.
