# alfred em container, para a rede Docker isolada `ia_local`.
#
# O binário `claude` NÃO vem da internet: o publicar.sh copia o do servidor (o
# nativo da extensão do VS Code) para o contexto do build. Assim o container roda
# exatamente a versão já testada ali.
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Usuário sem privilégio; /data guarda só o progresso.json (médias de tempo).
RUN useradd --uid 10001 --user-group --home-dir /home/alfred --shell /usr/sbin/nologin alfred \
 && mkdir -p /app /data /home/alfred \
 && chown alfred:alfred /data /home/alfred

COPY --chmod=755 claude /usr/local/bin/claude
COPY --chmod=644 alfred.py /app/alfred.py

ENV PYTHONUNBUFFERED=1 \
    HOME=/home/alfred \
    ALFRED_ADDR=0.0.0.0:8765 \
    ALFRED_MEDIAS=/data/progresso.json \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

USER alfred
WORKDIR /home/alfred
# Sem EXPOSE de propósito: a porta só existe dentro da rede ia_local.
CMD ["python3", "/app/alfred.py"]
