# backupov2 — Assistente de Backup de Discos

Copia um lote de CDs/DVDs para uma sequência de pastas, um disco por pasta.
Você insere os discos; o programa cria as pastas, copia, verifica, ejeta e passa
para o próximo — deixando você livre para pular um disco, mandá-lo para outra
pasta, editar a lista no meio da execução ou fechar tudo e retomar depois.

## Como usar

```powershell
# abrir a interface
python run.pyw
```

Ou dê um duplo clique em `run.pyw`.

**Com fotos do protocolo (recomendado):**

1. Escolha a pasta de **Destino** — só isso, o resto vem da folha.
2. **Importar de fotos...** → escolha as páginas → **Ler com IA**.
3. Confira a pasta principal e a lista, corrija o que precisar, **Adicionar**.
4. Insira o primeiro disco. Após 10 segundos a cópia começa sozinha.
5. Durante a contagem: **Pular disco**, **Enviar para...** ou **Iniciar agora**.
6. Ao terminar, a bandeja abre. Insira o próximo.

**Digitando à mão:**

1. Escolha o **Destino** e digite o nome da **Pasta** do lote.
2. **Novo trabalho** → cole os nomes das pastas, um por linha, na ordem dos discos.
3. Daí em diante é igual.

### Importar nomes de fotos (IA)

Fotografe ou digitalize o **PROTOCOLO DE ENTREGA DE MATERIAL DE MÍDIA** e use
**Importar de fotos...**. As páginas são lidas por um modelo de visão do Gemini,
que transcreve os códigos dos CDs na ordem em que aparecem.

A folha descreve o lote inteiro, então ela também dá o nome da **pasta
principal**: a linha da caixa (`CAIXA 02 (EG 1841 ao EG 1852)`) é usada como
sugestão, e você só edita se quiser. Por isso o botão funciona **sem nenhum
trabalho aberto** — basta ter escolhido o Destino. Não é preciso digitar nada
antes.

A estrutura criada tem os três níveis da folha — caixa, EG e disco:

```
CAIXA 02 (EG 1841 ao EG 1852)/
  EG 1841 (BENGUELA - ABG)/
      ABG-DI-8963-GI-19 R0/
      ABG-DI-8963-GI-18 R0/
      ...
  EG 1852 (LUANDA - ALU-3)/
      ALU-3-DI-1852-SD-03/
      ...
  EG 1885 (CAPANDA - PAC)/
      PAC-DI-1885-GM-01/
```

Desmarque **Criar subpasta por EG** se preferir tudo achatado direto na pasta
principal.

A primeira vez, clique em **Chave de API...** e cole a chave do Gemini — ela
fica só neste computador (`%LOCALAPPDATA%/backupov2/config.json`) e **nunca**
vai para a pasta de backup. Alternativamente, defina `GEMINI_API_KEY`.

Nada entra no trabalho sem revisão. A tabela mostra, lado a lado, o nome
proposto e **como está no papel**, destacando o que precisa de atenção:

| Situação | Por quê |
|---|---|
| `caractere invalido no Windows: /` | `... R1 (1/3)` vira `... R1 (1-3)` — a barra é proibida em nomes de pasta |
| `nome repetido na folha (2o)` | o mesmo código aparece duas vezes **no mesmo EG**; são **dois CDs**, então o segundo vira `... (2)` em vez de ser descartado |
| `leitura incerta` | o modelo sinalizou dúvida na leitura daquela linha |

Clique duas vezes em qualquer nome para corrigi-lo. O texto original de cada
linha fica guardado no trabalho, então sempre dá para conferir depois o que o
papel dizia.

Pela linha de comando:

```powershell
python -m backupov2.tools.extract foto1.jpg foto2.jpg
python -m backupov2.tools.extract --names-only *.jpg > nomes.txt
```

### Limpar arquivos de controle

Ao terminar um lote, **Limpar arquivos de controle** apaga da pasta os arquivos
que o programa escreve (`_backupov2-job.json`, o `.bak`, um `.tmp` perdido e o
registro) — útil para entregar a pasta sem os resíduos da ferramenta.

Só esses nomes fixos são apagados: as pastas dos discos e tudo que foi copiado
não são tocados, e nenhuma varredura recursiva acontece. A cópia local em
`%LOCALAPPDATA%` é mantida, então o trabalho continua abrível em **Recentes**.
Se ainda houver pastas pendentes, o aviso diz que o arquivo volta a ser criado
na próxima gravação.

### A tela

No topo, uma faixa mostra **qual lote está aberto agora**: o nome da pasta
principal em destaque, o caminho completo logo abaixo, o quanto já foi copiado
e um botão **Abrir pasta**. Sem trabalho aberto ela fica cinza e diz o que
fazer.

Os campos *Destino* e *Pasta do lote* servem só para **criar** um trabalho —
assim que um está aberto eles ficam desabilitados, porque editá-los ali não
mudaria nada. Para trocar de lote use **Abrir...** ou **Recentes**.

Na lista, o EG aparece só na linha em que muda e cada EG recebe um fundo
alternado, então dá para ver onde um grupo termina e o outro começa. A linha
do próximo disco fica destacada em azul.

As colunas podem ser arrastadas para a largura que você quiser e **ficam
assim** — nomes longos de CD cabem inteiros. Quando a soma passa da largura da
janela, a barra de rolagem horizontal embaixo da tabela leva até o resto.

Atalhos: `Espaço` iniciar · `Esc` cancelar · `Ctrl+N` novo · `Ctrl+O` abrir ·
`Alt+↑`/`Alt+↓` reordenar · duplo clique no nome da pasta para renomear.

## Requisitos

- Windows 10/11, Python 3.11+ (com Tkinter)
- Para CDs de áudio: `ffmpeg` com o dispositivo de entrada `libcdio`
  (opcional — sem ele, discos de áudio são marcados como pulados e os discos
  de dados continuam normalmente)

## Estado atual

Funcional para **discos de dados**: detecção, contagem regressiva, cópia
verificada, pular, pausar e cancelar durante a transferência, redirecionar,
acumular vários discos numa pasta, marcar um disco como defeituoso (durante
a cópia ou sem nem tentar), ejeção automática, retomada e registro.

Funcional também a **importação de nomes por foto** (Gemini). O modelo de
dados acomodou isso sem nenhuma mudança de esquema: os nomes lidos entram pelo
mesmo `EntryDraft` de uma digitação manual.

Pendente: **CD de áudio** (`audio.py`, ripagem para MP3 320 kbps).

## Enviar um disco para outra pasta

**Enviar para...** abre um seletor com duas partes: **Usadas recentemente**
(as últimas pastas concluídas, mais recente primeiro, em destaque) e **Todas
as pastas**, na ordem do trabalho. Útil quando um segundo disco físico precisa
entrar na mesma pasta que você acabou de terminar.

Assim que você clica em **Enviar para...**, a contagem regressiva é pausada —
nada começa sozinho enquanto o seletor está aberto, mesmo que você demore para
escolher. Ao confirmar, a cópia começa imediatamente, sem nova contagem: você
já fez a escolha deliberada, não há mais nada a confirmar. Cancelar volta
exatamente para onde estava.

**Nada é apagado, nunca.** Enviar um disco para uma pasta que já tem conteúdo
sempre soma — nunca substitui. Um arquivo com o mesmo nome mas conteúdo
diferente é salvo como `arquivo (2).ext`, e o original permanece intocado; um
arquivo idêntico (mesmo tamanho e data) é reconhecido como já copiado e não é
duplicado. Quando isso acontece a pasta fica marcada ⚠ e a lista exata está no
registro e no resultado do trabalho.

## Durante a cópia: pular, pausar ou cancelar

Uma cópia em andamento pode ser interrompida de três maneiras, e a diferença
entre elas é só o que acontece com o disco:

| Botão | O que faz | O disco |
|---|---|---|
| **Pausar** | congela a transferência onde está; **Retomar** continua do mesmo ponto, sem reler nada | fica na bandeja |
| **Pular** | para a transferência e **ejeta** | volta para a sua mão |
| **Cancelar** | para a transferência | fica na bandeja, para você tentar de novo |

**Nada do que já foi gravado é perdido em nenhum dos três casos.** A pasta
volta para *pendente* com a marca de parcial, os arquivos já copiados
permanecem, e reinserir o disco continua de onde parou — a cópia reconhece o
que já está lá e não regrava. Pular também **não consome a pasta**: ela segue
sendo a próxima da fila.

A pausa vale entre arquivos **e dentro de um arquivo grande** — um ISO de 4 GB
para em segundos, não ao terminar. O tempo parado não entra no cálculo de
velocidade nem de tempo restante, então uma pausa de dez minutos não faz o
programa relatar que a rede ficou lenta.

O botão **Ejetar** continua desabilitado durante a cópia: a unidade está em
uso, e **Pular** é o caminho seguro para abrir a bandeja — ele espera a
transferência soltar o drive antes de ejetar.

## Disco riscado ou que travou

Um arquivo ilegível **não** para o disco: são 3 tentativas, depois ele é
pulado e registrado, e a cópia segue. Isso é o certo para um risco isolado — e
é por isso que não aparece botão nenhum nesse caso.

Quando o problema é o **disco**, não o arquivo, o programa oferece uma saída.
Aparece um aviso vermelho no painel do disco e, junto dele, o botão **Disco
defeituoso**, em duas situações:

- **3 ou mais arquivos** não puderam ser lidos; ou
- a unidade **parou de responder** por 45 segundos — sem erro, sem progresso,
  sem nada.

Nada acontece sozinho: um disco lento que ainda vai terminar é melhor que um
disco abandonado por palpite. A decisão é sua.

Ao confirmar **Disco defeituoso**:

1. a transferência para;
2. a pasta é marcada como **falhou** (`disc_corrupted`) com ⚠ — nunca dá para
   confundir depois com uma pasta concluída;
3. **os arquivos que deram certo continuam lá**;
4. um arquivo **`_DISCO_COM_DEFEITO.txt`** é criado dentro da pasta do disco,
   dizendo que o conteúdo está incompleto e listando cada arquivo que não pôde
   ser lido, com o erro exato;
5. o disco é ejetado e o próximo disco vai para a **próxima pasta pendente**.

```
_DISCO_COM_DEFEITO.txt
----------------------
DISCO COM DEFEITO

Este disco foi marcado como defeituoso durante a copia.
O CONTEUDO DESTA PASTA ESTA INCOMPLETO.

Disco.......................: PROJ2019_07 (D:)
Numero de serie.............: 0x499602D2
Motivo......................: 4 arquivo(s) nao puderam ser lidos
Arquivos copiados com sucesso: 812
Arquivos que falharam........: 4

ARQUIVOS QUE NAO PUDERAM SER LIDOS
VIDEO_TS\VTS_01_3.VOB
    [WinError 23] Data error (cyclic redundancy check)
...
```

Esse arquivo fica **dentro da pasta do disco**, não na pasta do lote, então
**não** é removido por *Limpar arquivos de controle* — ele descreve os dados,
não a ferramenta. Apague-o quando o disco for recuperado ou substituído.

### Quando você já sabe que o disco está ruim

Nem sempre dá para começar a cópia: o disco chega trincado, ou o leitor
simplesmente não o reconhece. Nesse caso não há nada para interromper — use o
botão direito na pasta, na lista, e **Marcar como pulada - disco
defeituoso...**.

Abre uma janela para confirmar e, se quiser, escrever uma **observação**
("disco trincado ao meio", "não é reconhecido pelo leitor"). Vale a pena: um
protocolo de entrega precisa ser respondido dizendo *qual* disco não pôde ser
lido e por quê, e ninguém lembra disso duas semanas depois.

Ao confirmar, a pasta fica **pulada (defeito)** na lista — não *falhou*,
porque ela não foi tentada e sim descartada — e recebe o mesmo
`_DISCO_COM_DEFEITO.txt`, que nesse caso diz que **nenhum arquivo foi
copiado** e traz a sua observação. A observação também fica guardada no
trabalho. Nada que já esteja na pasta é apagado, e o próximo disco vai para a
próxima pasta pendente.

Se a pasta já tiver conteúdo de uma tentativa anterior, o relatório diz
*incompleto* em vez de *nenhum arquivo*, e informa quantos arquivos estão lá.

A opção é recusada enquanto aquela pasta estiver sendo copiada — aí existe uma
transferência para parar primeiro, e quem faz isso é o botão **Disco
defeituoso** do painel do disco.

### Se o disco for recuperado depois

Use *Tentar novamente* no menu de contexto da lista: a pasta volta a pendente
e a cópia aproveita o que já está lá. Quando essa nova cópia terminar bem, o
`_DISCO_COM_DEFEITO.txt` é **removido automaticamente** — senão a pasta ficaria
se contradizendo, cheia de arquivos e com um bilhete dentro dizendo que nada
pôde ser copiado.

**Se a unidade travar de vez**, o programa espera 10 segundos pela parada e
então **abandona** a leitura em vez de ficar preso a ela: a pasta é marcada,
o relatório é gravado dizendo que a unidade parou de responder, e o lote
segue. Se a leitura travada finalmente voltar, o resultado dela é ignorado —
ela não reabre uma pasta que você já deu por encerrada.

## Vários discos na mesma pasta (acumular)

Quando um único conjunto vem em três, quatro ou dez discos, não faz sentido
criar uma pasta para cada um. Marque a pasta para **acumular** e ela passa a
receber **todos** os discos seguintes, um atrás do outro, até você desligar.

Para ligar, use qualquer um dos dois caminhos:

- botão direito na pasta, na lista → **Acumular discos nesta pasta**;
- em **Enviar para...**, marque *Continuar enviando os próximos discos para
  esta pasta* antes de confirmar.

Enquanto está ligado, uma faixa âmbar no topo da janela diz em qual pasta está
acumulando, quantos discos já entraram e traz o botão **Parar de acumular**. A
linha correspondente na lista fica âmbar e a coluna *Situação* mostra
`acumulando (N)` em vez de `concluída`, porque a pasta não está concluída —
está esperando o próximo disco.

O que acontece nos bastidores:

- a **ordem da lista não muda**. Só o sinalizador redireciona os discos, então
  desligá-lo devolve tudo exatamente ao que era: o próximo disco volta para a
  primeira pasta pendente;
- **cada disco fica registrado**. A pasta guarda o total acumulado (arquivos,
  bytes) e, separadamente, a ficha de cada disco que entrou — rótulo, número
  de série e quanto cada um contribuiu. Sem isso o registro do último disco
  simplesmente apagaria o do anterior;
- **o trabalho não se declara concluído** enquanto uma pasta estiver
  acumulando. Ela está pronta com os discos que tem e aberta para os
  próximos;
- **reinserir um disco que já entrou continua avisando.** A verificação de
  duplicata olha todos os discos da pasta, não só o último;
- colisões de nome entre discos são **esperadas** aqui (`AUTORUN.INF`,
  `INDEX.HTM`). Continuam sendo resolvidas como `arquivo (2).ext` sem apagar
  nada, mas não marcam a pasta com ⚠ — só uma falha de leitura marca;
- ligar o sinalizador durante a contagem regressiva **redireciona o disco que
  já está na bandeja**, não apenas o próximo. Uma cópia em andamento nunca
  muda de destino no meio do caminho;
- o estado sobrevive a fechar e reabrir o programa.

Só uma pasta acumula por vez — duas disputariam cada disco e o desempate
acabaria sendo a ordem da lista, que não é uma decisão de ninguém. Ao ligar
numa segunda pasta, o programa pede confirmação e transfere.

## Segurança dos dados

- Cada arquivo copiado tem o tamanho conferido logo após a gravação.
- Um setor ilegível é tentado 3 vezes, depois pulado e registrado — o disco
  termina marcado com ⚠ e a lista exata dos arquivos que falharam, em vez de
  perder o disco inteiro.
- Um disco que você já sabe que está ruim pode ser descartado direto pela
  lista, sem tentativa, deixando o mesmo relatório na pasta.
- Vários arquivos ilegíveis, ou uma unidade que parou de responder, oferecem
  **Disco defeituoso**: a pasta fica marcada como falha e recebe um
  `_DISCO_COM_DEFEITO.txt` listando o que não pôde ser lido, em vez de o lote
  ficar preso num disco que não vai terminar.
- Falhas de **destino** (rede caiu, disco cheio) encerram o disco em vez de
  serem tratadas arquivo a arquivo: não são culpa de um arquivo.
- Uma colisão de nome com conteúdo diferente nunca sobrescreve: o arquivo
  novo é salvo com outro nome e o antigo é mantido.
- O trabalho é gravado de forma atômica em dois lugares: `%LOCALAPPDATA%` (que
  manda) e `_backupov2-job.json` dentro da pasta do lote (que torna a pasta
  autoexplicativa). O local vem primeiro porque um arquivo que só existe no
  compartilhamento de rede não consegue registrar a queda desse mesmo
  compartilhamento.

## Ferramentas de linha de comando

```powershell
python -m backupov2.tools.drivecheck            # unidades, disco carregado, tipo
python -m backupov2.tools.drivecheck --eject    # abre a bandeja
python -m backupov2.tools.drivecheck --size     # mede o disco carregado

python -m backupov2.tools.jobdump "<caminho>\_backupov2-job.json"
python -m backupov2.tools.extract protocolo*.jpg   # ler nomes de fotos
```

## Testes

```powershell
python -m unittest discover -s tests -t . -v
```

366 testes, nenhum deles exige uma unidade óptica. O scanner, o copiador, o
ejetor e o relógio são injetados, então uma sessão inteira de discos — inserir,
pular, redirecionar, disco duplicado, acumular vários discos numa pasta só,
pausar e pular no meio de uma transferência, disco riscado marcado como
defeituoso, disco descartado sem ser lido, unidade travada abandonada, disco
removido no meio da cópia, rede caindo, falha ao ejetar, fechar e retomar — é
reproduzida de forma determinística em `tests/test_runner.py`.
`tests/test_integration.py` faz o mesmo com arquivos de verdade em disco.

## Arquitetura

```
backupov2/
  core.py       validação de nomes, varredura e cópia (com política de erro)
  errors.py     taxonomia de erros do Windows -> política
  winapi.py     TODO o ctypes: unidades, presença de mídia, ejeção, caminhos longos
  media.py      MediaInfo, OpticalScanner (Win32Scanner / FakeScanner), tipo de disco
  jobmodel.py   Job / DiscEntry / EntryDraft
  jobstore.py   gravação atômica, cópia local + portátil
  reconcile.py  compara o trabalho salvo com o que está no disco (retomada)
  vision.py     leitura dos protocolos por foto (Gemini), saneamento de nomes
  runner.py     a máquina de estados da cópia automática
  strings.py    vocabulário pt-BR compartilhado
  ui/           Tkinter: app, lista de pastas, painel do disco, diálogos
  tools/        drivecheck, jobdump, extract
```

Duas regras rígidas, verificadas por `tests/test_layering.py`:

- nada fora de `ui/` importa `tkinter`;
- nada fora de `winapi.py` importa `ctypes`;
- nenhuma função de thread na interface chama `.after()` — o resultado volta
  por uma fila que a thread principal consome.

É isso que mantém a máquina de estados testável sem interface e sem hardware.

### O detalhe que faz tudo funcionar

Não existe um índice de "disco atual" em lugar nenhum. O próximo destino é uma
*consulta derivada* — a primeira entrada ainda `PENDING`. Pular, redirecionar,
reordenar e inserir no meio da execução saem de graça, porque não há ponteiro
que possa discordar da lista. A identidade de uma entrada é o `entry_id`
(uuid4); a posição na lista é só ordem.
