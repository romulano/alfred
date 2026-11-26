#!/usr/bin/env bash
# Prova que o alfred do servidor responde e que só a rede ia_local fala com ele.
#
#   SERVIDOR=root@host deploy/validar.sh
#
# Sai com 0 só se tudo passar. A chamada de IA de verdade (POST com token) é
# a única que custa; ela pede uma palavra só.
set -uo pipefail
SERVIDOR=${SERVIDOR:?defina SERVIDOR=usuario@host}
IP_PUBLICO=${SERVIDOR#*@}
falhas=0
ok()   { echo "  ok    $*"; }
erro() { echo "  FALHA $*"; falhas=$((falhas + 1)); }

echo "== da internet (esta máquina → $IP_PUBLICO)"
for porta in 8765 80 443; do
  if timeout 6 bash -c "exec 3<>/dev/tcp/$IP_PUBLICO/$porta" 2>/dev/null; then
    [ "$porta" = 8765 ] && erro "porta $porta ABERTA na internet" || echo "  info  porta $porta aberta (borda nginx, já existia)"
  else
    [ "$porta" = 8765 ] && ok "porta $porta fechada na internet" || echo "  info  porta $porta fechada para este IP (Cloudflare-only)"
  fi
done

ssh "$SERVIDOR" bash -s <<'REMOTO'
set -uo pipefail
falhas=0
ok()   { echo "  ok    $*"; }
erro() { echo "  FALHA $*"; falhas=$((falhas + 1)); }

IP=$(docker inspect -f '{{(index .NetworkSettings.Networks "ia_local").IPAddress}}' alfred)
GW=$(docker network inspect -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}' ia_local)
PUB=$(ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1)

# Cliente de teste: a própria imagem do alfred (tem python), sem privilégio.
# Imprime "<código http> <corpo>" ou "sem-conexao <motivo>".
sonda() { # $1 = rede ("host" vale), $2 = url, $3 = GET | POST | POST-token, $4 = prazo
  docker run --rm --network "$1" --env-file /opt/alfred/.env --cap-drop ALL \
    --security-opt no-new-privileges --entrypoint python3 alfred:local -c '
import json, os, sys, urllib.error, urllib.request
url, modo, prazo = sys.argv[1], sys.argv[2], int(sys.argv[3])
corpo, cab = None, {}
if modo != "GET":
    corpo = json.dumps({"messages": [{"role": "user", "content": "Responda apenas com a palavra: pong"}]}).encode()
    cab["Content-Type"] = "application/json"
    if modo == "POST-token":
        cab["Authorization"] = "Bearer " + os.environ["ALFRED_TOKEN"]
try:
    r = urllib.request.urlopen(urllib.request.Request(url, data=corpo, headers=cab), timeout=prazo)
    print(r.status, r.read().decode()[:400])
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode()[:400])
except Exception as e:
    print("sem-conexao", type(e).__name__, str(e)[:120])
' "$2" "$3" "${4:-6}" 2>&1
}
espera_codigo() { # $1 = código esperado, $2 = descrição, resto = sonda
  local quer=$1 desc=$2; shift 2
  local r; r=$(sonda "$@")
  [ "${r%% *}" = "$quer" ] && ok "$desc → ${r:0:90}" || erro "$desc → ${r:0:300}"
}
bloqueado() { # $1 = descrição, resto = sonda
  local desc=$1; shift
  local r; r=$(sonda "$@")
  case "$r" in sem-conexao*) ok "$desc → bloqueado (${r:12:60})" ;; *) erro "$desc → RESPONDEU: ${r:0:120}" ;; esac
}
host_bloqueado() { # $1 = url
  local c; c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$1")
  [ "$c" = 000 ] && ok "host → $1 → bloqueado" || erro "host → $1 → RESPONDEU $c"
}

echo "== o container"
estado=$(docker inspect -f '{{.State.Status}}/{{.State.Health.Status}}' alfred)
[ "$estado" = running/healthy ] && ok "alfred $estado" || erro "alfred $estado"
redes=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' alfred)
[ "$redes" = "ia_local " ] && ok "só na rede ia_local ($IP)" || erro "redes: $redes"
[ -z "$(docker port alfred)" ] && ok "nenhuma porta publicada no host" || erro "portas publicadas: $(docker port alfred)"
ss -Htln | grep -q ':8765\b' && erro "algo escuta 8765 no host" || ok "nada escuta 8765 no host"
interno=$(docker network inspect -f '{{.Internal}}' ia_local)
echo "  info  ia_local: bridge br-ia_local, internal=$interno (precisa sair para a API do Claude)"

echo "== de um container NA rede ia_local (deve funcionar)"
espera_codigo 200 "GET  http://alfred:8765/healthz" ia_local http://alfred:8765/healthz GET
espera_codigo 401 "POST sem token" ia_local http://alfred:8765/v1/chat/completions POST
espera_codigo 200 "POST com token (IA de verdade)" ia_local http://alfred:8765/v1/chat/completions POST-token 180

echo "== do host / IP da máquina (deve falhar)"
host_bloqueado "http://$IP:8765/healthz"
host_bloqueado "http://$GW:8765/healthz"
host_bloqueado "http://$PUB:8765/healthz"
host_bloqueado "http://127.0.0.1:8765/healthz"
bloqueado "container network_mode=host → $IP:8765" host "http://$IP:8765/healthz" GET
ping -c1 -W2 "$IP" >/dev/null 2>&1 && erro "host pinga o alfred" || ok "host → ping $IP → bloqueado"

echo "== de containers em OUTRAS redes (deve falhar)"
bloqueado "rede bridge → $IP:8765" bridge "http://$IP:8765/healthz" GET
bloqueado "rede web → $IP:8765" web "http://$IP:8765/healthz" GET
bloqueado "rede web → alfred:8765 (nome)" web "http://alfred:8765/healthz" GET

echo
[ "$falhas" = 0 ] && echo "servidor: tudo certo" || echo "servidor: $falhas falha(s)"
exit "$falhas"
REMOTO
r=$?
falhas=$((falhas + r))
echo
[ "$falhas" = 0 ] && echo "RESULTADO: tudo certo" || echo "RESULTADO: $falhas falha(s)"
exit "$falhas"
