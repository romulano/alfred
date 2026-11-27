<p align="center">
  <img src="docs/logo.svg" alt="alfred" width="160">
</p>

<h1 align="center">alfred</h1>

<p align="center">
  <b>O mordomo das suas IAs.</b><br>
  Uma API local, no formato da OpenAI, que serve o Claude Code, o Codex e o Ollama da sua máquina —
  e lembra de tudo o que fez, num histórico que é só seu.
</p>

---

## O que é

Os CLIs de IA (`claude`, `codex`) são programas de terminal: leem o prompt no
stdin e escrevem a resposta no stdout. Muita coisa útil, porém, só sabe falar
HTTP. O **alfred** é a ponte: um servidor local que recebe um
`POST /v1/chat/completions` (o formato da OpenAI), entrega o pedido à IA certa e
devolve a resposta no formato que o cliente espera.

```
seu programa ──POST /v1/chat/completions──▶ alfred ──┬─ claude -p ─────▶ Claude Code
             ◀── choices.0.message.content ──        ├─ codex exec ────▶ Codex (GPT)
                                                     └─ HTTP /api/chat ▶ Ollama (local)
                                                            │
                                                            ▼
                                             ~/.local/share/alfred/alfred.db
                                               (histórico e conversas, 0600)
```

- **Usa o login que já está na máquina**: sem chave de API, sem conta nova.
- **Três provedores, uma API**: o campo `model` decide quem responde
  (`sonnet`, `GPT-6.2-Sol`, `ollama:qwen3-coder:30b`…).
- **Memória local**: toda pergunta e resposta fica num SQLite do dono, com
  conversas que continuam de onde pararam. Nada sai da máquina; exportar é um comando.
- **Seguro por padrão**: a IA responde **sem ferramenta nenhuma** (não lê disco,
  não roda comando), o prompt nunca passa por argv, e nenhum pedido HTTP consegue
  afrouxar isso.
- **Fotos e vídeos**, **tickets** para trabalhos longos, **cancelamento** quando o
  cliente desiste, e custo/tokens de cada chamada.
- **Python 3 puro**, só biblioteca padrão: nada para compilar, nada para `pip install`.

## Instalação, passo a passo

### 1. Pré-requisitos

| o quê | para quê | obrigatório? |
|---|---|---|
| Linux ou macOS com **Python 3.10+** | rodar o alfred | sim |
| **Claude Code** (`claude`) com login feito | modelos `opus`, `sonnet`, `haiku`, `fable` | ao menos um provedor |
| **Codex** (`codex`) com login feito | modelos `GPT-…` | opcional |
| **Ollama** rodando, com algum modelo baixado | modelos `ollama:<nome>`, 100% locais | opcional |
| **ffmpeg** | vídeo e fotos grandes ou em HEIC | opcional |
| `curl` | o `./iniciar.sh status` e os exemplos | recomendado |

Confira o que já existe:

```bash
python3 --version
claude --version    # Claude Code
codex --version     # Codex (opcional)
ollama list         # Ollama (opcional)
ffmpeg -version     # ffmpeg (opcional)
```

### 2. Prepare os provedores que for usar

```bash
# Claude Code: instale e faça login uma vez (abre o navegador)
claude          # siga o login e saia com /exit

# Codex (opcional)
codex login

# Ollama (opcional): suba o serviço e baixe um modelo
ollama serve &              # se ele ainda não roda como serviço
ollama pull qwen3-coder:30b # ou qualquer outro do `ollama list`
```

O alfred usa esses logins como estão — ele não guarda nem vê senha ou token de
nenhum provedor.

### 3. Baixe o alfred

```bash
git clone https://github.com/romulano/alfred.git
cd alfred
```

### 4. Configure

```bash
./iniciar.sh status   # na primeira vez cria o alfred.conf (0600, fora do git) e diz que está fora do ar
$EDITOR alfred.conf
```

O mínimo é escolher o modelo padrão e onde escutar. Para uso só nesta máquina:

```ini
ALFRED_MODELO=sonnet
ALFRED_ADDR=127.0.0.1:8765
```

Todas as opções estão em [Configuração](#configuração).

### 5. Suba e teste

```bash
./iniciar.sh fundo    # em segundo plano, log em alfred.log
./iniciar.sh status   # no ar? quais provedores? quanto já gastou?

curl -s localhost:8765/v1/chat/completions \
  -d '{"messages":[{"role":"user","content":"Diga olá em uma palavra."}]}'
```

A resposta vem em `choices[0].message.content`. Para parar: `./iniciar.sh parar`.
Para rodar no terminal, com o log na tela: `./iniciar.sh` (Ctrl+C sai).

### 6. Aponte seus programas para ele

Qualquer cliente compatível com a OpenAI funciona trocando a URL base para
`http://127.0.0.1:8765/v1`. A chave de API pode ser qualquer texto — ou o
`ALFRED_TOKEN`, se você configurar um.

```python
from openai import OpenAI
ia = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="alfred")
r = ia.chat.completions.create(model="sonnet", messages=[{"role": "user", "content": "oi"}])
print(r.choices[0].message.content)
```

## Configuração

Tudo fica no `alfred.conf` (criado a partir do [`alfred.conf.exemplo`](alfred.conf.exemplo)).
Qualquer variável do ambiente vence o arquivo (`ALFRED_MODELO=opus ./iniciar.sh`), e
argumentos extras vão direto ao `alfred.py` (`./iniciar.sh -e high`).

| variável | padrão | o quê |
|---|---|---|
| `ALFRED_MODELO` | padrão do CLI | modelo de quem não pede um: `opus`, `sonnet`, `haiku`, `fable`, nome completo (`claude-opus-5-5`…), um GPT (`GPT-6.2-Sol`) ou `ollama:<nome>`. O cliente pode pedir outro por chamada no campo `model`. |
| `ALFRED_ESFORCO` | padrão do CLI | quanto o modelo pensa: `low`, `medium`, `high`, `xhigh`, `max` (e `ultra`, só no Codex). O cliente pode pedir por chamada (`reasoning_effort`). O Ollama ignora. |
| `ALFRED_ADDR` | `127.0.0.1:8765` | onde escutar; vários endereços separados por vírgula. Um endereço que ainda não existe (a rede de um Docker que não subiu) é tentado de novo a cada 15 s. |
| `ALFRED_TOKEN` | vazio | com valor, todo POST e a leitura do histórico pedem `Authorization: Bearer <token>`. **Configure sempre que escutar fora do 127.0.0.1.** |
| `ALFRED_PARALELAS` | `2` | quantas chamadas rodam ao mesmo tempo; as outras esperam na fila. |
| `ALFRED_CLAUDE` | `claude` do PATH | caminho do CLI do Claude Code. |
| `ALFRED_CODEX` | `codex` do PATH | caminho do CLI do Codex. |
| `ALFRED_OLLAMA` | `http://127.0.0.1:11434` | endereço da API do Ollama. |
| `ALFRED_DB` | `~/.local/share/alfred/alfred.db` | o SQLite do histórico. |
| `ALFRED_HISTORICO` | `1` | `0` desliga o histórico: nada é gravado e `conversa_id` é recusado. |
| `ALFRED_FFMPEG` | `ffmpeg` do PATH | caminho do ffmpeg. |
| `ALFRED_QUADROS_VIDEO` | `8` | quantos quadros tirar de cada vídeo. |
| `ALFRED_MAX_CORPO_MB` | `64` | teto de um pedido, contando fotos e vídeos em base64. |
| `ALFRED_JOB_TETO` | `1800` | quanto um trabalho que virou ticket pode rodar, em segundos. |

Os comandos do dia a dia:

```bash
./iniciar.sh                          # no terminal (Ctrl+C sai)
./iniciar.sh fundo                    # em segundo plano, log em alfred.log
./iniciar.sh status                   # estado, provedores, chamadas e custo
./iniciar.sh parar                    # encerra (espera soltar as portas)
./iniciar.sh exportar historico.json  # o histórico inteiro em JSON (0600)
```

## Escolhendo a IA

O provedor sai do campo `model`, por chamada:

| `model` | vai para |
|---|---|
| `opus`, `sonnet`, `haiku`, `fable`, `claude-…`, `claude-code` ou vazio | Claude Code (`claude -p`) |
| `GPT-6.2-Sol`, `gpt-6.1-terra`, `GPT-6 Luna`… (qualquer caixa), `codex:<slug>`, `openai:<slug>` | Codex (`codex exec`) |
| `ollama:<nome>`, ex. `ollama:qwen3-coder:30b` | Ollama, direto pela API HTTP |

`GET /v1/models` lista tudo o que esta máquina tem disponível.

### Ollama

`model: "ollama:<nome>"` (o nome como o `ollama list` mostra: `ollama:qwen3-coder:30b`,
`ollama:dolphin-mistral`) vai direto ao `/api/chat` do Ollama, por HTTP — sem
processo para abrir. Prompt de sistema, fotos (campo `images`), prazo, ticket e
cancelamento valem igual; fechar a conexão faz o Ollama parar de gerar. Ferramentas
e esforço não existem lá e são ignorados. Custo sai 0; os tokens vêm no `usage`.
Os modelos instalados aparecem no `GET /v1/models` com o prefixo `ollama:`.

### Codex (modelos GPT)

O provedor sai do nome do modelo, por chamada:

| `model` | vai para |
|---|---|
| `opus`, `sonnet`, `haiku`, `fable`, `claude-…`, `claude-code`, vazio | `claude -p` (como sempre) |
| `GPT-6.2-Sol`, `gpt-6.2-astra`, `GPT-6.1 Terra`, `gpt-6-luna`… (qualquer caixa; espaço vira `-`) | `codex exec` |
| `codex:<slug>` ou `openai:<slug>` | `codex exec`, para um modelo novo que não comece com `gpt-` |

Os modelos que o Codex desta máquina conhece aparecem no `GET /v1/models` (lidos
do `~/.codex/models_cache.json`): GPT-6.2-Sol, GPT-6.2-Astra, GPT-6.1-Terra,
GPT-6.1-Sol, GPT-6-Astra, GPT-6-Sol, GPT-6-Luna, GPT-5.6-Sol…

```bash
curl -s localhost:8765/v1/chat/completions -d '{"model":"GPT-6.2-SOL","reasoning_effort":"high",
  "messages":[{"role":"user","content":"oi"}]}'
```

Mesmas garantias do caminho do Claude: prompt pelo stdin, prompt de sistema num
arquivo `0600` (`model_instructions_file`, que substitui as instruções de agente
do Codex — a chamada cai de ~17 mil para ~6 mil tokens de entrada), sandbox
`read-only`, shell/apps/plugins/navegador/subagentes desligados (`--disable`),
sessão efêmera, sem a `config.toml` pessoal (o login do `~/.codex` continua
valendo) e cwd temporário. `tools: ["WebSearch"]` liga a pesquisa na web do
Codex; nada mais é liberado. Esforço: `low` … `max` e também `ultra` (só no
Codex). Foto e quadro de vídeo vão como arquivo anexado (`-i`), apagado no fim.
O Codex não informa custo: `custo_usd` sai 0, mas os tokens vêm no `usage`.
Ticket (`agendar`), cancelamento e `progresso_rotulo` funcionam igual.

### Fotos e vídeos

Mande no `content` da mensagem, como numa API da OpenAI ou da Anthropic:

```json
{"messages": [{"role": "user", "content": [
  {"type": "text", "text": "Que equipamento é este?"},
  {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/…"}},
  {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAAA…"}}
]}], "video_quadros": 6}
```

Também vale `{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "…"}}`
(e `"video"` do mesmo jeito). No `/run`, use `"imagens": [...]` e `"videos": [...]`
com data URLs. Só data URL base64: endereço de internet ou caminho de arquivo é
recusado (400), de propósito.

Como funciona: a mensagem vai ao `claude -p` pelo stdin em `stream-json` (o formato
do SDK), **sem ligar ferramenta nenhuma** — o modelo vê a imagem sem poder ler o
disco. O vídeo vira `ALFRED_QUADROS_VIDEO` quadros espaçados (padrão 8), em ordem,
cada um com o instante; foto acima de ~3,5 MB ou em formato que o Claude não lê
(HEIC, BMP…) é convertida em JPEG. As duas coisas usam o `ffmpeg`. O `/healthz`
diz o que a ponte aceita em `midia`.

## Histórico e conversas

Toda pergunta fica num SQLite **local, só do dono** (arquivo `0600`, pasta `0700`):
rota, quem pediu, provedor, modelo, prompt de sistema, o que entrou (foto e vídeo
viram só a marca `{"type": …, "omitido": true}`), resposta ou erro, custo, tokens,
duração. Nada sai da máquina. `"historico": false` no pedido não grava aquela chamada.

**Conversa:** mande `"conversa_id"` (um id seu, ou `"nova"` para o alfred inventar
um — ele volta em `alfred.conversa_id`) e **só a mensagem nova**. O alfred põe na
frente o que já foi dito, e grava pergunta e resposta quando a chamada dá certo
(falha não entra na conversa). Vale no `/run` também.

```bash
curl -s localhost:8765/v1/chat/completions -d '{"conversa_id":"nova","messages":[{"role":"user","content":"meu número é 47"}]}'
curl -s localhost:8765/v1/chat/completions -d '{"conversa_id":"<id>","messages":[{"role":"user","content":"qual é o meu número?"}]}'
```

| rota | o quê |
|---|---|
| `GET /v1/conversas` | as conversas, da mais recente (`?limite=`) |
| `GET /v1/conversas/<id>` | uma conversa com as mensagens |
| `DELETE /v1/conversas/<id>` | apaga a conversa e as chamadas dela |
| `GET /v1/historico` | as últimas chamadas (`?limite=`, `?conversa=<id>`) |
| `GET /v1/exportar` | tudo, num JSON só |

Essas rotas só abrem **a partir desta máquina** (127.0.0.1) quando não há token;
com `ALFRED_TOKEN`, pedem o token. Para levar o histórico embora:
`./iniciar.sh exportar historico.json` (o arquivo sai `0600`).

## Segurança: o que um pedido não consegue

Um pedido HTTP não liga ferramenta nem afrouxa permissão. `permission_mode` aceita
só `plan` e `default` (os outros, como `bypassPermissions`, só o operador liga,
por `--permissao`); `disallowed_tools` só **tira** ferramentas da lista liberada;
`tools` só liga `WebSearch` e `WebFetch`.

`DELETE /jobs/<id>` (com o token, se houver) cancela um ticket e encerra a IA que
estava trabalhando nele.

**A porta é uma chave de execução.** Quem consegue fazer POST no alfred usa as
suas IAs com a sua conta pagando. Fique no `127.0.0.1`; se precisar escutar em
outro endereço (uma rede Docker, a rede local), configure o `ALFRED_TOKEN`.

## Testes

```bash
python3 teste_alfred.py
```

Nenhum teste gasta IA de verdade: o `claude`, o `codex` e o Ollama dos testes são
falsos e registram o que receberam (argv, stdin, cwd, permissão dos arquivos), e o
histórico vai para uma pasta temporária.

## Referência detalhada

O alfred nasceu como a ponte entre o `analista` e o CLI do Claude Code; as
seções abaixo explicam as decisões de projeto e os detalhes de cada recurso.

### A ponte do analista

O `analista` sabe falar com **qualquer IA que fale HTTP+JSON**: o `analista.json`
descreve o corpo que sai e o caminho onde ler o texto que volta. O Claude Code
**não fala HTTP** — ele é um processo de terminal que lê o prompt no stdin e
escreve a resposta no stdout. Este programa é o que falta no meio.

```
ctfmotor -ia ──▶ analista ──POST /v1/chat/completions──▶ alfred ──claude -p──▶ Claude Code
                          ◀── choices.0.message.content ──       ◀── {"result": …} ──
```

Sem ele, `ctfmotor -ia` sobe, chama o analista, e o analista morre em
`connection refused` — o padrão de fábrica dele aponta para
`http://127.0.0.1:8765/v1/chat/completions`, que é exatamente o que o alfred
serve. Por isso **não há nada para configurar**: suba a ponte e o modo IA passa
a funcionar.

### Uso direto

```bash
./alfred.py                              # 127.0.0.1:8765, sem ferramenta, modo plan
./alfred.py -m sonnet                    # modelo mais barato para as tarefas de julgamento
./alfred.py -m claude-fable-5-1 -e max   # Fable 5.1 pensando o máximo
./alfred.py -n 4                         # 4 chamadas ao claude em paralelo (padrão: 2)
curl -s localhost:8765/healthz           # está no ar? quanto já gastou?
```

Depois, numa outra janela:

```bash
analista ping                                   # a IA responde?
analista validar -achado achado.json            # julgar um achado
ctfmotor alvo.exemplo -ia -ia-executar          # a corrida inteira com IA
```

Python 3 puro, biblioteca padrão, sem build — ele roda onde o `claude` roda, e é
a única peça do `programas/` que não é Go justamente por isso: não vai para
dentro de nenhum alvo, fica na sua máquina, ao lado do CLI.

### Os dois caminhos

O analista traz dois backends de fábrica, e a ponte serve os dois:

| rota | backend do analista | resposta |
|---|---|---|
| `POST /v1/chat/completions` | `alfred` (padrão) | compatível com OpenAI — `choices.0.message.content` |
| `POST /run` | `alfred-run` (legado) | `{"ok":true,"result":"…","raw":{…}}` |
| `GET /jobs/<id>` | — | consulta um ticket (ver *Agendamento*) |
| `GET /healthz` | — | estado, versão do CLI, chamadas, falhas, custo acumulado, tickets |
| `GET /v1/models` | — | lista, para clientes que perguntam antes |

O `/v1` é o que não muda; o `/run` existe porque o contrato dele **já mudou uma
vez** (a saída saía em `stdout`, hoje sai em `result`), e o efeito de errar esse
campo não é um erro na largada — é o analista dizendo *"campo não existe"* no
meio de uma corrida, com a chamada já paga.

Os campos que o `/run` aceita (`permission_mode`, `max_turns`,
`timeout_seconds`, `disallowed_tools`) também valem no `/v1`. `max_turns` e
`timeout_seconds` vencem as flags do servidor (até o teto); `permission_mode` só
aceita `plan` e `default`, e `disallowed_tools` só tira ferramentas — nenhum
pedido liga o que o operador não ligou.

### Três decisões que não são detalhe

**1. O prompt vai pelo stdin, nunca por argv.** Ele carrega a `-ia-instrucao` da
corrida, e essa instrução costuma trazer a senha da conta de teste — argv é
público na máquina inteira (`ps`). É a mesma razão pela qual o motor já passa a
instrução ao analista por arquivo. O prompt de sistema vai por arquivo `0600`
(`--system-prompt-file`), apagado assim que a chamada termina.

**2. O Claude sobe sem ferramenta e sem contexto de projeto.** O analista não
pede execução: ele pede julgamento sobre um JSON que já vem no prompt. Um Claude
Code com Bash ligado, lendo o `CLAUDE.md` do diretório e as configurações do
usuário, faria trabalho que ninguém pediu e cobraria por ele — a chamada trivial
medida aqui caiu de **US$ 0,108 para US$ 0,007** só por trocar o prompt de
sistema embutido pelo do analista. Quem quiser ferramenta liga na ponte
(`--ferramentas Read,Grep`); um pedido só consegue ligar `WebSearch` e
`WebFetch` (campo `tools`).

**3. Quando o cliente some, o Claude morre junto.** O motor mata chamadas em voo
quando o orçamento estoura; um `claude` órfão continuaria pensando — e cobrando
— minutos depois que ninguém mais espera a resposta. Cada processo sobe em
sessão própria e é morto pelo grupo, ele e os filhos.

E uma quarta, menor, que só apareceria numa corrida: **o `stderr` de uma
resposta boa não viaja**. O analista lê `raw.stderr` como campo de erro, o CLI
escreve aviso ali de vez em quando, e repassá-lo faria o analista descartar uma
resposta que chegou inteira. Isso não apareceria como bug — apareceria como a IA
falhando sem motivo.

### Modelo e esforço

`--modelo` aceita alias (`opus`, `sonnet`, `haiku`, `fable`) ou nome completo, e
alias entrega **a última versão daquela família**. Para fixar uma versão — a
diferença entre "o Fable de hoje" e "este Fable" —, use o nome inteiro:

```bash
./alfred.py -m claude-fable-5-1 -e max
```

`--esforco` é quanto o modelo pensa antes de responder: `low`, `medium`, `high`,
`xhigh`, `max`. Ele é o **piso** — uma chamada pode pedir outro no corpo
(`effort`, ou `reasoning_effort` para um cliente que fale OpenAI), porque julgar
um achado merece esforço que somar duas listas não merece. Esforço inventado
morre com 400 antes de abrir processo, em vez de custar uma chamada para falhar
no fim.

Direto no CLI, sem a ponte, é a mesma dupla de flags:

```bash
echo 'sua pergunta' | claude -p --model claude-fable-5-1 --effort max
```

Medido aqui, com `-e max` em Fable 5.1: um `analista ping` custou US$ 0,059 e
6,3s (o mesmo ping em `sonnet` sem esforço declarado: US$ 0,010 e 4,0s). Numa
corrida com dezenas de chamadas, essa diferença é a conta inteira — vale onde a
resposta decide o próximo passo, não em tudo.

### Flags

```
--addr 127.0.0.1:8765   endereço de escuta
--claude claude         caminho do CLI
-m, --modelo            opus, sonnet, haiku, claude-fable-5-1…; vazio = o padrão do CLI
-e, --esforco           low|medium|high|xhigh|max; vazio = o padrão do CLI
-n, --paralelas 2       chamadas simultâneas ao claude
-t, --timeout 240       prazo padrão de uma chamada, em segundos (fila inclusive)
--timeout-max 900       teto do prazo que o cliente pode pedir
--turnos 3              --max-turns do claude
--permissao plan        --permission-mode do claude
--ferramentas ''        liberadas (ex.: Read,Grep); vazio = nenhuma
--dir ''                cwd do claude; vazio = um temporário só dele
--token ''              exige Authorization: Bearer <token>
--ollama URL            API do Ollama (padrão: http://127.0.0.1:11434)
--db ARQUIVO            o SQLite do histórico (padrão: ~/.local/share/alfred/alfred.db)
--sem-historico         não grava nada no histórico
--exportar ARQUIVO      escreve o histórico em JSON (- = tela) e sai
-v, --verboso           loga o argv de cada chamada
```

O prazo vale da **chegada** da chamada até a resposta, fila incluída: com `-n 2`,
a terceira pergunta simultânea não ganha um prazo novo ao pegar vaga, ela herda o
que sobrou. Isso é o que mantém a promessa que o analista precisa — ele desiste em
240s (o teto por pergunta do `servir`) e o alfred tem de responder antes disso. Se
a fila comeu quase tudo, a chamada é recusada na hora (`fila cheia`) em vez de
subir um Claude Code que só terminaria depois de o cliente ir embora. E se o
cliente fecha a conexão enquanto a chamada espera vaga, ela é largada ali: nenhum
processo sobe, nenhum token é pago.

### Agendamento: a pergunta que não cabe numa requisição

A forja pede ao modelo o código **inteiro** de uma ferramenta nova, e o ctfmotor
reserva 30 minutos para isso (`-forja-teto`). Uma requisição HTTP razoável dura
4. O excedente virava `FALHOU: a chamada passou de 240s e foi encerrada` — o
trabalho jogado fora justamente no ponto mais caro da corrida.

Mande `"agendar": true` no corpo. Aí passam a existir **dois relógios**: o prazo
de RESPOSTA (`-t`, quanto a requisição HTTP espera) e o prazo de TRABALHO
(`--job-teto`, ou `job_seconds` no corpo). Estourado o primeiro, o alfred
devolve `202` com um ticket e o `claude` **segue trabalhando**:

```bash
curl -s -d '{"agendar":true,"messages":[{"role":"user","content":"…"}]}' \
     localhost:8765/v1/chat/completions
# {"id":"a1b2…","object":"alfred.job","status":"executando","consultar_em":"/jobs/a1b2…"}

curl -s localhost:8765/jobs/a1b2…     # 202 enquanto executa
# {"id":"a1b2…","status":"executando","decorrido_ms":48120}
curl -s localhost:8765/jobs/a1b2…     # 200 quando termina, na forma da rota de origem
# {"choices":[{"message":{"content":"…"}}], …}
```

Detalhes que importam no consumo:

- **É opt-in.** Sem `agendar`, nada muda: o prazo continua encerrando a chamada.
  Um cliente que não conhece ticket receberia um `202` sem `choices` e quebraria
  em *"campo não existe"* no meio de uma corrida — o modo de falha mais caro
  desta ponte.
- O ticket **lembra de qual rota nasceu**: quem chamou `/run` lê `result`, quem
  chamou `/v1/chat/completions` lê `choices.0.message.content`. O corpo é o
  mesmo que a chamada síncrona teria devolvido.
- Ler o ticket **não o consome**: ele fica `--job-ttl` (900s) depois de pronto.
- Os tickets vivem **só na memória**. Alfred que reinicia perde os tickets, e
  isso é honesto: o `claude` que os produzia morreu junto. `GET` de um ticket que
  sumiu devolve 404 dizendo isso.
- Quem espera do outro lado é o analista, pelo bloco `agendamento` do backend —
  ver o README dele. O prazo total é o do ctx de quem chamou (30min na forja),
  nunca um teto inventado no meio do caminho.

Tudo também por ambiente — ver [Configuração](#configuração).

**A porta é local de propósito.** Quem pode fazer POST aqui manda no seu Claude
Code, com a sua conta pagando. Se precisar expor (`--addr 0.0.0.0:8765`), use
`--token` — e saiba que continua sendo uma chave de execução, não de leitura.

### Custo

Cada pergunta do analista é uma chamada paga. Medido nesta máquina, com
`-m sonnet`: um `analista ping` custou US$ 0,01; um `analista validar` completo,
US$ 0,07 e 62 segundos. Uma corrida com `-ia` faz dezenas dessas — o
`/healthz` acumula o total, e o log do servidor mostra o custo linha a linha:

```
[alfred] /v1/chat/completions 61.8s  US$ 0.0722  in=5216 out=1103 turnos=1  ok
```
