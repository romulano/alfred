#!/usr/bin/env bash
# Publica o alfred no servidor, dentro da rede Docker isolada `ia_local`.
#
#   SERVIDOR=root@host deploy/publicar.sh
#
# Repetível: rodar de novo só atualiza o que mudou. O .env do servidor (token do
# alfred e autenticação do Claude) é criado na primeira vez e nunca sobrescrito.
set -euo pipefail
SERVIDOR=${SERVIDOR:?defina SERVIDOR=usuario@host}
DESTINO=/opt/alfred
aqui="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ssh "$SERVIDOR" "mkdir -p $DESTINO && chmod 700 $DESTINO"
rsync -a --chmod=F644 "$aqui/../alfred.py" "$aqui/Dockerfile" "$aqui/compose.yaml" "$aqui/.dockerignore" "$SERVIDOR:$DESTINO/"
rsync -a --chmod=F755 "$aqui/ia-local-isolamento.sh" "$SERVIDOR:/usr/local/sbin/"
rsync -a --chmod=F644 "$aqui/ia-local-isolamento.service" "$SERVIDOR:/etc/systemd/system/"

ssh "$SERVIDOR" DESTINO="$DESTINO" bash -s <<'REMOTO'
set -euo pipefail
cd "$DESTINO"

# 1. A rede. A bridge tem nome fixo porque é por ele que o firewall a encontra.
if ! docker network inspect ia_local >/dev/null 2>&1; then
  docker network create --driver bridge \
    --opt com.docker.network.bridge.name=br-ia_local ia_local
fi
ponte=$(docker network inspect -f '{{index .Options "com.docker.network.bridge.name"}}' ia_local)
if [ "$ponte" != br-ia_local ]; then
  echo "!! a rede ia_local já existe com a bridge '$ponte' (esperado br-ia_local): o firewall não a cobriria" >&2
  exit 1
fi

# 2. O firewall, antes de existir qualquer container na rede.
systemctl daemon-reload
systemctl enable ia-local-isolamento.service
systemctl restart ia-local-isolamento.service

# 3. Segredos: criados uma vez, nunca impressos.
if [ ! -f .env ]; then
  umask 077
  cat >.env <<EOF
# Token do alfred: todo POST precisa de "Authorization: Bearer <token>".
ALFRED_TOKEN=$(openssl rand -hex 32)

# Autenticação do Claude Code dentro do container. Gere com "claude setup-token"
# (vale 1 ano, é só do alfred) e descomente:
# CLAUDE_CODE_OAUTH_TOKEN=
EOF
fi
chmod 600 .env

# 4. O claude: o binário nativo mais novo da extensão do VS Code deste servidor.
bin=$(ls -d /root/.vscode-server/extensions/anthropic.claude-code-*-linux-x64/resources/native-binary/claude | sort -V | tail -1)
cmp -s "$bin" claude || cp "$bin" claude

# 5. Sobe.
docker compose up -d --build
REMOTO
