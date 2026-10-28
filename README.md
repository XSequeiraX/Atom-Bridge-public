# Bridge-Control

Controlo de câmaras e equipamento de filmagem a partir de um único bridge: iniciar e parar a gravação de
todos os aparelhos de uma vez, sincronizar o relógio, receber timecode (LTC) e registar cada take num shoot log.
Funciona numa consola (curses) e numa página web (telemóvel, iPad).

## Aparelhos suportados

| Aparelho | Ligação | Notas |
|---|---|---|
| Canon (EOS R6 Mark II, R10 e outras) | Wi-Fi, CCAPI | Ligam-se ao AP das câmaras |
| Blackmagic Pocket Cinema Camera | Bluetooth LE, pelo gateway C3 (Wi-Fi) | REC, estado completo e timecode. O Bluetooth do Atom fica como plano B |
| BMPCC extra (`bm2`, `bm3`...) | Cada uma no seu gateway C3 | REC confirmado pela câmara, bateria e timecode. Até 8 |
| Zoom F6 | USB-MIDI (protocolo FRC-8), de duas maneiras: **por cabo ao Atom**, ou **pelo gateway S3 dedicado** (cabo USB do F6 ao S3, que fala com o Atom por **Wi-Fi**: modo "F6 com o Atom") | REC/STOP com confirmação do próprio F6. O S3 pode também comandar o F6 sozinho, sem o Atom ("F6 autónomo", ver mais abaixo) |
| Telemóveis com a app Blackmagic Camera: iPhones (testado); Android (a app 3.4 ou mais nova também tem controlo REST: **por validar**) | Wi-Fi, REST | Vários telemóveis como alvos de REC. O tipo no bridge continua a chamar-se `iphone` |
| GoPro HERO8 | Bluetooth LE, via gateway(s) ESP32-S3 | Relógio por QR (GoPro Labs). Até 4 gateways S3 (mais alcance, mais GoPro) |
| Sony (Alpha, FX, ZV: A7 IV, FX3, A1, A9 III, ZV-E1... os modelos do Camera Remote SDK) | **Wi-Fi sem gateway** (a câmara entra no AP das câmaras, como as Canon) ou USB | REC confirmado pela câmara, bateria, cartão, ISO/íris/obturador, Time Code Preset. Pelo SDK da Sony (o ajudante já compilado vem no repositório). Ver `atom/app/sony/LEIA-ME.txt` e `AVISOS-LICENCAS.md` |
| TC-1 (Deity) | Cabo ou Bluetooth | Leitura de LTC para o relógio do bridge |

O comando FIZ (Magic FIZ) **não faz parte do bridge**: vive só dentro do gateway C3 e só no modo Autónomo, em
que o C3 dispara por ele a sua BMPCC. No modo Bridge o C3 nem o procura. O bridge não o vê nem mostra o estado dele.

## Componentes

- **`atom/app/`**: o código que corre no Atom (`/opt/camera-bridge`) e a **fonte única**: o bridge Flask
  (`bridge.py` arranca o `nucleo.py`; a API está em `rotas_*.py`, por área), as frotas (`bmcam.py`, `gopro.py`, `bmfleet.py`), o gateway C3 (`c3_bm.py`), o F6, a
  página web (`ui.html`). Descrição módulo a módulo em `atom/LEIA-ME.txt`.
- **`bridge-ctl.py`**: a consola curses (dashboard, menus, wizard de arranque). Win+L (no teclado do Atom) ou F12 bloqueiam o
  ecra; o PIN da página (GLOBAL SETTINGS > Web PIN) desbloqueia. A gravação continua e o ecra bloqueado mostra o estado.
- **`docs/FRC-8-protocolo.pdf`**: o protocolo FRC-8 do Zoom F6 por USB-MIDI (ligação, mensagens, estado, limites). A fonte é `docs/frc8_protocolo.html` (PDF gerado com o Chromium).
- **`firmware/`**: o firmware estável do S3 (GoPro, F6 com o Atom, F6 autónomo), pronto a gravar, com `gravar.py` e `LEIA-ME.md`.
- **`testes/`**: as experiências (sonda BLE, ferramentas de ensaio do F6). Não fazem parte do produto; ver `testes/LEIA-ME.md`.
- **`c3_fiz_bm/`**: firmware MicroPython do gateway C3 (BMPCC por Bluetooth, Wi-Fi, FIZ). Ver `c3_fiz_bm/LEIA-ME.txt`.
- **`_esp32_tools/`**: firmware dos ESP32-S3 (gateway S3: modo GoPro ou modo F6 (FRC-8); ponte BLE).
- **`atom-install/`**: instalador (`install_atom.sh`), serviços systemd.


## Modo de treino

Na consola: **GLOBAL SETTINGS > TRAINING > Demo mode**. O bridge simula duas GoPro (DEMO-1, DEMO-2, no gateway DEMO)
e uma BMPCC (DEMO-BM) e junta-as ao rig; o topo do ecrã mostra **DEMO MODE**. Arma-se, grava-se e pára-se como no
plateau, na consola, na página ou no iPad; os takes ficam no shoot log com os nomes DEMO. Desligar no mesmo sítio tira-as
do rig e dos alvos de REC (recusado com uma câmara DEMO a gravar). Não sobrevive a um reinício do bridge.

O simulador (`atom/app/simulador.py`) também corre à parte, para outro PC ou para os testes: `/sim/drop?secs=10` simula
uma queda do gateway. Não anuncia beacons: nunca entra sozinho num rig verdadeiro.

## Stress test

`atom/app/stress_bridge.py` (no Atom: `/opt/camera-bridge/stress_bridge.py`) carrega o bridge a sério: ciclos de REC/STOP do
grupo com vários leitores de `/api/status` em paralelo (iPads, telemóveis, consola), medindo a latência (p50/p95/p99/máx.),
erros, e a memória, CPU, fios e ficheiros abertos do processo do bridge, a carga e a temperatura do Atom, mais os erros do
journal. Confirma ainda, no `/api/status`, que **cada aparelho** passa a gravar e pára dentro de `--confirm` segundos, e
regista quanto demora (por aparelho). Por omissão só usa as câmaras do **modo de treino** (GLOBAL SETTINGS > TRAINING >
Demo mode) e recusa-se a correr com ele desligado ou com alvos de REC que não sejam DEMO (o F6, por exemplo). Com `--real`
testa o equipamento **verdadeiro** dos alvos de REC: grava a sério (cartões, baterias, calor), pede `SIM` e deixa takes de
teste no shoot log, que se apagam no fim.

```bash
python3 /opt/camera-bridge/stress_bridge.py                                      # treino: 20 ciclos, 4 leitores
python3 /opt/camera-bridge/stress_bridge.py --cycles 100 --hold 2 --clients 8 --duration 600
sudo python3 /opt/camera-bridge/stress_bridge.py --real --cycles 30              # equipamento verdadeiro (takes de 6 s)
python3 /opt/camera-bridge/stress_bridge.py --cycles 0 --clients 0 --duration 300    # linha de base, sem gravar
```

Escreve em `~/stress-results/<data>/`: `report.txt` (veredicto PASSOU / ATENCAO / FALHOU e números), `report.html` (gráficos),
`samples.csv` e `report.json`. Uma subida contínua da memória, dos fios ou dos ficheiros abertos aponta para uma fuga.
`GET /sim/drop?secs=10` no simulador (portas 8170 e 8172 no modo de treino) simula a queda de um gateway durante o teste.

## API

Toda a API do bridge está em [`docs/API.md`](docs/API.md), gerada das rotas do bridge (`nucleo.py` e `rotas_*.py`). As regras (nomes, ids, formato das
respostas) estão no topo de `atom/app/api_check.py`, que também verifica que **todos os clientes** (página, consola, USB,
stress) chamam rotas que existem. Corre-se com `python3 atom/app/api_check.py` (e `--docs` para refazer o `API.md`).

**Antes de cada push**: `python3 ci/verificar.py` (precisa de `pip install pyflakes pyte` e do `node`). Verifica que o Python
compila sem nomes indefinidos, as regras da API, o JavaScript das páginas e que a consola arranca e responde num simulador
(`ci/consola_sim.py`, que também se corre à mão: `python3 ci/consola_sim.py 100x40 normal '\e' 2`). O GitHub corre o mesmo
em cada alteração (workflow Verificar).

Regras da API, em resumo: `/api/<domínio>/...` em snake_case; **cada aparelho tem um só id estável** (o tipo — `canon`,
`iphone`, `gopro`, `bmpcc`, `bmpcc_extra`, `f6` — é um campo, nunca parte do caminho) e tudo o que é dele vive em
`/api/devices/<id>/<ação>` (o REC de um só aparelho é `POST /api/devices/<id>/rec`). `GET /api/devices` lista-os (`?type=` filtra),
`POST /api/devices` com `{"type": ...}` acrescenta um, `POST /api/devices/batch` faz uma ação sobre vários (`order`, `reset`,
`settings_polling`, `lead`); remove, restore e forget são `POST /api/devices/<id>/<ação>` (não há `DELETE`). Um id desconhecido
dá 404, uma ação que o tipo não tem dá 409. Todo o POST e todo o erro respondem `{"ok": bool, "error": texto}`.

## Por integrar

- **Outras câmaras por gateway S3**: o S3 só fala com GoPro. O bridge já aceita vários S3; outra câmara BLE pede o
  firmware do gateway e um módulo da frota próprio.

## Consola

- **Dashboard**: setas escolhem a linha (câmara, F6 ou gateway); **Enter** abre só as opções dessa linha; **1-9** corre
  uma opção da barra (que passa a várias linhas se não couber); **Esc** abre o menu geral.
- **Menu geral (Esc)**: WIZARD, DEVICES, TIME, LOGS, POWER, GLOBAL SETTINGS.
  - **DEVICES**: iPhone, Canon (com Force Canons idle), GoPro, BMPCC extra, SONY (Add a Sony, Sony SDK and search), ZOOM (F6 screen, SD Card reader mode),
    gateways por USB, **WIRELESS** (Restart NetworkManager e uma entrada "Settings <rede>" por interface Wi-Fi: estado,
    aparelhos ligados e memorizados, LED, reiniciar o AP), BLUETOOTH, USB (tudo o que está ligado por USB, menos o que é
    do próprio Atom, e os controlos MIDI/HID), aparelhos guardados.
  - **POWER**: Restart bridge, Reboot, Shut down. Só aqui (sem tecla no dashboard) e recusado a gravar.
  - **GLOBAL SETTINGS**: verificações (preflight, versão, info) e atualizações (GitHub, gateways).

## Instalar e atualizar

Instalação (Atom com Debian): copiar o repositório e correr, como root:

```bash
sudo bash atom-install/install_atom.sh
```

O script é idempotente. Copia `atom/app` para `/opt/camera-bridge` com cópia de segurança e repõe-na sozinho se o
bridge novo não arrancar. Pede o nome (SSID) e a palavra-passe do AP das câmaras, que têm de ser os mesmos que as
câmaras já têm guardados (ou `AP_SSID` e `AP_PSK` no ambiente).

Atualizar: `git push` e, na consola do Atom, **GLOBAL SETTINGS > Update from GitHub**. O bridge reinicia e desliga o F6:
volta a ligá-lo com FRC-8 connect e o cabo desligado. A configuração do rig (`settings.json`, `c3.json`,
`bmpccs.json`, `iphones.json`, `gopros.json`, `devices.json`, shoot log) fica fora de
`atom/app` e nunca é tocada.

## Mais do que uma BMPCC

A principal fica no C3 que se anuncia primeiro (ou no endereço fixo de `c3.json`). Para acrescentar outra:
1. Pôr o segundo C3 em modo **Bridge**, na mesma rede, e emparelhar-lhe a câmara (consola: GATEWAYS > C3 > Pair camera,
   ou a página do C3).
2. Acrescentá-la na página (secção "BMPCC (EXTRA)" > + ADD) ou na consola (Devices > Add a BMPCC). Os C3 que se
   anunciaram e ainda não são de nenhuma câmara são sugeridos.
3. Pô-la entre os alvos de REC como as outras.

## Firmware dos ESP32

Os gateways (C3 e S3) são **sempre configurados e atualizados pelo Atom**; nada se grava num gateway a partir de outro sítio.
- **Configuração inicial (e qualquer recuperação): por USB, no Atom.** Liga-se o gateway (em branco ou antigo) ao Atom por USB e,
  na consola, **Devices > Set up / update a gateway by USB** (ou GATEWAYS > o gateway > Setup / update by USB). O Atom identifica a
  placa (C3 ou S3), grava-lhe o firmware de raiz se estiver em branco e deixa-lhe o Wi-Fi da rede das câmaras (lido do AP do
  Atom, sem aparecer no ecrã, no registo nem em linhas de comandos) ou outro que escreveres. Usa `esptool`, `mpremote` e `pyserial`,
  que o instalador instala. Na shell: `sudo python3 /opt/camera-bridge/gw_usb.py --ap [/dev/ttyACM0]`.
- **Depois: por Wi-Fi.** Atualizações sem cabo, como abaixo. Com um cabo ao Atom, a mesma opção atualiza também (um gateway que já
  funciona mantém o Wi-Fi e as câmaras).

- **C3 (MicroPython)**: a primeira gravação (MicroPython, `boot.py`, `main.py`, o programa já compilado `app.mpy` e `netcfg.mpy`, e o Wi-Fi) faz-se pelo Atom, por USB, como acima.
  O MicroPython do C3 descarrega-se uma vez de micropython.org (o instalador tenta; se não houver Internet, põe-se o `.bin` em
  `fw/c3/micropython-v1.24.1.bin`). A versão está fixa (`MP_VERSAO` em `gw_usb.py`): com o MicroPython 1.29 o Bluetooth do
  C3 não liga (`ble.active` dá `EIO`) e o Atom propõe reinstalar a 1.24.1 num C3 que a tenha. O firmware vem do repositório: o
  `git pull` e o instalador deixam-no no Atom, que o envia ao C3 e às BMPCC extra (cada uma com o seu C3), com
  reposição automática se o novo não arrancar.
- **S3 (Arduino)**: o firmware **não leva o Wi-Fi compilado**. O S3 guarda até duas redes na sua memória e, ao arrancar,
  tenta-as por ordem (15 s cada). Se não houver nenhuma, ou nenhuma ligar, abre o AP aberto `GOPRO-S3-XXXX` com o portal
  em `http://192.168.4.1` (lista as redes, escolhe-se a do bridge e escreve-se a palavra-passe), como o gateway C3. O AP fica
  3 min (mais enquanto alguém estiver ligado) e, havendo redes guardadas, volta a tentá-las. O LED fica magenta no modo
  de configuração. Um S3 com o firmware anterior guarda sozinho a rede que já usava ao receber o novo.
  Como o `.bin` não tem segredos, o **GitHub compila-o** (`.github/workflows/s3-firmware.yml`) e deixa-o em
  `atom/app/fw/s3/gopro_gw.bin` (a placa do workflow é a *ESP32S3 Dev Module* com USB CDC no arranque, 8 MB de flash e
  esquema de partições com OTA, porque o core do Arduino não tem a M5Stack StampS3; ajusta-se em `FQBN`); o `git pull` leva-o ao Atom e o Atom grava-o em todos os S3. A primeira gravação de um S3
  novo é pelo Atom, por USB, com a imagem completa que o mesmo workflow também guarda no repositório
  (`atom/app/fw/s3/gopro_gw.merged.bin`). Para gravar a partir de um PC sem o repositório há a pasta `firmware/` (cópia da versão estável, `gravar.py` e o `LEIA-ME.md`).
- **Placas S3**: o mesmo `.bin` serve a **M5Stack StampS3** (a de referência) e a **Seeed XIAO ESP32S3** (que tem conector u.FL para antena
  externa). Só o LED muda: a StampS3 tem um LED RGB, a XIAO um LED simples. Na consola, GATEWAYS > S3 > **LED type** escolhe `rgb`
  (StampS3, por omissão), `simple` (XIAO) ou `off`; por USB, o comando série `LED rgb|simple|off`. Numa XIAO nova, até o mudares, o
  LED pode piscar de forma estranha (o sinal do RGB num LED simples): é só estético. Uma XIAO com o resto igual (GoPro por Bluetooth, F6 pelo USB,
  F6 autónomo) fica por validar em hardware: o arranque como anfitrião USB com a alimentação a vir do F6 e o conector USB-C dela.
- **Atualizar**: consola, **GLOBAL SETTINGS > Update gateways** (mostra o estado de cada gateway e atualiza os atrasados, um de
  cada vez, nunca a gravar). A atualização automática (ligar no mesmo menu) vem desligada: um gateway
  reinicia e ficava uns segundos sem câmaras entre takes. Ligada, só atua com o rig parado há 10 min. Um gateway em que a atualização falhou só volta a ser tentado quando o firmware mudar.
- **Do GitHub, num gateway**: consola, GATEWAYS > C3/S3 > Firmware > **Update from GitHub** (ou, na página, FIRMWARE no
  painel GoPro e C3 FIRMWARE nas BMPCC extra). Se o GitHub tiver novidades, o Atom atualiza-se primeiro (o bridge
  reinicia) e, ao voltar, grava esse gateway sozinho; sem novidades, grava logo o que o Atom tem. Sempre pelo Atom.

## Tirar e voltar a pôr gateways

Qualquer gateway (o C3 principal, os C3 das BMPCC extra, os S3, também o primeiro) pode ser tirado e voltar a ser posto:
- Recusa-se a tirar, desligar ou mudar um gateway enquanto algo grava.
- Ao tirar um S3, as suas GoPro saem das listas e dos alvos de REC; o S3 em si (e os emparelhamentos) ficam como estão.
- Voltar a pô-lo (mesmo endereço) devolve-lhe o mesmo número e os mesmos ids (`gp1`, `gp11`...), e uma BMPCC extra volta
  com o mesmo id (`bm2`) e nome, por isso os nomes de referência e o shoot log continuam a bater certo. Os alvos de REC
  escolhem-se de novo.
- Um C3 não se junta como S3, nem um S3 como C3 (o Atom pergunta ao aparelho o que ele é), e o mesmo C3 não serve duas câmaras.
- Sem nenhum S3 a lista fica vazia sem alarmes, e o menu só oferece juntar um. Uma instalação nova começa com o
  primeiro S3 por preencher.

## Comando do F6 pelo iPhone / iPad / Watch, sem o Atom

O S3 tem 3 modos (consola: GATEWAYS > S3 > **Mode**): **GoPro**, **F6 com o Atom** (o S3 faz de FRC-8 pela USB e o Atom comanda o F6) e
**F6 autónomo** (o S3 faz de FRC-8 e **ele próprio** serve o comando, sem o Atom). No autónomo o S3 abre o seu Wi-Fi
`F6-REMOTE-XXXX` (WPA2, a palavra-passe escolhes-la ao mudar de modo, 8 a 63 caracteres) e serve uma página em
`http://192.168.4.1/` com **REC / STOP / PLAY**, o estado (a gravar, tempo, F6 ligado) e os **medidores das faixas** (as 6 mais a
saída L/R, com as faixas armadas marcadas). REC, STOP e PLAY pedem um toque longo (cerca de meio segundo): um toque sem querer não
faz nada, e a página avisa se o F6 não confirmar.

1. Consola: GATEWAYS > o S3 > **Mode** > *F6 standalone*, e escreve a palavra-passe duas vezes. O S3 reinicia e o Atom deixa de o ver.
2. Cabo do F6 ao S3 primeiro; depois, no F6, MENU > SYSTEM > USB > FRC-8 > connect. O Wi-Fi sobe quando o F6 está ligado.
3. No iPhone: junta-te ao Wi-Fi `F6-REMOTE-XXXX`, abre `http://192.168.4.1/` no Safari e *Partilhar > Adicionar ao ecrã principal*.
4. Voltar ao Atom: a ligação **Back to Atom mode** no fundo da página (só a partir deste modo, e só quem sabe a palavra-passe do Wi-Fi), ou
   GATEWAYS > Setup / update by USB com o S3 ligado ao Atom.

No iPhone, deixa o ecrã ligado enquanto usas a página (Ajustes > Ecrã e luminosidade > Bloqueio automático > Nunca): a página é
HTTP, e o Safari só deixa um site manter o ecrã ligado em HTTPS. Com o ecrã bloqueado a página pára de ler o estado.

Apple Watch (por testar no terreno): um Atalho com a acção *Obter conteúdo do URL*, método POST, em
`http://192.168.4.1/api/f6/press?note=24` (REC), `note=22` (STOP) ou `note=23` (PLAY), que se põe no Watch ou numa complicação. O S3 recusa o PLAY enquanto o F6 grava (o F6 poria uma marca na take e deixava a faixa em pausa). Precisa
de o iPhone (ou o Watch) estar nesse Wi-Fi. Os níveis das faixas são os medidores; **o F6 não reporta os faders nem o trim pelo FRC-8**,
por isso não há volumes de ganho nem o dos headphones. O Wi-Fi do S3 em modo F6 emite a 11 dBm para poupar a corrente da USB do F6: alcance de
algumas dezenas de metros em linha de vista. No autónomo só se aceitam REC, STOP e PLAY (as outras notas do FRC-8 e o MIDI livre
continuam a pedir o token do Atom).

## Segurança

- O Atom e a rede privada Tailscale entram sem PIN. Quem está noutra rede (iPads e telemóveis na rede das câmaras, o
  Wi-Fi do local) entra com um **PIN** (consola: GLOBAL SETTINGS > Web PIN): 4 a 12 dígitos, guardado só como hash com
  sal, comparação em tempo constante, bloqueio ao fim de 5 erros (15 min), sessão em cookie HttpOnly/SameSite=Strict
  guardada só no Atom, limite de pedidos. Sem PIN definido, essas redes não entram. Ideia do
  [DumbAssets](https://github.com/DumbWareio/dumbassets).
- Pedidos que mudam coisas, vindos de um browser, têm de vir da própria página (uma página qualquer aberta no portátil
  não consegue mandar reiniciar o Atom).
- Gateways: o Atom grava-lhes um token pela USB na configuração inicial (nunca pela rede). Os C3 exigem-no para o
  firmware por Wi-Fi. Os S3 exigem-no para o firmware, o rollback, os resets, os emparelhamentos, adicionar/retirar
  GoPro, o REC, o reinício por UDP e mudar o Wi-Fi (fora do modo de configuração). Um gateway sem token aceita tudo,
  como antes; a consola mostra "NO TOKEN: set it up by USB" na linha dele.
- Depois de um firmware novo por Wi-Fi, o C3 dá-o por bom com o Wi-Fi ligado 60 s. Se em 5 min não ligar e nenhuma
  das redes guardadas estiver ao alcance, também (a culpa não é do firmware); se a rede está lá e não ligou, ao 3.º
  arranque volta ao anterior.
- As credenciais nunca aparecem na interface nem em linhas de comandos (o SFTP usa paramiko, não sshpass; a password
  do AP das câmaras vai para um ficheiro 0600 do NetworkManager).
- O código não tem palavras-passe escritas. Os endereços de exemplo (`192.168.4.x`) são da rede local do rig.

## Estado

Projeto em uso num rig real, em desenvolvimento contínuo. Algumas funções ainda não foram validadas em
todas as configurações; as notas do que falta estão em `TODO.md` (fora do repositório).

## Licença

O código do Atom Bridge está sob a licença MIT ([`LICENSE`](LICENSE)). Os ficheiros de terceiros (as bibliotecas do
SDK da Sony, os tipos de letra, as bibliotecas compiladas no firmware) mantêm as suas licenças, e há termos da Sony e
da Canon que quem usa o bridge tem de conhecer: ver [`AVISOS-LICENCAS.md`](AVISOS-LICENCAS.md). O Atom Bridge não é
feito nem suportado pela Sony, Canon, Blackmagic Design, GoPro, Zoom ou Deity.

Os avisos de licença aparecem uma só vez, **antes de a consola carregar** e na primeira visita da página, iguais para
qualquer câmara (atual ou futura): sem os aceitar, o bridge não se usa. O aceite fica guardado (`termos.json`) até o
texto mudar.
