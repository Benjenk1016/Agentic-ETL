# Admin Docker Guide

This guide explains how to build and push Docker images for this project, and where to update image names/tags so consumer deployments use the correct versions.

## #) Build Images From Project Root

Run these commands from the working repository root directory.

```powershell
docker build -f docker/Dockerfile.api -t [DOCKER_USER_NAME]/agentic-end-to-end-api:v#.# .
docker build -f docker/Dockerfile.web -t [DOCKER_USER_NAME]/agentic-end-to-end-web:v#.# .
```



Notes:
- The final `.` is required. It is the build context.
- Using `docker/Dockerfile.api` and `docker/Dockerfile.web` ensures Docker uses the correct files.
- :v#.# is a tag for versions. They should be incremented but can be overwritten which is less safe.

## 2) Push Images

```powershell
docker push [DOCKER_USER_NAME]/agentic-end-to-end-api:v#.#
docker push [DOCKER_USER_NAME]/agentic-end-to-end-web:v#.#
```

## 3) Update Image Names/Tags Used By Consumer Bundle

The consumer deployment reads image tags from environment variables first, then falls back to defaults in compose.

#### Update all three places so there is no mismatch:

### A. Runtime values (highest priority)
File: consumer-bundle/.env
Lines : 7 and 8
Set:

```dotenv
WEB_IMAGE=[DOCKER_USER_NAME]/agentic-end-to-end-web:v#.#
API_IMAGE=[DOCKER_USER_NAME]/agentic-end-to-end-api:v#.#
```

### B. Template defaults for new users
File: consumer-bundle/.env.example
Lines : 7 and 8
Set:

```dotenv
WEB_IMAGE=[DOCKER_USER_NAME]/agentic-end-to-end-web:v#.#
API_IMAGE=[DOCKER_USER_NAME]/agentic-end-to-end-api:v#.#
```

### C. Compose fallback defaults
File: consumer-bundle/docker-compose.yml
Lines : 3 and 10
Confirm these match:

```yaml
image: ${WEB_IMAGE:-[DOCKER_USER_NAME]/agentic-end-to-end-web:v#.#}
image: ${API_IMAGE:-[DOCKER_USER_NAME]/agentic-end-to-end-api:v#.#}
```

## 4) Restart After Updating Tags or Compose

After changing image names/tags or compose settings, recreate containers:
2 methods : 

A. Use .bat files to stop and restart images, this does the same as powershell commands. 

```powershell
docker compose down
docker compose pull
docker compose up -d
```

You do not need to rebuild images on consumer machines when using pushed images from Docker Hub.

## 5) Important Versioning Rule

If you change backend or frontend code (for example backend/api/app.py), those changes are not included in already-pushed tags.

You must : 
1. Build a new image tag (recommended: bump version, for example v0.2).
2. Push that new tag.
3. Update consumer-bundle/.env and .env.example and compose fallback defaults to the new tag.
4. Recreate containers.

## 6) Recommended Release Workflow#
1. Make code changes.
2. Build API and WEB images with new version tags.
3. Push both tags.
4. Update consumer-bundle/.env, consumer-bundle/.env.example, and consumer-bundle/docker-compose.yml defaults.
5. Restart stack with docker compose down ; docker compose pull ; docker compose up -d.

This keeps deployments predictable and avoids image-tag drift between files.
