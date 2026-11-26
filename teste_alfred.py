#!/usr/bin/env python3
# teste_alfred.py — o que a ponte promete ao analista, verificado sem gastar
# uma chamada de IA de verdade.
#
# O `claude` aqui é falso: um script que registra o argv, o stdin, o cwd e o
# prompt de sistema que recebeu, e responde o que o teste mandar. É ele que
# permite afirmar as duas coisas que só se descobririam no meio de uma corrida:
# que o prompt NÃO passa por argv, e que o stderr de uma resposta boa não vaza
# para o campo que o analista lê como erro.
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alfred  # noqa: E402

FALSO = r'''#!/usr/bin/env python3
import json, os, stat, sys, time
log = os.environ["FALSO_LOG"]
modo = os.environ.get("FALSO_MODO", "ok")
if "--version" in sys.argv:
    print("9.9.9 (falso)"); sys.exit(0)
argv = sys.argv[1:]
entrada = sys.stdin.read()
sistema, modo_arq = None, None
if "--system-prompt-file" in argv:
    p = argv[argv.index("--system-prompt-file") + 1]
    sistema = open(p).read()
    modo_arq = stat.S_IMODE(os.stat(p).st_mode)
json.dump({"argv": argv, "stdin": entrada, "cwd": os.getcwd(),
           "sistema": sistema, "modo_arq": modo_arq}, open(log, "w"))
if "--input-format" in argv and argv[argv.index("--input-format") + 1] == "stream-json":
    # foto/vídeo: uma mensagem JSON no stdin; responde em eventos, um por linha
    blocos = json.loads(entrada.strip().splitlines()[0])["message"]["content"]
    n = sum(1 for b in blocos if b.get("type") == "image")
    textos = " | ".join(b["text"] for b in blocos if b.get("type") == "text")
    print(json.dumps({"type": "system", "subtype": "init"}))
    print(json.dumps({"type": "assistant", "message": {}}))
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": f"IMAGENS={n} TEXTO={textos[:300]}", "num_turns": 1,
                      "total_cost_usd": 0.001, "usage": {"input_tokens": 5, "output_tokens": 2}}))
    sys.exit(0)
if "--include-partial-messages" in argv:
    # andamento: o raciocínio e a resposta chegam em pedaços, como no CLI de verdade
    def ev(d):
        print(json.dumps(d), flush=True)
    passo = float(os.environ.get("FALSO_PASSO", "0.3"))
    ev({"type": "system", "subtype": "init"})
    ev({"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}})
    for _ in range(4):
        time.sleep(passo)
        ev({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "x" * 50}}})
    ev({"type": "stream_event", "event": {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}}})
    for _ in range(3):
        time.sleep(passo)
        ev({"type": "stream_event", "event": {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "y" * 20}}})
    ev({"type": "assistant", "message": {"content": [{"type": "text", "text": "y" * 60}]}})
    ev({"type": "result", "subtype": "success", "is_error": False, "result": "PRONTO " + entrada.strip()[:20],
        "num_turns": 1, "total_cost_usd": 0.002, "usage": {"input_tokens": 5, "output_tokens": 30}})
    sys.exit(0)
if modo == "demora":
    time.sleep(60); sys.exit(0)
time.sleep(float(os.environ.get("FALSO_ESPERA", "0")))
if modo == "lixo":
    print("texto puro, sem json nenhum"); sys.exit(0)
if modo == "lixo-com-erro":
    print("meia resposta"); print("estourou o contexto", file=sys.stderr); sys.exit(1)
if modo == "erro":
    print(json.dumps({"type": "result", "subtype": "error_during_execution",
                      "is_error": True, "result": "credit balance is too low",
                      "num_turns": 1, "total_cost_usd": 0.0})); sys.exit(1)
if modo == "avisa":
    print("aviso: sua versao esta velha", file=sys.stderr)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "RESPOSTA: " + entrada.strip()[:40],
                  "session_id": "sess-1", "num_turns": 1, "total_cost_usd": 0.0042,
                  "duration_ms": 12, "usage": {"input_tokens": 10, "output_tokens": 3,
                                               "cache_read_input_tokens": 5}}))
'''

# O `codex` falso: registra argv, stdin, cwd, as instruções de sistema (com o
# modo do arquivo) e as imagens anexadas; responde em eventos JSONL como o
# `codex exec --json` de verdade.
FALSO_CODEX = r'''#!/usr/bin/env python3
import json, os, stat, sys, time
if "--version" in sys.argv:
    print("codex-cli 0.0.0 (falso)"); sys.exit(0)
argv = sys.argv[1:]
entrada = sys.stdin.read()
sistema = modo_arq = None
imagens = []
for i, a in enumerate(argv):
    if a == "-c" and argv[i + 1].startswith("model_instructions_file="):
        p = json.loads(argv[i + 1].split("=", 1)[1])
        sistema = open(p).read()
        modo_arq = stat.S_IMODE(os.stat(p).st_mode)
    if a == "-i":
        imagens.append({"bytes": len(open(argv[i + 1], "rb").read()),
                        "modo": stat.S_IMODE(os.stat(argv[i + 1]).st_mode)})
json.dump({"argv": argv, "stdin": entrada, "cwd": os.getcwd(), "sistema": sistema,
           "modo_arq": modo_arq, "imagens": imagens}, open(os.environ["FALSO_LOG_CODEX"], "w"))
def ev(d):
    print(json.dumps(d), flush=True)
modelo = argv[argv.index("-m") + 1]
ev({"type": "thread.started", "thread_id": "thr-1"})
ev({"type": "turn.started"})
if os.environ.get("FALSO_MODO") == "erro":
    ev({"type": "error", "message": f"The '{modelo}' model is not supported"})
    ev({"type": "turn.failed", "error": {"message": f"The '{modelo}' model is not supported"}})
    sys.exit(1)
time.sleep(float(os.environ.get("FALSO_PASSO", "0")))
ev({"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "x" * 40}})
ev({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "rascunho"}})
ev({"type": "item.completed", "item": {"id": "item_2", "type": "agent_message",
    "text": f"CODEX {modelo} IMAGENS={len(imagens)}: " + entrada.strip()[:60]}})
ev({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 80,
    "output_tokens": 7, "reasoning_output_tokens": 2}})
'''


class Base(unittest.TestCase):
    def sobe(self, *extra, modo="ok", espera=0):
        d = tempfile.mkdtemp(prefix="teste-alfred-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", d]))
        falso = os.path.join(d, "claude-falso")
        with open(falso, "w") as f:
            f.write(FALSO)
        os.chmod(falso, 0o755)
        self.log = os.path.join(d, "chamada.json")
        os.environ["FALSO_LOG"], os.environ["FALSO_MODO"] = self.log, modo
        os.environ["FALSO_ESPERA"] = str(espera)
        codex = os.path.join(d, "codex-falso")
        with open(codex, "w") as f:
            f.write(FALSO_CODEX)
        os.chmod(codex, 0o755)
        self.log_codex = os.path.join(d, "chamada-codex.json")
        os.environ["FALSO_LOG_CODEX"] = self.log_codex

        cfg = alfred.flags(["--claude", falso, "--codex", codex, *extra])
        self.ponte = alfred.Ponte(cfg)
        alfred.Handler.ponte, alfred.Handler.cfg = self.ponte, cfg
        srv = ThreadingHTTPServer(("127.0.0.1", 0), alfred.Handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(self.ponte.fechar)
        self.base = f"http://127.0.0.1:{srv.server_address[1]}"
        return self.base

    def post(self, rota, doc, cabecalhos=None):
        req = urllib.request.Request(self.base + rota, data=json.dumps(doc).encode(),
                                     headers={"Content-Type": "application/json",
                                              **(cabecalhos or {})})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(self, rota):
        try:
            with urllib.request.urlopen(self.base + rota, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def chamada(self):
        with open(self.log) as f:
            return json.load(f)

    def chamada_codex(self):
        with open(self.log_codex) as f:
            return json.load(f)


class ContratoV1(Base):
    """O caminho padrão do analista: backend `alfred`, resposta em
    choices.0.message.content. Errar esse campo não dá erro na largada — dá
    "campo não existe" no meio da corrida, com a chamada já paga."""

    def test_resposta_no_caminho_que_o_analista_le(self):
        self.sobe()
        codigo, doc = self.post("/v1/chat/completions", {
            "model": "claude-code", "temperature": 0,
            "messages": [{"role": "system", "content": "voce e um analista"},
                         {"role": "user", "content": "julgue este achado"}]})
        self.assertEqual(codigo, 200)
        # O mesmo caminho que backend.go percorre: choices.0.message.content.
        texto = doc["choices"][0]["message"]["content"]
        self.assertTrue(texto.startswith("RESPOSTA: julgue este achado"), texto)
        self.assertEqual(doc["choices"][0]["finish_reason"], "stop")
        self.assertEqual(doc["usage"]["prompt_tokens"], 15)  # input + cache_read
        self.assertEqual(doc["usage"]["completion_tokens"], 3)
        self.assertEqual(doc["alfred"]["custo_usd"], 0.0042)

    def test_o_prompt_nao_passa_por_argv(self):
        """A razão de existir do stdin aqui: o prompt carrega a -ia-instrucao da
        corrida, e ela costuma trazer a senha da conta de teste. argv é público
        na máquina inteira (`ps`)."""
        self.sobe()
        segredo = "a conta de teste e admin/Senha123"
        self.post("/v1/chat/completions", {
            "messages": [{"role": "system", "content": "sistema: " + segredo},
                         {"role": "user", "content": "pergunta: " + segredo}]})
        c = self.chamada()
        self.assertNotIn(segredo, " ".join(c["argv"]))
        self.assertIn(segredo, c["stdin"])
        # O de sistema vai por arquivo, e o arquivo é só do dono.
        self.assertIn(segredo, c["sistema"])
        self.assertEqual(c["modo_arq"], 0o600, oct(c["modo_arq"]))

    def test_claude_sobe_sem_ferramenta_e_sem_contexto_de_projeto(self):
        self.sobe()
        self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "oi"}]})
        argv = self.chamada()["argv"]
        self.assertIn("--tools", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        for f in ("--no-session-persistence", "--strict-mcp-config",
                  "--disable-slash-commands", "--permission-prompts"):
            self.assertIn(f, argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        # cwd próprio: rodar de onde o alfred foi lançado faria o Claude Code
        # descobrir o CLAUDE.md e o git dali, em toda chamada, pago por token.
        self.assertNotEqual(self.chamada()["cwd"], os.getcwd())

    def test_ferramentas_variadicas_ficam_no_fim(self):
        """`--tools` e `--disallowed-tools` engolem o que vem depois. Foi assim
        que a primeira medição do CLI perdeu o prompt."""
        self.sobe("--ferramentas", "Read,Grep")
        self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "oi"}],
                                           "disallowed_tools": ["Bash", "Write"]})
        argv = self.chamada()["argv"]
        self.assertEqual(argv[-2:], ["--disallowed-tools", "Bash,Write"])

    def test_tools_por_chamada_so_libera_a_pesquisa_na_web(self):
        """Um cliente liga a pesquisa na análise de um artigo; o texto do artigo é de
        terceiros, então o pedido nunca consegue ligar Bash ou arquivo."""
        self.sobe()
        self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "oi"}],
                                           "tools": ["WebSearch", "WebFetch", "Bash", "Read"]})
        argv = self.chamada()["argv"]
        self.assertEqual(argv[-2:], ["--tools", "WebSearch,WebFetch"])
        self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "oi"}],
                                           "tools": "Bash,Write"})
        argv = self.chamada()["argv"]
        self.assertEqual(argv[-2:], ["--tools", ""])

    def test_modelo_generico_nao_vira_flag(self):
        """`claude-code` é o rótulo que o analista põe no campo model, não um
        modelo — repassá-lo faria o CLI recusar a chamada."""
        self.sobe()
        self.post("/v1/chat/completions", {"model": "claude-code",
                                           "messages": [{"role": "user", "content": "oi"}]})
        self.assertNotIn("--model", self.chamada()["argv"])
        self.post("/v1/chat/completions", {"model": "sonnet",
                                           "messages": [{"role": "user", "content": "oi"}]})
        argv = self.chamada()["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")

    def test_esforco_vai_como_flag_e_o_pedido_vence_a_do_servidor(self):
        """`-e max` é o piso; uma chamada pode pedir mais (ou menos) por
        tarefa — julgar um achado merece esforço que somar duas listas não."""
        self.sobe("-m", "claude-fable-5-1", "-e", "high")
        self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "oi"}]})
        argv = self.chamada()["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-fable-5-1")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "oi"}],
                                           "effort": "max"})
        argv = self.chamada()["argv"]
        self.assertEqual(argv[argv.index("--effort") + 1], "max")
        # `reasoning_effort` é como um cliente compatível com OpenAI chamaria.
        self.post("/run", {"prompt": "oi", "reasoning_effort": "low"})
        argv = self.chamada()["argv"]
        self.assertEqual(argv[argv.index("--effort") + 1], "low")

    def test_esforco_inventado_morre_antes_de_abrir_processo(self):
        self.sobe()
        codigo, doc = self.post("/v1/chat/completions",
                                {"messages": [{"role": "user", "content": "oi"}],
                                 "effort": "maximo"})
        self.assertEqual(codigo, 502)
        self.assertIn("não existe", doc["error"]["message"])
        self.assertFalse(os.path.exists(self.log), "abriu processo para um esforço inválido")

    def test_falha_da_ia_vira_erro_legivel(self):
        self.sobe(modo="erro")
        codigo, doc = self.post("/v1/chat/completions",
                                {"messages": [{"role": "user", "content": "oi"}]})
        self.assertEqual(codigo, 502)
        self.assertIn("credit balance", doc["error"]["message"])

    def test_corpo_sem_mensagem_e_recusado_antes_de_gastar(self):
        self.sobe()
        codigo, doc = self.post("/v1/chat/completions", {"messages": []})
        self.assertEqual(codigo, 400)
        self.assertFalse(os.path.exists(self.log), "chamou o claude para um corpo inválido")


class ContratoRun(Base):
    """O caminho legado, backend `alfred-run`: texto em `result`, ok em `ok`,
    erro em `raw.stderr`."""

    def test_sucesso(self):
        self.sobe()
        codigo, doc = self.post("/run", {"prompt": "julgue isto", "permission_mode": "plan",
                                         "max_turns": 3, "timeout_seconds": 240,
                                         "disallowed_tools": ["Bash", "Write", "Edit"]})
        self.assertEqual(codigo, 200)
        self.assertTrue(doc["ok"])
        self.assertTrue(doc["result"].startswith("RESPOSTA: julgue isto"))
        self.assertEqual(doc["session_id"], "sess-1")

    def test_stderr_de_resposta_boa_nao_vaza_para_o_campo_de_erro(self):
        """O analista lê `raw.stderr` como ERRO. O CLI escreve aviso ali de vez
        em quando, e repassá-lo numa resposta boa faria o analista descartar uma
        resposta inteira — e isso não aparece como bug numa corrida, aparece
        como a IA falhando sem motivo."""
        self.sobe(modo="avisa")
        _, doc = self.post("/run", {"prompt": "oi"})
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["raw"]["stderr"], "")

    def test_falha_explica_no_lugar_certo(self):
        self.sobe(modo="erro")
        codigo, doc = self.post("/run", {"prompt": "oi"})
        self.assertEqual(codigo, 200)
        self.assertFalse(doc["ok"])
        self.assertIn("credit balance", doc["raw"]["stderr"])
        self.assertIn("credit balance", doc["erro"])


class Bordas(Base):
    def test_saida_sem_json_com_codigo_zero_ainda_e_resposta(self):
        self.sobe(modo="lixo")
        codigo, doc = self.post("/v1/chat/completions",
                                {"messages": [{"role": "user", "content": "oi"}]})
        self.assertEqual(codigo, 200)
        self.assertEqual(doc["choices"][0]["message"]["content"], "texto puro, sem json nenhum")

    def test_saida_sem_json_com_codigo_de_erro_e_falha(self):
        self.sobe(modo="lixo-com-erro")
        codigo, doc = self.post("/v1/chat/completions",
                                {"messages": [{"role": "user", "content": "oi"}]})
        self.assertEqual(codigo, 502)
        self.assertIn("estourou o contexto", doc["error"]["message"])

    def test_prazo_encerra_a_chamada_em_vez_de_pendurar(self):
        self.sobe("--timeout", "2", modo="demora")
        t0 = time.time()
        codigo, doc = self.post("/v1/chat/completions",
                                {"messages": [{"role": "user", "content": "oi"}]})
        gasto = time.time() - t0
        self.assertEqual(codigo, 502)
        self.assertIn("2s", doc["error"]["message"])
        self.assertLess(gasto, 20, "o prazo não encerrou a chamada")

    def test_prazo_pedido_nao_passa_do_teto(self):
        self.sobe("--timeout", "2", "--timeout-max", "3", modo="demora")
        t0 = time.time()
        codigo, _ = self.post("/v1/chat/completions",
                              {"messages": [{"role": "user", "content": "oi"}],
                               "timeout_seconds": 600})
        self.assertEqual(codigo, 502)
        self.assertLess(time.time() - t0, 20)

    def test_token_quando_configurado(self):
        self.sobe("--token", "abc")
        codigo, doc = self.post("/v1/chat/completions",
                                {"messages": [{"role": "user", "content": "oi"}]})
        self.assertEqual(codigo, 401)
        codigo, _ = self.post("/v1/chat/completions",
                              {"messages": [{"role": "user", "content": "oi"}]},
                              {"Authorization": "Bearer abc"})
        self.assertEqual(codigo, 200)

    def test_healthz_e_rota_desconhecida(self):
        self.sobe()
        codigo, doc = self.get("/healthz")
        self.assertEqual(codigo, 200)
        self.assertTrue(doc["ok"])
        self.assertIn("9.9.9", doc["claude"])
        codigo, _ = self.get("/nao-existe")
        self.assertEqual(codigo, 404)

    def test_uma_vaga_por_vez_quando_paralelas_e_um(self):
        self.sobe("-n", "1")
        ordem = []

        def bate():
            ordem.append(self.post("/v1/chat/completions",
                                   {"messages": [{"role": "user", "content": "oi"}]})[0])
        ts = [threading.Thread(target=bate) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        self.assertEqual(ordem, [200] * 4)


class Fila(Base):
    """O prazo de uma chamada é UM só, e vale da chegada à resposta.

    O defeito: `vaga.acquire(timeout=timeout)` e depois `rodar(..., timeout)`
    davam o prazo inteiro DUAS vezes. Com o padrão de 240s, uma chamada que
    esperasse 200s na fila ainda ganhava 240s de processo — 440s contra um
    analista que desiste em 240s (o teto por pergunta do `servir`) ou 300s (o
    http.Client). O log enchia de "cliente fechou a conexão antes da resposta"
    e a inferência do trecho final era paga para escrever num socket morto.
    """

    def ocupa_a_vaga(self):
        """Toma a única vaga e devolve uma função que a solta."""
        self.ponte.vaga.acquire()
        soltou = threading.Event()

        def solta():
            if not soltou.is_set():
                soltou.set()
                self.ponte.vaga.release()

        self.addCleanup(solta)
        return solta

    def test_espera_na_fila_sai_do_prazo_da_chamada(self):
        self.sobe("--timeout", "30", "-n", "1")
        solta = self.ocupa_a_vaga()
        threading.Timer(2.0, solta).start()

        t0 = time.time()
        restante = self.ponte.esperar_vaga(30, None)
        esperou = time.time() - t0

        self.assertIsNotNone(restante, "a vaga foi liberada; a chamada devia seguir")
        self.ponte.vaga.release()
        self.assertGreater(esperou, 1.5, "o teste não chegou a exercitar a fila")
        # O que sobra é o prazo MENOS a fila. Antes, `restante` seria 30 de novo.
        self.assertLess(restante, 30 - 1.5,
                        f"a fila comeu {esperou:.1f}s e o processo ainda recebeu {restante:.1f}s: "
                        "o prazo está sendo gasto duas vezes")
        self.assertGreater(restante, 30 - esperou - 1.0, "sobrou menos prazo do que o devido")

    def test_cliente_que_desiste_na_fila_nao_gasta_inferencia(self):
        self.sobe("--timeout", "30", "-n", "1", modo="demora")
        self.ocupa_a_vaga()
        os.unlink(self.log) if os.path.exists(self.log) else None

        t0 = time.time()
        r = self.ponte.chamar("sistema", "prompt", vivo=lambda: False)
        gasto = time.time() - t0

        self.assertFalse(r.ok)
        self.assertIn("desistiu", r.erro)
        self.assertLess(gasto, 5, "o alfred ficou preso na fila em vez de largar a chamada")
        self.assertFalse(os.path.exists(self.log),
                         "subiu um Claude Code pago para um cliente que já tinha ido embora")

    def test_prazo_curto_com_fila_ocupada_recusa_em_vez_de_subir_processo(self):
        self.sobe("--timeout", "3", "-n", "1", modo="demora")
        self.ocupa_a_vaga()
        os.unlink(self.log) if os.path.exists(self.log) else None

        t0 = time.time()
        r = self.ponte.chamar("sistema", "prompt")
        gasto = time.time() - t0

        self.assertFalse(r.ok)
        self.assertIn("fila cheia", r.erro)
        self.assertLess(gasto, 5)
        self.assertFalse(os.path.exists(self.log))


class Agendamento(Base):
    """Pergunta cara não morre no prazo da requisição: vira ticket.

    A forja pede ao modelo o código INTEIRO de uma ferramenta e o ctfmotor
    reserva 30 minutos para isso (`-forja-teto`), enquanto uma requisição HTTP
    razoável dura 4. O excedente virava `a chamada passou de 240s e foi
    encerrada` — trabalho jogado fora no ponto mais caro da corrida.
    """

    def ate_ficar_pronto(self, ident, limite=30):
        """Consulta o ticket como o analista faz, até sair de `executando`."""
        for _ in range(limite):
            codigo, doc = self.get(f"/jobs/{ident}")
            if doc.get("status") != "executando":
                return codigo, doc
            time.sleep(0.5)
        self.fail(f"o ticket {ident} nunca ficou pronto")

    def test_passar_do_prazo_vira_ticket_em_vez_de_matar_o_trabalho(self):
        self.sobe("--timeout", "1", espera=4)
        t0 = time.time()
        codigo, doc = self.post("/v1/chat/completions",
                                {"agendar": True,
                                 "messages": [{"role": "user", "content": "forje uma ferramenta"}]})
        self.assertEqual(codigo, 202, doc)
        self.assertEqual(doc["status"], "executando")
        self.assertTrue(doc["id"], "ticket sem identificador é inútil para consultar")
        self.assertLess(time.time() - t0, 3, "o 202 devia sair no prazo de RESPOSTA")

        # E o trabalho seguiu vivo: a resposta chega inteira depois.
        codigo, doc = self.ate_ficar_pronto(doc["id"])
        self.assertEqual(codigo, 200, doc)
        self.assertIn("RESPOSTA:", doc["choices"][0]["message"]["content"])

    def test_ticket_pode_ser_lido_mais_de_uma_vez(self):
        self.sobe("--timeout", "1", espera=3)
        _, tk = self.post("/v1/chat/completions",
                          {"agendar": True, "messages": [{"role": "user", "content": "oi"}]})
        primeiro = self.ate_ficar_pronto(tk["id"])
        segundo = self.get(f"/jobs/{tk['id']}")
        self.assertEqual(primeiro[0], segundo[0])
        self.assertEqual(primeiro[1]["choices"][0]["message"]["content"],
                         segundo[1]["choices"][0]["message"]["content"])

    def test_rota_run_tambem_agenda_e_devolve_no_formato_dela(self):
        self.sobe("--timeout", "1", espera=3)
        codigo, tk = self.post("/run", {"agendar": True, "prompt": "oi"})
        self.assertEqual(codigo, 202, tk)
        codigo, doc = self.ate_ficar_pronto(tk["id"])
        self.assertEqual(codigo, 200)
        # O ticket lembra de qual rota nasceu: quem chamou /run lê `result`.
        self.assertTrue(doc["ok"])
        self.assertIn("RESPOSTA:", doc["result"])

    def test_sem_agendar_o_prazo_continua_matando_a_chamada(self):
        """A regressão que importa: cliente que não conhece ticket não pode
        receber um 202 sem `choices` — quebraria em "campo não existe" no meio
        de uma corrida, o modo de falha mais caro desta ponte."""
        self.sobe("--timeout", "2", modo="demora")
        codigo, doc = self.post("/v1/chat/completions",
                                {"messages": [{"role": "user", "content": "oi"}]})
        self.assertEqual(codigo, 502)
        self.assertIn("2s", doc["error"]["message"])

    def test_ticket_que_nao_existe_diz_o_que_houve(self):
        self.sobe()
        codigo, doc = self.get("/jobs/naoexiste")
        self.assertEqual(codigo, 404)
        self.assertIn("expirou", doc["error"]["message"])


# PNG de 1x1 válido
PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def video_de_teste():
    """Vídeo mp4 de 2 s gerado pelo ffmpeg (None se não houver ffmpeg)."""
    import base64, shutil
    if not shutil.which("ffmpeg"):
        return None
    d = tempfile.mkdtemp(prefix="teste-video-")
    caminho = os.path.join(d, "v.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
                    "-pix_fmt", "yuv420p", caminho], check=True)
    with open(caminho, "rb") as f:
        dados = base64.b64encode(f.read()).decode()
    subprocess.run(["rm", "-rf", d])
    return dados


class Midia(Base):
    def test_foto_formato_openai_vai_por_stream_json_sem_ferramenta(self):
        self.sobe()
        codigo, doc = self.post("/v1/chat/completions", {"messages": [
            {"role": "system", "content": "sistema X"},
            {"role": "user", "content": [{"type": "text", "text": "que é isso?"},
                                         {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG}}]}]})
        self.assertEqual(codigo, 200, doc)
        self.assertIn("IMAGENS=1", doc["choices"][0]["message"]["content"])
        argv = self.chamada()["argv"]
        self.assertEqual(argv[argv.index("--input-format") + 1], "stream-json")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertEqual(argv[argv.index("--tools") + 1], "", "foto não pode ligar ferramenta")
        msg = json.loads(self.chamada()["stdin"])
        self.assertEqual(msg["message"]["content"][1]["source"]["data"], PNG)
        self.assertEqual(self.chamada()["sistema"], "sistema X")

    def test_foto_formato_anthropic(self):
        self.sobe()
        codigo, doc = self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}},
            {"type": "text", "text": "descreva"}]}]})
        self.assertEqual(codigo, 200, doc)
        self.assertIn("IMAGENS=1", doc["choices"][0]["message"]["content"])

    def test_texto_puro_continua_no_caminho_antigo(self):
        self.sobe()
        self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "oi"}]})
        argv = self.chamada()["argv"]
        self.assertNotIn("--input-format", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")

    def test_video_vira_quadros_em_ordem(self):
        video = video_de_teste()
        if video is None:
            self.skipTest("sem ffmpeg")
        self.sobe("--quadros-video", "4")
        codigo, doc = self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "avalie a execução"},
            {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + video}}]}]})
        self.assertEqual(codigo, 200, doc)
        texto = doc["choices"][0]["message"]["content"]
        self.assertIn("IMAGENS=4", texto)
        self.assertIn("Quadro 1", texto)
        self.assertIn("Vídeo de 2,0 s", texto)

    def test_run_aceita_imagens(self):
        self.sobe()
        codigo, doc = self.post("/run", {"prompt": "olhe", "imagens": ["data:image/png;base64," + PNG,
                                                                       {"mime": "image/png", "base64": PNG}]})
        self.assertEqual(codigo, 200, doc)
        self.assertIn("IMAGENS=2", doc["result"])

    def test_midia_sem_data_url_e_400_sem_chamar_o_claude(self):
        self.sobe()
        codigo, doc = self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://exemplo.com/foto.png"}}]}]})
        self.assertEqual(codigo, 400)
        self.assertIn("data URL", doc["error"]["message"])
        self.assertFalse(os.path.exists(self.log), "chamou o claude com mídia inválida")

    def test_healthz_diz_o_que_aceita(self):
        self.sobe()
        _, doc = self.get("/healthz")
        self.assertTrue(doc["midia"]["imagens"])
        self.assertIn("quadros_video", doc["midia"])


class Cancelamento(Base):
    def test_delete_mata_o_claude_do_ticket(self):
        self.sobe("--timeout", "2", modo="demora")
        codigo, doc = self.post("/v1/chat/completions",
                                {"messages": [{"role": "user", "content": "oi"}],
                                 "agendar": True, "job_seconds": 60})
        self.assertEqual(codigo, 202)
        req = urllib.request.Request(self.base + "/jobs/" + doc["id"], method="DELETE")
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(json.loads(r.read())["status"], "cancelado")
        for _ in range(40):
            codigo, fim = self.get("/jobs/" + doc["id"])
            if codigo != 202:
                break
            time.sleep(0.25)
        self.assertEqual(codigo, 502)
        self.assertIn("desistiu", fim["error"]["message"])

    def test_delete_pede_token_quando_configurado(self):
        self.sobe("--token", "abc")
        req = urllib.request.Request(self.base + "/jobs/qualquer", method="DELETE")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(e.exception.code, 401)


class TetoDoJob(Base):
    def test_ticket_vai_alem_do_timeout_max_ate_o_teto_de_job(self):
        # Antes o timeout_max (900 s por padrão) cortava também o ticket, e um
        # --job-teto maior não valia: o plano do Treino IA morria aos 15 min.
        self.sobe("--timeout", "1", "--timeout-max", "1", "--job-teto", "20", espera=2.5)
        codigo, doc = self.post("/v1/chat/completions",
                                {"messages": [{"role": "user", "content": "oi"}],
                                 "agendar": True, "timeout_seconds": 1, "job_seconds": 15})
        self.assertEqual(codigo, 202, doc)
        for _ in range(60):
            codigo, fim = self.get("/jobs/" + doc["id"])
            if codigo != 202:
                break
            time.sleep(0.25)
        self.assertEqual(codigo, 200, fim)

    def test_chamada_direta_continua_limitada_ao_timeout_max(self):
        self.sobe("--timeout", "1", "--timeout-max", "1", "--job-teto", "20", espera=2.5)
        codigo, doc = self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "oi"}], "timeout_seconds": 15})
        self.assertNotEqual(codigo, 200, doc)


class Andamento(Base):
    def _ticket(self):
        codigo, doc = self.post("/v1/chat/completions",
                                {"messages": [{"role": "user", "content": "monte o plano"}],
                                 "agendar": True, "timeout_seconds": 1, "job_seconds": 30,
                                 "progresso_rotulo": "plano"})
        self.assertEqual(codigo, 202, doc)
        return doc

    def _ate_o_fim(self, ident):
        vistos = []
        for _ in range(80):
            codigo, doc = self.get("/jobs/" + ident)
            if codigo != 202:
                return codigo, doc, vistos
            vistos.append(doc["progresso"])
            time.sleep(0.15)
        self.fail("o ticket não terminou")

    def test_ticket_conta_raciocinio_e_resposta_e_aprende_a_media(self):
        d = tempfile.mkdtemp(prefix="teste-medias-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", d]))
        medias = os.path.join(d, "medias.json")
        self.sobe("--timeout", "1", "--medias", medias)
        doc = self._ticket()
        self.assertEqual(doc["progresso"]["rotulo"], "plano")
        codigo, fim, vistos = self._ate_o_fim(doc["id"])
        self.assertEqual(codigo, 200, fim)
        self.assertIn("PRONTO", fim["choices"][0]["message"]["content"])
        fases = {v["fase"] for v in vistos}
        self.assertTrue(fases & {"pensando", "escrevendo"}, fases)
        self.assertGreater(max(v["pensamento_chars"] for v in vistos), 0)
        # sem histórico não há porcentagem inventada
        self.assertIsNone(vistos[0]["pct"])
        self.assertIn("--include-partial-messages", json.load(open(self.log))["argv"])
        salvo = json.load(open(medias))
        chave = next(iter(salvo))
        self.assertTrue(chave.startswith("plano|"), chave)
        self.assertEqual(salvo[chave]["chars"], 260)
        # a segunda chamada do mesmo tipo já tem % (pela média da primeira)
        doc = self._ticket()
        _, _, vistos = self._ate_o_fim(doc["id"])
        pcts = [v["pct"] for v in vistos if v["pct"] is not None]
        self.assertTrue(pcts, vistos)
        self.assertLessEqual(max(pcts), 97)
        self.assertEqual(pcts, sorted(pcts))

    def test_sem_rotulo_continua_em_json_sem_andamento(self):
        self.sobe()
        codigo, doc = self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "oi"}]})
        self.assertEqual(codigo, 200, doc)
        argv = json.load(open(self.log))["argv"]
        self.assertNotIn("--include-partial-messages", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")


class VariosEnderecos(unittest.TestCase):
    """A cópia em ~/job/alfred escuta em mais de um endereço: o loopback do
    analista e o gateway da rede Docker do Treino IA."""

    def test_enderecos_de(self):
        self.assertEqual(alfred.enderecos_de("127.0.0.1:8765, 172.31.87.1:8765"),
                         [("127.0.0.1", 8765), ("172.31.87.1", 8765)])
        self.assertEqual(alfred.enderecos_de("8766"), [("127.0.0.1", 8766)])
        self.assertEqual(alfred.enderecos_de(" , "), [])

    def test_sobe_em_dois_enderecos_e_espera_o_que_nao_existe(self):
        import socket
        portas = []
        for _ in range(2):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            portas.append(s.getsockname()[1])
            s.close()
        parar = threading.Event()
        # 192.0.2.1 (TEST-NET-1) não existe nesta máquina: fica pendente sem derrubar os outros
        addr = f"127.0.0.1:{portas[0]},127.0.0.2:{portas[1]},192.0.2.1:{portas[0]}"
        t = threading.Thread(target=alfred.main, args=(["--addr", addr, "--claude", "/bin/false"], parar), daemon=True)
        t.start()
        try:
            for host, porta in (("127.0.0.1", portas[0]), ("127.0.0.2", portas[1])):
                for _ in range(50):
                    try:
                        with urllib.request.urlopen(f"http://{host}:{porta}/healthz", timeout=5) as r:
                            self.assertTrue(json.loads(r.read())["ok"])
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    self.fail(f"{host}:{porta} não subiu")
        finally:
            parar.set()
            t.join(10)
        self.assertFalse(t.is_alive(), "main não encerrou com o evento parar")


class Formato(unittest.TestCase):
    def test_separar_junta_sistemas_e_marca_os_papeis(self):
        s, p = alfred.separar([{"role": "system", "content": "a"},
                               {"role": "system", "content": "b"},
                               {"role": "user", "content": "pergunta"}])
        self.assertEqual(s, "a\n\nb")
        self.assertEqual(p, "pergunta")  # uma só: sem rótulo
        s, p = alfred.separar([{"role": "user", "content": "1"},
                               {"role": "assistant", "content": "2"},
                               {"role": "user", "content": "3"}])
        self.assertEqual(p, "[user]\n1\n\n[assistant]\n2\n\n[user]\n3")

    def test_conteudo_em_blocos(self):
        self.assertEqual(alfred.texto_de([{"type": "text", "text": "a"},
                                          {"type": "text", "text": "b"}]), "a\nb")
        self.assertEqual(alfred.texto_de("x"), "x")
        self.assertEqual(alfred.texto_de(None), "")

    def test_lista_aceita_string_e_lista(self):
        self.assertEqual(alfred.lista("Bash, Write"), ["Bash", "Write"])
        self.assertEqual(alfred.lista(["Bash"]), ["Bash"])
        self.assertEqual(alfred.lista(None), [])


class Codex(Base):
    """Modelo GPT-… vai para o `codex exec`; opus, sonnet e o resto continuam
    no Claude Code exatamente como antes."""

    def oi(self, **extra):
        return self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "oi"}], **extra})

    def test_gpt_vai_para_o_codex_com_o_nome_da_tela(self):
        self.sobe()
        codigo, doc = self.oi(model="GPT-6.2-SOL")
        self.assertEqual(codigo, 200, doc)
        self.assertEqual(doc["choices"][0]["message"]["content"], "CODEX gpt-6.2-sol IMAGENS=0: oi")
        self.assertEqual(doc["model"], "gpt-6.2-sol")
        self.assertEqual(doc["usage"], {"prompt_tokens": 100, "completion_tokens": 7, "total_tokens": 107})
        self.assertEqual(doc["alfred"]["sessao"], "thr-1")
        self.assertFalse(os.path.exists(self.log), "chamou o claude para um modelo GPT")
        # "GPT-6.1 Terra" (com espaço, como aparece na tela) e o prefixo codex:
        self.oi(model="GPT-6.1 Terra")
        argv = self.chamada_codex()["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.1-terra")
        self.oi(model="codex:gpt-reserve")
        argv = self.chamada_codex()["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-reserve")

    def test_opus_continua_no_claude(self):
        self.sobe("-m", "gpt-6.2-sol")
        self.oi(model="opus")
        argv = self.chamada()["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertFalse(os.path.exists(self.log_codex), "chamou o codex para opus")

    def test_modelo_padrao_do_servidor_pode_ser_gpt(self):
        self.sobe("-m", "GPT-6.2-Astra")
        codigo, doc = self.oi(model="claude-code")
        self.assertEqual(codigo, 200, doc)
        argv = self.chamada_codex()["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.2-astra")

    def test_codex_sobe_sem_ferramenta_e_prompt_nao_passa_por_argv(self):
        self.sobe()
        segredo = "a conta de teste e admin/Senha123"
        self.post("/v1/chat/completions", {"model": "gpt-6.2-sol", "messages": [
            {"role": "system", "content": "sistema: " + segredo},
            {"role": "user", "content": "pergunta: " + segredo}]})
        c = self.chamada_codex()
        argv = c["argv"]
        self.assertEqual(argv[0], "exec")
        self.assertNotIn(segredo, " ".join(argv))
        self.assertIn(segredo, c["stdin"])
        self.assertIn(segredo, c["sistema"])
        self.assertEqual(c["modo_arq"], 0o600, oct(c["modo_arq"]))
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        for f in ("--json", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check"):
            self.assertIn(f, argv)
        desligados = [argv[i + 1] for i, a in enumerate(argv) if a == "--disable"]
        self.assertIn("shell_tool", desligados)
        self.assertIn('web_search="disabled"', argv)
        self.assertNotEqual(c["cwd"], os.getcwd())

    def test_pesquisa_na_web_por_chamada(self):
        self.sobe()
        self.oi(model="gpt-6.2-sol", tools=["WebSearch", "Bash"])
        self.assertIn('web_search="live"', self.chamada_codex()["argv"])

    def test_esforco_do_codex_aceita_ultra_e_o_claude_nao(self):
        self.sobe()
        codigo, _ = self.oi(model="gpt-6.2-sol", reasoning_effort="ultra")
        self.assertEqual(codigo, 200)
        self.assertIn('model_reasoning_effort="ultra"', self.chamada_codex()["argv"])
        codigo, doc = self.oi(model="opus", effort="ultra")
        self.assertEqual(codigo, 502)
        self.assertIn("não existe", doc["error"]["message"])
        self.assertFalse(os.path.exists(self.log))

    def test_falha_do_codex_vira_erro_legivel(self):
        self.sobe(modo="erro")
        codigo, doc = self.oi(model="gpt-9-inexistente")
        self.assertEqual(codigo, 502)
        self.assertIn("not supported", doc["error"]["message"])

    def test_run_tambem_vai_para_o_codex(self):
        self.sobe()
        codigo, doc = self.post("/run", {"prompt": "oi", "model": "gpt-6-luna"})
        self.assertEqual(codigo, 200)
        self.assertTrue(doc["ok"], doc)
        self.assertEqual(doc["result"], "CODEX gpt-6-luna IMAGENS=0: oi")

    def test_foto_vai_como_arquivo_anexado(self):
        self.sobe()
        codigo, doc = self.post("/v1/chat/completions", {"model": "gpt-6.2-sol", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "que é isso?"},
                                         {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG}}]}]})
        self.assertEqual(codigo, 200, doc)
        c = self.chamada_codex()
        self.assertEqual(len(c["imagens"]), 1)
        self.assertEqual(c["imagens"][0]["modo"], 0o600)
        self.assertIn("que é isso?", c["stdin"])
        self.assertIn("[imagem 1 anexada]", c["stdin"])
        # as imagens somem quando a chamada termina
        self.assertEqual([a for a in os.listdir(self.ponte.dir) if a.startswith("midia-")], [])

    def test_andamento_conta_eventos_do_codex(self):
        d = tempfile.mkdtemp(prefix="teste-medias-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", d]))
        self.sobe("--medias", os.path.join(d, "medias.json"))
        codigo, doc = self.oi(model="gpt-6.2-sol", progresso_rotulo="teste-codex")
        self.assertEqual(codigo, 200, doc)
        chaves = [k for k in self.ponte.medias if k.startswith("teste-codex|gpt-6.2-sol")]
        self.assertTrue(chaves, self.ponte.medias)

    def test_models_lista_os_gpt(self):
        self.sobe()
        _, doc = self.get("/v1/models")
        ids = [m["id"] for m in doc["data"]]
        self.assertIn("opus", ids)
        self.assertTrue(any(i.startswith("gpt-") for i in ids), ids)
        _, saude = self.get("/healthz")
        self.assertIn("falso", saude["codex"])

    def test_modelo_codex(self):
        self.assertEqual(alfred.modelo_codex("GPT-6.2-SOL"), "gpt-6.2-sol")
        self.assertEqual(alfred.modelo_codex(" GPT-6.1 Terra "), "gpt-6.1-terra")
        self.assertEqual(alfred.modelo_codex("openai:o3"), "o3")
        self.assertEqual(alfred.modelo_codex("codex:--json"), "")
        for m in ("opus", "sonnet", "claude-opus-5-5", "fable", "", "claude-code"):
            self.assertEqual(alfred.modelo_codex(m), "", m)


if __name__ == "__main__":
    unittest.main(verbosity=2)
