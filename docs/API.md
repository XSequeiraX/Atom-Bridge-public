# API do bridge

Gerado por `atom/app/api_check.py --docs` a partir das rotas do bridge (`nucleo.py`, `rotas_*.py`): nao se edita a mao. As regras estao no topo do script.

## backup

| Metodo | Caminho | O que faz |
|---|---|---|
| POST | `/api/backup/restore` | {"usb_file": nome} (da pen) ou {"backup": {...}}: repoe a configuracao e reinicia o bridge e o servico USB. Recusado a gravar. |
| POST | `/api/backup/send` | Envia os registos para o servidor. Corre em segundo plano. |
| GET,POST | `/api/backup/sftp` | Le e grava a configuracao do servidor SFTP. |
| GET,POST | `/api/backup/usb` | GET: as copias que estao na pen USB. POST: guarda uma copia nova na pen. |

## bluetooth

| Metodo | Caminho | O que faz |
|---|---|---|
| POST | `/api/bluetooth/reset` | Restart bluetoothd, then bring the BLE devices back. |

## demo

| Metodo | Caminho | O que faz |
|---|---|---|
| GET,POST | `/api/demo` | GET: {"on"}. POST {"on": true/false}: liga/desliga o modo de treino. |

## devices

| Metodo | Caminho | O que faz |
|---|---|---|
| GET | `/api/devices` | Todos os aparelhos do rig: devices (em servico, um item cada com id e type), removed (retirados, para restaurar ou esquecer), order, alias e o bloco bmpcc. ?type= filtra por tipo (canon, mobile (ou iphone), gopro, bmpcc, bmpcc_extra, sony, f6). |
| POST | `/api/devices` | Acrescenta um aparelho: {"type": "canon", name, ip, port, https} / {"type": "mobile", name, host, port} / {"type": "gopro", mac, model, name, gw} / {"type": "bmpcc", address, alias} / {"type": "bmpcc_extra", name, host} / {"type": "sony", name, model, via: "ip" / "usb", host / serial, ssh, user, password} (409 se os avisos de licenca ainda nao foram aceites: GET/POST /api/system/terms). Devolve o id novo (o da BMPCC e sempre "bmpcc"). |
| GET | `/api/devices/<id>` | Um aparelho (em servico ou retirado) pelo seu id: {"device": {...}}; 404 se o id nao existe. |
| POST | `/api/devices/batch` | Uma accao sobre varios aparelhos: {"action": ..., "type": ...}. order {ids: [...]} (ordem no ecra principal); reset {type: canon} (tira as Canon ligadas de um estado preso); settings_polling {type: canon, on: bool} (liga/desliga a leitura de Av/Tv/ISO...); lead {type: gopro, ms: N} (avanco do QR da hora). |

### `devices/<id>/<accao>`

Tudo o que e de um aparelho. O id e o do `GET /api/devices`; 404 se nao existe, 409 se o tipo nao tem a accao ou o aparelho esta retirado (so restore e forget valem). Tipos: canon, mobile, gopro, bmpcc, bmpcc_extra, sony, f6.

| Metodo | Accao | Tipos | O que faz |
|---|---|---|---|
| POST | `af` | canon | So foca. Devolve se a Canon ACEITOU o comando -- nao se focou mesmo (ver trigger_af() no canon_ccapi.py: o "Focus OK/NG" real da CCAPI so vem no stream de Live View incidental info, nao implementado aqui). |
| POST | `afauto` | canon | Volta ao AF automatico (o metodo de antes do primeiro clique). |
| GET | `afframe` | canon | Moldura(s) de AF seleccionada(s) e recorte do live view (escala da imagem fixa). |
| POST | `afpoint` | canon | Move a moldura de AF para um ponto do live view (u, v em 0..1). |
| POST | `alias` | canon, mobile, gopro, bmpcc, bmpcc_extra, sony, f6 | Alias da camara, um nome curto de referencia (vai para a coluna alias do shoot log): {"alias": texto}. Vazio apaga. |
| GET | `card/ls` | f6 | Lista uma pasta do cartao: ?path=. |
| POST | `card/op` | f6 | mkdir / rename / rename_take / move / trash / empty_trash no cartao. |
| POST | `card/play` | f6 | Toca um WAV do cartao pela saida de som do Atom (o LTC larga-a entretanto). |
| GET | `card/report.csv` | f6 | O CSV da ultima leitura do cartao (descarrega). |
| POST | `card/scan` | f6 | Le o cartao (so leitura) e cruza com o shoot log; deixa um CSV. |
| GET | `card/status` | f6 | Estado do cartao SD do F6 (montado, leitura em curso, leitor de som). |
| POST | `card/stop` | f6 | Para o som do cartao. |
| POST | `clock` | gopro | Hora da camara (por BLE, 1 s) contra a do bridge: serve para ver se o QR a acertou. |
| POST | `config` | mobile, bmpcc_extra, sony | Nome / endereco / porta / ligado: mobile {name, host, port, enabled}; BMPCC extra {name, host}; Sony {name, model, via, host, serial, ssh, user, password, enabled}. |
| POST | `connect` | canon, bmpcc | Liga a camara: a Canon pelo CCAPI; a BMPCC pelo Bluetooth (so uma tentativa de cada vez). |
| POST | `disconnect` | canon, bmpcc | Desliga a camara (recusado a gravar). |
| POST | `forget` | canon, mobile, bmpcc, sony | Apaga de vez um aparelho retirado da lista dos guardados. |
| POST | `iso` | bmpcc | ISO da BMPCC principal: {"iso": 800}. |
| POST | `jam` | f6 | 5 s de LTC a andar pela saida de auscultadores, para o F6 acertar o TC. |
| GET | `liveview.mjpg` | canon | O live view da Canon em MJPEG (multipart/x-mixed-replace). |
| POST | `note` | f6 | Nota da PROXIMA take no F6 (metadado NOTE), pelo teclado do FRC-8. |
| POST | `path` | bmpcc | Por onde vai a BMPCC principal: {"via": "c3"} (o gateway C3, o normal) ou {"via": "bluez"} (o Bluetooth do proprio Atom, plano B, com a camara emparelhada ao Atom: Bluetooth > Pair a new device). Recusado a gravar. |
| POST | `rec` | canon, mobile, gopro, bmpcc, bmpcc_extra, sony, f6 | REC/STOP so deste device, sem mexer nos alvos nem nos outros: o botao REC de cada linha, da consola e do USB. Passa pelo mesmo REC de grupo (fila, intencao e confirmacao) com um so alvo. |
| POST | `remove` | canon, mobile, gopro, bmpcc, bmpcc_extra, sony | Tira o aparelho de servico (recusado a gravar). Canon, mobile, BMPCC e Sony ficam na lista dos retirados (restore); a GoPro e a BMPCC extra apagam-se. |
| POST | `restore` | canon, mobile, bmpcc, sony | Volta a pôr em servico um aparelho retirado (a BMPCC que estava em servico fica guardada). |
| POST | `scene` | mobile, f6 | Cena da claquete: mobile {scene} (proximos clips); F6 {name} ou {inc: true} (cena +1). |
| POST | `setting/<name>` | canon | Muda um ajuste simples da Canon (Av, Tv, ISO, modo de obturador, qualidade de video...): {"value": ...}. |
| POST | `sync_time` | canon | Acerta o relogio da Canon pelo do bridge (accao do operador, recusada a gravar). |
| POST | `tc` | mobile, sony | Mobile: mede ja o desvio do timecode para o LTC (10 amostras). Sony: escreve o Time Code Preset (camara parada): {"tc": "HH:MM:SS:FF"}, ou sem tc o LTC de agora. |
| POST | `track_name` | f6 | Nome de uma pista no F6 (metadado TRACK NAME da proxima take). |
| POST | `user` | f6 | Tecla USER do F6 pela funcao: mark, clear_clip, circled (f6_ctl.USER_KEYS). |

## fault

| Metodo | Caminho | O que faz |
|---|---|---|
| POST | `/api/fault/clear` |  |
| POST | `/api/fault/test` | Raise a fake fault so the alarm can be verified before a shoot. |

## gateways

| Metodo | Caminho | O que faz |
|---|---|---|
| GET | `/api/gateways/c3` | O gateway C3 da BMPCC principal e os C3 anunciados que nao pertencem a nenhuma BMPCC (found). |
| POST | `/api/gateways/c3/enabled` |  |
| GET,POST | `/api/gateways/c3/firmware` | Firmware do gateway C3 por Wi-Fi: GET compara o do C3 com o do repositorio; POST envia o que mudou (?action=rollback volta ao anterior). Recusado a gravar: o C3 reinicia. |
| POST | `/api/gateways/c3/host` |  |
| GET,POST | `/api/gateways/firmware` | GET: o estado do firmware de todos os gateways. POST {"ids": [...]} (omitido = todos os atrasados): actualiza-os. POST {"auto": true/false} liga/desliga a actualizacao automatica. |
| GET | `/api/gateways/s3` | Os gateways S3 das GoPro (o primeiro em gateway, todos em gateways) e as GoPro que se anunciam (found). |
| POST | `/api/gateways/s3/add` | Mais um gateway S3: {host, name}. O primeiro nunca se acrescenta (ja existe). |
| POST | `/api/gateways/s3/enabled` | Liga/desliga o gateway das GoPro so no bridge (o gateway fisico desligado nao e avaria). {"on": true/false}: so um booleano conta (um texto como "no" nao liga nada por engano). |
| GET,POST | `/api/gateways/s3/firmware` | Firmware do gateway S3 (GoPro) por Wi-Fi: POST grava no S3 o .bin que veio do GitHub (atom/app/fw/s3); ?action=rollback volta ao anterior. Nao aceita um .bin enviado de fora. Recusado a gravar. |
| POST | `/api/gateways/s3/host` | Endereco do gateway ESP32-S3 das GoPro. |
| POST | `/api/gateways/s3/led` | O tipo de LED da placa S3: {"type": "rgb"/"simple"/"off", "gw": n}. Aplica-se logo. |
| POST | `/api/gateways/s3/mode` | O selector do S3: {"mode": "gopro"/"f6"/"f6solo", "gw": n, "psk": "..."}. O S3 guarda e reinicia. `psk`: a palavra-passe do Wi-Fi do comando autonomo (f6solo); nao se regista. |
| POST | `/api/gateways/s3/remove` |  |
| POST | `/api/gateways/s3/reset` | Repoe o gateway ESP32-S3: reboot / bonds / factory. |
| POST | `/api/gateways/s3/scan` | Procura GoPro a anunciar-se (o gateway varre; demora uns segundos). |
| GET,POST | `/api/gateways/usb` | Configuracao inicial e actualizacao de qualquer gateway (C3 ou S3) ligado por USB ao Atom: GET diz as portas, as ferramentas e as imagens; POST {"port", "use_ap": bool, "wifi": {"ssid", "psk"}} identifica a placa, grava-lhe o firmware e o Wi-Fi (o das cameras com use_ap). As passwords nunca vao para o registo nem para a resposta. "kind": "m5stick" grava o comando de bolso do F6 (M5StickS3): "mode" setup (imagem completa + o Wi-Fi do S3 do F6 em "wifi"), update (so a aplicacao) ou wifi (so o nome e a palavra-passe do AP). |

## log

| Metodo | Caminho | O que faz |
|---|---|---|
| GET | `/api/log/events.log` | O event log inteiro, do disco -- e nao as 50 entradas em memoria. |
| POST | `/api/log/events/new` | Arquiva o event log em disco e comeca um vazio. |
| POST | `/api/log/mark` | Assinala a take que acabou de parar. |
| GET | `/api/log/shoot` | As ultimas linhas do log, para a grelha na propria pagina. |
| GET | `/api/log/shoot.csv` | Downloadable CSV of every recording start/stop this session — see log_shot() / SHOOT_LOG_PATH above. |
| POST | `/api/log/shoot/delete` | Apaga uma linha do log. |
| POST | `/api/log/shoot/edit` | Corrige uma linha do log: o evento, a nota, ou ambos. |
| POST | `/api/log/shoot/new` | Arquiva o shoot log actual e comeca um vazio. |
| POST | `/api/log/shoot/note` | Escreve uma nota no shoot log, na take que estiver a decorrer. |

## login

| Metodo | Caminho | O que faz |
|---|---|---|
| POST | `/api/login` |  |

## logout

| Metodo | Caminho | O que faz |
|---|---|---|
| POST | `/api/logout` |  |

## net

| Metodo | Caminho | O que faz |
|---|---|---|
| POST | `/api/net/action` |  |
| GET | `/api/net/status` | Wi-Fi picture for the recovery panel. |
| GET | `/api/net/tailscale` | Estado do Tailscale, que e o caminho de acesso remoto ao Atom. |
| POST | `/api/net/wifi/connect` | Liga o uplink a uma rede GUARDADA, escolhida pelo operador. |
| GET | `/api/net/wifi/list` | Redes guardadas que dao para escolher, com sinal e banda. |

## preflight

| Metodo | Caminho | O que faz |
|---|---|---|
| GET | `/api/preflight` | One answer to "posso filmar?", built only from state we already hold. |

## profiles

| Metodo | Caminho | O que faz |
|---|---|---|
| GET,POST | `/api/profiles` | GET: os perfis e o estado actual. POST {"save": nome} guarda o actual; {"apply": nome} aplica (recusado a gravar); {"delete": nome} apaga. |

## rec

| Metodo | Caminho | O que faz |
|---|---|---|
| POST | `/api/rec` | Grava/para so as camaras seleccionadas que estiverem em modo video (as Canon so gravam em modo video). |
| POST | `/api/rec/strict` |  |
| POST | `/api/rec/targets` |  |

## security

| Metodo | Caminho | O que faz |
|---|---|---|
| GET,POST | `/api/security/pin` | O PIN da pagina: GET diz se ha PIN; POST {"pin": "1234"} define-o, {"pin": ""} apaga-o. So do Atom ou do Tailscale (quem entrou com o PIN nao o muda). O PIN nunca volta na resposta nem vai para o registo. |

## services

| Metodo | Caminho | O que faz |
|---|---|---|
| GET,POST | `/api/services` | GET: {web, usb} ligados ou nao. POST {web/usb: true/false}: liga ou desliga (fica assim nos arranques seguintes). Desligar a pagina a partir de outro aparelho corta-o a ele: so pela consola se volta a ligar. |

## slate

| Metodo | Caminho | O que faz |
|---|---|---|
| GET,POST | `/api/slate` | GET: cena, reel e o take do clip seguinte. POST {scene, reel}: muda-os (vazio tira); as camaras recebem-nos entre takes. |
| POST | `/api/slate/beep` | Regista uma claquete sonora emitida pelo telemovel. |
| GET,POST | `/api/slate/take` | A pagina de dados da take (consola, no fim de uma gravacao). GET: o nome da sessao, a cena, o reel, a descricao do plano, a ultima take e a seguinte. POST {name, scene, reel, shot, good, note}, todos opcionais, so o que vier: `name` e o da sessao ou sequencia (um nome novo volta a numerar as takes a partir de 1; o mesmo nome continua a contar; vazio limpa o nome sem reiniciar), `scene` e `reel` seguem para as camaras pela claquete, `shot` fica numa nota da take seguinte, `good` ("good" / "no-good") marca a take que acabou e `note` fica nessa take. Recusado a gravar. Guardar (mesmo com o corpo vazio) fecha a pagina em todos os clientes; /api/slate/take/skip fecha-a sem guardar. Com a pagina por fechar o bridge recusa um REC novo (/api/slate/take/lock desliga isso). |
| GET,POST | `/api/slate/take/lock` | GET: o bloqueio esta ligado? POST {"enabled": true/false}: liga ou desliga o bloqueio (ligado, um REC novo e recusado ate a pagina de dados da take ser guardada ou saltada; desligado, a pagina abre na mesma mas nao impede nada). |
| POST | `/api/slate/take/skip` | Salta a pagina de dados da take em todos os clientes (consola, pagina web) sem guardar nada: a take fica sem dados e o REC volta a poder comecar. Funciona sempre, tambem a gravar. |

## status

| Metodo | Caminho | O que faz |
|---|---|---|
| GET | `/api/status` |  |

## system

| Metodo | Caminho | O que faz |
|---|---|---|
| POST | `/api/system/led` |  |
| POST | `/api/system/reboot` | Reinicia o Atom (recusado a gravar). |
| POST | `/api/system/shutdown` | Desliga o Atom (recusado a gravar). |
| GET | `/api/system/sony_sdk` | O ajudante da Sony (Camera Remote SDK): {available, why, sdk, models, caps, found}. ?scan=1 procura camaras (USB e rede local; a lista chega em found uns segundos depois). |
| GET | `/api/system/terms` | Os avisos de licenca (Sony, Canon, Blackmagic...) e se o operador ja os aceitou: {accepted, version, at, text}. A consola mostra-os antes de arrancar e a pagina na primeira visita: uma vez, nao a cada camara. |
| POST | `/api/system/terms` | Aceita os avisos de licenca: {"accept": true}. Fica guardado (termos.json) ate o texto mudar. |
| GET | `/api/system/update` |  |
| POST | `/api/system/update` |  |

## time

| Metodo | Caminho | O que faz |
|---|---|---|
| POST | `/api/time/cameras/sync` | Set every connected camera's clock, on the operator's say-so. |
| POST | `/api/time/ltc/clear` |  |
| POST | `/api/time/ltc/jam` | Agarra o timecode do gerador. Bloqueia alguns segundos a gravar audio. |
| POST | `/api/time/ltc/send` | LTC a andar pela saida de auscultadores para um aparelho que faz jam pela entrada de audio (DJI Mic 3, como o F6). {"to": "DJI Mic 3", "secs": 15} |
| POST | `/api/time/ltc/set_system_clock` | Acerta o relogio do Atom pelo gerador de timecode. |
| POST | `/api/time/ntp/clear` | Esquece o NTP como fonte. NAO desacerta o relogio do Atom. |
| POST | `/api/time/ntp/sync` | Forca uma sincronizacao NTP imediata e deixa o NTP ligado. |
| POST | `/api/time/phone/adopt` | Set the Atom's clock from the phone's — operator action, never automatic. |
| POST | `/api/time/phone/clear` | Esquece o telemovel como fonte. Tambem nao desacerta o relogio do Atom. |
| POST | `/api/time/phone/offer` | The web UI hands us the phone's clock. Stored, never applied on its own. |
| GET | `/api/time/reference` | A hora do bridge para a pagina do QR: `src=ltc` (a do LTC, se houver) ou `system`. |
| POST | `/api/time/source` | Escolhe qual das quatro fontes manda. Nao faz fetch de nada. |

## (paginas)

| Metodo | Caminho | O que faz |
|---|---|---|
| GET | `/` |  |
| GET | `/eventlog` |  |
| GET | `/f6card` |  |
| GET | `/favicon.ico` |  |
| GET | `/fonts/<path:nome>` | Serve os tipos de letra a partir do proprio Atom. |
| GET | `/gopro_qr` | Codigo QR animado com a hora do bridge (GoPro Labs: 'oT...'): a camara le-o e acerta o relogio. |
| GET | `/icon-192.png` |  |
| GET | `/icon-512.png` |  |
| GET | `/login` |  |
| GET | `/manifest.json` |  |
| GET | `/shootlog` |  |
| GET | `/sw.js` |  |
| GET | `/view` |  |
