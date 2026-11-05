# Deskrab

**Português** · [English](#english)

O Deskrab é um companheiro de mesa para o [Claude Code](https://claude.com/claude-code): um caranguejo em pixel art numa telinha que mostra o que o Claude está fazendo agora (pensando, rodando comandos, editando, esperando sua permissão), com seus limites de uso de 5 horas e 7 dias embaixo.

Este repositório tem o plugin do Claude Code que alimenta o display, e a página de atualização do firmware.

## Requisitos

- **Python 3.8 ou mais novo** no computador. Não precisa instalar mais nada: o pyserial já vem junto.
- Claude Code (terminal ou app desktop).
- O display Deskrab, ligado pelo cabo USB que vem com ele (ou pelo Wi-Fi, depois do setup).

## Instalação

No Claude Code, digite:

```
/plugin marketplace add ricardomdcj/deskrab-plugin
/plugin install deskrab@deskrab
/deskrab:setup
```

O `/deskrab:setup` encontra o seu Python, instala a linha de status (se você já tinha uma, ela continua aparecendo), confere se o display responde e pergunta se você quer usar USB ou Wi-Fi. Depois, abra uma sessão nova do Claude Code.

## Wi-Fi

Com o display no cabo, peça Wi-Fi no `/deskrab:setup`. O nome e a senha da rede vão para o display pelo cabo, junto com um código de pareamento que só o seu computador recebe. Depois o cabo pode ir para qualquer carregador USB. O display só enxerga redes de **2,4 GHz**, e o computador precisa estar na mesma rede.

## Atualizar o firmware

Abra **https://ricardomdcj.github.io/deskrab-plugin/** no Chrome ou no Edge, com o display no cabo USB, e clique em instalar. Não precisa instalar nada no computador.

## Problemas

Peça ao Claude: "o Deskrab não está reagindo". O `/deskrab:setup` também diagnostica: cabo só de carga, porta ocupada por outro programa, permissão no Linux (grupo `dialout`).

## Privacidade

Tudo fica no seu computador e na sua rede local. O plugin não manda nada para a internet: ele lê os eventos do Claude Code e envia o estado para o display pelo cabo ou pela rede de casa.

---

## English

Deskrab is a desk companion for [Claude Code](https://claude.com/claude-code): a pixel crab on a small display that shows what Claude is doing right now (thinking, running commands, editing, waiting for your permission), with your 5-hour and 7-day usage limits underneath.

This repository holds the Claude Code plugin that feeds the display, and the firmware update page.

**Requirements:** Python 3.8+ (pyserial is bundled), Claude Code (CLI or desktop app), and the Deskrab display on its USB cable (or Wi-Fi after setup).

**Install**, in Claude Code:

```
/plugin marketplace add ricardomdcj/deskrab-plugin
/plugin install deskrab@deskrab
/deskrab:setup
```

`/deskrab:setup` finds your Python, installs the status line (an existing one keeps showing), checks the display and asks whether to use USB or Wi-Fi (2.4 GHz only; the network is sent over the cable together with a pairing code). Open a new Claude Code session afterwards.

**Firmware updates:** open https://ricardomdcj.github.io/deskrab-plugin/ in Chrome or Edge with the display on USB.

**Privacy:** everything stays on your computer and your local network.

## License

The plugin and the web page are MIT licensed (see [LICENSE](LICENSE)). The firmware images in `docs/firmware/` are distributed for use on Deskrab devices only. Open-source components are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
