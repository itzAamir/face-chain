# FaceChain Verifier

FaceChain Verifier is a localhost-first pipeline that currently:

1. detecting and encoding a face from an uploaded image,
2. performs a genuine Google Web Detection reverse-image search, and
3. confirms candidate images locally with SFace before showing their public pages.

The blockchain proof stage is intentionally reserved for the next build. This version targets exact and near-duplicate images—such as crops, resizes, and light edits—not unrelated photos of the same person.

## Architecture

- `app/`: FastAPI API, plain HTML/CSS/JavaScript frontend, and bundled YuNet/SFace models.
- `app/services/face.py`: in-memory face detection, selection, alignment, and comparison.
- `app/services/search.py`: Web Detection parsing, safe candidate retrieval, social URL classification, and ranking.
- `compose.yaml`: starts the isolated application container; blockchain services are optional-profile placeholders for the next build.
- `compose.google.yaml`: mounts the existing web-search credential read-only.
- `blockchain/`: preserved Hardhat scaffold, not used by the current application workflow.

Only the application port is published to the host. Uploaded images and face embeddings are processed in memory and are not persisted.

## Prerequisites

Install and start Docker Desktop, or install Docker Engine with the Docker Compose plugin.

Verify the installation:

```bash
docker --version
docker compose version
```

No host-level Python, Node.js, Hardhat, or Solidity installation is required.

## Run the project

### 1. Open the project directory

```bash
cd /Users/amirkhan/Desktop/Coding-Playground/work/goa-hackthon
```

### 2. Create the local environment file

```bash
cp .env.example .env
```

The default application address is <http://localhost:8000>.

If port `8000` is already occupied, edit `.env` and change:

```text
APP_PORT=18080
```

The application will then be available at <http://localhost:18080>.

### 3. Start the application

The application can start without search credentials so its health and configuration can be inspected:

```bash
docker compose up --build
```

Compose builds and starts only FastAPI, the static frontend, and the local OpenCV models. The blockchain profile does not start.

The first build can take several minutes while Docker installs Python dependencies. Later starts use the build cache.

To run in the background instead:

```bash
docker compose up --build -d
```

### 4. Start with the web-search credential

If you already have the credential JSON, save it as:

```text
secrets/google-credentials.json
```

Then start the application with both Compose files:

```bash
docker compose \
  -f compose.yaml \
  -f compose.google.yaml \
  up --build app
```

The credential is mounted read-only inside the application container. The `secrets/` directory is ignored by Git.

### 5. Open and verify the application

Open the port selected in `.env`, for example:

- <http://localhost:8000>
- <http://localhost:18080>

Check the running containers:

```bash
docker compose ps
```

Check the API:

```bash
curl http://localhost:8000/api/health
curl http://localhost:8000/api/status
```

Replace `8000` with your configured `APP_PORT` when necessary.

The health response should be:

```json
{"status":"ok"}
```

The status response reports application, face-model, and search-provider readiness without exposing credential data.

## API workflow

Detect faces first:

```bash
curl -X POST http://localhost:8000/api/faces/detect \
  -F 'image=@/absolute/path/to/photo.jpg'
```

The response contains normalized bounding boxes and stable face indexes. Send the original image again with the selected index to run the stateless search:

```bash
curl -X POST http://localhost:8000/api/search \
  -F 'image=@/absolute/path/to/photo.jpg' \
  -F 'face_index=0'
```

`status: "matched"` means at least one result passed Google full/partial image matching, local SFace confirmation, and recognized social-post URL classification. Confirmed general web pages can be returned with `status: "no_match"` when no qualifying social post is present.

## View logs

Follow the application:

```bash
docker compose logs -f app
```

Inspect the application service:

```bash
docker compose logs app
```

Press `Ctrl+C` to stop following logs. This does not stop containers that were started with `-d`.

## Run tests

Run the Python application tests:

```bash
docker compose run --rm --no-deps app pytest
```

The default test target runs the current application tests:

```bash
make test
```

The preserved blockchain scaffold can still be tested explicitly:

```bash
make test-contract
```

## Stop or restart

Stop the containers while preserving them:

```bash
docker compose stop
```

Restart stopped containers:

```bash
docker compose start
```

Remove the application container and private network:

```bash
docker compose down
```

Rebuild after changing dependencies or Dockerfiles:

```bash
docker compose up --build
```

## Troubleshooting

### Port is already allocated

Set another port in `.env`:

```text
APP_PORT=18080
```

Then recreate the application container:

```bash
docker compose up -d
```

### Application is running but the page does not load

Confirm the published port:

```bash
docker compose ps app
```

Then check the application logs:

```bash
docker compose logs app
```

### Credential file is not found

Confirm that this file exists with the exact filename:

```text
secrets/google-credentials.json
```

Use both Compose files when starting the credential-enabled stack.

## Blockchain

Blockchain recording and reverification are not connected in this build. The existing Hardhat scaffold is retained behind the `blockchain` Compose profile for the next phase.

To start that development scaffold explicitly:

```bash
docker compose --profile blockchain up --build
```

## Known limitations

- Matching is designed for exact and near-duplicate images, not identity search across unrelated photos.
- A successful hackathon result requires at least one locally confirmed URL whose hostname and path identify a social post.
- Search quality depends on public indexing and direct access to a matching image URL.
- Public social platforms may hide private, login-only, or unindexed posts.
- The application intentionally skips inaccessible candidates instead of claiming an unverified match.
- Blockchain proof is deferred to the next build.

## Privacy and model files

Uploaded images, candidate images, and face embeddings stay in memory for the duration of a request. The application does not create an upload directory, database record, or biometric log.

The bundled YuNet and SFace ONNX files and their SHA-256 values are documented in `app/models/README.md`; their upstream license files are stored alongside them.
