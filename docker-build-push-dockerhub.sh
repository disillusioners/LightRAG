#!/bin/bash
set -e

# Note: current tag version is: v1.6.2

# Load Docker Hub credentials
if [ -f ".env-dockerhub" ]; then
    set -a
    source .env-dockerhub
    set +a
else
    echo "Error: .env-dockerhub file not found"
    exit 1
fi

# Configuration
IMAGE_NAME="${DOCKERHUB_USERNAME}/lightrag"
DOCKERFILE="Dockerfile"
TAG="latest"

# Get version from git tags
VERSION=$(git describe --tags --abbrev=0 2>/dev/null || echo "dev")

echo "=================================="
echo "  Docker Hub Multi-Architecture Build"
echo "=================================="
echo "Image: ${IMAGE_NAME}:${TAG}"
echo "Version: ${VERSION}"
echo "Platforms: linux/amd64, linux/arm64"
echo "=================================="
echo ""

# Check Docker login status
if [ -z "$DOCKERHUB_TOKEN" ]; then
    if ! docker info 2>/dev/null | grep -q "Username"; then
        echo "⚠️  Warning: Not logged in to Docker Hub"
        echo "Please login first: docker login"
        echo ""
        read -p "Continue anyway? (y/n) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
else
    echo "Using DOCKERHUB_TOKEN for authentication"
    echo "$DOCKERHUB_TOKEN" | docker login -u "$DOCKERHUB_USERNAME" --password-stdin
fi

# Check if buildx builder exists, create if not
if ! docker buildx ls | grep -q "multiarch-builder"; then
    echo "Creating multi-platform buildx builder..."
    docker buildx create --name multiarch-builder --driver docker-container --use
    docker buildx inspect --bootstrap
else
    echo "Using existing buildx builder: multiarch-builder"
    docker buildx use multiarch-builder
fi

echo ""
echo "Building and pushing multi-architecture image..."
echo ""

# Build and push one platform at a time (sequential to avoid timeouts)
for PLATFORM in linux/amd64 linux/arm64; do
  echo ""
  echo "Building platform: ${PLATFORM}"
  echo ""

  docker buildx build \
    --platform ${PLATFORM} \
    --file ${DOCKERFILE} \
    --tag ${IMAGE_NAME}:${TAG} \
    --tag ${IMAGE_NAME}:${VERSION} \
    --push \
    .

  echo ""
  echo "✓ Platform ${PLATFORM} pushed successfully"
done

echo ""
echo "✓ Build and push complete!"
echo ""
echo "Images pushed:"
echo "  - ${IMAGE_NAME}:${TAG}"
echo "  - ${IMAGE_NAME}:${VERSION}"
echo ""
echo "Verifying multi-architecture manifest..."
echo ""

# Verify
docker buildx imagetools inspect ${IMAGE_NAME}:${TAG}

echo ""
echo "✓ Verification complete!"
echo ""
echo "Pull with: docker pull ${IMAGE_NAME}:${TAG}"
