"""Artifact Registry repository for container images."""

import pulumi_gcp as gcp

docker_repository = gcp.artifactregistry.Repository(
    "docker",
    repository_id="docker",
    location="us",
    format="DOCKER",
    description="Container images for nisse services",
)

docker_repository_url = docker_repository.location.apply(
    lambda loc: f"{loc}-docker.pkg.dev/{gcp.config.project}/docker",
)
