#!/bin/sh
# A rede Docker `ia_local` é uma ilha: só containers dentro dela falam com
# containers dentro dela. Conexão NOVA entrando nela por qualquer outro caminho é
# descartada:
#
#   - a internet (eth0), mesmo que um dia alguém publique uma porta ali;
#   - containers de outras redes Docker (roteados pelo host);
#   - o próprio host: processos, usuários e containers com network_mode: host,
#     pelo IP da máquina ou pelo da bridge.
#
# O que continua valendo: tráfego entre containers da ia_local (não passa pelo
# roteamento do host) e as respostas de conexões que os containers abriram para
# fora (o alfred precisa sair para a API do Claude).
#
#   FORWARD (DOCKER-USER): o que é roteado para dentro da bridge
#   OUTPUT:                o que o próprio host tenta abrir lá dentro
#
# Aplicado no boot e a cada reinício do Docker (ia-local-isolamento.service).
# Idempotente: insere cada regra só se ainda não existir.
set -eu
BRIDGE=br-ia_local

liga() { # insere a regra só se ainda não existir
  cmd=$1; shift
  $cmd -w -C "$@" 2>/dev/null || $cmd -w -I "$@"
}

for ipt in iptables ip6tables; do
  $ipt -w -N DOCKER-USER 2>/dev/null || true
  liga "$ipt" DOCKER-USER -o "$BRIDGE" ! -i "$BRIDGE" -m conntrack --ctstate NEW -j DROP
  liga "$ipt" OUTPUT -o "$BRIDGE" -m conntrack --ctstate NEW -j DROP
done
echo "ia_local isolada: só containers da própria rede abrem conexão lá dentro"
