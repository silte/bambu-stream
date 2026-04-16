FROM mcr.microsoft.com/devcontainers/python:1-3.11-bookworm

USER root

# Remove stale Yarn apt source that can break apt update in this base image.
RUN rm -f /etc/apt/sources.list.d/yarn.list \
	&& apt-get update \
	&& apt-get install -y --no-install-recommends ffmpeg \
	&& rm -rf /var/lib/apt/lists/*

WORKDIR /workspaces/bambu-stream

USER vscode
