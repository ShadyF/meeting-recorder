# Keep the runtime base stable so image rebuilds use the reviewed Ubuntu image.
FROM docker.io/library/ubuntu:24.04@sha256:1e0a86e57d247923571b75e0aaf48a1449cf8c543d51fb3e07a4a7d7bfa79316

# Keep release metadata overridable without adding a publishing step to the image build.
ARG OCI_TITLE="Smart Meeting Recorder"
ARG OCI_SOURCE="https://github.com/ShadyF/meeting-recorder"
ARG OCI_LICENSE="MIT"
ARG OCI_REVISION="unknown"
ARG OCI_VERSION="0.3.5"
LABEL org.opencontainers.image.title="${OCI_TITLE}" \
      org.opencontainers.image.source="${OCI_SOURCE}" \
      org.opencontainers.image.licenses="${OCI_LICENSE}" \
      org.opencontainers.image.revision="${OCI_REVISION}" \
      org.opencontainers.image.version="${OCI_VERSION}"

# Install only the runtime libraries needed by the GTK recorder and its capture backends.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
        ca-certificates \
        python3 \
        python3-gi \
        python3-gi-cairo \
        tzdata \
        gir1.2-gtk-3.0 \
        gir1.2-notify-0.7 \
        gir1.2-appindicator3-0.1 \
        libappindicator3-1 \
        gir1.2-secret-1 \
        libsecret-tools \
        ffmpeg \
        pulseaudio-utils \
        gstreamer1.0-tools \
        gstreamer1.0-pipewire \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        xdg-desktop-portal \
        libnotify-bin \
        xdg-utils \
        x11-utils \
        x11-xserver-utils \
    && rm -rf /var/lib/apt/lists/*

# Copy only the Python runtime, the container admission check, and the launcher.
COPY meeting_recorder/ /opt/meeting-recorder/meeting_recorder/
COPY container/preflight.py /opt/meeting-recorder/preflight.py
COPY container/meeting-recorder /usr/local/bin/meeting-recorder

# Install the existing desktop integration and icon assets at their standard paths.
COPY packaging/meeting-recorder.desktop /usr/share/applications/meeting-recorder.desktop
COPY packaging/meeting-recorder-settings.desktop /usr/share/applications/meeting-recorder-settings.desktop
COPY packaging/meeting-recorder.svg /usr/share/icons/hicolor/scalable/apps/meeting-recorder.svg
COPY packaging/meeting-recorder-recording.svg /usr/share/icons/hicolor/scalable/apps/meeting-recorder-recording.svg
COPY packaging/meeting-recorder-paused.svg /usr/share/icons/hicolor/scalable/apps/meeting-recorder-paused.svg

# Keep the application independent of the image's default user and Python settings.
WORKDIR /opt/meeting-recorder
ENV LANG=C.UTF-8 \
    XDG_CONFIG_HOME=/config \
    XDG_STATE_HOME=/state \
    XDG_CACHE_HOME=/cache \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Run through the admission check before the foreground daemon starts.
ENTRYPOINT ["/usr/local/bin/meeting-recorder"]
CMD ["run"]
