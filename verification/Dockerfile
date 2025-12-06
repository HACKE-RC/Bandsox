FROM archlinux:latest

# Basic environment
ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    TZ=Etc/UTC

# ------------------------------------------------------------------
# 1. System setup: Updates, Locales, and Core Packages
# ------------------------------------------------------------------
RUN pacman -Sy --noconfirm archlinux-keyring && \
    pacman -Syu --noconfirm && \
    pacman -S --noconfirm --needed \
        afl++ autoconf automake bandit base-devel bash-completion \
        ca-certificates cargo ccache checksec clang cmake cppcheck curl \
        fd flawfinder gcc gdb git libtool libxml2 lld lldb llvm ltrace \
        man-db man-pages nano nasm ninja nodejs npm openssh \
        pkgconf python ripgrep rust strace sudo tree-sitter \
        unzip valgrind vim wget which yasm zip \
    && \
    echo "en_US.UTF-8 UTF-8" > /etc/locale.gen && \
    locale-gen && \
    pacman -Scc --noconfirm

# ------------------------------------------------------------------
# 2. Install Tools (uv & yay)
# ------------------------------------------------------------------
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /root/.local/bin/uv /usr/local/bin/uv

RUN useradd --create-home build && \
    echo "build ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers && \
    su - build -c "git clone https://aur.archlinux.org/yay.git && cd yay && makepkg -si --noconfirm" && \
    userdel -r build && \
    sed -i '/build/d' /etc/sudoers

# ------------------------------------------------------------------
# 3. User Setup & UV Managed Environment
# ------------------------------------------------------------------
RUN useradd -m -s /bin/bash dev && \
    echo "dev ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/99-dev && \
    chmod 440 /etc/sudoers.d/99-dev

USER dev
WORKDIR /home/dev

ENV VIRTUAL_ENV=/home/dev/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

RUN uv venv $VIRTUAL_ENV
RUN uv tool install supervisor
RUN uv tool install git+https://github.com/pwno-io/treesitter-mcp
RUN uv tool install git+https://github.com/rcx86/git-tools-mcp
RUN uv tool install git+https://github.com/rcx86/shell-tools-mcp

USER root

RUN echo '[supervisord]' > /etc/supervisord.conf && \
    echo 'nodaemon=true' >> /etc/supervisord.conf && \
    echo 'user=root' >> /etc/supervisord.conf && \
    echo 'logfile=/dev/stdout' >> /etc/supervisord.conf && \
    echo 'logfile_maxbytes=0' >> /etc/supervisord.conf && \
    echo '' >> /etc/supervisord.conf && \
    \
    # 1. Code Analysis (TreeSitter) -> Port 8000 (Default for many, or specify)
    echo '[program:code-analysis]' >> /etc/supervisord.conf && \
    echo 'command=/home/dev/.local/bin/code-analysis-server --http' >> /etc/supervisord.conf && \
    echo 'user=dev' >> /etc/supervisord.conf && \
    echo 'autostart=true' >> /etc/supervisord.conf && \
    echo 'autorestart=true' >> /etc/supervisord.conf && \
    echo 'redirect_stderr=true' >> /etc/supervisord.conf && \
    echo 'stdout_logfile=/dev/stdout' >> /etc/supervisord.conf && \
    echo 'stdout_logfile_maxbytes=0' >> /etc/supervisord.conf && \
    echo '' >> /etc/supervisord.conf && \
    \
    # 2. Git Tools -> Port 6000
    echo '[program:git-tools]' >> /etc/supervisord.conf && \
    echo 'command=/home/dev/.local/bin/git-tools-mcp --http --port 6000' >> /etc/supervisord.conf && \
    echo 'user=dev' >> /etc/supervisord.conf && \
    echo 'autostart=true' >> /etc/supervisord.conf && \
    echo 'autorestart=true' >> /etc/supervisord.conf && \
    echo 'redirect_stderr=true' >> /etc/supervisord.conf && \
    echo 'stdout_logfile=/proc/self/fd/1' >> /etc/supervisord.conf && \
    echo 'stdout_logfile_maxbytes=0' >> /etc/supervisord.conf && \
    echo '' >> /etc/supervisord.conf && \
    \
    # 3. Shell Tools -> Port 7000
    echo '[program:shell-tools]' >> /etc/supervisord.conf && \
    echo 'command=/home/dev/.local/bin/shell-tools-mcp-server --http --port 7000' >> /etc/supervisord.conf && \
    echo 'user=dev' >> /etc/supervisord.conf && \
    echo 'autostart=true' >> /etc/supervisord.conf && \
    echo 'autorestart=true' >> /etc/supervisord.conf && \
    echo 'redirect_stderr=true' >> /etc/supervisord.conf && \
    echo 'stdout_logfile=/proc/self/fd/1' >> /etc/supervisord.conf && \
    echo 'stdout_logfile_maxbytes=0' >> /etc/supervisord.conf

EXPOSE 6000 7000 8000

CMD ["/home/dev/.local/bin/supervisord", "-c", "/etc/supervisord.conf"]