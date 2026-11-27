#!/usr/bin/env python3
# alfred — a ponte entre o analista e o CLI do Claude Code.
#
# O analista (programas/analista) sabe falar com "qualquer IA que fale HTTP+JSON":
# o config descreve o corpo que sai e o caminho onde ler o texto que volta. O
# Claude Code NÃO fala HTTP — ele é um processo de terminal que lê o prompt no
# stdin e escreve a resposta no stdout. Este arquivo é o que falta no meio: um
# servidor local que recebe o POST do analista, roda `claude -p`, e devolve a
# resposta no formato que o config já espera.
#
#   analista ──POST /v1/chat/completions──▶ alfred ──claude -p──▶ Claude Code
#            ◀── choices.0.message.content ──        ◀── {"result": …} ──
#
# Dois caminhos, porque o analista traz dois backends de fábrica (config.go):
#
#   /v1/chat/completions   compatível com OpenAI — o padrão, backend `alfred`
#   /run                   o legado, backend `alfred-run`: {"ok":…,"result":…}
#
# Três decisões que não são detalhe:
#
#  1. O PROMPT VAI PELO STDIN, NUNCA POR ARGV. Ele carrega a `-ia-instrucao` da
#     corrida, e essa instrução costuma trazer a senha da conta de teste — argv
#     é público na máquina inteira (`ps`). É a mesma razão pela qual o motor já
#     passa a instrução ao analista por arquivo, e não por linha de comando. O
#     prompt de sistema vai por arquivo 0600 (`--system-prompt-file`), que some
#     assim que a chamada termina.
#
#  2. O CLAUDE SOBE SEM FERRAMENTA E SEM CONTEXTO DE PROJETO. O analista não
#     pede execução: ele pede julgamento sobre um JSON que já vem no prompt.
#     Um Claude Code com Bash ligado, lendo o CLAUDE.md do diretório e a
#     configuração do usuário, faria trabalho que ninguém pediu e cobraria por
#     ele — a chamada trivial medida aqui caiu de US$ 0,108 para US$ 0,007 só
#     por trocar o prompt de sistema embutido pelo do analista. Quem quiser
#     ferramenta pede explicitamente, por flag ou pelo corpo do /run.
#
#  3. QUANDO O CLIENTE SOME, O CLAUDE MORRE JUNTO. O motor mata chamadas em voo
#     quando o orçamento estoura, e um `claude` órfão continuaria pensando (e
#     cobrando) por minutos depois que ninguém mais espera a resposta. Cada
#     processo sobe em sessão própria e é morto pelo grupo — ele e os filhos.
"""alfred — ponte HTTP local entre o analista e o CLI do Claude Code."""

import argparse
import base64
import binascii
import errno
import hmac
import http.client
import json
import os
import re
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSAO = "1.2"

# O corpo de uma requisição do analista é grande — o contexto do mundo somado
# aos achados chega a dezenas de KB — mas não é ilimitado: o `max_prompt_chars`
# dele corta em 60 mil caracteres antes de sair. Oito MiB é folga de sobra e
# ainda assim um teto: um POST que passe disso não é o analista.
MAX_CORPO = 8 << 20  # sem mídia; com foto e vídeo o teto é o --max-corpo-mb

# Fotos e vídeos. O claude recebe as imagens pelo stdin em stream-json (o mesmo
# formato do SDK), sem ferramenta nenhuma ligada: ninguém lê arquivo do disco.
# Vídeo não entra no modelo como vídeo: vira N quadros espaçados, em ordem, cada
# um com o instante — e quem corta os quadros é o ffmpeg.
TIPOS_IMAGEM = {"image/png", "image/jpeg", "image/gif", "image/webp"}
TETO_IMAGEM = 3_500_000   # bytes; a API do Claude recusa imagem acima de ~5 MB em base64
LADO_IMAGEM = 1568        # o maior lado que o Claude aproveita sem reduzir sozinho
LADO_QUADRO = 1024
MAX_IMAGENS = 40


class ErroMidia(Exception):
    """Foto ou vídeo que não dá para mandar: vira um 400 com a explicação."""

# Modelos que o config manda e que NÃO são nome de modelo: `claude-code` é o
# rótulo que o analista põe no campo `model` para dizer "é o Claude Code". Passar
# isso adiante como `--model claude-code` faria o CLI recusar.
MODELOS_GENERICOS = {"", "claude-code", "default", "padrao", "padrão"}

# Os níveis que o `--effort` do CLI aceita. Recusar aqui o que ele recusaria
# adiante troca uma chamada perdida (processo aberto, prompt enviado, erro no
# fim) por um 400 imediato.
ESFORCOS = ("low", "medium", "high", "xhigh", "max")
# Ferramentas que um cliente pode ligar por chamada (campo `tools`). Só as de
# pesquisa na web: o prompt costuma levar texto de terceiros (um artigo
# coletado), e uma injeção nele nunca pode ganhar Bash ou acesso a arquivo.
FERRAMENTAS_POR_CHAMADA = ("WebSearch", "WebFetch")
# Os modos de permissão que um cliente pode pedir por chamada. Os outros
# (`acceptEdits`, `bypassPermissions`, `auto`…) só o operador liga, por
# `--permissao`: vindos do pedido, um prompt com texto de terceiros ganharia a
# máquina. `disallowed_tools` também só TIRA ferramenta — nunca liga as outras.
PERMISSOES_POR_CHAMADA = ("plan", "default")

# Codex (OpenAI). O modelo escolhe o provedor: `gpt-…`, `codex-…` ou o prefixo
# `codex:`/`openai:` vão para `codex exec`; o resto (opus, sonnet, claude-…)
# segue no `claude`, como sempre. O nome chega como o usuário vê na tela do
# Codex ("GPT-6.2-Sol", "GPT-6.1 Terra") e vira o slug ("gpt-6.2-sol").
PREFIXOS_CODEX = ("codex:", "openai:")
RE_MODELO_CODEX = re.compile(r"^(gpt-|codex-|o\d)[a-z0-9._-]*$")
# O Codex aceita os mesmos níveis do Claude e mais um: `ultra`.
ESFORCOS_CODEX = (*ESFORCOS, "ultra")
# Os modelos da tela do Codex, para o /v1/models quando o cache dele não existir.
MODELOS_CODEX = ("gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.1-terra", "gpt-6.1-sol",
                 "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol")
# O que o `codex exec` traz ligado e que aqui só abriria porta: shell, apps,
# plugins, navegador, subagentes. Como no Claude, ele sobe só para responder.
CODEX_DESLIGADOS = ("shell_tool", "apps", "plugins", "image_generation", "multi_agent",
                    "browser_use", "computer_use", "hooks", "goals", "skill_search",
                    "tool_suggest", "sleep_tool")


def modelo_codex(modelo):
    """O slug do Codex se `modelo` for dele ("GPT-6.2-SOL" → "gpt-6.2-sol"),
    senão "" (é do Claude)."""
    bruto = (modelo or "").strip().lower()
    m = re.sub(r"\s+", "-", bruto)
    for p in PREFIXOS_CODEX:
        if bruto.startswith(p):
            m = re.sub(r"\s+", "-", bruto[len(p):].strip())
            # Com prefixo vale qualquer slug (um modelo novo que não começa
            # com gpt-), mas nunca algo que o CLI leria como flag.
            return m if re.match(r"^[a-z0-9][a-z0-9._-]*$", m) else ""
    return m if RE_MODELO_CODEX.match(m) else ""


# Ollama: `ollama:<nome>` ("ollama:qwen3-coder:30b", "ollama:hf.co/x/y") vai
# direto à API HTTP do Ollama — sem processo para abrir, sem ferramenta nenhuma
# para desligar (ele só gera texto).
PREFIXO_OLLAMA = "ollama:"
RE_MODELO_OLLAMA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")


def modelo_ollama(modelo):
    """O nome do modelo no Ollama se `modelo` for `ollama:<nome>`, senão ""."""
    bruto = (modelo or "").strip()
    if not bruto.lower().startswith(PREFIXO_OLLAMA):
        return ""
    nome = bruto[len(PREFIXO_OLLAMA):].strip()
    return nome if RE_MODELO_OLLAMA.match(nome) else ""


def codex_padrao():
    """`codex` do PATH; senão o que o app do Codex instala em ~/.codex."""
    achado = shutil.which("codex")
    if achado:
        return achado
    local = os.path.expanduser("~/.codex/packages/app-server-daemon/current/codex")
    return local if os.path.exists(local) else "codex"


# --------------------------------------------------------------------- ponte


class Resultado:
    """O que uma chamada ao `claude -p` produziu, com o bastante para a trilha
    explicar de onde veio: resposta sem custo nem duração é afirmação sem autor."""

    def __init__(self):
        self.ok = False
        self.texto = ""
        self.erro = ""
        self.stdout = ""
        self.stderr = ""
        self.saida = 0
        self.dur_ms = 0
        self.custo = 0.0
        self.turnos = 0
        self.sessao = ""
        self.modelo = ""
        self.provedor = ""
        self.esforco = ""
        # preenchidos por quem submeteu: a linha do histórico e a conversa
        self.chamada_id = ""
        self.conversa_id = ""
        self.tokens_in = 0
        self.tokens_out = 0
        self.limite_turnos = False


class Progresso:
    """O que o `claude` já fez numa chamada em andamento.

    Existe porque um plano com esforço alto leva dezenas de minutos, e do outro
    lado só se via "pensando…": a pessoa achava que tinha travado e cancelava. Com
    `--include-partial-messages` o CLI manda cada pedaço do raciocínio e da
    resposta conforme sai; aqui só se conta (nada do texto é guardado) e se
    estima o quanto falta pela média das chamadas anteriores do mesmo tipo.
    """

    def __init__(self, rotulo, chave):
        self.rotulo = rotulo
        self.chave = chave
        self.fase = "fila"
        self.pensamento = 0
        self.resposta = 0
        self.eventos = 0
        self.inicio = time.time()
        self.ultimo = self.inicio

    def total(self):
        return self.pensamento + self.resposta

    def evento(self, linha):
        try:
            ev = json.loads(linha)
        except (ValueError, TypeError):
            return
        if not isinstance(ev, dict):
            return
        self.eventos += 1
        self.ultimo = time.time()
        tipo = ev.get("type")
        if tipo == "system":
            self.fase = "iniciando"
        elif tipo == "stream_event":
            e = ev.get("event") or {}
            if e.get("type") == "content_block_start":
                bloco = (e.get("content_block") or {}).get("type")
                if bloco in ("thinking", "redacted_thinking"):
                    self.fase = "pensando"
                elif bloco == "text":
                    self.fase = "escrevendo"
                elif bloco == "tool_use":
                    self.fase = "ferramenta"
            elif e.get("type") == "content_block_delta":
                d = e.get("delta") or {}
                if d.get("type") == "thinking_delta":
                    self.pensamento += len(d.get("thinking") or "")
                    self.fase = "pensando"
                elif d.get("type") == "text_delta":
                    self.resposta += len(d.get("text") or "")
                    self.fase = "escrevendo"
                elif d.get("type") == "input_json_delta":
                    self.resposta += len(d.get("partial_json") or "")
        elif tipo == "result":
            self.fase = "concluindo"
        # Eventos do `codex exec --json`: chegam por item inteiro, não em pedaços
        elif tipo in ("thread.started", "turn.started"):
            self.fase = "iniciando" if tipo == "thread.started" else "pensando"
        elif tipo in ("item.started", "item.updated", "item.completed"):
            item = ev.get("item") or {}
            texto = item.get("text") if isinstance(item.get("text"), str) else ""
            if item.get("type") == "reasoning":
                self.fase = "pensando"
                if tipo == "item.completed":
                    self.pensamento += len(texto)
            elif item.get("type") == "agent_message":
                self.fase = "escrevendo"
                if tipo == "item.completed":
                    self.resposta += len(texto)
        elif tipo in ("turn.completed", "turn.failed"):
            self.fase = "concluindo"

    def json(self, media):
        """Estado para o cliente. `pct` sai da média de caracteres e de tempo das
        chamadas anteriores com a mesma chave (tarefa, modelo e esforço); sem
        histórico fica None — inventar um número seria pior que não ter."""
        agora = time.time()
        segundos = agora - self.inicio
        pct = None
        if media and self.fase != "fila":
            por_texto = self.total() / media["chars"] if media.get("chars") else 0
            por_tempo = segundos / media["seg"] if media.get("seg") else 0
            pct = int(min(97, 100 * max(por_texto, por_tempo)))
        return {"fase": self.fase, "pct": pct, "pensamento_chars": self.pensamento,
                "resposta_chars": self.resposta, "segundos": int(segundos),
                "sem_novidade_s": int(agora - self.ultimo), "rotulo": self.rotulo,
                "historico": bool(media), "chamadas_na_media": int((media or {}).get("n", 0))}


class Trabalho:
    """Uma chamada que continua viva depois que o cliente foi embora.

    Existe porque nem toda pergunta cabe no prazo de uma requisição HTTP. Uma
    forja pede ao modelo o código INTEIRO de uma ferramenta nova; o ctfmotor
    reserva 30 minutos para isso (`-forja-teto`), e o caminho até aqui
    estrangulava em 240s — `FALHOU: a chamada passou de 240s e foi encerrada`,
    com o trabalho jogado fora justamente quando era mais caro.

    O ticket resolve sem esticar requisição nenhuma: passou do prazo de
    RESPOSTA, o alfred devolve um `id` e o `claude` segue trabalhando até o
    prazo de TRABALHO. O cliente volta em `GET /jobs/<id>` quando quiser.
    """

    def __init__(self, ident, rota):
        self.id = ident
        self.rota = rota
        self.pronto = threading.Event()
        self.r = None
        self.criado = time.time()
        self.fim = 0.0
        self.promovido = False
        # DELETE /jobs/<id>: o cliente desistiu do ticket e o claude deve morrer
        self.cancelado = threading.Event()
        # andamento da chamada, quando o cliente pediu (`progresso_rotulo`)
        self.progresso = None

    def esperar(self, prazo):
        """O Resultado, ou None se o prazo de resposta acabou antes."""
        return self.r if self.pronto.wait(prazo) else None

    def estado(self):
        return "pronto" if self.pronto.is_set() else "executando"

    def decorrido_ms(self):
        fim = self.fim or time.time()
        return int((fim - self.criado) * 1000)


# ------------------------------------------------------------------ memória

# Tudo o que o alfred fez nesta máquina, para este usuário, num SQLite local.
# Nada sai daqui: o arquivo é do dono (0600, pasta 0700), e exportar é uma
# decisão dele (`./iniciar.sh exportar`). Duas tabelas de trabalho:
#
#   chamadas   uma linha por pergunta: quem pediu, a que modelo, o que entrou
#              (sem os bytes de foto e vídeo), o que saiu, custo, tokens, tempo
#   mensagens  as conversas com `conversa_id`, que o alfred devolve ao modelo
#              na pergunta seguinte — o cliente manda só a mensagem nova
ESQUEMA = """
CREATE TABLE IF NOT EXISTS conversas (
    id TEXT PRIMARY KEY,
    titulo TEXT NOT NULL DEFAULT '',
    criada REAL NOT NULL,
    atualizada REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mensagens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversa_id TEXT NOT NULL,
    chamada_id TEXT,
    papel TEXT NOT NULL,
    conteudo TEXT NOT NULL,
    criada REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS mensagens_conversa ON mensagens(conversa_id, id);
CREATE TABLE IF NOT EXISTS chamadas (
    id TEXT PRIMARY KEY,
    criada REAL NOT NULL,
    rota TEXT,
    cliente TEXT,
    conversa_id TEXT,
    provedor TEXT,
    modelo TEXT,
    esforco TEXT,
    sistema TEXT,
    entrada TEXT,
    midia TEXT,
    ok INTEGER,
    resposta TEXT,
    erro TEXT,
    custo_usd REAL,
    tokens_in INTEGER,
    tokens_out INTEGER,
    dur_ms INTEGER,
    ticket INTEGER
);
CREATE INDEX IF NOT EXISTS chamadas_criada ON chamadas(criada);
CREATE INDEX IF NOT EXISTS chamadas_conversa ON chamadas(conversa_id);
"""
RE_CONVERSA = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def caminho_db_padrao():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "alfred", "alfred.db")


class Memoria:
    """O histórico local. Uma conexão só, protegida por lock: as escritas são
    poucas (uma por pergunta) e o SQLite em WAL deixa ler enquanto escreve."""

    def __init__(self, caminho):
        self.caminho = caminho
        self.lock = threading.Lock()
        pasta = os.path.dirname(os.path.abspath(caminho))
        os.makedirs(pasta, mode=0o700, exist_ok=True)
        # Criado 0600 ANTES de o sqlite abrir: o histórico guarda prompts, e
        # prompt do analista costuma trazer senha de conta de teste. Os arquivos
        # -wal e -shm herdam a permissão deste.
        os.close(os.open(caminho, os.O_CREAT | os.O_RDWR, 0o600))
        os.chmod(caminho, 0o600)
        self.db = sqlite3.connect(caminho, check_same_thread=False, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(ESQUEMA)
            self.db.execute("PRAGMA user_version=1")

    def fechar(self):
        with self.lock:
            self.db.close()

    def historico(self, conversa_id):
        """As mensagens já trocadas na conversa, no formato OpenAI."""
        with self.lock:
            linhas = self.db.execute(
                "SELECT papel, conteudo FROM mensagens WHERE conversa_id=? ORDER BY id",
                (conversa_id,)).fetchall()
        return [{"role": l["papel"], "content": l["conteudo"]} for l in linhas]

    def gravar(self, reg, r, ticket=False):
        """Uma pergunta que terminou: a linha de `chamadas` sempre; a conversa
        só cresce quando deu certo (falha não entra no histórico, senão a
        próxima pergunta levaria ao modelo uma pergunta sem resposta)."""
        agora = time.time()
        cid = reg.get("conversa_id") or ""
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.execute(
                    "INSERT OR REPLACE INTO chamadas (id, criada, rota, cliente, conversa_id, provedor,"
                    " modelo, esforco, sistema, entrada, midia, ok, resposta, erro, custo_usd,"
                    " tokens_in, tokens_out, dur_ms, ticket)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r.chamada_id, reg.get("criada") or agora, reg.get("rota"), reg.get("cliente"),
                     cid or None, r.provedor, r.modelo, r.esforco, reg.get("sistema") or "",
                     reg.get("entrada") or "", json.dumps(reg.get("midia")) if reg.get("midia") else None,
                     1 if r.ok else 0, r.texto, r.erro, r.custo, r.tokens_in, r.tokens_out,
                     r.dur_ms, 1 if ticket else 0))
                if cid and r.ok:
                    novas = [(p, t) for p, t in reg.get("novas") or [] if t.strip()]
                    titulo = next((t for p, t in novas if p == "user"), "")[:80]
                    self.db.execute(
                        "INSERT INTO conversas (id, titulo, criada, atualizada) VALUES (?,?,?,?)"
                        " ON CONFLICT(id) DO UPDATE SET atualizada=excluded.atualizada,"
                        " titulo=CASE WHEN conversas.titulo='' THEN excluded.titulo ELSE conversas.titulo END",
                        (cid, " ".join(titulo.split()), agora, agora))
                    for papel, texto in [*novas, ("assistant", r.texto)]:
                        self.db.execute(
                            "INSERT INTO mensagens (conversa_id, chamada_id, papel, conteudo, criada)"
                            " VALUES (?,?,?,?,?)", (cid, r.chamada_id, papel, texto, agora))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def conversas(self, limite=50):
        with self.lock:
            linhas = self.db.execute(
                "SELECT c.id, c.titulo, c.criada, c.atualizada,"
                " (SELECT COUNT(*) FROM mensagens m WHERE m.conversa_id=c.id) AS mensagens"
                " FROM conversas c ORDER BY c.atualizada DESC LIMIT ?", (limite,)).fetchall()
        return [dict(l) for l in linhas]

    def conversa(self, conversa_id):
        with self.lock:
            c = self.db.execute("SELECT * FROM conversas WHERE id=?", (conversa_id,)).fetchone()
            if c is None:
                return None
            msgs = self.db.execute(
                "SELECT papel, conteudo, criada, chamada_id FROM mensagens WHERE conversa_id=? ORDER BY id",
                (conversa_id,)).fetchall()
        return {**dict(c), "mensagens": [dict(m) for m in msgs]}

    def apagar_conversa(self, conversa_id):
        """Some com a conversa e com as chamadas dela (o que entrou e saiu)."""
        with self.lock:
            self.db.execute("BEGIN")
            try:
                n = self.db.execute("DELETE FROM conversas WHERE id=?", (conversa_id,)).rowcount
                self.db.execute("DELETE FROM mensagens WHERE conversa_id=?", (conversa_id,))
                self.db.execute("DELETE FROM chamadas WHERE conversa_id=?", (conversa_id,))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return n > 0

    def chamadas(self, limite=50, conversa_id=""):
        sql, args = "SELECT * FROM chamadas", []
        if conversa_id:
            sql, args = sql + " WHERE conversa_id=?", [conversa_id]
        with self.lock:
            linhas = self.db.execute(sql + " ORDER BY criada DESC LIMIT ?", (*args, limite)).fetchall()
        return [self._chamada(l) for l in linhas]

    @staticmethod
    def _chamada(l):
        d = dict(l)
        d["ok"] = bool(d["ok"])
        d["ticket"] = bool(d["ticket"])
        d["midia"] = json.loads(d["midia"]) if d["midia"] else None
        return d

    def resumo(self):
        with self.lock:
            c = self.db.execute("SELECT COUNT(*), COALESCE(SUM(custo_usd),0) FROM chamadas").fetchone()
            n = self.db.execute("SELECT COUNT(*) FROM conversas").fetchone()[0]
        return {"chamadas": c[0], "conversas": n, "custo_usd": round(c[1], 6)}

    def exportar(self):
        """Tudo, num documento só: é o que o dono leva para outra máquina."""
        with self.lock:
            conversas = [dict(c) for c in self.db.execute("SELECT * FROM conversas ORDER BY criada")]
            msgs = self.db.execute(
                "SELECT conversa_id, papel, conteudo, criada, chamada_id FROM mensagens ORDER BY id").fetchall()
            chamadas = [self._chamada(l) for l in self.db.execute("SELECT * FROM chamadas ORDER BY criada")]
        por_conversa = {}
        for m in msgs:
            d = dict(m)
            por_conversa.setdefault(d.pop("conversa_id"), []).append(d)
        for c in conversas:
            c["mensagens"] = por_conversa.get(c["id"], [])
        return {"formato": "alfred-historico", "versao": 1, "alfred": VERSAO,
                "exportado_em": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "conversas": conversas, "chamadas": chamadas}


def sem_midia(msgs):
    """As mensagens como o histórico as guarda: o texto inteiro, e no lugar de
    cada foto ou vídeo só o tipo — os bytes encheriam o banco sem servir para
    reler a conversa."""
    saida = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, list):
            c = [b if not isinstance(b, dict) or b.get("type") in ("text", "input_text")
                 else {"type": b.get("type"), "omitido": True} for b in c]
        saida.append({"role": str(m.get("role") or "user"), "content": c})
    return saida


class Ponte:
    def __init__(self, cfg):
        self.cfg = cfg
        self.vaga = threading.BoundedSemaphore(cfg.paralelas)
        self.lock = threading.Lock()
        self.em_voo = 0
        self.chamadas = 0
        self.falhas = 0
        self.custo = 0.0
        self.inicio = time.time()
        # Um diretório vazio e só nosso: é o cwd de todo `claude` que subir
        # daqui. Rodar no diretório de onde o alfred foi lançado faria o Claude
        # Code descobrir CLAUDE.md, .claude/ e o git dali — contexto que ninguém
        # pediu, em toda chamada, pago por token.
        self.dir = cfg.dir or tempfile.mkdtemp(prefix="alfred-")
        os.makedirs(self.dir, exist_ok=True)
        self.dir_proprio = not cfg.dir
        # Trabalhos promovidos a ticket. Só na memória: um alfred que reinicia
        # perde os tickets, e isso é honesto — o `claude` que os produzia morreu
        # junto. Fingir persistência devolveria ticket para trabalho que não
        # existe mais, que é pior que o cliente redescobrir na hora.
        self.jobs = {}
        self.jobs_lock = threading.Lock()
        # Média de caracteres e de segundos por tipo de chamada, para estimar o %
        self.medias_arq = getattr(cfg, "medias", None) or os.path.join(os.path.dirname(os.path.abspath(__file__)), "progresso.json")
        self.medias_lock = threading.Lock()
        try:
            with open(self.medias_arq) as f:
                self.medias = json.load(f)
        except (OSError, ValueError):
            self.medias = {}
        # O histórico local (None = desligado com --sem-historico)
        self.memoria = None if getattr(cfg, "sem_historico", False) or not getattr(cfg, "db", "") \
            else Memoria(cfg.db)

    def media(self, chave):
        with self.medias_lock:
            return dict(self.medias[chave]) if chave in self.medias else None

    def aprender(self, pr):
        """Chamada que terminou bem entra na média (móvel: pesa mais o recente)."""
        if not pr.chave or pr.total() <= 0:
            return
        seg = time.time() - pr.inicio
        with self.medias_lock:
            m = self.medias.get(pr.chave)
            if not m:
                m = {"chars": float(pr.total()), "seg": seg, "n": 1}
            else:
                a = 0.4
                m = {"chars": m["chars"] * (1 - a) + pr.total() * a,
                     "seg": m["seg"] * (1 - a) + seg * a, "n": m["n"] + 1}
            self.medias[pr.chave] = m
            try:
                tmp = self.medias_arq + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(self.medias, f, indent=1)
                os.replace(tmp, self.medias_arq)
            except OSError as e:
                log(f"não consegui salvar as médias de progresso: {e}")

    def fechar(self):
        if self.dir_proprio:
            shutil.rmtree(self.dir, ignore_errors=True)
        if self.memoria is not None:
            self.memoria.fechar()
            self.memoria = None

    def versao_claude(self):
        return self.versao_de(self.cfg.claude)

    def versao_codex(self):
        return self.versao_de(self.cfg.codex)

    @staticmethod
    def versao_de(cli):
        try:
            p = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=20)
            return p.stdout.strip() or p.stderr.strip()
        except Exception as e:  # noqa: BLE001 — qualquer falha aqui é "não achei o CLI"
            return f"indisponível: {e}"

    def modelos_codex(self):
        """Os slugs que o Codex desta máquina conhece (o cache que ele mesmo
        mantém em ~/.codex); sem cache, a lista da tela dele."""
        try:
            with open(os.path.join(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"),
                                   "models_cache.json")) as f:
                slugs = [m["slug"] for m in json.load(f).get("models") or []
                         if isinstance(m, dict) and modelo_codex(str(m.get("slug") or ""))]
            return slugs or list(MODELOS_CODEX)
        except (OSError, ValueError, AttributeError, KeyError):
            return list(MODELOS_CODEX)

    def argv(self, sistema_arq, modelo, permissao, turnos, negadas, ferramentas, esforco, stream=False,
             parcial=False):
        # Com foto ou vídeo a mensagem vai como JSON (blocos de imagem) pelo stdin.
        # Com andamento, a saída vem em eventos, com os pedaços conforme saem.
        saida = (["--input-format", "stream-json"] if stream else []) + (
            ["--output-format", "stream-json", "--verbose"] if stream or parcial else ["--output-format", "json"])
        if parcial:
            saida.append("--include-partial-messages")
        a = [
            self.cfg.claude, "-p",
            *saida,
            # Ninguém está no teclado para responder a um pedido de permissão:
            # sem isto, uma chamada que esbarrasse numa permissão ficaria
            # pendurada até o timeout, gastando o orçamento da corrida em espera.
            "--permission-prompts", "none",
            "--permission-mode", permissao,
            "--max-turns", str(turnos),
            # Sessão não persiste: são perguntas de uma rodada só, e guardar
            # cada uma no disco encheria ~/.claude de conversas que ninguém vai
            # retomar. As três seguintes desligam o que o operador configurou
            # para SI e que aqui só adicionaria variação: MCP de outros
            # projetos, slash commands, settings de usuário.
            "--no-session-persistence",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--setting-sources", "",
        ]
        if modelo:
            a += ["--model", modelo]
        if esforco:
            # Quanto o modelo pensa antes de responder. Vale a pena onde a
            # resposta decide o próximo passo da corrida (`validar`, `propor`) e
            # é desperdício onde ela é mecânica — por isso o cliente pode pedir
            # um esforço por chamada, e o daqui é só o piso.
            a += ["--effort", esforco]
        if sistema_arq:
            a += ["--system-prompt-file", sistema_arq]
        # As duas variádicas (`--tools`, `--disallowed-tools`) ficam POR ÚLTIMO,
        # e é de propósito: elas engolem os argumentos seguintes. Foi assim que a
        # primeira tentativa de medir o CLI aqui perdeu o prompt — ele virou item
        # da lista de ferramentas proibidas e o `claude` respondeu "input must be
        # provided". Como o prompt vem pelo stdin, não há posicional para engolir.
        #
        # `--tools` vai SEMPRE. Sem ele o CLI liga todas as ferramentas, e era
        # isso que acontecia quando o pedido trazia `disallowed_tools`: a lista
        # de proibidas trocava a de liberadas, e Bash, Read e Write subiam
        # junto. As proibidas agora só tiram da lista liberada. Vazia (o padrão)
        # = nenhuma ferramenta: o analista quer julgamento sobre o JSON que está
        # no prompt, não uma investigação por conta própria.
        liberadas = [f for f in (ferramentas or "").split(",") if f.strip() and f.strip() not in negadas]
        a += ["--tools", ",".join(liberadas)]
        if negadas:
            a += ["--disallowed-tools", ",".join(negadas)]
        return a

    def argv_codex(self, instrucoes_arq, modelo, esforco, web=False, imagens=()):
        """`codex exec` com as mesmas garantias do `claude -p`: prompt pelo
        stdin (não há posicional), instruções de sistema por arquivo 0600, sem
        shell nem app nem plugin, sandbox só leitura, sessão que não fica no
        disco e sem a config pessoal do operador (o login continua valendo)."""
        a = [self.cfg.codex, "exec"]
        # `-i` é variádico: cada um vem antes de uma flag, que encerra a lista
        for img in imagens:
            a += ["-i", img]
        a += ["--json", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
              "--ignore-rules", "--sandbox", "read-only", "-C", self.dir, "-m", modelo]
        # Valores TOML: string JSON é string TOML válida (aspas e escapes iguais)
        if esforco:
            a += ["-c", f"model_reasoning_effort={json.dumps(esforco)}"]
        if instrucoes_arq:
            a += ["-c", f"model_instructions_file={json.dumps(instrucoes_arq)}"]
        a += ["-c", 'web_search="live"' if web else 'web_search="disabled"']
        for f in CODEX_DESLIGADOS:
            a += ["--disable", f]
        return a

    def submeter(self, rota, sistema, prompt, **kw):
        """Dispara a chamada numa thread e devolve o Trabalho que a representa.

        `vivo` é embrulhado: enquanto o trabalho é síncrono, o cliente sumir
        continua cancelando (é o que impede pagar inferência para um socket
        morto). Depois de promovido a ticket o cliente DEVE sumir — foi ele que
        pediu o ticket —, e o vigia passa a responder "vivo" para sempre.
        """
        t = Trabalho(uuid.uuid4().hex[:16], rota)
        reg = kw.pop("registro", None)
        rotulo = kw.pop("progresso_rotulo", "")
        if rotulo:
            modelo = (kw.get("modelo") or "").strip().lower()
            modelo = self.cfg.modelo if modelo in MODELOS_GENERICOS else modelo
            esforco = (kw.get("esforco") or self.cfg.esforco or "padrão").strip()
            t.progresso = Progresso(rotulo, f"{rotulo}|{modelo or 'padrão'}|{esforco}")
            kw["progresso"] = t.progresso
        vivo = kw.pop("vivo", None)
        # O vigia mata o claude quando `vivo` vira falso: cancelar o ticket faz isso
        kw["vivo"] = lambda: not t.cancelado.is_set() and (t.promovido or vivo is None or vivo())

        def corpo():
            try:
                t.r = self.chamar(sistema, prompt, **kw)
            except Exception as e:  # noqa: BLE001 — a thread não pode morrer calada
                r = Resultado()
                r.erro = f"{type(e).__name__}: {e}"
                t.r = r
            finally:
                t.fim = time.time()
                t.r.chamada_id = t.id
                t.r.conversa_id = (reg or {}).get("conversa_id") or ""
                # Grava ANTES de soltar a resposta: o cliente que recebe e já
                # manda a próxima mensagem da conversa tem de achar esta no banco.
                memoria = self.memoria
                if memoria is not None and reg and reg.get("gravar", True):
                    try:
                        memoria.gravar(reg, t.r, ticket=t.promovido)
                    except Exception as e:  # noqa: BLE001 — histórico não derruba a resposta
                        log(f"não consegui gravar a chamada {t.id} no histórico: {e}")
                t.pronto.set()
                # Quem já virou ticket não tem mais handler esperando por ele:
                # sem esta linha, a trilha perderia o custo e a duração
                # justamente das chamadas mais longas — as caras.
                if t.promovido:
                    r = t.r
                    estado = "ok" if r.ok else "FALHOU: " + primeiras_linhas(r.erro, 1)
                    log(f"{rota} ticket {t.id} terminou em {r.dur_ms/1000:.1f}s  "
                        f"US$ {r.custo:.4f}  in={r.tokens_in} out={r.tokens_out}  {estado}")

        threading.Thread(target=corpo, daemon=True).start()
        return t

    def promover(self, t):
        """Guarda o trabalho como ticket e devolve quantos há na memória."""
        t.promovido = True
        with self.jobs_lock:
            self.jobs[t.id] = t
            self._varrer()
            return len(self.jobs)

    def buscar_job(self, ident):
        with self.jobs_lock:
            self._varrer()
            return self.jobs.get(ident)

    def _varrer(self):
        """Some com o que já foi lido e com o que envelheceu. Chamada sempre com
        o lock tomado."""
        agora = time.time()
        for k, t in list(self.jobs.items()):
            if t.pronto.is_set() and agora - t.fim > self.cfg.job_ttl:
                del self.jobs[k]
        # Teto duro: um cliente que pede ticket e nunca volta não pode encher a
        # memória do servidor. Sai o mais velho primeiro.
        if len(self.jobs) > self.cfg.jobs_max:
            for k, _ in sorted(self.jobs.items(), key=lambda kv: kv[1].criado)[
                    :len(self.jobs) - self.cfg.jobs_max]:
                del self.jobs[k]

    # ------------------------------------------------------------- mídia

    def ffmpeg(self, argv, entrada=None, timeout=60):
        """Roda o ffmpeg e devolve (stdout, stderr). Sem ffmpeg, ErroMidia."""
        try:
            p = subprocess.run([self.cfg.ffmpeg, "-hide_banner", *argv], input=entrada,
                               capture_output=True, timeout=timeout)
        except FileNotFoundError:
            raise ErroMidia(f"este alfred não achou o ffmpeg em {self.cfg.ffmpeg!r} "
                            "(instale ou aponte --ffmpeg)") from None
        except subprocess.TimeoutExpired:
            raise ErroMidia("o ffmpeg passou do tempo lendo a mídia") from None
        return p.stdout, p.stderr.decode("utf-8", "replace")

    def arquivo_temp(self, dados, sufixo):
        fd, caminho = tempfile.mkstemp(prefix="midia-", suffix=sufixo, dir=self.dir)
        with os.fdopen(fd, "wb") as f:  # mkstemp já cria 0600
            f.write(dados)
        return caminho

    def imagem(self, mime, dados):
        """(mime, bytes) que o Claude aceita: formato suportado e até ~3,5 MB.
        O resto (HEIC, BMP, foto de 12 MB do celular) o ffmpeg converte em JPEG."""
        if mime in TIPOS_IMAGEM and len(dados) <= TETO_IMAGEM:
            return mime, dados
        caminho = self.arquivo_temp(dados, ".img")
        try:
            jpg, erros = self.ffmpeg(["-v", "error", "-i", caminho, "-frames:v", "1",
                                      "-vf", f"scale='min({LADO_IMAGEM},iw)':'min({LADO_IMAGEM},ih)':force_original_aspect_ratio=decrease",
                                      "-q:v", "3", "-f", "image2", "-vcodec", "mjpeg", "pipe:1"])
        finally:
            os.unlink(caminho)
        if not jpg:
            raise ErroMidia(f"não consegui ler esta imagem ({mime}): {primeiras_linhas(erros, 1)}")
        return "image/jpeg", jpg

    def quadros(self, mime, dados, n):
        """[(segundo, jpeg)] — n quadros espaçados ao longo do vídeo."""
        caminho = self.arquivo_temp(dados, ".video")
        try:
            _, info = self.ffmpeg(["-i", caminho])
            dur = None
            achado = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", info)
            if achado:
                h, m, s = achado.groups()
                dur = int(h) * 3600 + int(m) * 60 + float(s)
            if not dur:
                if "Invalid data" in info or "could not find" in info.lower():
                    raise ErroMidia(f"não consegui ler este vídeo ({mime})")
                instantes = [float(i) for i in range(n)]  # duração desconhecida: 1 por segundo
            else:
                instantes = [dur * (i + 0.5) / n for i in range(n)]
            saida = []
            for t in instantes:
                jpg, _ = self.ffmpeg(["-v", "error", "-ss", f"{t:.3f}", "-i", caminho, "-frames:v", "1",
                                      "-vf", f"scale='min({LADO_QUADRO},iw)':-2",
                                      "-q:v", "4", "-f", "image2", "-vcodec", "mjpeg", "pipe:1"])
                if jpg:
                    saida.append((t, jpg))
            if not saida:
                raise ErroMidia(f"não consegui tirar quadros deste vídeo ({mime})")
            return dur, saida
        finally:
            os.unlink(caminho)

    def preparar_midia(self, partes, quadros=0):
        """Partes ('texto'|'imagem'|'video') → blocos de conteúdo do Claude + resumo para o log."""
        n_quadros = max(1, min(quadros or self.cfg.quadros_video, 30))
        blocos, info = [], {"imagens": 0, "videos": 0, "quadros": 0}

        def img(mime, dados):
            mime, dados = self.imagem(mime, dados)
            blocos.append({"type": "image", "source": {"type": "base64", "media_type": mime,
                                                       "data": base64.b64encode(dados).decode()}})

        for p in partes:
            if p[0] == "texto":
                if p[1].strip():
                    blocos.append({"type": "text", "text": p[1]})
            elif p[0] == "imagem":
                img(p[1], p[2])
                info["imagens"] += 1
            else:
                dur, qs = self.quadros(p[1], p[2], n_quadros)
                info["videos"] += 1
                info["quadros"] += len(qs)
                quando = ", ".join(f"{t:.1f} s".replace(".", ",") for t, _ in qs)
                blocos.append({"type": "text", "text":
                               f"[Vídeo{f' de {dur:.1f} s'.replace('.', ',') if dur else ''} enviado como "
                               f"{len(qs)} quadros, em ordem, nos instantes: {quando}.]"})
                for i, (t, jpg) in enumerate(qs, 1):
                    blocos.append({"type": "text", "text": f"Quadro {i} — {t:.1f} s".replace(".", ",")})
                    img("image/jpeg", jpg)
        if info["imagens"] + info["quadros"] > MAX_IMAGENS:
            raise ErroMidia(f"são {info['imagens'] + info['quadros']} imagens; o teto é {MAX_IMAGENS}")
        return blocos, info

    def chamar(self, sistema, prompt, *, modelo="", permissao="", turnos=0,
               negadas=None, ferramentas="", timeout=0, esforco="", vivo=None, blocos=None, progresso=None):
        cfg = self.cfg
        permissao = permissao or cfg.permissao
        turnos = turnos or cfg.turnos
        # Chamada direta já chega limitada a timeout_max (despachar); trabalho que
        # virou ticket pode ir até o teto de job — antes o timeout_max cortava os
        # dois em 900 s, e um --job-teto maior não valia
        timeout = min(timeout or cfg.timeout, max(cfg.timeout_max, cfg.job_teto))
        if modelo.strip().lower() in MODELOS_GENERICOS:
            modelo = cfg.modelo
        # ollama:… vai ao Ollama; GPT-… vai para o Codex; o resto fica no Claude Code
        ollama = modelo_ollama(modelo)
        if modelo.strip().lower().startswith(PREFIXO_OLLAMA) and not ollama:
            r = Resultado()
            r.erro = f"nome de modelo do Ollama inválido: {modelo!r}"
            return r
        codex = "" if ollama else modelo_codex(modelo)
        if ollama:
            modelo = ollama
        elif codex:
            modelo = codex
        esforco = (esforco or cfg.esforco or "").strip()
        validos = ESFORCOS_CODEX if codex else ESFORCOS
        # O Ollama não tem nível de esforço: o do servidor (ou do pedido) é ignorado
        if esforco and not ollama and esforco not in validos:
            r = Resultado()
            r.erro = f"esforço {esforco!r} não existe (use: {', '.join(validos)})"
            return r

        r = Resultado()
        r.modelo = modelo or "(padrão do CLI)"
        r.provedor = "ollama" if ollama else ("codex" if codex else "claude")
        r.esforco = "" if ollama else esforco

        # `timeout` é o prazo da CHAMADA INTEIRA, fila inclusive. Gastá-lo duas
        # vezes — uma esperando vaga, outra rodando o processo — era o defeito
        # que enchia o log de "cliente fechou a conexão antes da resposta": com
        # o padrão de 240s, uma chamada que esperasse 200s na fila ainda ganhava
        # 240s de processo, 440s no total, contra um analista que desiste em 240s
        # (o teto por pergunta do `servir`) ou 300s (o http.Client). O cliente
        # ia embora antes da resposta por construção, e a inferência do trecho
        # final era paga para escrever num socket fechado.
        t0 = time.time()
        restante = self.esperar_vaga(timeout, vivo)
        if restante is None:
            r.dur_ms = int((time.time() - t0) * 1000)
            r.erro = ("fila cheia: nenhuma vaga livre dentro do prazo desta chamada"
                      if vivo is None or vivo() else
                      "o cliente desistiu da chamada enquanto ela esperava na fila")
            return r
        with self.lock:
            self.em_voo += 1
            self.chamadas += 1
        if progresso is not None:
            progresso.fase = "iniciando"
            progresso.ultimo = time.time()

        sistema_arq = ""
        imagens = []
        try:
            if ollama:
                self.chamar_ollama(r, modelo, sistema, prompt, blocos, restante, vivo)
                r.dur_ms = int((time.time() - t0) * 1000)
                return r
            if sistema.strip():
                fd, sistema_arq = tempfile.mkstemp(prefix="sistema-", suffix=".txt", dir=self.dir)
                with os.fdopen(fd, "w") as f:  # mkstemp já cria 0600
                    f.write(sistema)
            if codex:
                # O Codex não lê blocos de imagem pelo stdin: cada foto (ou quadro
                # de vídeo) vira um arquivo 0600 anexado com -i, e o texto marca
                # onde ela estava para a ordem não se perder.
                entrada, imagens = self.entrada_codex(prompt, blocos)
                liberadas = (ferramentas or cfg.ferramentas).split(",")
                argv = self.argv_codex(sistema_arq, modelo, esforco,
                                       web=bool({"WebSearch", "WebFetch"} & set(liberadas)), imagens=imagens)
            else:
                argv = self.argv(sistema_arq, modelo, permissao, turnos, negadas or [],
                                 ferramentas or cfg.ferramentas, esforco, stream=bool(blocos),
                                 parcial=progresso is not None)
                # Com mídia, o stdin é UMA mensagem de usuário em JSON com os blocos
                entrada = (json.dumps({"type": "user", "message": {"role": "user", "content": blocos}}) + "\n"
                           if blocos else prompt)
            if cfg.verboso:
                log("argv:", " ".join(argv))
            saida, erros, codigo, morto = self.rodar(argv, entrada, restante, vivo, progresso)
            r.dur_ms = int((time.time() - t0) * 1000)
            r.stdout, r.stderr, r.saida = saida, erros, codigo
            if codex:
                self.ler_codex(r, saida, erros, codigo, morto)
            else:
                self.ler(r, saida, erros, codigo, morto, restante, stream=bool(blocos) or progresso is not None)
            if r.ok and progresso is not None:
                self.aprender(progresso)
        except FileNotFoundError:
            r.dur_ms = int((time.time() - t0) * 1000)
            r.erro = (f"o CLI do Codex não está em {cfg.codex!r} — instale-o ou aponte --codex para o caminho certo"
                      if codex else
                      f"o CLI do Claude Code não está em {cfg.claude!r} — "
                      "instale-o ou aponte --claude para o caminho certo")
        except Exception as e:  # noqa: BLE001
            r.dur_ms = int((time.time() - t0) * 1000)
            r.erro = f"{type(e).__name__}: {e}"
        finally:
            for arq in [sistema_arq, *imagens]:
                if not arq:
                    continue
                try:
                    os.unlink(arq)
                except OSError:
                    pass
            with self.lock:
                self.em_voo -= 1
                self.custo += r.custo
                if not r.ok:
                    self.falhas += 1
            self.vaga.release()
        return r

    # ------------------------------------------------------------- ollama

    def _ollama(self, metodo, caminho, corpo=None, timeout=2.0):
        """(HTTPConnection, pedido) para o Ollama em --ollama. Sem rede além do
        endereço que o operador configurou: o pedido nunca escolhe o host."""
        u = urllib.parse.urlsplit(self.cfg.ollama)
        classe = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = classe(u.hostname or "127.0.0.1", u.port or (443 if u.scheme == "https" else 11434),
                      timeout=timeout)
        conn.request(metodo, u.path.rstrip("/") + caminho, body=corpo,
                     headers={"Content-Type": "application/json"} if corpo is not None else {})
        return conn

    def ollama_get(self, caminho, timeout=1.5):
        """GET rápido (versão, modelos). None se o Ollama não responder."""
        try:
            conn = self._ollama("GET", caminho, timeout=timeout)
            try:
                resp = conn.getresponse()
                return json.loads(resp.read()) if resp.status == 200 else None
            finally:
                conn.close()
        except (OSError, ValueError, http.client.HTTPException):
            return None

    def versao_ollama(self):
        d = self.ollama_get("/api/version")
        return str(d.get("version")) if isinstance(d, dict) and d.get("version") else "indisponível"

    def modelos_ollama(self):
        d = self.ollama_get("/api/tags")
        modelos = d.get("models") if isinstance(d, dict) else None
        return [str(m.get("name")) for m in modelos or [] if isinstance(m, dict) and m.get("name")]

    def chamar_ollama(self, r, modelo, sistema, prompt, blocos, timeout, vivo):
        """Uma pergunta ao /api/chat do Ollama, com o mesmo contrato do claude:
        prazo, e o cliente sumir encerra a geração (fechar a conexão faz o
        Ollama parar de gerar)."""
        msgs = [{"role": "system", "content": sistema}] if sistema.strip() else []
        usuario = {"role": "user", "content": prompt}
        if blocos:
            usuario["content"] = "\n\n".join(b["text"] for b in blocos if b.get("type") == "text")
            imagens = [b["source"]["data"] for b in blocos if b.get("type") == "image"]
            if imagens:
                usuario["images"] = imagens
        msgs.append(usuario)
        corpo = json.dumps({"model": modelo, "messages": msgs, "stream": False}).encode()

        caixa, conns = {}, []

        def pedir():
            try:
                conn = self._ollama("POST", "/api/chat", corpo, timeout=timeout)
                conns.append(conn)
                resp = conn.getresponse()
                caixa["codigo"], caixa["corpo"] = resp.status, resp.read()
            except Exception as e:  # noqa: BLE001 — vira erro legível abaixo
                caixa["erro"] = e

        th = threading.Thread(target=pedir, daemon=True)
        th.start()
        fim = time.time() + timeout
        morto = ""
        while th.is_alive():
            th.join(0.5)
            if not th.is_alive():
                break
            if vivo is not None and not vivo():
                morto = "o cliente desistiu da chamada antes da resposta"
            elif time.time() > fim:
                morto = f"a chamada passou de {timeout:.0f}s e foi encerrada"
            if morto:
                for c in conns:
                    try:
                        c.sock and c.sock.shutdown(socket.SHUT_RDWR)
                        c.close()
                    except OSError:
                        pass
                r.erro = morto
                return
        for c in conns:
            c.close()
        if "erro" in caixa:
            e = caixa["erro"]
            r.erro = (f"o Ollama não responde em {self.cfg.ollama} — ele está rodando? (ollama serve)"
                      if isinstance(e, (ConnectionRefusedError, socket.gaierror)) else
                      f"Ollama: {type(e).__name__}: {e}")
            return
        try:
            doc = json.loads(caixa.get("corpo") or b"{}")
        except ValueError:
            doc = {}
        if caixa.get("codigo") != 200:
            r.erro = f"Ollama ({caixa.get('codigo')}): {doc.get('error') or 'resposta sem explicação'}"
            return
        r.tokens_in = int(doc.get("prompt_eval_count") or 0)
        r.tokens_out = int(doc.get("eval_count") or 0)
        r.turnos = 1
        texto = (doc.get("message") or {}).get("content")
        if not isinstance(texto, str) or not texto.strip():
            r.erro = "o Ollama respondeu sem texto nenhum"
            return
        r.ok, r.texto = True, texto

    # Prazo mínimo de processo: abaixo disto o `claude` nem termina de subir, e
    # gastar a vaga para estourar em seguida é pior que recusar na hora.
    MIN_PROCESSO = 5

    def esperar_vaga(self, timeout, vivo):
        """Espera uma vaga sem passar do prazo da chamada e sem deixar de olhar
        para o cliente. Devolve os segundos que SOBRAM para o processo, ou None
        se não vale mais a pena subir o `claude`.

        A espera é fatiada porque o cliente pode desistir enquanto a chamada
        está na fila — e era aí que o alfred ficava cego: o vigia só nascia
        dentro de `rodar`, então uma chamada engavetada subia um Claude Code
        pago para um socket que já tinha fechado.
        """
        fim = time.time() + timeout
        # Caminho livre: vaga na hora, prazo inteiro para o processo. O piso não
        # vale aqui — ele é sobre o que a FILA come, não sobre o prazo que o
        # cliente escolheu. Quem pede 2s e encontra a vaga vazia tem direito aos
        # seus 2s; recusar seria inventar uma fila que não existe.
        if self.vaga.acquire(blocking=False):
            return timeout
        while True:
            if vivo is not None and not vivo():
                return None
            sobra = fim - time.time()
            if sobra <= self.MIN_PROCESSO:
                return None
            if self.vaga.acquire(timeout=min(0.5, sobra)):
                break
        sobra = fim - time.time()
        if sobra <= self.MIN_PROCESSO or (vivo is not None and not vivo()):
            self.vaga.release()
            return None
        return sobra

    def rodar(self, argv, prompt, timeout, vivo, progresso=None):
        """Roda o processo e devolve (stdout, stderr, código, motivo_da_morte).
        Com `progresso`, lê a saída linha a linha enquanto o claude trabalha."""
        p = subprocess.Popen(
            argv, cwd=self.dir,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # Sessão própria = grupo de processos próprio. É o que permite matar
            # o `claude` E os filhos dele de uma vez, sem deixar órfão pensando
            # depois que a resposta já não interessa a ninguém.
            start_new_session=True,
        )
        morto = ""
        parar = threading.Event()

        def vigia():
            # O cliente pode sumir antes da resposta: o motor mata chamadas em
            # voo quando o orçamento estoura. Continuar até o fim seria pagar
            # inferência para escrever num socket fechado.
            while not parar.wait(0.5):
                if vivo is not None and not vivo():
                    nonlocal morto
                    morto = "o cliente desistiu da chamada antes da resposta"
                    self.matar(p)
                    return

        v = threading.Thread(target=vigia, daemon=True)
        v.start()
        if progresso is None:
            try:
                saida, erros = p.communicate(prompt.encode(), timeout=timeout)
            except subprocess.TimeoutExpired:
                morto = f"a chamada passou de {timeout:.0f}s e foi encerrada"
                self.matar(p)
                saida, erros = p.communicate()
            finally:
                parar.set()
            return (saida or b"").decode("utf-8", "replace"), (erros or b"").decode("utf-8", "replace"), p.returncode, morto

        partes, partes_erro = [], []

        def ler_saida():
            for linha in iter(p.stdout.readline, b""):
                partes.append(linha)
                progresso.evento(linha)

        def ler_erros():
            partes_erro.append(p.stderr.read())

        leitores = [threading.Thread(target=ler_saida, daemon=True), threading.Thread(target=ler_erros, daemon=True)]
        for t in leitores:
            t.start()
        try:
            p.stdin.write(prompt.encode())
            p.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            morto = f"a chamada passou de {timeout:.0f}s e foi encerrada"
            self.matar(p)
        finally:
            parar.set()
        for t in leitores:
            t.join(5)
        return (b"".join(partes).decode("utf-8", "replace"), b"".join(partes_erro).decode("utf-8", "replace"),
                p.returncode, morto)

    @staticmethod
    def matar(p):
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except OSError:
                pass

    @staticmethod
    def ler(r, saida, erros, codigo, morto, timeout, stream=False):
        """Traduz o que o `claude -p --output-format json` (ou stream-json) escreveu."""
        if morto:
            r.erro = morto
            return
        doc = None
        bruto = saida.strip()
        if bruto.startswith("{"):
            try:
                doc = json.loads(bruto)
            except json.JSONDecodeError:
                # stream-json: um evento por linha; o que interessa é o último `result`
                doc = ultimo_resultado(bruto)
        if doc is None and stream:
            r.erro = primeiras_linhas(erros or "o claude não devolveu o resultado da mensagem com mídia")
            return
        if doc is None:
            # Sem JSON não há como distinguir resposta de mensagem de erro pelo
            # formato — então quem decide é o código de saída. Um CLI que
            # respondeu 0 com texto puro respondeu; qualquer outra coisa é falha,
            # e o stderr é o que explica.
            if codigo == 0 and bruto:
                r.ok, r.texto = True, bruto
            else:
                r.erro = primeiras_linhas(erros or bruto or
                                          f"o claude saiu com código {codigo} e não escreveu nada")
            return

        r.custo = float(doc.get("total_cost_usd") or 0.0)
        r.turnos = int(doc.get("num_turns") or 0)
        r.sessao = str(doc.get("session_id") or "")
        uso = doc.get("usage") or {}
        # As três parcelas somam o que a chamada LEU. Contar só `input_tokens`
        # daria quase zero em toda pergunta do analista: um prompt de dezenas de
        # KB entra inteiro como criação de cache na primeira vez e como leitura
        # de cache nas seguintes — medido aqui, 5.216 tokens de entrada
        # apareciam como "2".
        r.tokens_in = (int(uso.get("input_tokens") or 0)
                       + int(uso.get("cache_read_input_tokens") or 0)
                       + int(uso.get("cache_creation_input_tokens") or 0))
        r.tokens_out = int(uso.get("output_tokens") or 0)
        texto = doc.get("result")
        if not isinstance(texto, str):
            texto = ""
        subtipo = str(doc.get("subtype") or "")
        r.limite_turnos = subtipo == "error_max_turns"

        if doc.get("is_error") or codigo != 0 or subtipo not in ("success", ""):
            # O CLI põe a explicação da falha no mesmo campo `result`. Preferir
            # o stderr aqui esconderia a mensagem útil atrás de um log vazio.
            r.erro = primeiras_linhas(texto or erros or f"o claude falhou (subtype={subtipo!r}, saída {codigo})")
            return
        if not texto.strip():
            r.erro = "o claude respondeu sem texto nenhum"
            return
        r.ok, r.texto = True, texto

    def entrada_codex(self, prompt, blocos):
        """(texto do stdin, [arquivos de imagem]) para o `codex exec`."""
        if not blocos:
            return prompt, []
        textos, imagens = [], []
        try:
            for b in blocos:
                if b.get("type") == "text":
                    textos.append(b["text"])
                elif b.get("type") == "image":
                    fonte = b["source"]
                    sufixo = "." + fonte["media_type"].split("/")[-1].replace("jpeg", "jpg")
                    imagens.append(self.arquivo_temp(base64.b64decode(fonte["data"]), sufixo))
                    textos.append(f"[imagem {len(imagens)} anexada]")
        except Exception:
            for arq in imagens:
                os.unlink(arq)
            raise
        return "\n\n".join(textos), imagens

    @staticmethod
    def ler_codex(r, saida, erros, codigo, morto):
        """Traduz o que o `codex exec --json` escreveu: um evento por linha. A
        resposta é a última `agent_message`; `turn.completed` traz o uso e é o
        que diz que o turno terminou. O Codex não informa custo em dólar."""
        if morto:
            r.erro = morto
            return
        textos, falha, uso = [], "", None
        for linha in saida.splitlines():
            try:
                ev = json.loads(linha)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            tipo = ev.get("type")
            if tipo == "thread.started":
                r.sessao = str(ev.get("thread_id") or "")
            elif tipo == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    textos.append(item["text"])
            elif tipo == "turn.completed":
                uso = ev.get("usage") or {}
            elif tipo == "turn.failed":
                falha = str((ev.get("error") or {}).get("message") or "") or falha
            elif tipo == "error":
                falha = falha or str(ev.get("message") or "")
        if uso is None or codigo != 0:
            r.erro = primeiras_linhas(falha or erros or saida or
                                      f"o codex saiu com código {codigo} sem terminar o turno")
            return
        r.turnos = 1
        # No Codex as parcelas já estão dentro dos totais: `input_tokens` inclui
        # o cache e `output_tokens` inclui o raciocínio
        r.tokens_in = int(uso.get("input_tokens") or 0)
        r.tokens_out = int(uso.get("output_tokens") or 0)
        texto = textos[-1] if textos else ""
        if not texto.strip():
            r.erro = "o codex respondeu sem texto nenhum"
            return
        r.ok, r.texto = True, texto


# ---------------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 com Content-Length em toda resposta: o cliente Go do analista
    # reaproveita a conexão, e fechar a cada resposta custaria um handshake por
    # pergunta numa corrida que faz dezenas delas.
    protocol_version = "HTTP/1.1"
    server_version = f"alfred/{VERSAO}"
    ponte = None
    cfg = None

    # ------------------------------------------------------------- utilidades

    def log_message(self, fmt, *a):
        if self.cfg.verboso:
            log("http:", fmt % a)

    def responder(self, codigo, doc):
        corpo = json.dumps(doc, ensure_ascii=False).encode()
        try:
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
        except (BrokenPipeError, ConnectionResetError):
            # O cliente já foi embora. Não é erro do servidor: é exatamente o
            # caso que o vigia acima existe para tornar barato.
            log("cliente fechou a conexão antes da resposta")

    def erro(self, codigo, msg, tipo="invalid_request_error"):
        self.responder(codigo, {"error": {"message": msg, "type": tipo}})

    def corpo(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            self.erro(400, "corpo vazio")
            return None
        teto = max(MAX_CORPO, self.cfg.max_corpo_mb << 20)
        if n > teto:
            self.erro(413, f"corpo de {n} bytes passa do teto de {teto} (--max-corpo-mb)")
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except json.JSONDecodeError as e:
            self.erro(400, f"corpo não é JSON válido: {e}")
            return None

    def autorizado(self):
        if not self.cfg.token:
            return True
        dado = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        dado = dado or (self.headers.get("X-Token") or "").strip()
        if hmac.compare_digest(dado.encode(), self.cfg.token.encode()):
            return True
        self.erro(401, "token ausente ou errado", tipo="authentication_error")
        return False

    def vivo(self):
        """O cliente ainda está do outro lado? Um socket legível que devolve
        zero byte no MSG_PEEK é um socket fechado."""
        try:
            pronto, _, _ = select.select([self.connection], [], [], 0)
            if not pronto:
                return True
            return self.connection.recv(1, socket.MSG_PEEK) != b""
        except OSError as e:
            return e.errno == errno.EAGAIN
        except Exception:  # noqa: BLE001
            return True

    # --------------------------------------------------------------- rotas

    def do_GET(self):
        rota = self.path.split("?", 1)[0].rstrip("/") or "/"
        if rota in ("/healthz", "/health", "/"):
            p = self.ponte
            claude, codex, ollama = p.versao_claude(), p.versao_codex(), p.versao_ollama()
            historico = p.memoria.resumo() if p.memoria is not None else None
            with p.lock:
                self.responder(200, {
                    "ok": True, "servico": "alfred", "versao": VERSAO,
                    "claude": claude, "codex": codex, "ollama": ollama,
                    "historico": historico, "modelo": self.cfg.modelo or "(padrão do CLI)",
                    "esforco": self.cfg.esforco or "(padrão do CLI)",
                    "em_voo": p.em_voo, "paralelas": self.cfg.paralelas,
                    "chamadas": p.chamadas, "falhas": p.falhas,
                    "custo_usd": round(p.custo, 6), "uptime_s": int(time.time() - p.inicio),
                    "tickets": len(p.jobs), "job_teto_s": self.cfg.job_teto,
                    "midia": {"imagens": True, "video": shutil.which(self.cfg.ffmpeg) is not None,
                              "quadros_video": self.cfg.quadros_video, "max_corpo_mb": self.cfg.max_corpo_mb},
                })
            return
        if rota == "/v1/models":
            agora = int(time.time())
            self.responder(200, {"object": "list", "data": [
                {"id": m, "object": "model", "created": agora, "owned_by": "alfred"}
                for m in ("claude-code", "opus", "sonnet", "haiku")
            ] + [{"id": m, "object": "model", "created": agora, "owned_by": "codex"}
                 for m in self.ponte.modelos_codex()]
              + [{"id": PREFIXO_OLLAMA + m, "object": "model", "created": agora, "owned_by": "ollama"}
                 for m in self.ponte.modelos_ollama()]})
            return
        if rota.startswith("/jobs/") or rota.startswith("/v1/jobs/"):
            self.job(rota.rsplit("/", 1)[-1])
            return
        if self.rotas_historico("GET", rota, urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)):
            return
        self.erro(404, f"rota {rota} não existe (tente /v1/chat/completions, /run, /jobs/<id>, "
                       "/v1/conversas, /v1/historico, /v1/exportar, /healthz)")

    def do_DELETE(self):
        if not self.autorizado():
            return
        rota = self.path.split("?", 1)[0].rstrip("/") or "/"
        if self.rotas_historico("DELETE", rota, {}):
            return
        if not (rota.startswith("/jobs/") or rota.startswith("/v1/jobs/")):
            self.erro(404, f"rota {rota} não existe (tente DELETE /jobs/<id> ou /v1/conversas/<id>)")
            return
        t = self.ponte.buscar_job(rota.rsplit("/", 1)[-1])
        if t is None:
            self.erro(404, "ticket não existe (expirou, ou o alfred reiniciou)", tipo="not_found")
            return
        t.cancelado.set()
        log(f"ticket {t.id} cancelado pelo cliente")
        self.responder(200, {"id": t.id, "object": "alfred.job", "status": "cancelado"})

    def do_POST(self):
        if not self.autorizado():
            return
        rota = self.path.split("?", 1)[0].rstrip("/") or "/"
        if rota == "/v1/chat/completions":
            self.chat()
        elif rota == "/run":
            self.run()
        else:
            self.erro(404, f"rota {rota} não existe (tente /v1/chat/completions ou /run)")

    # O caminho padrão do analista (backend `alfred`): formato OpenAI na ida e
    # na volta. É o que não muda — o contrato do /run já mudou uma vez, e o
    # efeito de errar o campo não é um erro na largada, é o analista dizendo
    # "campo não existe" no meio de uma corrida com a chamada já paga.
    def chat(self):
        req = self.corpo()
        if req is None:
            return
        msgs = req.get("messages")
        if not isinstance(msgs, list) or not msgs:
            self.erro(400, "faltou `messages`")
            return
        cid, ok = self.conversa_de(req)
        if not ok:
            return
        # Com conversa, o cliente manda só a mensagem nova: o que já foi dito
        # vem do histórico, antes dela.
        antigas = self.ponte.memoria.historico(cid) if cid else []
        sistema, prompt = separar(antigas + msgs)
        try:
            midia = separar_midia(antigas + msgs)
            blocos, info = (self.ponte.preparar_midia(midia, int(req.get("video_quadros") or 0))
                            if midia else (None, None))
        except ErroMidia as e:
            self.erro(400, str(e))
            return
        if not prompt.strip() and not blocos:
            self.erro(400, "nenhuma mensagem de usuário com conteúdo")
            return
        if info:
            log(f"/v1/chat/completions com mídia: {info['imagens']} imagem(ns), "
                f"{info['videos']} vídeo(s) em {info['quadros']} quadros")
        # `temperature` chega e é ignorada: o CLI do Claude Code não a expõe. O
        # analista já manda 0 nas tarefas de julgamento; recusar a chamada por
        # isso seria trocar uma resposta boa por um erro de forma.
        self.despachar("/v1/chat/completions", req, sistema, prompt,
                       modelo=str(req.get("model") or ""),
                       timeout=int(req.get("timeout_seconds") or 0),
                       turnos=int(req.get("max_turns") or 0),
                       negadas=lista(req.get("disallowed_tools")),
                       ferramentas=ferramentas_de(req),
                       permissao=str(req.get("permission_mode") or ""),
                       esforco=esforco_de(req), blocos=blocos,
                       progresso_rotulo=str(req.get("progresso_rotulo") or "")[:40],
                       registro=self.registro(
                           "/v1/chat/completions", req, cid, separar(msgs)[0],
                           json.dumps(sem_midia(msgs), ensure_ascii=False),
                           [(str(m.get("role") or "user"), texto_de(m.get("content")))
                            for m in msgs if isinstance(m, dict) and m.get("role") != "system"],
                           info))

    # O caminho legado (backend `alfred-run`): {"ok":…, "result":…}, com o erro
    # em `raw.stderr` porque é ali que o config do analista o procura.
    def run(self):
        req = self.corpo()
        if req is None:
            return
        prompt = texto_de(req.get("prompt"))
        if not prompt.strip():
            self.erro(400, "faltou `prompt`")
            return
        cid, ok = self.conversa_de(req)
        if not ok:
            return
        novo = prompt
        if cid:
            _, prompt = separar(self.ponte.memoria.historico(cid) + [{"role": "user", "content": novo}])
        # Fotos e vídeos como data URL (ou {"mime","base64"}) em "imagens" e "videos"
        blocos, info = None, None
        try:
            partes = [("imagem", *midia_de(x)) for x in lista_bruta(req.get("imagens"))]
            partes += [("video", *midia_de(x)) for x in lista_bruta(req.get("videos"))]
            if partes:
                blocos, info = self.ponte.preparar_midia([("texto", prompt), *partes],
                                                         int(req.get("video_quadros") or 0))
                log(f"/run com mídia: {info['imagens']} imagem(ns), {info['videos']} vídeo(s) em {info['quadros']} quadros")
        except ErroMidia as e:
            self.erro(400, str(e))
            return
        sistema = texto_de(req.get("system") or req.get("sistema") or "")
        self.despachar("/run", req, sistema, prompt,
                       modelo=str(req.get("model") or ""),
                       permissao=str(req.get("permission_mode") or ""),
                       turnos=int(req.get("max_turns") or 0),
                       negadas=lista(req.get("disallowed_tools")),
                       timeout=int(req.get("timeout_seconds") or 0),
                       esforco=esforco_de(req), blocos=blocos,
                       registro=self.registro("/run", req, cid, sistema, novo, [("user", novo)], info))

    # ------------------------------------------------------------- memória

    def conversa_de(self, req):
        """(conversa_id, ok). `"nova"` pede ao alfred que invente um id; sem o
        campo, a pergunta é avulsa (como sempre foi), mas entra no histórico."""
        v = req.get("conversa_id") or req.get("conversation_id")
        if v is None or v == "":
            return "", True
        v = str(v).strip()
        if v == "nova":
            v = uuid.uuid4().hex[:16]
        if not RE_CONVERSA.match(v):
            self.erro(400, "conversa_id inválido: até 64 caracteres entre letras, números, ponto, hífen e _")
            return "", False
        if self.ponte.memoria is None:
            self.erro(400, "conversa_id precisa do histórico, e este alfred subiu com --sem-historico")
            return "", False
        return v, True

    def registro(self, rota, req, cid, sistema, entrada, novas, midia):
        """O que a thread da chamada grava no histórico quando ela terminar."""
        return {"rota": rota, "cliente": self.client_address[0], "conversa_id": cid,
                "sistema": sistema, "entrada": entrada, "novas": novas, "midia": midia,
                "criada": time.time(), "gravar": req.get("historico", True) is not False}

    def historico_liberado(self):
        """Ler ou apagar o histórico: com token, quem tem o token; sem token,
        só esta máquina (127.0.0.1). Sem esta regra, qualquer container da rede
        Docker em que o alfred escuta leria todas as conversas do dono."""
        if self.cfg.token:
            return self.autorizado()
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        self.erro(403, "o histórico só abre a partir desta máquina; para ler de fora, configure ALFRED_TOKEN",
                  tipo="permission_error")
        return False

    def rotas_historico(self, metodo, rota, consulta):
        """GET/DELETE de /v1/conversas, /v1/historico e /v1/exportar. True se a
        rota era daqui (respondida ou recusada)."""
        if not (rota.startswith("/v1/conversas") or rota in ("/v1/historico", "/v1/exportar")):
            return False
        if not self.historico_liberado():
            return True
        m = self.ponte.memoria
        if m is None:
            self.erro(404, "histórico desligado (--sem-historico)", tipo="not_found")
            return True
        try:
            limite = max(1, min(int((consulta.get("limite") or ["50"])[0]), 1000))
        except ValueError:
            limite = 50
        partes = rota.split("/")  # ['', 'v1', 'conversas', '<id>']
        ident = partes[3] if len(partes) > 3 else ""
        if metodo == "DELETE":
            if rota.startswith("/v1/conversas/") and ident:
                if m.apagar_conversa(ident):
                    self.responder(200, {"id": ident, "object": "alfred.conversa", "apagada": True})
                else:
                    self.erro(404, f"conversa {ident} não existe", tipo="not_found")
            else:
                self.erro(404, f"rota {rota} não existe (tente DELETE /v1/conversas/<id>)")
            return True
        if rota == "/v1/conversas":
            self.responder(200, {"object": "list", "data": m.conversas(limite)})
        elif ident:
            c = m.conversa(ident)
            if c is None:
                self.erro(404, f"conversa {ident} não existe", tipo="not_found")
            else:
                self.responder(200, c)
        elif rota == "/v1/historico":
            self.responder(200, {"object": "list",
                                 "data": m.chamadas(limite, (consulta.get("conversa") or [""])[0])})
        else:  # /v1/exportar
            self.responder(200, m.exportar())
        return True

    # ------------------------------------------------------- despacho e ticket

    def despachar(self, rota, req, sistema, prompt, **kw):
        """Roda a chamada e responde — direto, ou com um ticket se o cliente
        pediu agendamento e o prazo de resposta acabou antes do trabalho.

        `agendar` é opt-in de propósito. Um cliente que não conhece ticket
        receberia um 202 sem `choices` e quebraria em "campo não existe" no meio
        de uma corrida — o modo de falha mais caro que esta ponte tem.
        """
        permissao = kw.get("permissao") or ""
        if permissao and permissao not in PERMISSOES_POR_CHAMADA:
            self.erro(400, f"permission_mode {permissao!r} não pode vir no pedido "
                           f"(use: {', '.join(PERMISSOES_POR_CHAMADA)}; os outros só por --permissao)")
            return
        prazo_resposta = min(kw.get("timeout") or self.cfg.timeout, self.cfg.timeout_max)
        agendar = bool(req.get("agendar") or req.get("async"))
        if agendar:
            # O trabalho ganha o teto de JOB; a requisição HTTP continua com o
            # prazo de resposta. São dois relógios diferentes, e é essa separação
            # que faz o trabalho sobreviver ao fim da requisição.
            kw["timeout"] = min(int(req.get("job_seconds") or 0) or self.cfg.job_teto,
                                self.cfg.job_teto)
        else:
            kw["timeout"] = min(kw.get("timeout") or self.cfg.timeout, self.cfg.timeout_max)

        t = self.ponte.submeter(rota, sistema, prompt, vivo=self.vivo, **kw)
        r = t.esperar(prazo_resposta)
        if r is not None:
            self.registrar(rota, r)
            self.responder(*self.render(rota, r))
            return
        if not agendar:
            # Sem agendamento não há para onde adiar: o trabalho é largado no
            # prazo dele, como sempre foi.
            r = t.esperar(None)
            self.registrar(rota, r)
            self.responder(*self.render(rota, r))
            return
        n = self.ponte.promover(t)
        log(f"{rota} virou agendamento {t.id} depois de {prazo_resposta}s "
            f"({n} ticket(s) na memória)")
        self.responder(202, self.corpo_ticket(t, self.ponte))

    def render(self, rota, r):
        """(código, corpo) de um Resultado, na forma que a rota promete."""
        if rota == "/run":
            # ATENÇÃO ao `stderr` do sucesso: o analista lê `raw.stderr` como
            # campo de ERRO. O CLI escreve avisos ali de vez em quando, e
            # repassá-los numa resposta boa faria o analista descartar uma
            # resposta que chegou inteira. Numa corrida isso não aparece como
            # bug: aparece como a IA "falhando" sem motivo. Então stderr só
            # viaja quando houve falha.
            if r.ok:
                return 200, {
                    "ok": True, "result": r.texto,
                    "raw": {"stdout": r.texto, "stderr": "", "exit_code": r.saida},
                    "session_id": r.sessao, "custo_usd": r.custo,
                    "dur_ms": r.dur_ms, "num_turns": r.turnos,
                    "chamada_id": r.chamada_id, "conversa_id": r.conversa_id or None,
                }
            return 200, {
                "ok": False, "result": "", "erro": r.erro,
                "raw": {"stdout": primeiras_linhas(r.stdout, 3), "stderr": r.erro,
                        "exit_code": r.saida},
                "dur_ms": r.dur_ms,
            }
        if not r.ok:
            return 502, {"error": {"message": r.erro, "type": "claude_code_error"}}
        return 200, {
            "id": "chatcmpl-" + uuid.uuid4().hex[:24],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": r.modelo,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": r.texto},
                "finish_reason": "length" if r.limite_turnos else "stop",
            }],
            "usage": {
                "prompt_tokens": r.tokens_in,
                "completion_tokens": r.tokens_out,
                "total_tokens": r.tokens_in + r.tokens_out,
            },
            "alfred": {"dur_ms": r.dur_ms, "custo_usd": r.custo,
                       "turnos": r.turnos, "sessao": r.sessao, "provedor": r.provedor,
                       "chamada_id": r.chamada_id, "conversa_id": r.conversa_id or None},
        }

    @staticmethod
    def corpo_ticket(t, ponte=None):
        """O que o cliente recebe enquanto o trabalho não terminou.

        `status` e `id` no primeiro nível para o analista poder decidir com um
        caminho por pontos, sem entender de HTTP. `object` mantém a resposta
        reconhecível para quem já fala OpenAI.
        """
        return {
            "id": t.id,
            "object": "alfred.job",
            "status": "executando",
            "rota": t.rota,
            "decorrido_ms": t.decorrido_ms(),
            "consultar_em": f"/jobs/{t.id}",
            "progresso": t.progresso.json(ponte.media(t.progresso.chave) if ponte else None) if t.progresso else None,
        }

    def job(self, ident):
        t = self.ponte.buscar_job(ident)
        if t is None:
            self.erro(404, f"ticket {ident} não existe (expirou, ou o alfred reiniciou)",
                      tipo="not_found")
            return
        if not t.pronto.is_set():
            self.responder(202, self.corpo_ticket(t, self.ponte))
            return
        self.responder(*self.render(t.rota, t.r))

    def registrar(self, rota, r):
        estado = "ok" if r.ok else "FALHOU: " + primeiras_linhas(r.erro, 1)
        log(f"{rota} {r.dur_ms/1000:.1f}s  US$ {r.custo:.4f}  "
            f"in={r.tokens_in} out={r.tokens_out} turnos={r.turnos}  {estado}")


# ------------------------------------------------------------------ apoio


def separar(msgs):
    """Reparte as mensagens no formato OpenAI em (prompt de sistema, prompt).

    Várias mensagens de sistema viram um bloco só; a conversa vira texto com o
    papel marcado, porque o `claude -p` recebe UM prompt e não uma lista."""
    sistema, conversa = [], []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        papel = str(m.get("role") or "user")
        txt = texto_de(m.get("content"))
        if not txt.strip():
            continue
        if papel == "system":
            sistema.append(txt)
        else:
            conversa.append((papel, txt))
    if len(conversa) == 1:
        return "\n\n".join(sistema), conversa[0][1]
    junto = "\n\n".join(f"[{p}]\n{t}" for p, t in conversa)
    return "\n\n".join(sistema), junto


def midia_de(x):
    """Data URL ("data:image/png;base64,…") ou {"mime", "base64"} → (mime, bytes)."""
    if isinstance(x, dict):
        mime, dados = str(x.get("mime") or x.get("media_type") or ""), x.get("base64") or x.get("data")
    elif isinstance(x, str) and x.startswith("data:") and ";base64," in x:
        mime, dados = x[5:].split(";base64,", 1)
    else:
        raise ErroMidia("foto e vídeo vão como data URL base64 (data:<tipo>;base64,…); "
                        "endereço de internet ou caminho de arquivo não são aceitos")
    if not isinstance(dados, str) or not dados.strip():
        raise ErroMidia("mídia sem conteúdo")
    try:
        return mime.strip().lower(), base64.b64decode(dados, validate=False)
    except (binascii.Error, ValueError):
        raise ErroMidia("base64 inválido na mídia") from None


def lista_bruta(v):
    return v if isinstance(v, list) else ([v] if v else [])


def partes_de(conteudo):
    """Um `content` (texto ou lista de blocos, no formato OpenAI ou Anthropic)
    → [("texto", str) | ("imagem", mime, bytes) | ("video", mime, bytes)]."""
    if not isinstance(conteudo, list):
        t = texto_de(conteudo)
        return [("texto", t)] if t.strip() else []
    partes = []
    for b in conteudo:
        if isinstance(b, str):
            partes.append(("texto", b))
            continue
        if not isinstance(b, dict):
            continue
        tipo = b.get("type")
        if tipo in ("text", "input_text") and isinstance(b.get("text"), str):
            partes.append(("texto", b["text"]))
        elif tipo in ("image_url", "input_image"):
            u = b.get("image_url")
            partes.append(("imagem", *midia_de(u.get("url") if isinstance(u, dict) else u)))
        elif tipo in ("video_url", "input_video"):
            u = b.get("video_url")
            partes.append(("video", *midia_de(u.get("url") if isinstance(u, dict) else u)))
        elif tipo in ("image", "video") and isinstance(b.get("source"), dict):
            fonte = b["source"]
            if fonte.get("type") != "base64":
                raise ErroMidia(f"{tipo} precisa vir em base64 (source.type = base64)")
            partes.append(("imagem" if tipo == "image" else "video", *midia_de(fonte)))
    return partes


def separar_midia(msgs):
    """Se alguma mensagem trouxer foto ou vídeo: as partes da conversa, em ordem e
    com o papel marcado quando houver mais de uma mensagem. Sem mídia: None (o
    caminho de texto puro continua o mesmo de sempre)."""
    conversa = []
    for m in msgs:
        if not isinstance(m, dict) or str(m.get("role") or "user") == "system":
            continue
        partes = partes_de(m.get("content"))
        if partes:
            conversa.append((str(m.get("role") or "user"), partes))
    if not any(p[0] != "texto" for _, ps in conversa for p in ps):
        return None
    saida = []
    for papel, ps in conversa:
        if len(conversa) > 1:
            saida.append(("texto", f"[{papel}]"))
        saida.extend(ps)
    return saida


def ultimo_resultado(bruto):
    for linha in reversed(bruto.splitlines()):
        try:
            d = json.loads(linha)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and d.get("type") == "result":
            return d
    return None


def texto_de(conteudo):
    """O `content` pode ser string ou lista de blocos — aceitar os dois evita
    que um cliente OpenAI comum quebre aqui por uma diferença de forma."""
    if isinstance(conteudo, str):
        return conteudo
    if isinstance(conteudo, list):
        partes = []
        for b in conteudo:
            if isinstance(b, str):
                partes.append(b)
            elif isinstance(b, dict) and isinstance(b.get("text"), str):
                partes.append(b["text"])
        return "\n".join(partes)
    if conteudo is None:
        return ""
    return str(conteudo)


def esforco_de(req):
    """`effort` é o nome do CLI, `esforco` o daqui, `reasoning_effort` o que um
    cliente compatível com OpenAI manda sem saber que existe um CLI atrás."""
    for chave in ("effort", "esforco", "esforço", "reasoning_effort"):
        v = req.get(chave)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def ferramentas_de(req):
    """`tools` pedido pelo cliente, só com as de FERRAMENTAS_POR_CHAMADA; o
    resto é ignorado (vazio = as do servidor, que por padrão são nenhuma)."""
    return ",".join(f for f in lista(req.get("tools")) if f in FERRAMENTAS_POR_CHAMADA)


def lista(v):
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [p.strip() for p in v.split(",") if p.strip()]
    return []


def primeiras_linhas(s, n=3):
    linhas = [l for l in (s or "").strip().splitlines() if l.strip()]
    if len(linhas) > n:
        linhas = linhas[:n] + ["…"]
    return " | ".join(linhas)


def log(*a):
    print("[alfred]", *a, file=sys.stderr, flush=True)


# ------------------------------------------------------------------- main


def flags(argv=None):
    p = argparse.ArgumentParser(
        prog="alfred",
        description="Ponte HTTP local entre o analista (ctfmotor) e o CLI do Claude Code.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""exemplos:
  alfred.py                       # 127.0.0.1:8765, sem ferramenta, modo plan
  alfred.py -m sonnet -n 4        # modelo mais barato, 4 chamadas em paralelo
  alfred.py -m claude-fable-5-1 -e max           # Fable 5.1 pensando o máximo
  alfred.py -m gpt-6.2-sol -e high               # padrão no Codex (GPT-6.2-Sol)
  alfred.py --addr 127.0.0.1:8765,172.31.87.1:8765  # também na rede Docker do Treino IA
  alfred.py --ferramentas Read,Grep --turnos 4   # deixa o Claude ler arquivos
  curl -s localhost:8765/healthz  # está no ar?

o analista fala com ele sem configurar nada: o padrão de fábrica dele já é
http://127.0.0.1:8765/v1/chat/completions.""")
    p.add_argument("--addr", default=os.getenv("ALFRED_ADDR", "127.0.0.1:8765"),
                   help="endereço de escuta; vários separados por vírgula, ex.: "
                        "127.0.0.1:8765,172.31.87.1:8765 (padrão: 127.0.0.1:8765)")
    p.add_argument("--claude", default=os.getenv("ALFRED_CLAUDE", "claude"),
                   help="caminho do CLI do Claude Code")
    p.add_argument("--codex", default=os.getenv("ALFRED_CODEX") or codex_padrao(),
                   help="caminho do CLI do Codex, usado quando o modelo é GPT-… "
                        "(padrão: codex do PATH ou ~/.codex/packages/…/current/codex)")
    p.add_argument("-m", "--modelo", default=os.getenv("ALFRED_MODELO", ""),
                   help="modelo: opus, sonnet, haiku… vão ao Claude Code; gpt-6.2-sol, "
                        "GPT-6.2-Astra… (ou codex:<slug>) vão ao Codex; vazio = o padrão do CLI")
    p.add_argument("-e", "--esforco", default=os.getenv("ALFRED_ESFORCO", ""),
                   choices=["", *ESFORCOS],
                   help="quanto o modelo pensa antes de responder (low…max); vazio = o padrão do CLI")
    p.add_argument("-n", "--paralelas", type=int, default=int(os.getenv("ALFRED_PARALELAS", "2")),
                   help="chamadas simultâneas ao claude (padrão: 2)")
    p.add_argument("-t", "--timeout", type=int, default=240,
                   help="prazo padrão de uma chamada, em segundos (padrão: 240)")
    p.add_argument("--timeout-max", type=int, default=900,
                   help="teto do prazo que o cliente pode pedir (padrão: 900)")
    p.add_argument("--medias", default=os.getenv("ALFRED_MEDIAS") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "progresso.json"),
                   help="arquivo com a média de tamanho e tempo das chamadas, para estimar o %% (padrão: progresso.json ao lado do alfred.py)")
    p.add_argument("--job-teto", type=int, default=int(os.getenv("ALFRED_JOB_TETO", "1800")),
                   help="quanto um trabalho promovido a ticket pode rodar, em segundos "
                        "(padrão: 1800, ou ALFRED_JOB_TETO; a forja do ctfmotor pede 30min)")
    p.add_argument("--job-ttl", type=int, default=900,
                   help="quanto um ticket pronto fica guardado esperando ser lido (padrão: 900s)")
    p.add_argument("--jobs-max", type=int, default=64,
                   help="quantos tickets cabem na memória (padrão: 64)")
    p.add_argument("--turnos", type=int, default=3,
                   help="--max-turns do claude (padrão: 3)")
    p.add_argument("--permissao", default="plan",
                   choices=["plan", "manual", "dontAsk", "acceptEdits", "auto", "bypassPermissions"],
                   help="--permission-mode do claude (padrão: plan)")
    p.add_argument("--ferramentas", default="",
                   help="ferramentas liberadas (ex.: Read,Grep ou 'default'); vazio = nenhuma")
    p.add_argument("--dir", default="",
                   help="diretório de trabalho do claude; vazio = um temporário só dele")
    p.add_argument("--token", default=os.getenv("ALFRED_TOKEN", ""),
                   help="exige Authorization: Bearer <token> nos POSTs")
    p.add_argument("--ffmpeg", default=os.getenv("ALFRED_FFMPEG", "ffmpeg"),
                   help="caminho do ffmpeg, usado para vídeo e para reduzir fotos grandes")
    p.add_argument("--quadros-video", type=int, default=int(os.getenv("ALFRED_QUADROS_VIDEO", "8")),
                   help="quantos quadros tirar de cada vídeo (padrão: 8; o cliente pode pedir com video_quadros)")
    p.add_argument("--max-corpo-mb", type=int, default=int(os.getenv("ALFRED_MAX_CORPO_MB", "64")),
                   help="teto de um pedido, em MB, contando fotos e vídeos em base64 (padrão: 64)")
    p.add_argument("--ollama", default=os.getenv("ALFRED_OLLAMA") or "http://127.0.0.1:11434",
                   help="endereço da API do Ollama, para os modelos ollama:<nome> (padrão: http://127.0.0.1:11434)")
    p.add_argument("--db", default=os.getenv("ALFRED_DB") or caminho_db_padrao(),
                   help="o SQLite do histórico (padrão: ~/.local/share/alfred/alfred.db)")
    p.add_argument("--sem-historico", action="store_true",
                   default=os.getenv("ALFRED_HISTORICO", "1").strip().lower() in ("0", "nao", "não", "false", "off"),
                   help="não grava nada no histórico (e recusa conversa_id)")
    p.add_argument("--exportar", metavar="ARQUIVO",
                   help="escreve o histórico inteiro em JSON (- = saída padrão) e sai, sem subir o servidor")
    p.add_argument("-v", "--verboso", action="store_true", help="loga o argv de cada chamada")
    p.add_argument("-V", "--version", action="version", version=f"alfred {VERSAO}")
    return p.parse_args(argv)


def exportar(cfg):
    """`--exportar`: o histórico inteiro num JSON que é do dono (0600)."""
    if not os.path.exists(cfg.db):
        log(f"!! não há histórico em {cfg.db}")
        return 1
    m = Memoria(cfg.db)
    try:
        doc = json.dumps(m.exportar(), ensure_ascii=False, indent=1)
    finally:
        m.fechar()
    if cfg.exportar == "-":
        sys.stdout.write(doc + "\n")
        return 0
    fd = os.open(cfg.exportar, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(doc + "\n")
    log(f"histórico exportado para {cfg.exportar}")
    return 0


def enderecos_de(texto):
    """"127.0.0.1:8765, 8766" → [("127.0.0.1", 8765), ("127.0.0.1", 8766)]."""
    saida = []
    for a in texto.split(","):
        a = a.strip()
        if not a:
            continue
        if ":" not in a:
            a = "127.0.0.1:" + a
        host, porta = a.rsplit(":", 1)
        saida.append((host, int(porta)))
    return saida


class Servidor(ThreadingHTTPServer):
    # O padrão do socketserver é uma fila de 5 conexões esperando o accept: uma
    # rajada de 64 clientes ao mesmo tempo (medido) já tomava "connection reset".
    request_queue_size = 128
    daemon_threads = True


def abrir(host, porta):
    srv = Servidor((host, porta), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# Um endereço que ainda não existe na máquina — o gateway de uma rede Docker
# que só nasce quando o compose sobe — não derruba a ponte: ela fica no ar nos
# outros e tenta de novo a cada tantos segundos.
RETENTAR_S = 15


def main(argv=None, parar=None):
    cfg = flags(argv)
    if cfg.exportar:
        return exportar(cfg)
    cfg.paralelas = max(1, cfg.paralelas)
    enderecos = enderecos_de(cfg.addr)
    if not enderecos:
        log("!! nenhum endereço em --addr")
        return 2

    ponte = Ponte(cfg)
    Handler.ponte, Handler.cfg = ponte, cfg

    versao = ponte.versao_claude()
    if versao.startswith("indisponível"):
        log(f"!! {cfg.claude}: {versao}")
        log("!! o servidor sobe assim mesmo, mas toda chamada vai falhar até o CLI existir")
    versao_codex = ponte.versao_codex()
    if versao_codex.startswith("indisponível"):
        log(f"!! codex ({cfg.codex}): {versao_codex} — modelos GPT-… vão falhar")

    servidores, pendentes, em_uso = [], [], []
    for host, porta in enderecos:
        try:
            servidores.append(abrir(host, porta))
            log(f"alfred {VERSAO} no ar em http://{host}:{porta}  (claude: {versao}; codex: {versao_codex})")
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                em_uso.append((host, porta))
                log(f"!! {host}:{porta} já está em uso (outro alfred rodando?)")
            else:
                pendentes.append((host, porta))
                log(f"!! {host}:{porta} indisponível agora ({e.strerror}); "
                    f"tento de novo a cada {RETENTAR_S}s")
    if not servidores and not pendentes:
        ponte.fechar()
        return 1

    log(f"     /v1/chat/completions   compatível com OpenAI — backend `alfred` do analista")
    log(f"     /run                   legado {{ok,result}} — backend `alfred-run`")
    log(f"     modelo={cfg.modelo or '(padrão)'} esforco={cfg.esforco or '(padrão)'} "
        f"paralelas={cfg.paralelas} "
        f"timeout={cfg.timeout}s ferramentas={cfg.ferramentas or 'nenhuma'} permissao={cfg.permissao}")
    log(f"     ollama={cfg.ollama} ({ponte.versao_ollama()})  "
        f"histórico={'desligado' if ponte.memoria is None else cfg.db}")

    # SIGTERM (systemd, kill, `iniciar.sh parar`) encerra igual ao Ctrl+C: sem
    # isto o diretório temporário dos `claude` ficaria para trás.
    parar = parar or threading.Event()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: parar.set())
    try:
        while not parar.wait(RETENTAR_S if pendentes else 3600):
            for host, porta in list(pendentes):
                try:
                    servidores.append(abrir(host, porta))
                    pendentes.remove((host, porta))
                    log(f"{host}:{porta} apareceu: no ar também ali")
                except OSError:
                    pass
    except KeyboardInterrupt:
        pass
    log("encerrando")
    for srv in servidores:
        srv.shutdown()
        srv.server_close()
    ponte.fechar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
