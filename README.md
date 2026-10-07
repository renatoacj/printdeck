# 🖨️ PrintDeck

Fila de impressão 3D compartilhada. Cada um manda o G-code fatiado no Cura; o PrintDeck lê o tempo e o filamento do arquivo, calcula o custo, organiza a fila e mostra quanto cada pessoa já usou da impressora. O dono da impressora controla tudo por um painel, no computador ou no celular.

Feito para uma Creality Ender 3 / Ender 3 Pro com OctoPrint, mas funciona com qualquer impressora que aceite G-code do Cura (inclusive só com cartão microSD).

## O que ele faz

- **Fila com previsão**: tempo, filamento, custo e horário em que cada peça começa e termina.
- **Custos transparentes**: material (preço por kg) + energia (potência × tempo × tarifa), por impressão e por amigo.
- **Dashboard**: quem mais usou a impressora, gastos de cada um, falhas e o que está imprimindo agora.
- **Rolos de filamento**: cada impressão fica marcada com o código do rolo; o programa desconta o que saiu de verdade e avisa quando o rolo não vai dar para a fila.
- **OctoPrint**: enviar e imprimir com um clique, progresso e temperaturas ao vivo, pausar/retomar/cancelar, modelo 3D sendo construído na tela. A impressão **nunca começa sozinha**: só quando o dono clica.
- **Mesa conjunta**: junta vários G-codes do Cura em uma impressão só, encaixando as peças na mesa de 220 × 220 mm e dividindo o custo por peça.
- **Reimprimir com um clique** a partir do histórico.
- **Aplicativo de desktop** (Windows): janela própria, ícone na bandeja, sem terminal aberto.
- **Aplicativo no celular** (Android/iPhone): instalável direto do navegador.
- **Acesso pela internet** para os amigos, sem eles instalarem nada.

## Como instalar (Windows)

1. Instale o [Python 3.12+](https://www.python.org/downloads/) (marque "Add to PATH").
2. Baixe este repositório e dê dois cliques em **`iniciar.bat`**. Na primeira vez ele cria o ambiente e instala as dependências; depois abre http://localhost:5000.
3. Para usar como aplicativo (janela + bandeja, sem terminal):

   ```
   .venv\Scripts\python desktop.py --atalho
   ```

   Isso cria o atalho **PrintDeck** na Área de Trabalho e no menu Iniciar.

No Linux / Raspberry Pi: `./iniciar.sh`.

## Primeiros passos

1. Abra **Administrativo** (senha inicial `admin`).
2. Em **Configurações → Custos e senhas**, troque a senha do administrador e defina a **senha da galera** (a senha que você passa para os amigos). O acesso pela internet só liga depois que a senha de fábrica for trocada, e ela nunca é aceita pelo link público.
3. Ajuste a tarifa de energia, a potência da impressora e o preço do kg de cada material.
4. Cadastre o rolo de filamento em **Configurações → Filamentos**.
5. (Opcional) Em **Impressora (OctoPrint)**, informe o endereço do OctoPrint e a chave de API (OctoPrint → Configurações → *Application Keys*).

## Como os amigos acessam

No Administrativo, cartão **Link para os amigos → Ligar acesso pela internet**. Há dois jeitos, e o programa escolhe sozinho:

| | Link | O que precisa |
|---|---|---|
| **Tailscale Funnel** | Fixo: `https://nome-do-pc.sua-rede.ts.net` | [Tailscale](https://tailscale.com/download) instalado e logado neste computador, com o Funnel liberado (rode `tailscale funnel 5000` uma vez e aprove no endereço que ele mostrar) |
| **Cloudflare (túnel rápido)** | Muda quando o túnel reinicia | Só o `cloudflared` instalado (`winget install Cloudflare.cloudflared`); sem conta |

Se o Tailscale estiver pronto, o PrintDeck usa o link fixo; senão, cai no túnel do Cloudflare. Na mesma rede Wi-Fi também funciona `http://IP-DO-PC:5000`.

O link só funciona enquanto o programa estiver aberto (pode ficar na bandeja).

## Aplicativo no celular

Abra o link no **Chrome do Android** → **⋮ → Instalar app**. No iPhone: Safari → Compartilhar → **Adicionar à Tela de Início**. O dono pode entrar como administrador pelo **link fixo do administrador** (cartão "Link para os amigos") ou por QR Code.

## Como usar

**Amigo**: no Cura, fatiar → "Salvar no disco"; abrir o link, digitar o nome, escolher o rolo e enviar um ou mais `.gcode`. Dá para pedir que várias peças sejam impressas juntas na mesma mesa.

**Dono**: conferir a mesa e clicar em **Imprimir** (OctoPrint) ou baixar o G-code para o microSD e marcar **Imprimindo** / **Concluída** / **Falhou** à mão. Uma falha conta para o amigo por padrão; dá para desligar a cobrança de cada impressão.

Miniatura da peça na fila (opcional): no Cura, *Extensões → Pós-processamento → Modificar G-code → "Create Thumbnail"*.

## Como os custos são calculados

- **Filamento (g)** = metros (do Cura) × área da seção do filamento × densidade. Ex.: 1 m de PLA de 1,75 mm ≈ 2,98 g.
- **Material (R$)** = gramas ÷ 1000 × preço do kg.
- **Energia (R$)** = potência (W) ÷ 1000 × horas × tarifa (R$/kWh).
- Numa falha ou cancelamento, vale só o filamento que realmente saiu até aquele ponto do arquivo.
- Os custos ficam congelados quando a impressão termina; mudar preços só afeta o que ainda está na fila.

## Onde ficam os dados

Banco (`fila.db`) e G-codes ficam na pasta `dados/` do projeto, ou em `~/PrintDeck/dados` se essa pasta existir (recomendado quando o projeto está dentro de uma pasta sincronizada, como o OneDrive). Uma cópia do banco é feita por dia em `copias_de_seguranca/`. Para usar outro lugar, defina a variável `DATA_DIR`.

Nada disso vai para o repositório: senhas, chave do OctoPrint e arquivos dos amigos ficam só no seu computador.

## Segurança

- Senhas guardadas com hash; limite de tentativas por aparelho; sessão de administrador encerrada em todos os aparelhos ao trocar a senha ou o link fixo.
- Só o site da fila vai para a internet. O OctoPrint não fica exposto e os amigos não falam com ele.
- A identificação dos amigos é só pelo nome (não há contas): use entre pessoas de confiança e com a senha da galera definida.
- O G-code enviado é executado pela impressora como veio: confira o que vai imprimir.

## Tecnologia

Python 3.12, Flask, SQLite, three.js (visualizador), Chart.js, pywebview + pystray (aplicativo de desktop).
