# alfred — ponte HTTP para o Claude Code da máquina

> Roda sozinho, sem Docker, e serve qualquer programa que fale HTTP no formato
> da OpenAI, como o app **Treino IA** (`~/job/exercicio`) e o cve-php.
> Escuta em **vários endereços** ao mesmo tempo
> (`--addr a,b`), espera sozinha por um endereço que ainda não existe (a rede
> Docker do Treino IA só nasce quando o app sobe), encerra limpo com SIGTERM, e
> vem com `iniciar.sh` + `alfred.conf`.

## Rodar sozinho

```bash
cd ~/job/alfred
./iniciar.sh fundo     # em segundo plano (log em alfred.log)
./iniciar.sh status    # no ar? modelo? chamadas e custo acumulado
./iniciar.sh parar
./iniciar.sh           # no terminal, Ctrl+C para sair
```

Na primeira vez o `iniciar.sh` cria o `alfred.conf` (permissão 600, fora do git)
a partir do `alfred.conf.exemplo`. É nele que se escolhe:

| variável | o quê |
|---|---|
| `ALFRED_MODELO` | modelo padrão: `opus`, `sonnet`, `haiku` ou nome completo (`claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`, `claude-fable-5-1`) vão ao Claude Code; `gpt-6.2-sol`, `GPT-6.2-Astra`… vão ao **Codex** (ver abaixo). O cliente pode pedir outro por chamada no campo `model`. |
| `ALFRED_CODEX` | caminho do CLI do Codex; vazio = `codex` do PATH ou `~/.codex/packages/app-server-daemon/current/codex`. |
| `ALFRED_ESFORCO` | `low` … `max`; vazio = o do CLI. O cliente pode pedir por chamada (`reasoning_effort`). |
| `ALFRED_ADDR` | endereços separados por vírgula. Padrão: `127.0.0.1:8765` (analista) e `172.31.87.1:8765` (rede Docker do Treino IA — fora da rede Wi‑Fi). |
| `ALFRED_TOKEN` | com valor, todo POST precisa de `Authorization: Bearer <token>`. O analista não manda token: se ele for usar esta cópia, adicione o cabeçalho no `analista.json` (`"Authorization": "Bearer {{env:ALFRED_TOKEN}}"`). |
| `ALFRED_PARALELAS` | quantos `claude` ao mesmo tempo. |

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

Esta cópia também aceita `DELETE /jobs/<id>` (com o token, se houver): cancela o
ticket e encerra o `claude` que estava trabalhando nele. `./iniciar.sh parar`
espera o processo soltar as portas antes de voltar.

Qualquer variável do ambiente vence o arquivo: `ALFRED_MODELO=opus ./iniciar.sh`.
Argumentos extras vão direto ao `alfred.py`: `./iniciar.sh -e high`.

Testes (com um `claude` falso, sem gastar IA): `python3 teste_alfred.py`.

## Documentação original

### alfred — a ponte entre o analista e o CLI do Claude Code

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

## Uso

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

## Os dois caminhos

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
`timeout_seconds`, `disallowed_tools`) também valem no `/v1`, e vencem as flags
do servidor.

## Três decisões que não são detalhe

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
sistema embutido pelo do analista. Quem quiser ferramenta pede: `--ferramentas
Read,Grep` na ponte, ou `disallowed_tools` no corpo.

**3. Quando o cliente some, o Claude morre junto.** O motor mata chamadas em voo
quando o orçamento estoura; um `claude` órfão continuaria pensando — e cobrando
— minutos depois que ninguém mais espera a resposta. Cada processo sobe em
sessão própria e é morto pelo grupo, ele e os filhos.

E uma quarta, menor, que só apareceria numa corrida: **o `stderr` de uma
resposta boa não viaja**. O analista lê `raw.stderr` como campo de erro, o CLI
escreve aviso ali de vez em quando, e repassá-lo faria o analista descartar uma
resposta que chegou inteira. Isso não apareceria como bug — apareceria como a IA
falhando sem motivo.

## Modelo e esforço

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

## Flags

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

## Agendamento: a pergunta que não cabe numa requisição

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

Tudo também por ambiente: `ALFRED_ADDR`, `ALFRED_CLAUDE`, `ALFRED_MODELO`,
`ALFRED_ESFORCO`, `ALFRED_PARALELAS`, `ALFRED_TOKEN`.

**A porta é local de propósito.** Quem pode fazer POST aqui manda no seu Claude
Code, com a sua conta pagando. Se precisar expor (`--addr 0.0.0.0:8765`), use
`--token` — e saiba que continua sendo uma chave de execução, não de leitura.

## Custo

Cada pergunta do analista é uma chamada paga. Medido nesta máquina, com
`-m sonnet`: um `analista ping` custou US$ 0,01; um `analista validar` completo,
US$ 0,07 e 62 segundos. Uma corrida com `-ia` faz dezenas dessas — o
`/healthz` acumula o total, e o log do servidor mostra o custo linha a linha:

```
[alfred] /v1/chat/completions 61.8s  US$ 0.0722  in=5216 out=1103 turnos=1  ok
```

## Testes

```bash
python3 teste_alfred.py
```

Os testes não gastam uma chamada de IA: o `claude` e o `codex` deles são scripts falsos
que registra o argv, o stdin, o cwd e o modo do arquivo de sistema que recebeu.
É o que permite afirmar que o prompt não passa por argv e que o `stderr` de uma
resposta boa não vaza — as duas coisas que, sem teste, só se descobririam no
meio de uma corrida.
