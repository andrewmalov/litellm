#!/bin/bash
set -e

REGISTRY="${REGISTRY:-cr.selcloud.ru/ttkdc}"
PLATFORM="${PLATFORM:-linux/amd64}"
IMAGE_NAME="${IMAGE_NAME:-litellm}"
TAG="${TAG:-latest}"

FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}:${TAG}"

echo "=== Docker Build & Push ==="
echo "Registry: ${REGISTRY}"
echo "Platform: ${PLATFORM}"
echo "Image:    ${FULL_IMAGE}"
echo ""

docker buildx build \
  --platform "${PLATFORM}" \
  --build-arg REGISTRY="${REGISTRY}" \
  --build-arg PLATFORM="${PLATFORM}" \
  -t "${FULL_IMAGE}" \
  -f Dockerfile \
  --push \
  .

echo ""
echo "=== Done ==="
echo "Image pushed: ${FULL_IMAGE}"
