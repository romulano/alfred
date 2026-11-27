#!/usr/bin/env bash
# Sobe o alfred sozinho, sem Docker.
#
#   ./iniciar.sh            no terminal (Ctrl+C para sair)
#   ./iniciar.sh fundo      em segundo plano, log em alfred.log
#   ./iniciar.sh parar      encerra o que está em segundo plano
#   ./iniciar.sh status     está no ar? qual modelo? quanto já gastou?
#   ./iniciar.sh exportar [arquivo.json]   o histórico inteiro em JSON (sem arquivo: na tela)
#
# A configuração (modelo, esforço, endereços, token) fica em alfred.conf.
# Argumentos extras vão direto para o alfred.py: ./iniciar.sh -m opus -e high
set -euo pipefail

aqui="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
conf="$aqui/alfred.conf"
pid="$aqui/alfred.pid"
log="$aqui/alfred.log"

if [[ ! -f "$conf" ]]; then
  cp "$aqui/alfred.conf.exemplo" "$conf"
  echo "criei $conf — edite para trocar o modelo"
fi

# Lê o alfred.conf sem sobrescrever o que já veio do ambiente
while IFS='=' read -r chave valor; do
  [[ "$chave" =~ ^ALFRED_[A-Z_]+$ ]] || continue
  [[ -n "${!chave+x}" ]] && continue
  export "$chave=$valor"
done < "$conf"
[[ -z "${ALFRED_CLAUDE:-}" ]] && unset ALFRED_CLAUDE
[[ -z "${ALFRED_FFMPEG:-}" ]] && unset ALFRED_FFMPEG
[[ -z "${ALFRED_DB:-}" ]] && unset ALFRED_DB
[[ -z "${ALFRED_OLLAMA:-}" ]] && unset ALFRED_OLLAMA

rodando() { [[ -f "$pid" ]] && kill -0 "$(cat "$pid")" 2>/dev/null; }
primeiro_endereco() { echo "${ALFRED_ADDR%%,*}"; }

case "${1:-}" in
  fundo)
    shift
    if rodando; then echo "o alfred já está no ar (pid $(cat "$pid"))"; exit 0; fi
    nohup python3 "$aqui/alfred.py" "$@" >>"$log" 2>&1 &
    echo $! >"$pid"
    sleep 2
    if rodando; then
      echo "alfred no ar (pid $(cat "$pid")), log em $log"
      tail -n 8 "$log"
    else
      echo "o alfred não subiu; veja $log"; tail -n 20 "$log"; exit 1
    fi
    ;;
  parar)
    if rodando; then
      p=$(cat "$pid"); kill "$p"
      # espera soltar as portas, senão um "fundo" logo em seguida esbarra nelas
      for _ in $(seq 1 50); do kill -0 "$p" 2>/dev/null || break; sleep 0.2; done
      rm -f "$pid"; echo "alfred encerrado"
    else
      echo "o alfred não está rodando em segundo plano"; rm -f "$pid"
    fi
    ;;
  exportar)
    exec python3 "$aqui/alfred.py" --exportar "${2:--}"
    ;;
  status)
    curl -fsS --max-time 5 "http://$(primeiro_endereco)/healthz" && echo || { echo "fora do ar em $(primeiro_endereco)"; exit 1; }
    ;;
  *)
    exec python3 "$aqui/alfred.py" "$@"
    ;;
esac
