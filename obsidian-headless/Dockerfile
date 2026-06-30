FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    xvfb \
    x11vnc \
    wget \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libgtk-3-0 \
    libpangocairo-1.0-0 \
    libpango-1.0-0 \
    libcairo2 \
    libfontconfig1 \
    libdbus-1-3 \
    libexpat1 \
    libx11-6 \
    libxcb1 \
    libxext6 \
    libxi6 \
    libxrender1 \
    libxtst6 \
    libxss1 \
    && rm -rf /var/lib/apt/lists/*

ARG OBSIDIAN_VERSION=1.12.7
RUN cd /tmp \
    && wget -q "https://github.com/obsidianmd/obsidian-releases/releases/download/v${OBSIDIAN_VERSION}/Obsidian-${OBSIDIAN_VERSION}.AppImage" \
        -O obsidian.AppImage \
    && chmod +x obsidian.AppImage \
    && ./obsidian.AppImage --appimage-extract \
    && mv squashfs-root /opt/obsidian \
    && rm obsidian.AppImage

EXPOSE 27123 5900

VOLUME ["/vault", "/obsidian-config"]

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]
